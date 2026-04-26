from __future__ import annotations

import asyncio
import logging
import sys

from llvm_hackme.config import Config
from llvm_hackme.github import GitHubClient
from llvm_hackme.llm_review import OpenAIPatchReviewer
from llvm_hackme.service import HackmeService
from llvm_hackme.state import StateStore
from llvm_hackme.tui import HackmeTUI


def _create_objects() -> tuple[Config, StateStore, GitHubClient, OpenAIPatchReviewer]:
    config = Config.from_env()
    state = StateStore(config.state_db)
    github = GitHubClient(config.github_token, config.github_repository)
    reviewer = OpenAIPatchReviewer(config)
    return config, state, github, reviewer


async def plain_main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config, state, github, reviewer = _create_objects()
    service = HackmeService(config, state, github, reviewer)
    await service.run_forever()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("--plain", "-p"):
        asyncio.run(plain_main())
    else:
        config, state, github, reviewer = _create_objects()
        app = HackmeTUI(config, state, github, reviewer)
        app.run()


if __name__ == "__main__":
    main()
