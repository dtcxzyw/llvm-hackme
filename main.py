from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import subprocess
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from llvm_hackme.commands import find_opencode as find_opencode
from llvm_hackme.config import Config
from llvm_hackme.github import GitHubClient
from llvm_hackme.llm_review import OpenAIPatchReviewer
from llvm_hackme.service import HackmeService
from llvm_hackme.state import StateStore
from llvm_hackme.tui import HackmeTUI

_LOG_RETENTION_DAYS = 10


def _setup_logging(logs_dir: Path) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler = TimedRotatingFileHandler(
        str(logs_dir / "hackme.log"),
        when="midnight",
        interval=1,
        backupCount=_LOG_RETENTION_DAYS,
        encoding="utf-8",
    )
    handler.setFormatter(fmt)
    root.addHandler(handler)


def _validate_environment(config: Config) -> None:
    if not shutil.which("z3"):
        raise RuntimeError("z3 is not installed.  Install z3 and ensure it is on PATH.")

    if not shutil.which("re2c"):
        raise RuntimeError(
            "re2c is not installed."
            "  Required for alive2 build. Install re2c and ensure it is on PATH."
        )

    if not shutil.which("llvm-symbolizer"):
        raise RuntimeError(
            "llvm-symbolizer not found on PATH."
            "  Required for crash stacktrace generation."
        )

    opencode_bin = find_opencode()
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


async def plain_main(
    config: Config,
    state: StateStore,
    github: GitHubClient,
    reviewer: OpenAIPatchReviewer,
) -> None:
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(stderr_handler)
    service = HackmeService(config, state, github, reviewer)
    try:
        await service.run_forever()
    finally:
        with contextlib.suppress(Exception):
            await github.aclose()
        with contextlib.suppress(Exception):
            await reviewer.close()
        with contextlib.suppress(Exception):
            state.close()


def main() -> None:
    config, state, github, reviewer = _create_objects()
    _setup_logging(config.logs_dir)
    if len(sys.argv) > 1 and sys.argv[1] in ("--plain", "-p"):
        asyncio.run(plain_main(config, state, github, reviewer))
    else:
        try:
            app = HackmeTUI(config, state, github, reviewer)
            app.run()
        finally:
            with contextlib.suppress(Exception):
                asyncio.run(github.aclose())
            with contextlib.suppress(Exception):
                asyncio.run(reviewer.close())
            with contextlib.suppress(Exception):
                state.close()


if __name__ == "__main__":
    main()
