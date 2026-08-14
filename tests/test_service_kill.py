from __future__ import annotations

import asyncio
import contextlib
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


async def test_stale_requeue_does_not_mark_processed(tmp_path) -> None:
    """A run superseded by a new commit must not mark itself processed.

    The real `_handle_pr_update` runs: its post-build staleness check detects
    the SHA change, re-queues via `_schedule_pr_task(force=True)` (which
    cancels the running task), and the superseded run must not call
    `mark_processed`.  The re-queued task is blocked at fuzz so it never
    finishes and never marks the PR either.
    """
    pr = PullRequest(
        number=1,
        title="T",
        author_login="user",
        head_sha="oldsha",
        updated_at=MagicMock(),
        html_url="h",
    )
    update = PullRequestUpdate(pr=pr, patch="patch", patch_sha256="p")

    service = HackmeService.__new__(HackmeService)
    service._config = MagicMock()
    service._config.logs_dir = tmp_path
    service._config.debounce_seconds = 0
    service._state = MagicMock()
    service._state.get_pull_state.return_value = MagicMock(reproducer=None)
    service._github = MagicMock(spec=GitHubClient)
    service._github.get_pull_head_sha = AsyncMock(return_value="newsha")
    service._github.get_pull_patch = AsyncMock(return_value="newpatch")
    service._reviewer = MagicMock()
    service._reviewer.review = AsyncMock(return_value=MagicMock(accepted=True))
    service._builds = MagicMock()
    service._builds.prepare_pr_worktree = AsyncMock(return_value=("rev", True))
    service._builds.build_pr_opt = AsyncMock()
    toolchain = MagicMock()
    toolchain.baseline_revision = "rev"
    service._builds.toolchain_paths = MagicMock(return_value=toolchain)
    service._build_lock = asyncio.Lock()
    service._pr_tasks = {}
    service._pr_in_build = set()
    service._hack_tasks = {}
    service._service_login = "svc"
    service._status_callback = None
    service._maybe_backoff = AsyncMock()
    service._fuzzer = MagicMock()
    service._run_hack_agent = AsyncMock(return_value=(None, []))
    service._emit_status = AsyncMock()

    # The re-queued task (T2) must not complete during this test; block it
    # at its fuzz phase so it can never reach mark_processed.
    fuzz_gate = asyncio.Event()

    async def gated_fuzz(*args: object, **kwargs: object) -> object:
        await fuzz_gate.wait()
        return MagicMock(reproducer=None, mutation_count=0)

    service._fuzzer.run = gated_fuzz

    with (
        patch("llvm_hackme.service.guess_pass_name", return_value="instcombine"),
        patch("llvm_hackme.service.set_command_log_path"),
        patch("llvm_hackme.service.append_command_log_message"),
    ):
        t1 = asyncio.create_task(service._handle_pr_update(update))
        service._pr_tasks[1] = t1
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(t1, timeout=5)
        # The superseded run itself must not have marked the PR processed.
        assert not service._state.mark_processed.called
        assert not service._state.reset_retry.called
        # T2 is the freshly re-queued task; cancel it so it never finishes.
        t2 = service._pr_tasks.get(1)
        if t2 is not None and t2 is not t1:
            t2.cancel()
            fuzz_gate.set()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(t2, timeout=5)
        # Still not marked: the re-queued run never completed.
        assert not service._state.mark_processed.called
