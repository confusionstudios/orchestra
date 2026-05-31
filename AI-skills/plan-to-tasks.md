Turn a Codex or Claude plan.md into ordered Kanban Orchestra tasks.

Use this skill when the user asks you to convert a written implementation
plan into Kanban Orchestra tasks, queue tasks from a plan, or prepare a task
breakdown from a `plan.md` file.

This is an operator skill. You are managing Kanban task records, not
implementing the plan yourself.

## Inputs

Start by identifying:

- The plan file path. If the user did not specify one, look for an obvious
  `plan.md` in the current repo or ask for the path.
- The target branch. Prefer an explicit user-provided branch; otherwise infer
  a feature branch from the plan only when it is unambiguous.
- Whether the user wants draft tasks only or wants tasks queued as ready.

Do not silently assume draft vs ready. Phrases such as "create tasks",
"break this into tasks", or "draft tasks" mean draft task records. Phrases
such as "queue these", "set them ready", or "run this plan" mean create the
tasks and queue them.

## Read the plan

Read the whole plan before creating tasks. Extract runnable slices that can
land independently, in order. A good slice has:

- One clear outcome.
- A bounded set of files or behavior to change.
- Verification that can be run by the task's coder.
- A dependency relationship that is obvious from the sequence.

Avoid making tasks for narrative headings, open questions, or vague cleanup.
If the plan contains a required decision that must happen before coding,
create an `other` task for that decision or leave the affected coding task in
`none` with the blocker called out in its description.

## Choose task shape

Use ordinary commit tasks for code, tests, docs that should land in git, and
repo-local configuration changes.

Use `other` tasks for work that should not produce a commit, such as an
external decision, manual release action, or durable note.

Use `pull_request` tasks only when the task is specifically to create or
update PR metadata for an already prepared branch.

Use a supertask only when the user asked for a reviewed decomposition inside
Kanban itself. In the normal plan-to-tasks workflow, create the child tasks
directly from the plan.

## Write task titles

Keep titles short, imperative, and outcome-oriented:

- Good: `Add parser fixtures`
- Good: `Wire retry state into dashboard`
- Avoid: `Step 2`
- Avoid: `Implement all API changes`

When the order matters and the tasks are top-level tasks, prefix titles with
`1/N`, `2/N`, and so on. This makes ordering visible in dashboards and review
comments even before task IDs are known.

## Write descriptions

Descriptions are Markdown source. Keep them concise but complete enough that
a coder can work without rereading the entire plan. Include:

- `## Outcome` with the specific result for this task.
- `## Scope` with the relevant files, modules, or behavior.
- `## Notes` with dependencies or plan context that matters.
- `## Acceptance` with concrete checks or expected behavior.

Do not paste the whole plan into every task. Preserve only the context needed
for that slice.

## Branch safety

Every runnable commit or pull-request task needs a branch. Prefer a feature
branch. Do not queue tasks on `master` or `main` unless the work repo has
explicitly opted in with a standalone `ALLOW_TASKS_ON_MASTER` line in
`AGENTS.md`.

If the plan names `master` or `main` and the marker is absent, stop and ask
for a feature branch or choose a clearly named feature branch when the user
has authorized you to infer one.

Child tasks under a supertask inherit the parent branch; omit `--branch` for
those children.

## Next step

Set a meaningful `next_step` before any task is made ready:

- `commit-make` for ordinary commit tasks whose plan review is being skipped
  or already handled.
- `commit-plan` for commit tasks where the coder should first produce an
  implementation plan.
- `commit-make-supertask` for supertasks that still need decomposition.
- `pull-request-make` for pull request tasks.
- `other-make` for non-commit tasks.

Never leave a queued task at `next_step: none`. A task with no meaningful
next step can be dropped immediately by the orchestrator.

## Draft vs ready

Drafting tasks means creating or updating records while leaving `status` as
`none`. This is the right default when the user asks to review the breakdown
before running it.

Queueing tasks means setting `status` to `ready` after branch and `next_step`
are correct. `ready` is the normal backlog, not a concurrency request. It is
correct to set a whole ordered batch to `ready`; the orchestrator serializes
pickup and enforces branch, blocked-state, lifecycle, and sequence checks.

Do not keep later tasks at `none` merely because earlier tasks should run
first. Use numeric titles, `sequence_index` where applicable, dependencies in
descriptions, blocked states, stop markers, or explicit validation tasks when
a real gate is needed.

## Command pattern

Define the task helper once:

```bash
task() { "$ORCHESTRA_DIR/bin/ko-task" "$@"; }
```

For draft top-level commit tasks:

```bash
task add "1/3 Add parser fixtures" \
  --description "$description" \
  --branch feature/parser-plan
task set <id> --next-step commit-make
```

For queued top-level commit tasks:

```bash
task add "1/3 Add parser fixtures" \
  --description "$description" \
  --branch feature/parser-plan
task set <id> --next-step commit-make
task set <id> --status ready
```

For a batch, create or update all records first, verify them with `task show`
or `task list`, then set each task to `ready` in planned order if the user
asked for queueing.

For child tasks under a supertask:

```bash
task add "Add parser fixtures" \
  --description "$description" \
  --parent <supertask-id> \
  --sequence-index 100
```

Children default to `ready` and are gated by the parent and sibling order.

## Verification

Before reporting completion:

1. Run `task list --branch <branch>` or inspect the created task IDs.
2. Confirm titles are ordered and understandable.
3. Confirm each task has the intended type, branch, and `next_step`.
4. Confirm `status` is `none` for drafts and `ready` only for tasks the user
   asked to queue.
5. Record any assumptions, skipped plan items, or blockers in your response
   or in a durable task comment when operating inside an orchestrated task.
