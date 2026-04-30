from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from llvm_hackme.models import PullRequest

LOGGER = logging.getLogger(__name__)

_SKIP_LABELS = frozenset(
    {
        "libc++",
        "libcxx",
        "libcxxabi",
        "libunwind",
        "lld",
        "lldb",
        "flang",
        "mlir",
        "compiler-rt",
        "openmp",
        "bolt",
    }
)

_SKIP_LABEL_PREFIXES = ("clang-", "clang:")


def _should_skip_by_labels(pr: PullRequest) -> bool:
    labels = pr.labels
    if not labels:
        return False
    for label in labels:
        if label not in _SKIP_LABELS and not label.startswith(_SKIP_LABEL_PREFIXES):
            return False
    return True


class GitHubError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, GitHubError) and exc.retryable


@dataclass(frozen=True)
class GitHubResponse:
    status_code: int
    headers: httpx.Headers
    json_payload: Any | None
    text: str


@dataclass(frozen=True)
class IssueComment:
    id: int
    html_url: str
    body: str
    author_login: str


class GitHubClient:
    def __init__(self, token: str, repository: str) -> None:
        self.repository = repository
        self._token = token
        self._client = self._make_client()

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "llvm-hackme",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    async def reset_client(self) -> None:
        await self._client.aclose()
        self._client = self._make_client()

    async def aclose(self) -> None:
        await self._client.aclose()

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential_jitter(initial=1, max=10),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> GitHubResponse:
        try:
            response = await self._client.request(
                method, path, headers=headers, params=params, json=json
            )
        except httpx.HTTPError as exc:
            raise GitHubError(str(exc), retryable=True) from exc

        if not response.is_success:
            retryable = response.status_code >= 500 or response.status_code in {
                403,
                429,
            }
            LOGGER.warning(
                "GitHub API %s %s failed with status=%s retryable=%s body=%s",
                method,
                path,
                response.status_code,
                retryable,
                response.text[:500],
            )
            raise GitHubError(
                f"GitHub API {method} {path} failed with {response.status_code}",
                retryable=retryable,
            )

        json_payload: Any | None
        try:
            if response.headers.get("content-type", "").startswith("application/json"):
                json_payload = response.json()
            else:
                json_payload = None
        except (ValueError, TypeError):
            LOGGER.warning("GitHub API %s %s returned unparseable JSON", method, path)
            raise GitHubError(
                f"GitHub API {method} {path} returned unparseable JSON",
                retryable=True,
            ) from None
        return GitHubResponse(
            status_code=response.status_code,
            headers=response.headers,
            json_payload=json_payload,
            text=response.text,
        )

    async def get_authenticated_login(self) -> str:
        response = await self._request("GET", "/user")
        payload = _expect_json_object(response)
        login = payload.get("login")
        if isinstance(login, str):
            return login
        raise GitHubError(
            "GitHub /user response missing 'login' field", retryable=False
        )

    async def list_recent_open_pull_requests(
        self, watermark: datetime | None, overlap_seconds: int
    ) -> tuple[list[PullRequest], datetime | None]:
        cutoff = (
            watermark - timedelta(seconds=overlap_seconds)
            if watermark is not None
            else datetime.now(timezone.utc) - timedelta(seconds=overlap_seconds)
        )
        prs: list[PullRequest] = []
        newest_seen: datetime | None = watermark
        page = 1
        while True:
            response = await self._request(
                "GET",
                f"/repos/{self.repository}/pulls",
                params={
                    "state": "open",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": 100,
                    "page": page,
                },
            )
            payload = _expect_json_list(response)
            if not payload:
                break
            stop = False
            for item in payload:
                updated_at = _parse_github_datetime(item["updated_at"])
                newest_seen = (
                    updated_at
                    if newest_seen is None or updated_at > newest_seen
                    else newest_seen
                )
                if updated_at < cutoff:
                    stop = True
                    continue
                if _is_draft(item) or not _targets_main(item) or _is_revert(item):
                    continue
                pr = _parse_pull_request(item)
                if _should_skip_by_labels(pr):
                    continue
                prs.append(pr)
            if stop or "next" not in _parse_link_relations(response.headers):
                break
            page += 1
        return prs, newest_seen

    async def list_pull_files(self, pull_number: int) -> list[str]:
        files: list[str] = []
        page = 1
        while True:
            response = await self._request(
                "GET",
                f"/repos/{self.repository}/pulls/{pull_number}/files",
                params={"per_page": 100, "page": page},
            )
            payload = _expect_json_list(response)
            for item in payload:
                filename = item.get("filename")
                if filename:
                    files.append(str(filename))
            if "next" not in _parse_link_relations(response.headers):
                break
            page += 1
        return files

    async def get_pull_patch(self, pull_number: int) -> str:
        response = await self._request(
            "GET",
            f"/repos/{self.repository}/pulls/{pull_number}",
            headers={"Accept": "application/vnd.github.v3.diff"},
        )
        return response.text

    async def get_pull_head_sha(self, pull_number: int) -> str:
        response = await self._request(
            "GET",
            f"/repos/{self.repository}/pulls/{pull_number}",
        )
        payload = response.json_payload
        return str(payload["head"]["sha"])

    async def get_pull_request(self, pull_number: int) -> PullRequest:
        response = await self._request(
            "GET",
            f"/repos/{self.repository}/pulls/{pull_number}",
        )
        return _parse_pull_request(response.json_payload)

    async def list_issue_comments(self, issue_number: int) -> list[IssueComment]:
        comments: list[IssueComment] = []
        page = 1
        while True:
            response = await self._request(
                "GET",
                f"/repos/{self.repository}/issues/{issue_number}/comments",
                params={"per_page": 100, "page": page},
            )
            payload = _expect_json_list(response)
            comments.extend(_parse_issue_comment(item) for item in payload)
            if "next" not in _parse_link_relations(response.headers):
                break
            page += 1
        return comments

    async def create_issue_comment(self, issue_number: int, body: str) -> IssueComment:
        response = await self._request(
            "POST",
            f"/repos/{self.repository}/issues/{issue_number}/comments",
            json={"body": body},
        )
        return _parse_issue_comment(_expect_json_object(response))

    async def update_issue_comment(self, comment_id: int, body: str) -> IssueComment:
        response = await self._request(
            "PATCH",
            f"/repos/{self.repository}/issues/comments/{comment_id}",
            json={"body": body},
        )
        return _parse_issue_comment(_expect_json_object(response))

    async def create_request_changes_review(self, pull_number: int, body: str) -> None:
        await self._request(
            "POST",
            f"/repos/{self.repository}/pulls/{pull_number}/reviews",
            json={"event": "REQUEST_CHANGES", "body": body},
        )


def _parse_link_relations(headers: httpx.Headers) -> set[str]:
    link = headers.get("link")
    if not link:
        return set()
    relations: set[str] = set()
    for part in link.split(","):
        for parameter in part.split(";"):
            parameter = parameter.strip()
            if parameter.startswith('rel="') and parameter.endswith('"'):
                relations.add(parameter.removeprefix('rel="').removesuffix('"'))
    return relations


def _expect_json_object(response: GitHubResponse) -> dict[str, Any]:
    if not isinstance(response.json_payload, dict):
        raise GitHubError("GitHub response was not a JSON object", retryable=False)
    return response.json_payload


def _expect_json_list(response: GitHubResponse) -> list[Any]:
    if not isinstance(response.json_payload, list):
        raise GitHubError("GitHub response was not a JSON list", retryable=False)
    return response.json_payload


def _parse_github_datetime(value: str) -> datetime:
    if not value:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _is_draft(item: dict[str, Any]) -> bool:
    return bool(item.get("draft", False))


def _targets_main(item: dict[str, Any]) -> bool:
    base = item.get("base")
    return isinstance(base, dict) and base.get("ref") == "main"


def _is_revert(item: dict[str, Any]) -> bool:
    title = item.get("title")
    return isinstance(title, str) and title.startswith("Revert ")


def _parse_pull_request(item: dict[str, Any]) -> PullRequest:
    user = item.get("user")
    head = item.get("head")
    raw_labels: list[dict[str, Any]] = item.get("labels") or []
    label_names: list[str] = [
        str(lb["name"]) for lb in raw_labels if isinstance(lb, dict) and lb.get("name")
    ]
    return PullRequest(
        number=int(item.get("number", 0)),
        title=str(item.get("title", "")),
        author_login=str(user.get("login")) if isinstance(user, dict) else "",
        head_sha=str(head.get("sha")) if isinstance(head, dict) else "",
        updated_at=_parse_github_datetime(str(item.get("updated_at", ""))),
        html_url=str(item.get("html_url", "")),
        draft=bool(item.get("draft", False)),
        base_ref=_get_base_ref(item),
        patch_url=item.get("patch_url"),
        labels=label_names,
    )


def _get_base_ref(item: dict[str, Any]) -> str:
    base = item.get("base")
    if isinstance(base, dict):
        return str(base.get("ref", ""))
    return ""


def _parse_issue_comment(item: dict[str, Any]) -> IssueComment:
    user = item.get("user")
    return IssueComment(
        id=int(item.get("id", 0)),
        html_url=str(item.get("html_url", "")),
        body=str(item.get("body") or ""),
        author_login=str(user.get("login")) if isinstance(user, dict) else "",
    )
