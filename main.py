from __future__ import annotations

import asyncio
import logging

from llvm_hackme.config import Config
from llvm_hackme.github import GitHubClient
from llvm_hackme.llm_review import OpenAIPatchReviewer
from llvm_hackme.service import HackmeService
from llvm_hackme.state import StateStore


async def async_main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config.from_env()
    state = StateStore(config.state_db)
    github = GitHubClient(config.github_token, config.github_repository)
    reviewer = OpenAIPatchReviewer(config)
    service = HackmeService(config, state, github, reviewer)
    await service.run_forever()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
