Narrate Kanban Orchestra live in this session and run a durable background smart-unblock watcher.

This skill coordinates two independent loops for the current repository:

- **Narration** runs inside your agent session. You sample every 10 seconds and
  stop when the queue is genuinely finished or the user stops you.
- **Smart unblocking** runs as a durable background process started with
  `$ORCHESTRA_DIR/bin/ko-unblock`. It checks once per minute and keeps running
  until it is explicitly stopped, including after narration ends. A running
  orchestrator already runs this same watcher loop natively as part of its own
  process, sharing the same repo-scoped lock — `ko-unblock start` against a
  repo with an active orchestrator simply reports `already running` rather
  than duplicating the work. Start it explicitly here so unblocking keeps
  running even when the orchestrator itself is stopped.

Narration is read-only reporting: monitor the repo with
`$ORCHESTRA_DIR/bin/ko-get-update` and `$ORCHESTRA_DIR/bin/ko-task`, and leave
every recovery decision to the watcher.

## Prerequisites

1. Resolve the repo root with `git rev-parse --show-toplevel`. Stop if the
   command fails.
2. Require `$ORCHESTRA_DIR`, `bin/ko-get-update`, `bin/ko-task`, and
   `bin/ko-unblock`.
3. Require `<repo-root>/kanban-orchestra.db`.
4. Monitor only this repo. A running orchestrator is preferred, but report
   stopped or stale state honestly.

## Start smart unblocking first

Before the opening report, make sure the background watcher is running for this
repo:

```bash
"$ORCHESTRA_DIR/bin/ko-unblock" status
"$ORCHESTRA_DIR/bin/ko-unblock" start
```

`start` is safe to call when a watcher already runs: only one watcher may hold
the repo lock, and a second start reports `"started": false` with reason
`already running` instead of launching a duplicate. Stop it only when the user
asks:

```bash
"$ORCHESTRA_DIR/bin/ko-unblock" stop
```

`stop` also ends any consultation the watcher is in the middle of, so no agent
can comment on or continue a task after stop reports success.

Report in one clause whether the watcher was already running or you started it.
Never stop the watcher because narration is ending.

## Opening report

Immediately sample state and give one short plain-language summary:

- Orchestrator state: running, idle, stopped, stale, or error
- Active task, phase, and agent when known
- Ready queue depth or blocked task
- One grounded clause from new agent output when available

Use the update prefix below. Do not paste raw update output, JSON, logs,
transcript dumps, or tables.

## Update prefix

Immediately before every visible narration update, run:

```bash
date '+%H:%M:%S'
```

Prefix the update with the clock time, primary task, verb, and exact persisted
`review_round`:

```text
[14:37:09] Task 162 · commit-review · round 2 — <short narration>
```

Choose the primary task in this order: active task, blocking leaf task, next
ready task. Always read that task with `ko-task show <id>` to obtain its exact
`review_round` and task fields. Resolve an active task's verb from runtime
`current_step`, falling back to task `next_step`; resolve a ready task's verb
from `next_step`.

For a blocked task, use the latest verb-bearing run-log entry. If the stored
`resume_next_step` exists and differs from that verb, include it as a
transition:

```text
[14:38:12] Task 162 · blocked · commit-review → commit-make · round 2 — <short narration>
```

If the resume step is absent or matches the blocked verb, show only the
blocked verb. If no authoritative verb exists, write `verb unknown`; never
guess one:

```text
[14:38:12] Task 162 · blocked · verb unknown · round 2 — <short narration>
```

Use the task row's numeric round without converting it to one-based display.
If there is genuinely no active, blocked, or ready task, use:

```text
[14:39:05] Idle — No active or queued work.
```

Generate the time immediately before writing the update so investigation does
not leave it stale. Do not include a timezone or fractional seconds. Recovery
notices, unchanged-state updates, questions, and the final idle report all use
the same prefix contract.

## Monitoring loop

Sample every 10 seconds until the user stops you or the queue is genuinely
idle with no active, ready, or blocked work. Keep this loop in your agent
session: narration ends with the session, and the only background process you
start is the smart-unblock watcher above.

Each sample:

1. Run `"$ORCHESTRA_DIR/bin/ko-get-update"`.
2. Identify the primary task. Run `ko-task show <id>` for its persisted
   `review_round`, `next_step`, and block metadata. For a blocked task, inspect
   `ko-task show-run-log <id>` to resolve the latest authoritative verb.
3. Read other targeted detail only when needed:

   ```bash
   "$ORCHESTRA_DIR/bin/ko-task" show <id>
   "$ORCHESTRA_DIR/bin/ko-task" show-comments <id>
   "$ORCHESTRA_DIR/bin/ko-task" show-run-log <id>
   "$ORCHESTRA_DIR/bin/ko-task" list --status blocked
   ```

4. Sample the current phase's latest transcript using the recipe below.
5. Narrate material change in one or two sentences. If nothing changed, say
   so briefly without inventing progress.
6. If work is blocked, report the block and what the watcher has recorded for
   it; do not recover it yourself.

## Live agent output

Use new text from the active phase's transcript under:

```text
<repo-root>/.kanban-orchestra/artifacts/task-<id>/
```

Match the dashboard's `_latest_agent_transcript_for_step` rule:

- Require a real task id and non-empty current step.
- Glob `task-<id>/*-<step>-*.log`.
- Choose the maximum `(st_mtime_ns, basename)`.
- Do not reuse a transcript from another step.

Compact selection and bounded read:

```bash
REPO="$(git rev-parse --show-toplevel)"
ART="$REPO/.kanban-orchestra/artifacts/task-${TASK_ID}"
TRANSCRIPT="$(
  python3 -c '
import glob, os, sys
art, step = sys.argv[1:3]
cands = [p for p in glob.glob(os.path.join(art, "*-%s-*.log" % step)) if os.path.isfile(p)]
print(max(cands, key=lambda p: (os.stat(p).st_mtime_ns, os.path.basename(p))) if cands else "", end="")
' "$ART" "$STEP"
)"
if [ -n "$TRANSCRIPT" ]; then
  stat -f 'path=%N size=%z mtime=%m' "$TRANSCRIPT" 2>/dev/null \
    || stat -c 'path=%n size=%s mtime=%Y' "$TRANSCRIPT"
  tail -n 80 "$TRANSCRIPT"
fi
```

Re-run selection each sample. Remember the previous path, size, mtime, or a
short tail hash. Summarize concrete new actions and results; distinguish an
agent's stated intention from completed work. Header-only output is not
progress.

## Smart unblocking

The watcher owns every recovery decision. Once per minute it collects current
evidence for each blocked task — task row, durable comments, run log, latest
transcript, `git stash list`, and worktree status — and hands that evidence to
the configured LLM. The LLM decides why the task is blocked, whether recovery
is safe, and then either runs `ko-task continue` itself or records one comment
explaining the block and what the user must decide. Nothing in the watcher
hard-codes a clean-worktree or resume-step rule.

The watcher also suppresses repeats: it fingerprints the evidence it sent and
skips a blocked task whose evidence has not changed since the last
consultation. Everything the watcher produced — comments it authored as
`smart-unblock`, its run-log entries, and its own consultation transcripts — is
excluded from that fingerprint, so an explanation it wrote does not retrigger
itself. The prompt requires the LLM to pass `--author smart-unblock` on the one
comment it leaves; the watcher still shows that prior reasoning back to the LLM
on later cycles as context.

Commands:

```bash
"$ORCHESTRA_DIR/bin/ko-unblock" start [--agent <agent>] [--interval <seconds>]
"$ORCHESTRA_DIR/bin/ko-unblock" status
"$ORCHESTRA_DIR/bin/ko-unblock" stop
```

`--agent` defaults to `$ORCHESTRA_DEFAULT_UNBLOCKER` (falling back to `sonnet`)
and `--interval` defaults to 60 seconds. Watcher artifacts live under
`<repo-root>/.kanban-orchestra/`: `smart-unblock.lock` (the singleton lock plus
pid/agent metadata and the process group of any live consultation, which is how
`stop` reaches an in-flight agent), `smart-unblock-state.json` (fingerprints),
and `smart-unblock.log` (watcher stdout). Per-consultation agent transcripts are
written to the usual `artifacts/task-<id>/` directory with the
`smart-unblock` verb.

While narrating, treat the watcher's comments and run-log entries as evidence:
report what it decided in one or two sentences. Do not run `ko-task continue`,
edit task fields, or touch the worktree yourself. If the watcher is not running
and the user wants unblocking, start it. If the blocked-task gate leaves
nothing runnable, keep sampling state every 10 seconds but report the blocker
only once until the evidence changes or the watcher records a decision.

## Situation handling

| Situation | Action |
| --- | --- |
| Active task, new transcript text | Summarize the concrete new work |
| Active task, unchanged transcript | Say there is no new agent output |
| Phase transition | Name the new phase and reset transcript baseline |
| Review decision | Summarize the durable approval or rejection |
| Blocked task, watcher running | Report the block and the watcher's latest decision |
| Blocked task, watcher stopped | Say so and start the watcher |
| No active, ready, or blocked work | Give a final idle sentence and stop narrating; leave the watcher running |

Keep updates short, current-repo-only, and grounded in Kanban state plus fresh
transcript evidence.
