from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
from pathlib import Path

from llvm_hackme.config import Config
from llvm_hackme.github import GitHubClient
from llvm_hackme.llm_review import OpenAIPatchReviewer
from llvm_hackme.service import HackmeService
from llvm_hackme.state import StateStore
from llvm_hackme.tui import HackmeTUI


def _find_opencode() -> str | None:
    which = shutil.which("opencode")
    if which:
        return which
    candidates = [Path.home() / ".opencode" / "bin" / "opencode"]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def _validate_environment(config: Config) -> None:
    if not shutil.which("z3"):
        raise RuntimeError("z3 is not installed.  Install z3 and ensure it is on PATH.")

    if not shutil.which("llvm-symbolizer"):
        raise RuntimeError(
            "llvm-symbolizer not found on PATH."
            "  Required for crash stacktrace generation."
        )

    opencode_bin = _find_opencode()
    if opencode_bin is None:
        raise RuntimeError(
            "opencode binary not found.  Install opencode or set it on PATH."
        )

    result = subprocess.run(
        [opencode_bin, "models"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    available = result.stdout.splitlines()
    model = config.hack_model
    if model not in available:
        raise RuntimeError(
            f"opencode hack model {model!r} is not available.  "
            f"Available models:\n" + "\n".join(f"  {m}" for m in available)
        )


def _create_objects() -> tuple[Config, StateStore, GitHubClient, OpenAIPatchReviewer]:
    config = Config.from_env()
    _validate_environment(config)
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
