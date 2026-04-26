from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest

from llvm_hackme.github import (
    GitHubClient,
    _is_draft,
    _parse_issue_comment,
    _parse_pull_request,
    _targets_main,
)


@pytest.fixture
def mock_httpx_client() -> MagicMock:
    return MagicMock(spec=httpx.AsyncClient)


@pytest.fixture
def github_client() -> GitHubClient:
    return GitHubClient(token="test-token", repository="llvm/llvm-project")


class TestPullRequestParsing:
    def test_parse_pull_request(self) -> None:
        item = {
            "number": 123,
            "title": "Test PR",
            "user": {"login": "testuser"},
            "head": {"sha": "abc123def"},
            "updated_at": "2024-01-15T10:30:00Z",
            "html_url": "https://github.com/llvm/llvm-project/pull/123",
        }
        pr = _parse_pull_request(item)
        assert pr.number == 123
        assert pr.title == "Test PR"
        assert pr.author_login == "testuser"
        assert pr.head_sha == "abc123def"
        assert pr.updated_at == datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        assert pr.html_url == "https://github.com/llvm/llvm-project/pull/123"
        assert pr.draft is False
        assert pr.base_ref == ""

    def test_parse_pull_request_with_draft_and_base(self) -> None:
        item = {
            "number": 124,
            "title": "Draft PR",
            "user": {"login": "user"},
            "head": {"sha": "def456"},
            "updated_at": "2024-06-01T12:00:00Z",
            "html_url": "https://github.com/llvm/llvm-project/pull/124",
            "draft": True,
            "base": {"ref": "main"},
        }
        pr = _parse_pull_request(item)
        assert pr.number == 124
        assert pr.draft is True
        assert pr.base_ref == "main"

    def test_is_draft(self) -> None:
        assert _is_draft({"draft": True}) is True
        assert _is_draft({"draft": False}) is False
        assert _is_draft({}) is False

    def test_targets_main(self) -> None:
        assert _targets_main({"base": {"ref": "main"}}) is True
        assert _targets_main({"base": {"ref": "develop"}}) is False
        assert _targets_main({}) is False
        assert _targets_main({"base": {}}) is False

    def test_parse_issue_comment(self) -> None:
        item = {
            "id": 456,
            "html_url": "https://github.com/llvm/llvm-project/issues/123#issuecomment-456",
            "body": "comment body",
            "user": {"login": "commenter"},
        }
        comment = _parse_issue_comment(item)
        assert comment.id == 456
        assert comment.body == "comment body"
        assert comment.author_login == "commenter"

    def test_parse_issue_comment_empty_body(self) -> None:
        item = {
            "id": 789,
            "html_url": "https://example.com",
            "body": None,
            "user": {"login": "x"},
        }
        comment = _parse_issue_comment(item)
        assert comment.id == 789
        assert comment.body == ""
