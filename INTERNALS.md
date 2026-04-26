# Internals

## Architecture

```mermaid
flowchart TD
    main.py --> HackmeTUI
    HackmeTUI --> HackmeService
    HackmeTUI -.-> status_callback
    HackmeTUI --> RichLog
    HackmeService --> PullRequestScanner
    HackmeService --> OpenAIPatchReviewer
    HackmeService --> BuildManager
    HackmeService --> FuzzRunner
    HackmeService --> report_result["report_result (reporting.py)"]
    HackmeService --> opencode_headless["opencode headless<br/>(hack agent)"]
```

Each component is a self-contained module under `llvm_hackme/`:

| Module          | Responsibility |
|-----------------|---------------|
| `config.py`     | Environment-variable-based configuration |
| `state.py`      | SQLite persistence for scan watermark and PR state |
| `github.py`     | GitHub REST API client (PRs, comments, reviews, patches) |
| `scanner.py`    | Finds open PRs with relevant file changes |
| `llm_review.py` | OpenAI patch review before any build/execution |
| `builds.py`     | LLVM baseline + PR worktrees, build orchestration |
| `passes.py`     | Pass name guessing from patch file paths |
| `fuzzer.py`     | Mutation-based fuzzing with opt + Alive2 |
| `verification.py` | Regression verification (baseline vs PR opt) |
| `reporting.py`  | GitHub comment and review creation |
| `service.py`    | Orchestrator: scan → review → build → fuzz → verify → report |
| `tui.py`        | Textual TUI header, PR list, and log panel |
| `commands.py`   | Subprocess runner with timeouts and memory limits |
| `models.py`     | Dataclasses for PullRequest, Reproducer, etc. |
| `paths.py`      | Re-exports `is_relevant_pr_file` from `passes.py` |

## PR State Machine

A PR lives in one of these states across scan cycles:

```mermaid
stateDiagram-v2
    [*] --> IDLE

    IDLE --> PROCESSING : scanner picks up<br/>(head_sha changed or<br/>processed_at is null)

    state PROCESSING {
        REVIEW : LLM review
        BUILD : build & check
        CHECK_OLD : check old reproducer
        FUZZ : run fuzz
        HACK : run hack agent
        [*] --> REVIEW
        REVIEW --> BUILD : accept
        REVIEW --> REJECTED : reject
        BUILD --> CHECK_OLD : has old reproducer
        BUILD --> FUZZ : no old reproducer,<br/>patch has tests
        BUILD --> HACK : no old reproducer,<br/>source-only patch
        CHECK_OLD --> BUG_FOUND : still reproduces
        CHECK_OLD --> FUZZ : no longer reproduces,<br/>patch has tests
        CHECK_OLD --> HACK : no longer reproduces,<br/>source-only patch
        FUZZ --> BUG_FOUND : found bug
        FUZZ --> HACK : no bug found
        HACK --> BUG_FOUND : found bug
        HACK --> PASSED : no bug found
    }

    PROCESSING --> REVIEW_REJECTED : LLM reject
    PROCESSING --> BUG_FOUND : bug confirmed
    PROCESSING --> PASSED : clean
    PROCESSING --> PROCESSING : new head_sha<br/>cancels debounce

    REVIEW_REJECTED --> IDLE : new head_sha
    BUG_FOUND --> IDLE : new head_sha
    PASSED --> IDLE : new head_sha
```

## State Persistence

**SQLite schema (`pull_state` table):**

| Column | Purpose |
|--------|---------|
| `pr_number` | GitHub PR number (PK) |
| `head_sha` | Last processed PR head commit |
| `patch_sha256` | SHA-256 of the last processed patch |
| `comment_id` | GitHub comment ID if a bug was reported |
| `comment_url` | URL of the posted comment |
| `reproducer_json` | Serialized `Reproducer` (only when a bug was found) |
| `processed_at` | UTC timestamp when processing fully completed |
| `updated_at` | Last update timestamp |

**Scanner logic on each cycle:**

1. Fetch open, non-draft PRs targeting `main`, updated since `scan_watermark - overlap`.
2. For each PR, fetch changed files and skip if `is_relevant_pr_file()` returns false for all.
3. Fetch the patch and compute `patch_sha256`.
4. Check `pull_state`:
   - If `head_sha == pr.head_sha` AND `patch_sha256 == computed` AND `processed_at is not null`: skip (already processed).
   - Otherwise: record `head_sha` and `patch_sha256` in state, enqueue processing task.

This means after a crash, any PR whose `processed_at` is still null will be re-picked up on restart.

## Pass Guessing

`passes.py` maps file paths (from `diff --git a/...` lines in the patch) to opt pass name pipelines.  The logic has three layers in strict priority order:

1. **Test paths** (`llvm/test/Transforms/<pass>/...`, excluding PhaseOrdering) -- checked first; highest priority.
2. **Source paths** (`llvm/lib/...`, `llvm/include/...`) -- medium priority; analysis files (KnownBits, ValueTracking, ConstantFolding, etc.) always map to `instcombine<no-verify-fixpoint>`.
3. **PhaseOrdering test paths** (`llvm/test/Transforms/PhaseOrdering/...`) -- lowest priority; only used when no other test or source path matches.

The same keyword list drives `is_relevant_pr_file()`, which determines whether a PR is interesting enough to process at all.

## Hack Agent

When mutation-based fuzzing finds no bug (or is skipped for source-only patches), a lightweight LLM agent runs. The agent is defined in `.opencode/agents/hack.md` and invoked via `opencode run --agent hack --model <model>` in headless mode. Both stdout and stderr are merged into `hack_dir/opencode.log` for post-run auditing.

The model is set via the required `LLVM_HACKME_HACK_MODEL` environment variable in `provider/model` format (e.g. `deepseek/deepseek-v4-pro`). At startup the service validates the model is present in `opencode models` output and that `z3` is on `PATH`.

### Two-pipe handshake

The Python service and the hack agent communicate through two named pipes (FIFOs) in the hack work directory:

```
Python (service.py)                   opencode (hack agent)
─────────────────────                  ─────────────────────
write context.json
os.mkfifo(submit.pipe)
os.mkfifo(response.pipe)
                                       hack_context tool reads context.json
                                       LLM analyzes patch & constructs IR
                                       hack_submit(ir, pass_name, kind, desc)
                                          │
read(submit.pipe)  ◄──────────────────── write(submit.pipe, payload)
                                          │
_hack_verify(payload)                    open(response.pipe) blocks
  ├─ regression confirmed ──► write(response.pipe, {success: true})
  │   kill opencode ────────► (process terminated)
  └─ rejected ──────────────► write(response.pipe, {success: false, reason})
                                  │
                               read(response.pipe) → return reason to LLM → retry
```

1. **Context file** (`context.json`) — written by the service; contains all binary paths, the patch file path, pass name, work directories, LLVM source tree paths, and `opt_memory_limit_bytes` (for the TS-side `prlimit` wrapper). The `hack_context` tool reads it.
2. **Submit pipe** (`submit.pipe`) — agent writes a JSON payload `{ir, pass_name, kind, description}`. The Python service reads it and runs verification (`check_crash` / `check_miscompilation` on both baseline and PR opt).
3. **Response pipe** (`response.pipe`) — Python writes `{success: true}` on confirmed regression (then kills opencode) or `{success: false, reason}` on failed verification (agent may retry).

### Permissions and safety

- `bash: deny`, `webfetch: deny`, `write: deny`, `edit: deny` — the agent cannot modify files or run shell commands.
- `external_directory` — restricted to the two LLVM source trees (`llvm-project/`, `llvm-project-pr/`) and the hack scratch directory.
- All opt/alive2 invocations go through custom TypeScript tools (`.opencode/tools/hack_*.ts`):
  - **Path confinement** — `ir_path` arguments are resolved via `path.resolve` and checked to stay within `work_dir`; absolute paths and `..` traversal are rejected.
  - **Memory limits** — opt and alive2 are wrapped with `prlimit --as=<bytes>` using the `opt_memory_limit_bytes` from context (1 GiB default).
  - **Environment isolation** — all tool spawns use `minimalEnv()` (only `HOME`, `PATH`, `TMPDIR`, `LANG`, `LC_ALL`); secrets like `GITHUB_TOKEN` and `OPENAI_AUTH_KEY` are never exposed to child processes.
  - **Output truncation** — stdout/stderr are truncated to the last 8 000 bytes for opt/alive2 and 12 000 bytes for z3.
- `hack_submit` enforces a 10 MB IR payload limit; larger submissions are rejected.
- z3 is invoked with memory (4 GB) and time (30 s) limits via its own `-memory:` and `-T:` flags.
- The Python service enforces the overall hack time budget (`LLVM_HACKME_HACK_BUDGET_SECONDS`, default 1200 s).
- Server-side verification (`_hack_verify`) also applies `memory_limit_bytes` when running `check_crash` / `check_miscompilation`.

## IR Reproducer

When a bug is confirmed, the comment body embeds the IR inline as a ` ```llvm ` code block, with a `; RUN: opt ...` header line derived from the failing command. No local filesystem paths are exposed in the comment.

The `source_content` field in `Reproducer` stores the full IR text. It is captured at fuzzer output time and propagated through verification → reporting so the comment never needs to read from disk.

## LLM Review

Before any build or execution, the patch is split into chunks and each chunk is sent to an OpenAI-compatible API with a strict prompt that classifies it as `innocuous` or `malicious`. If any chunk is classified as non-innocuous, the PR is skipped entirely.

This gate runs as the very first step in `_handle_pr_update()`, before the build lock is acquired.
