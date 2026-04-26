# Internals

## Architecture

```
main.py ──▶ HackmeTUI (Textual) ──▶ HackmeService
               │                        │
               ├─ status_callback       ├─ PullRequestScanner
               └─ RichLog               ├─ OpenAIPatchReviewer
                                        ├─ BuildManager
                                        ├─ FuzzRunner
                                        └─ report_result (reporting.py)
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

Each PR transitions through these phases:

```
                     +-----------------+
                     |       IDLE      |
                     +--------+--------+
                              |
                          scan PR
                              |
                     +--------v--------+   LLM reject   +-------------------+
                     |   IN_PROGRESS   +--------------->|  REVIEW_REJECTED  |
                     +--------+--------+                +-------------------+
                              |
                         build & check
                              |
                     +--------v--------+
                     |   RE-VERIFY     +----> still reproduces ----+
                     +--------+--------+                          |
                              | no                                |
                              v                                   |
                     +--------+--------+                          |
                     |      FUZZ       |                          |
                     +--------+--------+                          |
                              |                                   |
                    +---------+---------+                         |
                    |                   |                         |
                    v                   v                         |
           +--------+--------+  +------+------+                   |
           |    BUG_FOUND    |  |    PASSED   |                   |
           +--------+--------+  +------+------+                   |
                    |                   |                         |
                    +---------+---------+                         |
                              |                                   |
                              v                                   |
                     +--------+--------+                          |
              +----->|      IDLE       |<-------------------------+
              |      +-----------------+
              |
              +---- (next scan loop)
```

**State stored in SQLite (`pull_state` table):**

| Column             | Purpose |
|--------------------|---------|
| `pr_number`        | GitHub PR number (PK) |
| `head_sha`         | Last seen PR head commit |
| `patch_sha256`     | SHA-256 of the last fetched patch |
| `comment_id`       | GitHub comment ID if posted |
| `comment_url`      | URL of the posted comment |
| `reproducer_json`  | Serialized `Reproducer` if a bug was found |
| `processed_at`     | Timestamp when processing completed (enables resume after crash) |
| `updated_at`       | Last update timestamp |

## Resume-After-Interruption

When the service restarts:

1. `scan_watermark` in the `metadata` table records the most recent PR
   `updated_at` seen by the scanner.
2. The scanner skips a PR **only** when `head_sha`, `patch_sha256`,
   and `processed_at` all match the stored values.
3. If `processed_at` is `NULL`, the PR was previously seen but never
   fully processed; the scanner picks it up again on the next cycle.

This means a crash during the debounce, LLM review, build, fuzz, or
reporting phase is safe -- the PR will be re-processed on restart.

## Pass Guessing

`passes.py` maps file paths (from `diff --git a/...` lines in the
patch) to opt pass name pipelines.  The logic has two layers:

1. **Test paths** (`test/Transforms/...`) -- checked first; if the
   patch modifies a test file under a recognized transform directory,
   the corresponding pass is used (e.g. `test/Transforms/GVN` →
   `gvn`).
2. **Source paths** (`lib/...`, `include/...`) -- checked second as a
   fallback; analysis files (KnownBits, ValueTracking, etc.) always
   map to `instcombine<no-verify-fixpoint>`.

The same keyword list drives `is_relevant_pr_file`, which determines
whether a PR is interesting enough to process at all.

## IR Reproducer

When a bug is confirmed, the comment body follows this structure:

```
The following correctness issue was found by llvm-hackme.

<!-- llvm-hackme-state: bug_found -->
<!-- llvm-hackme-baseline: ... -->
<!-- llvm-hackme-head-sha: ... -->
<!-- llvm-hackme-patch-sha256: ... -->
<!-- llvm-hackme-kind: crash -->

... (boilerplate) ...

## Reproducer

**Kind**: crash

**IR Reproducer**:
```llvm
; RUN: opt -passes=instcombine<no-verify-fixpoint> -S
define i32 @foo(i32 %x) {
  ret i32 %x
}
```

**Stacktrace**:
```
SIGSEGV ...
```

**Baseline Revision**: `...`
**PR Head SHA**: `...`
**Patch SHA256**: `...`
```

The `IR Reproducer` block:
- Starts with a `; RUN: opt ...` header line derived from the failing
  command.
- Contains the full LLVM IR source (reduced by llvm-reduce for
  crashes, or llvm-extract'd for miscompilations).
- Is embedded directly as inline code with ` ```llvm ` fences -- no
  local file paths are exposed in the comment.
