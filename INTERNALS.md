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

A PR lives in one of these states across scan cycles:

```
                         +---------+
                    +--->|  IDLE   |<-----------------------------+
                    |    +----+----+                              |
                    |         |                                   |
                    |    scanner picks up                         |
                    |    (head_sha or patch changed               |
                    |     or processed_at is null)                |
                    |         |                                   |
                    |    +----v-----+                             |
                    |    |PROCESSING|<--------+                   |
                    |    +----+-----+         |                   |
                    |         |               |                   |
               LLM  |    +----+----+          |  new head_sha     |
             rejects|    |         |          |  pushed (debounce |
                    |    |  LLM    |          |  cancels task)    |
                    v    | review  |          |                   |
             +------+---+         |          |                   |
             |  REVIEW   |  pass   +----------+                   |
             | _REJECTED |         |                              |
             +-----------+    +----v-----+                        |
                              |  build &  |                        |
                              |   check   |                        |
                              +----+-----+                        |
                                   |                              |
                    +--------------+--------------+               |
                    |                             |               |
                    v                             v               |
         +----------+----------+    +------------+----+           |
         |  old reproducer     |    |  no old reproducer |           |
         |  in state?          |    |  in state          |           |
         +----------+----------+    +------------+----+           |
                    |                             |               |
         re-verify  |                             |  run fuzz     |
            +-------v-------+                     v               |
            |               |              +------+------+        |
            | still crashes?|              |  fond crash |        |
            |               |              |  or miscomp?|        |
            +---+-------+---+              +---+-----+---+        |
                |       |                      |     |            |
            yes |       | no                   |yes  |no          |
                |       |                      |     |            |
                v       v                      v     v            |
         +------+-+  +--+----+    +-------+  +-------+----+      |
         | report  |  | fuzz  |   | report |  | mark as     |      |
         | same bug|  +-------+   | new bug|  | PASSED      |------+
         +---------+              +--------+  +------------+      |
                                                                  |
                                                                  |
         +--------------------------------------------------------+
         |  (next scan: if head_sha/patch changed, re-enter PROCESSING;
         |   if head_sha/patch unchanged and processed_at is set, stay IDLE)
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

`passes.py` maps file paths (from `diff --git a/...` lines in the patch) to opt pass name pipelines. The logic has two layers:

1. **Test paths** (`llvm/test/Transforms/<pass>/...`) — checked first; if the patch modifies a test file under a recognized transform directory, the corresponding pass is used.
2. **Source paths** (`llvm/lib/...`, `llvm/include/...`) — checked second as a fallback; analysis files (KnownBits, ValueTracking, ConstantFolding, etc.) always map to `instcombine<no-verify-fixpoint>`.

The same keyword list drives `is_relevant_pr_file()`, which determines whether a PR is interesting enough to process at all.

## IR Reproducer

When a bug is confirmed, the comment body embeds the IR inline as a ` ```llvm ` code block, with a `; RUN: opt ...` header line derived from the failing command. No local filesystem paths are exposed in the comment.

The `source_content` field in `Reproducer` stores the full IR text. It is captured at fuzzer output time and propagated through verification → reporting so the comment never needs to read from disk.

## LLM Review

Before any build or execution, the patch is split into chunks and each chunk is sent to an OpenAI-compatible API with a strict prompt that classifies it as `innocuous` or `malicious`. If any chunk is classified as non-innocuous, the PR is skipped entirely.

This gate runs as the very first step in `_handle_pr_update()`, before the build lock is acquired.
