# Kanban Orchestra Task Context
<!-- Injected at the top of every agent prompt by orchestrator.py:build_prompt() -->

You are working on one task tracked in the repo-local Kanban Orchestra
database. Advance only the current task toward its defined completion state.

## Task CLI Shorthand

In `## CLI Commands Available`, `task` means:

    "$ORCHESTRA_DIR/bin/ko-task"

Expand `task` to that full path when running commands, or define:

    task() { "$ORCHESTRA_DIR/bin/ko-task" "$@"; }

## Lifecycle

- Commit tasks run through planning, `commit-make`, review, and finalization.
  The active step-specific instructions below define exactly what to do now.
- Supertask, pull request, and other-task flows use their own maker/reviewer
  prompts and may be commit-free by design.
- If a prior `commit-make` saved WIP via `git stash`, this prompt includes the
  stash recovery section before the normal `commit-make` instructions.
- Skippable review or planning steps are handled by the orchestrator.

## Workspace

Work inside the checkout that launched this task. If the target path is
ambiguous, resolve the root with `git rev-parse --show-toplevel`.

Before your first file edit, state the repo root and at least one target path
you intend to modify so the task trail is auditable.

## Operating Rules

- One commit task maps to one commit. During Path A `commit-make`, stage the
  complete candidate diff with `git add .` before stopping for review.
- Pull request and other tasks do not create commits unless their step prompt
  explicitly says otherwise.
- Use only the task fields and comment commands listed in
  `## CLI Commands Available`; the orchestrator owns `status` and `next_step`.
- Record durable outcomes with `task comment`. Use `task log` only for
  ephemeral progress notes.
- `commit_hash` records a landed commit when one exists; the DB task `id`
  remains the durable task identity.
- Tasks on `master` or `main` require an `ALLOW_TASKS_ON_MASTER` standalone
  marker in `AGENTS.md`; otherwise use a feature branch.

## Recording Decisions

Reviewers record exactly one decision for the current round using the matching
approval or rejection command in `## CLI Commands Available`. Reviewers are
read-only: leave files and task fields untouched.
