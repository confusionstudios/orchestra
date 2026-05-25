Print a concise plain-text Kanban Orchestra status update for the current repo.

This command is for quickly reading the state of a Kanban Orchestra pipeline without opening the dashboard. It is especially useful for external agents or scripts that need to reason about pipeline health and act accordingly.

## When to use

- You want a quick summary of whether the pipeline is healthy
- You need to check what task is active, what step it is on, and how review is progressing
- You want to know what work is queued or blocked

## How it works

Runs the canonical status wrapper from `$ORCHESTRA_DIR` against the current repo's `kanban-orchestra.db`, then prints a structured plain-text report covering:

- **ORCHESTRATOR** — status (idle / running / starting / stopped / hard-break / stale / error) and heartbeat age
- **ACTIVE TASK** — ID, title, branch, coder, step, review-round, and review progress if in commit-review
- **READY** — tasks queued and waiting to run, with their next step
- **BLOCKED** — blocked tasks with a short note from the most recent comment
- **RECENTLY DONE** — the last 3 completed tasks and their commit hashes
- **ATTENTION** — a single sentence saying what likely needs action next

## Prerequisites

- `$ORCHESTRA_DIR` must point at the Orchestra checkout root and contain `bin/ko-get-update`
- The current repo root must contain `kanban-orchestra.db`
- The current repo root is the Orchestra instance identity; the
  `kanban-orchestra.lock` metadata and `.kanban-orchestra/` files apply only to
  that repo.

## Command

Run from anywhere inside the repo:

```bash
"$ORCHESTRA_DIR/bin/ko-get-update"
```

Optional flag:

```bash
"$ORCHESTRA_DIR/bin/ko-get-update" --db <path-to-kanban-orchestra.db>
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
- `orchestrator is stopped but N task(s) are ready — restart the orchestrator` — restart to resume work
- `hard-break state recorded — inspect blocked task/worktree before restarting` — emergency interruption state needs human review
- `N task(s) are blocked — review and unblock them` — inspect blocked tasks and resolve
- `queue is empty and orchestrator is idle — add tasks when ready` — pipeline is waiting for new work

## Steps

1. Resolve the repo root with `git rev-parse --show-toplevel`; this is the
   Orchestra instance identity.
2. If this fails, stop and tell the user they must run from inside a git repo.
3. Check that `$ORCHESTRA_DIR` is set and that `"$ORCHESTRA_DIR/bin/ko-get-update"` exists. If not, stop and tell the user the Orchestra tooling environment is not configured.
4. Check that `$(git rev-parse --show-toplevel)/kanban-orchestra.db` exists. If not, stop and tell the user this repo does not have Kanban Orchestra initialized.
5. Run: `"$ORCHESTRA_DIR/bin/ko-get-update"`
6. Report the output to the user verbatim, then offer a brief interpretation if they want one.
