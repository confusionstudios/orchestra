# No-Commit Task Support Design

## Status

Proposed design. This document records the intended shape of first-class
no-commit tasks before implementation.

## Problem

Kanban Orchestra currently has a `done-without-commit` comment kind, but it is
only a finalization signal in Path B after review approval. That works as an
escape valve for rare approved tasks that intentionally finish without changing
`HEAD`, but it does not make no-commit work first-class:

- the task still looks like a commit task throughout planning, make, review,
  and dashboard surfaces
- Path A still expects a staged diff and a commit-message comment
- `commit-review` still tells the reviewer to inspect `git diff --cached`
- completed tasks with no `commit_hash` can read as failed or incomplete

No-commit tasks need an explicit contract so agents, reviewers, and operators
can tell the difference between "this task intentionally has no git artifact"
and "the commit path failed to produce one."

## Goals

- Support tasks whose durable output is a comment, external action, decision,
  investigation result, or operator action rather than a git commit.
- Preserve the normal commit workflow for code and repository documentation
  changes.
- Keep review meaningful even when there is no staged diff.
- Make dashboard and status wording present no-commit completion as a valid
  outcome.
- Keep `done-without-commit` as the terminal audit event for commit-free work.

## Non-Goals

- Do not infer no-commit behavior from title keywords such as "docs",
  "discussion", or "operator". Those words are too ambiguous.
- Do not make no-commit mode an escape hatch for hard-to-land diffs.
- Do not remove or weaken the one-commit contract for normal repository
  changes.

## Recommendation

Add an explicit task completion mode, not a new status.

```text
completion_mode: commit | no-commit
```

`commit` remains the default for all existing and newly-created tasks. A task
uses `no-commit` only when the operator or task creator explicitly opts in with
CLI/UI support such as:

```bash
task add "Check production deploy status" --mode no-commit --branch ops
task set <id> --mode no-commit
```

The existing `done-without-commit` comment kind should remain, but its role
changes from an exceptional Path B signal to the normal terminal event for
approved `no-commit` tasks. Backward compatibility is straightforward: any
historical done task with `commit_hash IS NULL` and a `done-without-commit`
comment can be displayed as `no-commit` completion even if the row predates
`completion_mode`.

## Classification Rules

Use `completion_mode=commit` when the intended output changes files in the work
repo, including source, tests, prompts, specs, images, screenshots, or tracked
documentation.

Use `completion_mode=no-commit` when the intended output is outside git or is
captured only in task history:

- investigation or design findings recorded as a task comment
- operator actions such as restarting a service or checking deployment state
- external-system updates that are already complete outside the repo
- discussion or decision records where no tracked file should change
- intentionally empty validation tasks

When in doubt, use `commit`. If a no-commit task discovers that a repository
change is needed, the agent should stop and ask for the task to be converted or
split rather than silently landing repo changes through the no-commit path.
No-commit tasks should still carry a branch in the initial implementation so
queue eligibility and branch safety policy remain consistent with normal tasks.

## Workflow Semantics

No new `status` values are needed. No-commit tasks can reuse the existing
make/review/finalize loop with mode-aware prompts and checks:

### Path A: execute and record outcome

For `completion_mode=no-commit`, `commit-make` means "do the work and record
the outcome."

Required coder behavior:

- perform the requested non-git work
- leave the worktree clean, with no staged or unstaged repo changes
- write a fresh validation comment for the current run
- write a fresh outcome comment describing what was done, what evidence was
  checked, and why no commit is expected
- do not write a commit-message comment

The orchestrator should enforce a fresh outcome comment instead of a fresh
commit-message comment for no-commit Path A. It should also fail the run if the
worktree is dirty after a no-commit task, because dirty files contradict the
selected completion mode.

### Review: inspect the outcome

For `completion_mode=no-commit`, `commit-review` means "review the recorded
outcome."

Required reviewer behavior:

- read the task description, latest outcome comment, latest validation comment,
  and relevant prior comments
- verify that the worktree is clean or trust an orchestrator-provided clean
  worktree signal
- check whether the recorded outcome satisfies the requested task
- approve or reject exactly once for the current review round

There is no staged diff to review. A reviewer should reject if the task actually
needed a repository change, the evidence is too vague to audit, or the outcome
does not satisfy the request.

### Path B: close without commit

For `completion_mode=no-commit`, approved finalization should not invoke
`git commit`. The finalizer records a `done-without-commit` comment that
summarizes the approved outcome, and the orchestrator marks the task done with
`commit_hash=NULL`.

If review is skipped for a no-commit task, successful Path A may close directly
with `done-without-commit` after writing validation and outcome comments. That
keeps no-review no-commit tasks from needing a second agent pass solely to
state that no commit will be created.

## Review Meaning

Review for no-commit tasks is an audit of evidence, not a code review.

The reviewer should answer:

- Is the selected no-commit mode appropriate for this task?
- Is the stated outcome concrete enough for a future operator to understand?
- Does the validation evidence support the outcome?
- Is the worktree clean?
- Are any follow-up repository changes needed?

Approval means "the recorded outcome is sufficient and the task can be closed
without a git commit." Rejection means "more work, clearer evidence, or a normal
commit task is required."

## UI And Status Wording

Dashboard and CLI surfaces should avoid showing a missing commit hash as a
blank failure state.

Recommended wording:

- Done table column: `Completion` instead of `Commit`
- Commit task completion: short hash, e.g. `abc12345`
- No-commit completion: `No commit`
- Task detail: `Completion mode: commit` or `Completion mode: no-commit`
- Done task detail with no commit: `Completed without commit`
- Current step labels:
  - commit mode `commit-make`: `Build commit`
  - no-commit mode `commit-make`: `Do task`
  - commit mode `commit-review`: `Review diff`
  - no-commit mode `commit-review`: `Review outcome`
  - commit mode approved Path B: `Finalize commit`
  - no-commit mode approved Path B: `Close without commit`

The raw database step names can remain unchanged for compatibility; the UI
should render mode-aware labels.

## Data Model

Add `completion_mode` to `tasks`:

```sql
completion_mode TEXT NOT NULL DEFAULT 'commit'
    CHECK(completion_mode IN ('commit', 'no-commit'))
```

Add an `outcome` comment kind for Path A no-commit evidence:

```sql
CHECK(kind IN (..., 'outcome', 'done-without-commit', ...))
```

Keep `commit_hash` nullable. For `completion_mode=commit`, `status=done`
normally implies `commit_hash IS NOT NULL`. For `completion_mode=no-commit`,
`status=done` implies `commit_hash IS NULL` plus a terminal
`done-without-commit` comment.

## Implementation Plan

1. Add `completion_mode` storage, CLI options, task display, and tests.
2. Add the `outcome` comment kind and no-commit Path A enforcement.
3. Make prompt construction inject mode-specific instructions for make and
   review.
4. Update Path B finalization to treat no-commit mode as a first-class close
   path.
5. Update dashboard and `ko-get-update` wording from `Commit` to `Completion`
   where done tasks are listed.
6. Add regression tests for:
   - no-commit Path A requires outcome and validation comments
   - no-commit Path A fails on dirty worktree
   - no-commit review consumes outcome context
   - approved no-commit finalization writes `done-without-commit`
   - done lists show `No commit` instead of `-`
   - existing `done-without-commit` history remains readable

## Open Questions

- Should no-commit tasks allow `commit-plan` and `commit-plan-review`? The
  simplest answer is yes: planning is orthogonal to whether final output lands
  in git.
- Should a human be able to convert `completion_mode` after work begins? Allow
  conversion while `status` is `none`, `ready`, or `blocked`; disallow it while
  `running` unless the orchestrator is stopped.
- Should the dashboard expose mode as a filter? This is useful after mode exists
  in the database, but it is not required for the initial implementation.
