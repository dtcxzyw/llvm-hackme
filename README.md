# llvm-hackme

An automated LLVM correctness checking service that monitors open pull
requests to [llvm/llvm-project](https://github.com/llvm/llvm-project),
performs fuzzing on proposed middle-end patches, and reports bugs found
(opt crashes or Alive2 miscompilations) as PR review comments.

## Motivation

LLVM receives hundreds of middle-end patches each week. Reviewer
bandwidth is limited, and subtle correctness bugs often survive code
review.  This service automates the most tedious part of correctness
verification -- mutation-based fuzzing -- so that reviewers can focus
on high-level design decisions while the bot catches regressions
mechanically.

The service checks PRs that touch passes such as InstCombine,
InstSimplify, GVN, EarlyCSE, SCCP, Reassociate, SimplifyCFG,
ConstraintElimination, VectorCombine, AggressiveInstCombine,
CorrelatedValuePropagation, and PhaseOrdering, as well as shared
analysis infrastructure: KnownBits, KnownFPClass, ValueTracking,
ConstantFolding, and InstructionSimplify.

## Configuration

The following environment variables are **required**:

| Variable          | Description                       |
|-------------------|-----------------------------------|
| `GITHUB_TOKEN`    | GitHub API token (with repo read and PR write scopes) |
| `OPENAI_ENDPOINT` | OpenAI-compatible API base URL    |
| `OPENAI_AUTH_KEY` | API authentication key           |
| `OPENAI_MODEL`    | Model name (e.g. `gpt-4o-mini`)  |

Optional variables (with defaults):

| Variable                                | Default                    |
|-----------------------------------------|----------------------------|
| `LLVM_HACKME_GITHUB_REPOSITORY`         | `llvm/llvm-project`        |
| `LLVM_HACKME_GITHUB_LOGIN`              | auto-detected from `/user` |
| `LLVM_HACKME_WORK_DIR`                  | `work/llvm-hackme`         |
| `LLVM_HACKME_STATE_DB`                  | `<work_dir>/state.db`      |
| `LLVM_HACKME_SCAN_INTERVAL_SECONDS`     | `60`                       |
| `LLVM_HACKME_DEBOUNCE_SECONDS`          | `300`                      |
| `LLVM_HACKME_FUZZ_BUDGET_SECONDS`       | `600`                      |
| `LLVM_HACKME_MAX_FUZZ_PARALLELISM`      | `1`                        |
| `LLVM_HACKME_BASELINE_UPDATE_INTERVAL_SECONDS` | `3600`               |

## Quick Start

```bash
# 1. Set environment variables
export GITHUB_TOKEN=ghp_...
export OPENAI_ENDPOINT=https://api.openai.com/v1
export OPENAI_AUTH_KEY=sk-...
export OPENAI_MODEL=gpt-4o-mini

# 2. Install dependencies
pip install -e .

# 3. Run (TUI mode by default)
python main.py

# Or headless mode
python main.py --plain
```

The first run will clone `llvm/llvm-project` and `alive2`, then build the
LLVM toolchain (opt, llvm-extract, llvm-reduce, alive-tv, fuzz tools).
Subsequent runs only rebuild when the baseline moves forward.

## How It Works

1. **Scan** -- polls GitHub for open, non-draft PRs targeting `main`
   (excluding reverts).  Checks if the PR touches relevant middle-end
   files.
2. **LLM Review** -- a lightweight OpenAI call classifies each patch
   chunk as malicious or innocuous before any build or execution.
3. **Build** -- prepares a clean LLVM worktree at the PR head, builds
   `opt`, and assembles the full toolchain (baseline + PR opt,
   alive-tv, mutation tools).
4. **Fuzz** -- extracts seed functions from `.ll` test files changed
   in the patch, mutates them, and runs the PR `opt` with the guessed
   opt pipeline.  Alive2 checks correctness.
5. **Verify** -- each suspected bug is regression-tested against the
   baseline `opt` to confirm it is a new issue.
6. **Report** -- posts a GitHub issue comment with the IR reproducer
   (crash stacktrace or Alive2 counterexample) and requests changes on
   the PR.

## Future Scope

- Expand coverage to additional LLVM middle-end passes and analyses.
- Tighten integration with the
  [llvm-autofix](https://github.com/dtcxzyw/llvm-autofix) pipeline so
  that confirmed bugs can be automatically narrowed down to a minimal
  test case and submitted to the LLVM issue tracker.

## License

Apache-2.0 -- see the [LICENSE](LICENSE) file.
