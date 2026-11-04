Narrate Kanban Orchestra live in this session, including the current dashboard URL when narration starts.

This skill is a read-only narration loop for the current repository. A running
orchestrator already recovers blocked tasks on its own: about once a minute it
collects evidence, consults the configured unblocker agent, and either continues
the task or leaves a durable `smart-unblock` comment explaining what the
operator must decide. Narration does not start, stop, or manage any recovery
process. It may report native smart-unblock decisions as observed Kanban events.

Leave every recovery decision to that native loop. Monitor the repo with
`$ORCHESTRA_DIR/bin/ko-get-update` and `$ORCHESTRA_DIR/bin/ko-task`.

## Prerequisites

1. Resolve the repo root with `git rev-parse --show-toplevel`. Stop if the
   command fails.
2. Require `$ORCHESTRA_DIR`, `bin/ko-get-update`, and `bin/ko-task`.
3. The current worktree must have a queue in the central store. A legacy
   `kanban-orchestra.db` is not that queue.
4. Monitor only this repo. A running orchestrator is preferred, but report
   stopped or stale state honestly. Native unblocking runs only while the
   orchestrator itself is running.

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

## Opening Report

Immediately sample state and report the Status Line above, followed by the
current dashboard URL. Include orchestrator health or native smart-unblock
state only when it explains a wait or blocker.

Take the dashboard URL from the current `ko-get-update` `Dashboard:` line, or
from this repo's queue runtime metadata (`dashboard.json` beside the lock file,
`url`) when that metadata matches the monitored instance. Prefer the exact
Tailscale Serve HTTPS mapping when one exists; otherwise use the localhost URL.
Do not hard-code a host, Tailnet name, suffix, or dashboard URL. If the dashboard
is not running, say so. Include the link only in the first visible update.

Choose the primary task in this order: active task, blocking leaf task, next
ready task. Use the same Status Line for later progress updates, recovery
notices, unchanged-state updates, and the final idle report. Do not paste JSON,
logs, transcript dumps, or tables unless explicitly requested.

## Monitoring loop

Sample every 10 seconds until the user stops you or the queue is genuinely
idle with no active, ready, or blocked work. Keep this loop in your agent
session: narration ends with the session. Do not start any background process.

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
6. If work is blocked, report the block and what native smart unblocking has
   recorded for it; do not recover it yourself.

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

## Native smart unblocking

The running orchestrator owns every recovery decision. About once a minute it
collects current evidence for each blocked task — task row, durable comments,
run log, latest transcript, `git stash list`, and worktree status — and hands
that evidence to the effective `unblocker` role (environment, repo-local,
shared, then product fallback `sonnet`). The LLM records exactly one durable
comment explaining whether recovery is safe. The orchestrator then either
continues the task or leaves that explanation for the operator. The assessment uses current evidence;
the orchestrator suspends consultations and task dispatch while `waiting-dirty`.

Repeats are suppressed: unchanged evidence is skipped until something in the
task's evidence changes. Everything the loop produced — comments authored as
`smart-unblock`, its run-log entries, and its own consultation transcripts —
is excluded from that fingerprint, so an explanation it wrote does not
retrigger itself. The prompt requires the LLM to pass `--author smart-unblock`
on the one comment it leaves.

Per-consultation agent transcripts are written to the usual
`artifacts/task-<id>/` directory with the `smart-unblock` verb.

While narrating, treat those comments and run-log entries as evidence: report
what native recovery decided in one or two sentences. Do not run
`ko-task continue`, edit task fields, or touch the worktree yourself. If the
orchestrator is stopped, say that native unblocking is idle and keep
narrating; do not start a replacement watcher. If the blocked-task gate leaves
nothing runnable, keep sampling state every 10 seconds but report the blocker
only once until the evidence changes or a `smart-unblock` comment records a
decision.

## Situation handling

| Situation | Action |
| --- | --- |
| Active task, new transcript text | Summarize the concrete new work |
| Active task, unchanged transcript | Say there is no new agent output |
| Phase transition | Name the new phase and reset transcript baseline |
| Review decision | Summarize the durable approval or rejection |
| Blocked task, orchestrator running | Report the block and the latest `smart-unblock` decision |
| Blocked task, orchestrator stopped | Say native unblocking is idle; do not start a watcher |
| No active, ready, or blocked work | Give a final idle sentence and stop narrating |

Keep updates short, current-repo-only, and grounded in Kanban state plus fresh
transcript evidence.
