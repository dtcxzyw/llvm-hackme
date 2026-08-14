from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from llvm_hackme.github import GitHubClient
from llvm_hackme.models import PullRequest, PullRequestUpdate
from llvm_hackme.service import HackmeService


def _make_update(number: int, sha: str) -> PullRequestUpdate:
    pr = PullRequest(
        number=number,
        title="T",
        author_login="user",
        head_sha=sha,
        updated_at=MagicMock(),
        html_url="h",
    )
    return PullRequestUpdate(pr=pr, patch="patch", patch_sha256=f"p{sha}")


def _make_service() -> HackmeService:
    service = HackmeService.__new__(HackmeService)
    service._config = MagicMock()
    service._state = MagicMock()
    service._github = MagicMock(spec=GitHubClient)
    service._reviewer = MagicMock()
    service._builds = MagicMock()
    service._fuzzer = MagicMock()
    service._build_lock = asyncio.Lock()
    service._pr_tasks = {}
    service._pr_in_build = set()
    service._hack_tasks = {}
    service._service_login = "service-login"
    service._status_callback = None
    service._handle_pr_update = AsyncMock()
    return service


async def test_schedule_pr_task_kills_hack_agents_on_new_update() -> None:
    service = _make_service()

    hack_task1 = asyncio.create_task(asyncio.sleep(60))
    hack_task2 = asyncio.create_task(asyncio.sleep(60))
    service._hack_tasks[1] = {hack_task1, hack_task2}

    existing = asyncio.create_task(asyncio.sleep(60))
    service._pr_tasks[1] = existing

    service._schedule_pr_task(_make_update(1, "newsha"))
    await asyncio.sleep(0)

    assert hack_task1.cancelled()
    assert hack_task2.cancelled()
    assert existing.cancelled()
    # new task replaces the old one
    assert service._pr_tasks[1] is not existing


async def test_schedule_pr_task_force_cancels_running_task() -> None:
    service = _make_service()

    existing = asyncio.create_task(asyncio.sleep(60))
    service._pr_tasks[1] = existing
    service._pr_in_build.add(1)

    # without force: update is dropped (skip cancel)
    service._schedule_pr_task(_make_update(1, "sha2"))
    await asyncio.sleep(0)
    assert not existing.cancelled()

    # with force: running task is cancelled and replaced
    service._schedule_pr_task(_make_update(1, "sha3"), force=True)
    await asyncio.sleep(0)
    assert existing.cancelled()
    assert service._pr_tasks[1] is not existing


async def test_check_pr_stale_passes_force_to_schedule() -> None:
    service = _make_service()
    service._github.get_pull_head_sha = AsyncMock(return_value="newsha")
    service._github.get_pull_patch = AsyncMock(return_value="newpatch")

    existing = asyncio.create_task(asyncio.sleep(60))
    service._pr_tasks[1] = existing
    service._pr_in_build.add(1)

    stale = await service._check_pr_stale(
        1, "oldsha", _make_update(1, "oldsha"), force=True
    )
    assert stale is True
    await asyncio.sleep(0)
    assert existing.cancelled()
    assert service._pr_tasks[1] is not existing


async def test_hack_agent_registry_cleaned_up(tmp_path) -> None:
    service = _make_service()
    service._config.hack_work_dir = tmp_path
    service._config.hack_context_file = tmp_path / "context.json"
    service._config.llvm_project_dir = tmp_path / "llvm-project"
    service._config.llvm_project_pr_dir = tmp_path / "llvm-project-pr"
    service._config.opt_memory_limit_bytes = 1024

    toolchain = MagicMock()
    toolchain.baseline_opt = "/bin/true"
    toolchain.pr_opt = "/bin/true"
    toolchain.alive_tv = "/bin/true"

    async def fake_agent(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(0)

    with (
        patch.object(
            service,
            "_run_single_hack_agent",
            new=fake_agent,
        ),
        patch("llvm_hackme.service.find_opencode", return_value="/bin/true"),
    ):
        update = _make_update(1, "sha")
        result = await service._run_hack_agent(update, toolchain, "instcombine")

    assert result == (None, [])
    assert 1 not in service._hack_tasks
