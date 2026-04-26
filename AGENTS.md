# AGENTS

## Project Overview

## Repository Language Rules

- Write all repository content in English and reply to the user in the user's language.

## User Interaction Rules

- If requirements or plans are ambiguous, eliminate disagreement instead of guessing: first explore the codebase when that can answer the question, otherwise ask clarifying questions aggressively, preferably one at a time.
- Clarifying questions must drive toward shared understanding by walking the design tree and resolving decision dependencies; each question must cover purpose, constraints, success criteria, or scope boundaries as appropriate, include all viable options, avoid requiring free-form input, and state the recommended answer.
- After the user approves execution, keep going until the full planned task is complete. Do not stop for intermediate progress reports, return control while the approved goal is only partially implemented, or ask whether to continue with obvious next steps, natural follow-ups, or clear previews, summaries, or refinements unless a genuine blocker, real ambiguity, or material risk requires user input.
- If something should obviously be done now, do it instead of deferring it to "later", "next", or a "follow-up". When reviewing or refining in-progress work, immediately implement any concrete fix you understand and that is not blocked.
- Resolve encountered difficulties autonomously whenever possible.
- When handing control back to the user after task completion, end the response with `Done.`

## Design and Planning Rules

- Before proposing a design or implementation, inspect the current project context, including relevant files, documentation, and recent changes.
- State assumptions explicitly. If materially different interpretations remain after exploration, surface and resolve them instead of silently choosing one.
- For feature work, behavior changes, and other creative or architectural tasks, complete a design step before implementation. If the request is too broad for one coherent spec or plan, decompose it into smaller subprojects and handle them one at a time.
- After gathering enough context, present 2-3 viable approaches with trade-offs and a recommended option, size the design sections to the topic, and validate the design with the user before implementation.
- Before substantial implementation, define concrete success criteria and explicit checks that prove completion; prefer tests, builds, or other verifiable validation over vague goals.
- Unless the user explicitly asks for a temporary workaround, limited experiment, reduced-scope language design, or backwards compatibility, design and implement the final intended product: complete behavior, durable interfaces, maintainable structure, the full language design rather than a staged or minimal subset, and clean breaking changes instead of compatibility shims during rapid iteration.
- If a requested feature depends on a lower-level prerequisite, implement that prerequisite as part of the work instead of lowering the acceptance bar, trimming scope, or presenting a partial workaround as complete.
- Do not limit the solution to the smallest easy increment or patch size when the correct solution requires broader structural changes. Make decisive, coherent refactors, including renaming APIs, reshaping module boundaries, or replacing flawed internal structures when needed.
- Design systems as small, well-bounded units with clear responsibilities, explicit interfaces, and dependencies that are easy to understand and test independently.
- In existing codebases, follow established patterns unless changing them is necessary to support the current goal.
- Apply YAGNI strictly. Make focused changes, avoid unrelated reformatting or cleanup, and remove only unrequested features, speculative abstractions, unnecessary complexity, or artifacts made unused by your own change unless broader cleanup was requested.

## Git Commit Rules

- Preserve a linear history. Do not force push except when rebasing a PR branch onto the latest `origin/main` and force-pushing that same PR branch update.
- Do not amend commits that have already been pushed, and do not rewrite, replace, or reorder existing commits unless the user explicitly requests it.
- Before starting work, ensure the worktree is clean; if prior changes are present, review them and commit the relevant ones before beginning new task work.
- Commit changes proactively instead of letting them accumulate, and after each completed milestone, especially before pausing for user input at the end of a task, ensure the worktree is clean and all finished changes are committed.
- Never commit temporary files, scratch artifacts, ad hoc notes, or other non-durable byproducts.
- Use specific Conventional Commits for every commit. When a commit is non-trivial, include a body describing what changed, why it changed, and what validation was performed; make the subject identify the precise logical unit and the body record the concrete scope of the change.
- Split commits by logical unit so they stay focused and reviewable.
- Ensure relevant tests pass before creating a commit, assume pre-commit hooks will run, and do not bypass them with `--no-verify`.
