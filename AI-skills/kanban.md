Use Kanban Orchestra in the current working repo.

## Role check — read this first

Before doing anything else, determine which role applies:

| Role | How to tell | What to do |
|------|-------------|------------|
| **Observer / operator** | Running ad-hoc, no orchestrator spawned you, or you are checking status for a user | Use `get-kanban-update` for status. Use `task list/show/add/set` for task management. |
| **Sticky coder** | The orchestrator explicitly spawned you as the designated build agent for a specific task | Follow the full workflow for that task. |

Default role: observer.

Do not infer agent choices from previous runs; leave agent flags unset and rely on configured defaults unless the user explicitly overrides them.

Kanban Orchestra is a local task orchestration system for AI agent work.
Tasks live in a SQLite database in the work repo, the orchestrator processes
`ready` tasks one at a time, each task carries its configured code reviewer
for `commit-review`, and the dashboard shows live state.

This file is the operational quickstart. The canonical spec lives at:

```text
$ORCHESTRA_DIR/tasks/kanban-orchestra-spec.md
```

Use the spec as the source of truth for task lifecycle, semantics, recovery,
and runtime behaviour. Use this file for bootstrap, paths, concrete CLI
commands, and quick stuck/not-stuck checks.

This skill requires `$ORCHESTRA_DIR` to point at the Orchestra installation
or checkout root. That installation provides the wrapper commands, including
`ko-kanban`, `ko-task`, `ko-orchestrator`, `ko-fleet`, and `ko-get-update`. Invoke them through
`"$ORCHESTRA_DIR/bin/..."`; do not assume they are on `PATH`.

## Task CLI shorthand

The CLI is exposed through the `ko-task` wrapper. Define `task` once in your shell:

```bash
task() { "$ORCHESTRA_DIR/bin/ko-task" "$@"; }
```

All commands below use that shorthand. If you don't set the function, use
`"$ORCHESTRA_DIR/bin/ko-task"` directly.

## Workspace rule

`$ORCHESTRA_DIR` points at the Orchestra checkout that provides the tooling.
The shell's current git repo is the launched work repo whose Kanban database,
tasks, git diff, and runtime state the wrappers operate on unless a command
explicitly documents another target. These may be the same checkout when
working on Orchestra itself.

Edit only files inside the current checkout root. If `$ORCHESTRA_DIR` resolves
to that same root, files such as `kanban-orchestra/...` and `AI-skills/...` are
normal task targets. If `$ORCHESTRA_DIR` is a different checkout, treat it as
tooling/reference unless the user explicitly asks you to edit there. Before
your first edit, state the repo root (`git rev-parse --show-toplevel`) and at
least one target path you plan to modify under that root.

## Bootstrap

1. Treat the current working repo as the work repo.
2. Resolve the repo root with `git rev-parse --show-toplevel`.
3. Require `$ORCHESTRA_DIR`.
4. Remember the path relationship:
   - Current checkout / repo root: the work repo the wrappers operate on.
   - `$ORCHESTRA_DIR`: the Orchestra checkout that provides wrappers and
     implementation files; it may be the same path as the current repo root.
5. Canonical paths:
   - DB: `<repo-root>/kanban-orchestra.db`
   - SQL dump: `<repo-root>/kanban-orchestra.sql`
   - Runtime dir: `<repo-root>/.kanban-orchestra/`
   - Orchestrator output: `<repo-root>/.kanban-orchestra/orchestrator.log`
   - Orchestra repo root: `$ORCHESTRA_DIR`
   - Kanban Orchestra root: `$ORCHESTRA_DIR/kanban-orchestra/`
   - Scripts: `$ORCHESTRA_DIR/kanban-orchestra/scripts/`
   - Prompts: `$ORCHESTRA_DIR/kanban-orchestra/prompts/`
6. Bootstrap the repo:
   ```bash
   "$ORCHESTRA_DIR/bin/ko-kanban"
   ```
7. Report the resolved context: repo root, orchestra path, kanban orchestra
   root, DB path, orchestrator output path, and whether the DB was created
   or reused.

## Task lifecycle

A commit task moves through these steps:

1. `commit-plan` — coder drafts a plan. *Skippable* via `--skip commit-plan`
   on `task add` (or `task set --add-skip`); skipping it also bypasses
   `commit-plan-review`.
2. `commit-plan-review` — reviewer assesses the plan. *Skippable when a plan
   exists.*
3. `commit-make` (Path A) — coder builds and stages the change. Always runs.
4. `commit-review` — reviewer inspects `git diff --cached`. *Skippable.*
5. `commit-make` (Path B) — same coder considers approval notes and finalizes the commit after approval.
   Always runs.

Supertasks substitute `commit-make-supertask` and `commit-review-supertask`
for steps 3–4; the supertask itself never lands a commit.
`commit-review-supertask` is *skippable*.

Pull request tasks use `pull-request-make` and `pull-request-review`.
Other tasks use `other-make` and `other-review`, and must leave durable
completion evidence in a task comment instead of a commit or PR.

When a prior `commit-make` saved WIP via `git stash`, the orchestrator
prepends Path C (`commit-make-stash-recovery.md`) to the next `commit-make`
prompt so the coder restores that work first.

Valid skip values: `commit-plan`, `commit-plan-review`, `commit-review`,
`commit-review-supertask`, `pull-request-review`, `other-review`.

## Queueing semantics

`ready` is the runnable backlog, not a concurrency request. Multiple tasks in
`ready` are normal and expected. The orchestrator selects eligible `ready`
tasks one at a time, applies branch, blocked-state, worktree, and lifecycle
checks, and serializes execution through the configured `next_step`.

Operationally:
- `none` means the task exists but is not queued yet.
- `ready` means the task is queued and eligible for pickup.
- Setting many tasks to `ready` does not bypass sequencing or validation, and
  it does not make them run concurrently.
- Do not hold later tasks at `none` merely because earlier tasks should run
  first. Use ordering fields, explicit dependencies, blocked states, stop
  markers, or separate validation tasks when the workflow requires a gate.

Operator guidance:
- If the user asks to queue or cue a batch of tasks, create or update them in
  numeric or planned order, set each task's branch and meaningful `next_step`,
  then set each task to `ready` unless the user explicitly asks to pause
  between tasks.
- Do not invent validation gates from plan prose. If gates are required, model
  them explicitly as tasks, blocked states, stop markers, or leave specific
  tasks at `none` only when the user requested that.
- If the user says to set tasks ready one by one in order, perform the updates
  sequentially in that order; do not wait for each task to complete before
  readying the next one unless the user says to.

## Task management

All commands assume the `task` alias above.

By default, Kanban tasks may not be queued or run on `master` or `main`.
Use a feature branch, or explicitly opt the repo in by adding
`ALLOW_TASKS_ON_MASTER` as a standalone line in the work repo's `AGENTS.md`.

```bash
task add "<title>" \
    [--description "<description-as-markdown>"] \
    [--branch <branch>] [--coder-agent <agent>] [--reviewer-agent <agent>] \
    [--type <commit|pull_request|supertask|other>] [--kind <legacy-kind>] [--parent <task-id>] \
    [--sequence-index <n>] [--skip <step>] [--allow-when-blocked]

task list [--status <status>] [--next-step <step>] [--branch <branch>] [--page <n>]
task show <task-id>
task show-comments <task-id>
task show-run-log <task-id>

task set <task-id> \
    [--status <status>] [--next-step <step>] [--branch <branch>] \
    [--description "<description-as-markdown>"] [--commit <hash>] [--coder-agent <agent>] [--reviewer-agent <agent>] \
    [--review-round <n>] [--last-review-decision <decision>] \
    [--commit-plan "<text>"] [--allow-when-blocked <bool>] \
    [--add-skip <step>] [--remove-skip <step>]

# Comments (use --message-stdin for multi-line / shell-sensitive text):
cat <<'EOF' | task comment <task-id> --message-stdin [--comment|--commit-message|--validation] [--author <name>]
<message>
EOF

cat <<'EOF' | task comment <task-id> --message-stdin [--approval|--rejection] --author <name> --review-round <n>
<message>
EOF

cat <<'EOF' | task comment <task-id> --message-stdin [--plan-approval|--plan-rejection] --author <name>
<message>
EOF

task log <task-id> "<message>"
```

## Process control

```bash
"$ORCHESTRA_DIR/bin/ko-orchestrator"
```

`ko-orchestrator` is the repo instance: it runs the durable worker and starts
the matching dashboard for the same git repo root. Resolve the current git repo
root and treat that launch directory as the Orchestra instance identity. The
orchestrator records that identity in the repo-scoped `kanban-orchestra.lock`,
and the dashboard writes `.kanban-orchestra/dashboard.json`.

Use status commands and repo-local metadata instead of PID hunting:

```bash
"$ORCHESTRA_DIR/bin/ko-get-update"
"$ORCHESTRA_DIR/bin/ko-fleet" status
"$ORCHESTRA_DIR/bin/ko-fleet" precheck
"$ORCHESTRA_DIR/bin/ko-fleet" start
"$ORCHESTRA_DIR/bin/ko-fleet" stop <repo-label>
"$ORCHESTRA_DIR/bin/ko-fleet" restart <repo-label>
"$ORCHESTRA_DIR/bin/ko-fleet" attach <repo-label>
"$ORCHESTRA_DIR/bin/ko-fleet" logs <repo-label>
"$ORCHESTRA_DIR/bin/ko-fleet" dashboard <repo-label>
```

Fleet config lives at `~/.config/orchestra/fleet.repos` by default. It is a
private flat list: one git repo root per non-empty line, with `~`, environment
variables, blank lines, and `#` comments supported. Every configured repo gets
one orchestrator and one matching dashboard. `ko-fleet start` refuses to launch
anything if any selected repo is dirty or invalid.

Graceful stop after the current task finishes:

```bash
touch KANBAN_ORCHESTRATOR_STOP_AFTER_TASK
```

The graceful stop marker is separate from `stop`: the orchestrator detects it
at the top of the next polling loop after the current task finishes, logs the
detection, deletes the file, and exits.

Notes:
- Use `ko-fleet dashboard <repo-label>` to open a running instance dashboard.
- Use `ko-fleet dashboard-open <repo-label>` when a script wants an explicit
  open verb; it is an alias for `dashboard`.
- The dashboard chooses a free port at runtime and records it in
  `.kanban-orchestra/dashboard.json`.
- The dashboard is the repo-scoped read-only status surface. The old
  process-manager UI and its `BREAK` command have been removed; stop or
  interrupt the repo instance, inspect with `ko-get-update`, use `ko-task` to
  record or unblock the affected task, and restart explicitly.

## Default operating pattern

- `task add` to create tasks.
- `task list` and `task show` to inspect work.
- `task set` to move tasks between `none`, `ready`, `blocked`, and `done`.
- Before setting a task to `ready`, set `next_step` to a meaningful step
  (typically `commit-make`); tasks left at `next_step: none` get picked up
  and immediately dropped with no work executed.
- `task add --branch master/main`, `task set --branch master/main`, and
  `task set --status ready` for a task whose branch resolves to `master` or
  `main` require the repo-local `ALLOW_TASKS_ON_MASTER` marker.
- Use `--allow-when-blocked` for tasks that should stay runnable while
  another task is blocked.
- `task comment` for durable notes and review outcomes; `task log` for
  ephemeral progress notes.

Batch queueing example:

```bash
task add "1/3 Add parser fixture" --branch feature/parser-cleanup
task add "2/3 Update parser behavior" --branch feature/parser-cleanup
task add "3/3 Document parser behavior" --branch feature/parser-cleanup

task set 101 --next-step commit-make
task set 102 --next-step commit-make
task set 103 --next-step commit-make

task set 101 --status ready
task set 102 --status ready
task set 103 --status ready
```

This queues all three tasks for serialized pickup. Do not use `master` or
`main` for `--branch` unless the work repo has opted in with the standalone
`ALLOW_TASKS_ON_MASTER` marker in `AGENTS.md`.

## Important notes

- Orchestra is sensitive to dirty worktrees. The orchestrator refuses to
  launch when the work repo is dirty, and active queue processing can block if
  unexpected uncommitted changes appear.
- A task needs a branch and a meaningful `next_step` before it can be set
  to `ready`.
- If the orchestrator is idle and the work repo is dirty, `task set --status
  ready` is rejected. Clean or stash the worktree before queueing more work.
- Pass `--branch` explicitly when creating tasks; do not rely on prompts.
- Use feature branches by default. The `ALLOW_TASKS_ON_MASTER` `AGENTS.md`
  marker is an explicit opt-in for repos that intentionally operate on
  `master` or `main`.
- `task comment --message-stdin` is the safe form for multi-line or
  shell-sensitive text.
- Code-review approvals/rejections (`--approval`, `--rejection`) require
  both `--author` and `--review-round`.
- Plan-review decisions (`--plan-approval`, `--plan-rejection`) require
  `--author` only.

## Self-hosting Orchestra

When the current repo root is the same path as `$ORCHESTRA_DIR`, Kanban state
still belongs to the launched repo root. The database, SQL dump, runtime files,
locks, logs, and dashboard metadata live beside the Orchestra source and are
gitignored.

After pulling or editing Orchestra code in a self-hosted checkout, restart any
running orchestrator or fleet-owned instances. Long-lived Python processes may
continue using the code they started with, while new wrapper commands use the
current checkout.

## Stuck/not-stuck checks

First choice: `ko-get-update`.

```bash
"$ORCHESTRA_DIR/bin/ko-get-update"
```

Use `ko-fleet dashboard <repo-label>` to open the HTML dashboard for a running
fleet instance. The dashboard shows orchestrator status, the active task,
reviewer state, and current orchestrator output.

CLI-level state check via `orchestrator_runtime`:

```bash
sqlite3 kanban-orchestra.db "select status, current_task_id, current_step, current_branch, review_round, active_agents, status_message, last_heartbeat_at from orchestrator_runtime;"
```

Read the result like this:
- `status = running` with a fresh heartbeat usually means active work.
- `status = starting` means a launcher accepted a start request and is
  launching the orchestrator.
- `status = idle` with a fresh heartbeat means the orchestrator is healthy
  and waiting.
- `status_message` starting with `STALLED:` means the orchestrator is
  waiting for an agent ping acknowledgment and retries every 60 seconds.
- An old `last_heartbeat_at` means stale or dead.
- `status = stopped` means the supervised orchestrator was stopped or paused.
- `status = hard-break` means an emergency interruption state was recorded;
  check the blocked task comment and worktree before restarting.
- `status = error` means an orchestrator-level failure.
- List blocked tasks with `task list --status blocked`.

When any task is `blocked`, only `ready` tasks with `allow_when_blocked`
enabled remain eligible for pickup.

Stay conversational with the user, but use these concrete commands when
operating the kanban system.
