Print a concise plain-text Kanban Orchestra status update for the current repo.

This command is for quickly reading the state of a Kanban Orchestra pipeline without opening the dashboard. It is especially useful for external agents or scripts that need to reason about pipeline health and act accordingly.

## Status Line

Start every user-facing status update with this format:

> Task **5 of 6** · **Make** · coder **Codex** · review round **5** — **Grok approved; now building and testing.**

For planning, use **Plan** and **planner** in place of **Make** and **coder**.
Map `commit-plan*` steps to Plan and `commit-make`, `commit-review`, and
`commit-finalize` to Make. Keep the maker's identity visible during review;
name the reviewer and its decision in the final clause when relevant.
Use the task's saved agent choice, including role patches, rather than today's
default. Show unknown fields as unknown; do not guess.

Use the task's position and total only for an identifiable ordered batch or
parent task, including its completed tasks. Do not derive them from task IDs
or unrelated queue history. If the batch is unknown, use **Task #162**.
Read `ko-task show <id>` for the exact persisted `review_round`; do not add one.
Resolve the current step from runtime, falling back to the task's `next_step`.
For blocked tasks, use the latest verb-bearing run-log entry and describe any
recorded resume-step transition in the final clause. If the phase cannot be
established, write **Phase unknown**.

Ground the final clause in current runtime, durable review comments, run logs,
or fresh output from the current phase. Distinguish intended work from work
actually underway. If there is no fresh evidence, say **No new agent output.**
For queued, blocked, waiting-dirty, stopped, or stale work, state that condition
plainly; do not imply execution. If there is no active, blocked, or ready task,
use **Idle — No active or queued work.**, qualifying stopped or unhealthy state
when applicable.

Keep the update to that line unless a blocker, a decision for the user, or an
explicit request needs more detail. Narration also includes its dashboard link
at startup. Show raw command output only when explicitly requested.

## When to use

- You want a quick summary of whether the pipeline is healthy
- You need to check what task is active, what step it is on, and how review is progressing
- You want to know what work is queued or blocked

## How it works

Runs the canonical status wrapper from `$ORCHESTRA_DIR` against the current worktree's queue in the central Orchestra database, then prints a structured plain-text report covering:

- **ORCHESTRATOR** — status (idle / waiting-dirty / running / starting / stopped / hard-break / stale / error) and heartbeat age
- **ACTIVE TASK** — ID, title, branch, coder, step, review-round, and review progress if in commit-review
- **READY** — tasks queued and waiting to run, with their next step
- **BLOCKED** — blocked tasks with a short note from the most recent comment
- **RECENTLY DONE** — the last 3 completed tasks and their commit hashes
- **ATTENTION** — a single sentence saying what likely needs action next

## Prerequisites

- `$ORCHESTRA_DIR` must point at the Orchestra checkout root and contain `bin/ko-get-update`
- The command infers the queue from the current worktree. A legacy
  `kanban-orchestra.db` in that worktree is a migration-required diagnostic;
  it is not opened or deleted. Inventory it with `ko-migrate preview` and
  import it only with an approved `ko-migrate cutover`.
- Queue lock, log, and dashboard files live under the central state directory
  for that queue. Agent preference files stay in the checkout.

## Command

Run from anywhere inside the repo:

```bash
"$ORCHESTRA_DIR/bin/ko-get-update"
```

Optional central-database override:

```bash
"$ORCHESTRA_DIR/bin/ko-get-update" --db <path-to-orchestra.db>
```

## Exit codes

- `0` — success (output was produced; orchestrator may still be stopped or stale)
- `1` — database not found, or command was run outside a git repo

## Interpreting the output

The **ATTENTION** line is the quick answer. Common values:

- `work is progressing normally` — nothing to do
- `orchestrator heartbeat is stale — it may have crashed; check the process` — investigate and restart
- `orchestrator is in error state — investigate: <message>` — read the message and fix
- `orchestrator is starting — wait for runtime to become idle or running` — startup was requested through a launcher
- `worktree is dirty; N ready task(s) remain queued and will start automatically once clean` — the live service is safely waiting without claiming work
- `orchestrator is stopped but N task(s) are ready — restart the orchestrator` — restart to resume work
- `hard-break state recorded — inspect blocked task/worktree before restarting` — emergency interruption state needs human review
- `N task(s) are blocked — review and unblock them` — inspect blocked tasks and resolve
- `queue is empty and orchestrator is idle — add tasks when ready` — pipeline is waiting for new work

## Steps

1. Resolve the repo root with `git rev-parse --show-toplevel`; this is the
   Orchestra instance identity.
2. If this fails, stop and tell the user they must run from inside a git repo.
3. Check that `$ORCHESTRA_DIR` is set and that `"$ORCHESTRA_DIR/bin/ko-get-update"` exists. If not, stop and tell the user the Orchestra tooling environment is not configured.
4. Run `"$ORCHESTRA_DIR/bin/ko-kanban"` when this worktree has no central queue yet. A legacy `kanban-orchestra.db` in the worktree is not that queue; leave it for `ko-migrate`.
5. Run: `"$ORCHESTRA_DIR/bin/ko-get-update"`
6. Read the primary task (active, blocking leaf, then next ready) with `ko-task show <id>`. Inspect comments, run logs, or current-phase output as needed to ground its status.
7. Report the Status Line above; include raw output only when explicitly requested.
