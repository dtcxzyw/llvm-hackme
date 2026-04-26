from __future__ import annotations

import hashlib
import logging

from llvm_hackme.config import Config
from llvm_hackme.github import GitHubClient
from llvm_hackme.models import PullRequestUpdate
from llvm_hackme.paths import is_relevant_pr_file
from llvm_hackme.state import StateStore

LOGGER = logging.getLogger(__name__)


class PullRequestScanner:
    def __init__(self, config: Config, state: StateStore, github: GitHubClient) -> None:
        self.config = config
        self.state = state
        self.github = github

    async def scan_once(self) -> list[PullRequestUpdate]:
        watermark = self.state.get_scan_watermark()
        prs, newest_seen = await self.github.list_recent_open_pull_requests(
            watermark, self.config.scan_overlap_seconds
        )
        updates: list[PullRequestUpdate] = []
        for pr in prs:
            try:
                files = await self.github.list_pull_files(pr.number)
            except Exception:
                LOGGER.exception("Failed to list files for PR #%s", pr.number)
                continue

            if not any(is_relevant_pr_file(path) for path in files):
                continue

            try:
                patch = await self.github.get_pull_patch(pr.number)
            except Exception:
                LOGGER.exception("Failed to fetch patch for PR #%s", pr.number)
                continue

            patch_sha256 = hashlib.sha256(patch.encode()).hexdigest()
            stored = self.state.get_pull_state(pr.number)
            if stored.head_sha == pr.head_sha and stored.patch_sha256 == patch_sha256:
                continue

            self.state.record_pr_update(
                pr.number, head_sha=pr.head_sha, patch_sha256=patch_sha256
            )
            updates.append(PullRequestUpdate(pr, patch, patch_sha256))

        if newest_seen is not None:
            self.state.set_scan_watermark(newest_seen)
        LOGGER.info("PR scan found %s relevant update(s)", len(updates))
        return updates
