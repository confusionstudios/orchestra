Check the current Kanban queue once, apply one clearly safe unblock when it is justified, and return.

This is a one-shot operator check for the current repository. Inspect once,
perform one clearly safe local recovery when existing authorization and
authoritative evidence make that exact recovery unambiguous, and return the
session to the user. Stay in this conversation. Do not narrate, poll, wait
for task completion, start a background process, or spawn another
investigation agent.

This skill is not read-only narration and it is not the orchestrator's
autonomous recovery. `narrate` watches the queue and leaves every recovery
to the native loop. Native smart-unblock, when the orchestrator is running,
consults the effective `unblocker` about once a minute and either continues
a task or leaves a durable `smart-unblock` comment. This skill does not
start, replace, or repeat that loop. It cooperates with it: a live
consultation owns the task, and a refused recovery is not retried.

## Prerequisites

1. Resolve the repo root with `git rev-parse --show-toplevel`. Stop if that fails.
2. Require `$ORCHESTRA_DIR`, `bin/ko-get-update`, and `bin/ko-task`.
3. The current worktree must already have a queue in the central store. A
   legacy `kanban-orchestra.db` in the worktree is not that queue. Do not
   open, import, or delete it.
4. Operate only on this repo's queue. Do not edit the database, bypass locks,
   or switch queues.

Define the task CLI once:

```bash
task() { "$ORCHESTRA_DIR/bin/ko-task" "$@"; }
```

## Status line

Start the user-facing report with this format:

> Task **5 of 6** · **Make** · coder **Codex** · review round **5** — **Grok approved; now building and testing.**

For planning, use **Plan** and **planner** in place of **Make** and **coder**.
Map `commit-plan*` steps to Plan and `commit-make`, `commit-review`, and
`commit-finalize` to Make. Keep the maker's identity visible during review;
name the reviewer and its decision in the final clause when relevant.
Use the task's saved agent choice, including role patches, rather than today's
default. Show unknown fields as unknown; do not guess.

Use the task's position and total only for an identifiable ordered batch or
parent task, including its completed tasks. A title prefix `N/M`, or a
complete sibling list from `task list --parent <id>` with `sequence_index`,
is that batch. Do not derive a position from task ids, a paged listing, or
unrelated queue history. If the batch is unknown, use **Task #162**.
Read `task show <id>` for the exact persisted `review_round`; do not add one.
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

After that line, add text only for the recovery you performed or the user
decision you need. Omit the dashboard link. Show raw command output only when
the user explicitly asks for it.

## One pass

Run `"$ORCHESTRA_DIR/bin/ko-get-update"` once. Trust its `ORCHESTRATOR` line
for health. It already reports `stale` when a heartbeat is too old, and it
keeps `starting`, `stopping`, `stopped`, and `hard-break` as their own states.
Use the `lock:` path's directory as this queue's runtime directory.

Classify from that report before reading anything else. Use the first matching row:

| Evidence | Action |
| --- | --- |
| `starting` | Say startup was requested, then stop. Do not continue a task while startup is in progress |
| `hard-break` | Name the emergency state. If a task is blocked, include its latest block comment, then ask before any restart or continue |
| One or more blocked tasks | Inspect the blocking leaf once, using the bounded reads below |
| `waiting-dirty` | Say pickup is paused until the worktree is clean, then stop |
| `stopped`, `stopping`, `stale`, or `error` | Name that state and the ready count. Ask before any restart. Stop |
| No active, ready, or blocked task | Idle status line, then stop |
| Orchestrator `running` or `idle` | Status line for the active task, or the next ready task when nothing is active, then stop |

Healthy and idle outcomes end here. Do not sample transcripts, ping agents,
or wait for the active task to finish.

A stopped or stale orchestrator is a worker-health fact, not by itself a
blocked task. Ready tasks are queued work, not blocked work. `waiting-dirty`
means the live service is paused on a dirty tree; it is not a request to
clean, stash, or commit.

## Blocking leaf

If a blocked task is a supertask, target its first blocked child from
`task list --parent <id> --status blocked`. Otherwise target the first
blocked task in the update. Confirm the leaf with one `task show <id>`.
Use `task list --status blocked` only to identify that leaf.
If several tasks are blocked, inspect the leaf only. Mention the remaining
count. Do not walk the rest of the queue in this pass.

On that task, read only the evidence that explains the current block:

```bash
task show <id>
task show-comments <id>
task show-run-log <id>
```

`show-comments` is oldest first. Use the latest orchestrator block comment
and any later human or `smart-unblock` comments. `show-run-log` is newest
first. Use the newest row with a non-empty `verb`, and note whether that
verb is `smart-unblock`. Skip older history once the current block is clear.

Read a transcript only when those fields still leave the block unexplained.
Take the step from that verb, not from `next_step` when it is `none`.
Transcripts live in `<runtime>/artifacts/task-<id>/`, beside the lock from
`ko-get-update`. Select the dashboard's newest `*-<step>-*.log` by
`(st_mtime_ns, basename)` and read at most the last 80 lines. If that file
is absent, say the transcript was not found and decide from the task fields.
Do not read `orchestrator.log`, other tasks, or a second step.
Do not run health pings, `ko-agent-smoke`, model refresh, or another agent.

Probe native ownership before any mutation. The probe is read-only: if the
lock file is absent, no consultation is live. If it exists, take a
non-blocking exclusive flock, release it immediately when the acquire
succeeds, and do not create, truncate, or write the file. A held flock is
the watcher. Trust `consultation_task_id` only while that flock is held.
Leftover lock text after a crash is not a live consultation.

```bash
python3 -c '
import fcntl, sys
from pathlib import Path
path = Path(sys.argv[1])
if not path.is_file():
    print("running=no")
    print("consultation_task_id=")
    raise SystemExit(0)
handle = path.open("rb")
try:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        task_id = ""
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key == "consultation_task_id" and value.isdigit():
                task_id = value
                break
        print("running=yes")
        print(f"consultation_task_id={task_id}")
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        print("running=no")
        print("consultation_task_id=")
finally:
    handle.close()
' "<runtime>/smart-unblock.lock"
```

If `consultation_task_id` is the target task, native recovery owns it.
Report that and return. If the probe cannot finish promptly, say the
consultation state is unknown and do not mutate.

## Safe continue

Apply a recovery only when both existing authorization and the evidence
select one exact local command.

Authorization is one of:

- The user asked for this check. That authorizes one flagless continue that
  the checklist below already makes exact. It does not authorize a review-cap
  increase, a service restart, a credential repair, a file cleanup, or a code fix.
- A durable human comment newer than the current block. Ignore authors
  `orchestrator` and `smart-unblock`. The current block starts at the latest
  orchestrator comment whose message starts with `Blocked:` or contains
  `blocked with task WIP`. The human message must be a single line matching
  `CONTINUE` or `RESUME`, optionally followed by `+N` (for example
  `CONTINUE +1`). While the watcher flock is held, leave that comment for
  native recovery to apply. When the watcher is not running, this comment
  authorizes the matching command below.

The flagless command is:

```bash
task continue <id>
```

Use it only when every item below is true:

- `status` is `blocked`.
- `resume_next_step` is non-empty, and `block_reason` is not `review_cap`.
- The latest block comment and the latest non-`smart-unblock` verb agree
  that this is a transient resume at that stored step. Reviewer
  infrastructure exhaustion, with `block_reason` `reviewer_unavailable`,
  is that case. A failed stash restore, follow-up reconciliation, a maker
  or finalization failure, or a comment that names missing credentials,
  login, auth, or a product, code, or stash repair is not.
- No later `smart-unblock` comment on this same block records `BLOCKED`,
  unless a still-later human `CONTINUE` or `RESUME` authorizes this command.
- `git status --porcelain` is empty. A recorded `stash_ref` may hold the
  task's WIP; leave that ref in place. A clean worktree on another branch
  still passes; do not check out the task branch. Dirty or unattributed
  files fail this check. Do not stash, pop, reset, clean, or commit.
- Saved `coder_agent`, `reviewer_agent`, `review_round`, and `stash_ref`
  stay as they are. The command preserves them. Do not set agents, skips,
  review decisions, or `status` directly.
- No live consultation is published for this task.

A human `+N` on a `review_cap` block, when the watcher is not running,
authorizes only:

```bash
task continue <id> --add-review-rounds N
```

Use the N from that comment. Do not choose N yourself, and do not pass
`--next-step`. Structured review-cap metadata already stores the resume
step. A `+N` on any other block is not a cap grant; ask instead of applying
it. Legacy blocks without `block_reason` and `resume_next_step` need an
explicit operator decision; do not invent the legacy two-flag command.

`task set --status ready` is not a recovery. Do not use it to bypass
`review_cap` or `reviewer_unavailable` metadata.

Run the one command. Then run `task show <id>` again. Accept the recovery
only when `status` is `ready`, `next_step` is the stored resume step,
`block_reason` and `resume_next_step` are clear, and `review_round`,
`stash_ref`, and both saved agents are unchanged. Report the verified
fields in the final clause. If the orchestrator is running, say the task
is requeued and stop. If it is stopped or stale, say the task is ready and
will not run until a start is requested. Do not start it, and do not wait
for execution.

If the command exits non-zero, including the consultation-gate refusal,
report the error and stop. Do not retry, write a substitute comment, or
edit the task another way.

## Ask the user

When the checklist fails, return the concrete blocker, the single proposed
command, and the decision or prerequisite you need. Then stop.

- Review cap, with no authorized `+N`: report `review_round`,
  `max_review_rounds`, and `resume_next_step`. Propose
  `task continue <id> --add-review-rounds N` and ask for N.
- Dirty or unattributed files, a failed stash, or WIP whose owner is
  unclear: report `stash_ref` and a short porcelain summary. Ask whether
  those files belong to this task. Leave them untouched.
- Missing credentials or a comment that names a failed login or auth:
  name that prerequisite. Do not ping, log in, or continue past it.
- Stopped, stale, error, hard-break, or starting workers: name the state
  and ask before any restart. Do not start the orchestrator, fleet, or
  dashboard. A stored reviewer-infrastructure resume is the flagless
  continue above, not a service restart.
- Ambiguous ownership, a code fix, an external action, or a destructive
  git action: name the choice and wait.
- Native `BLOCKED` comment still covering this block: quote its requested
  decision. A later human `CONTINUE`/`RESUME` is new authorization; the
  old refusal is not something to override.
- Two or more safe leaves: continue only the primary leaf, say that the
  others remain blocked, and stop.

## Representative outcomes

- Healthy running queue, no blocked task: status line grounded in the
  active phase, then stop.
- Empty queue: the idle line, plus stopped or stale when the orchestrator
  line says so, then stop.
- `reviewer_unavailable`, stored resume step, clean tree, no live
  consultation, and no later `smart-unblock` `BLOCKED` comment:
  `task continue <id>`, verify, then stop.
- Dirty tree or a stash restore that the block comment says needs repair:
  propose nothing that touches files. Ask who owns the WIP.
- `review_cap`: ask for N. Do not grant rounds.
- `consultation_task_id` equals the blocked task: say native smart-unblock
  is consulting it, then stop.
