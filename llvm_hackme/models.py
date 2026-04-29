from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class BugKind(str, Enum):
    CRASH = "crash"
    MISCOMPILATION = "miscompilation"


class CommentState(str, Enum):
    BUG_FOUND = "bug_found"
    STILL_REPRODUCES = "still_reproduces"
    NO_ISSUE_FOUND_FOR_CURRENT_PATCH = "no_issue_found_for_current_patch"


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    author_login: str
    head_sha: str
    updated_at: datetime
    html_url: str
    draft: bool = False
    base_ref: str = ""
    patch_url: str | None = None
    labels: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PullRequestUpdate:
    pr: PullRequest
    patch: str
    patch_sha256: str


@dataclass(frozen=True)
class ReviewDecision:
    accepted: bool
    reason: str


@dataclass(frozen=True)
class Reproducer:
    kind: BugKind
    source_path: Path
    command: list[str]
    baseline_revision: str
    pr_head_sha: str
    patch_sha256: str
    stacktrace: str | None = None
    alive2_counterexample: str | None = None
    alive2_args: str | None = None
    opt_output: str | None = None
    source_content: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "source_path": str(self.source_path),
            "command": self.command,
            "baseline_revision": self.baseline_revision,
            "pr_head_sha": self.pr_head_sha,
            "patch_sha256": self.patch_sha256,
            "stacktrace": self.stacktrace,
            "alive2_counterexample": self.alive2_counterexample,
            "alive2_args": self.alive2_args,
            "opt_output": self.opt_output,
            "source_content": self.source_content,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Reproducer:
        kind_raw = payload.get("kind", "")
        try:
            kind = BugKind(kind_raw)
        except ValueError:
            kind = BugKind.CRASH
        return cls(
            kind=kind,
            source_path=Path(str(payload.get("source_path", "."))),
            command=list(payload.get("command", [])),
            baseline_revision=str(payload.get("baseline_revision", "")),
            pr_head_sha=str(payload.get("pr_head_sha", "")),
            patch_sha256=str(payload.get("patch_sha256", "")),
            stacktrace=payload.get("stacktrace"),
            alive2_counterexample=payload.get("alive2_counterexample"),
            alive2_args=payload.get("alive2_args"),
            opt_output=payload.get("opt_output"),
            source_content=payload.get("source_content"),
        )
