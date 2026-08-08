Narrate Kanban Orchestra live and smartly resume tasks when recovery is explicit and safe.

This is an agent instruction skill, not a daemon or new Python command. Monitor
the current repository with `$ORCHESTRA_DIR/bin/ko-get-update` and
`$ORCHESTRA_DIR/bin/ko-task`. Narrate material progress and intervene only
through the bounded recovery rules below.

## Prerequisites

1. Resolve the repo root with `git rev-parse --show-toplevel`. Stop if the
   command fails.
2. Require `$ORCHESTRA_DIR`, `bin/ko-get-update`, and `bin/ko-task`.
3. Require `<repo-root>/kanban-orchestra.db`.
4. Monitor only this repo. A running orchestrator is preferred, but report
   stopped or stale state honestly.

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
idle with no active, ready, or blocked work. Keep the loop in this agent
session; do not start a background process, cron job, or repository wrapper.

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
6. If work is blocked, apply the recovery decision below.

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

## Smart unblock decision

Investigate before acting. The task row, durable comments, and run log must
show an authoritative recovery path. Remember every `(task id, block reason)`
you attempt during this invocation and never auto-recover the same pair twice.
When a blocked child has also propagated `blocked` to its parent supertask,
recover the leaf child only; successful child continuation restores the parent
when no blocked siblings remain.

### Recover automatically

Use only these cases:

1. **Structured review-cap block:** `block_reason=review_cap` and
   `resume_next_step` is present. First inspect `task show-comments <id>`. If an
   operator comment records an earlier review-round grant, ask the user rather
   than granting more automatically. Otherwise grant three additional rounds:

   ```bash
   "$ORCHESTRA_DIR/bin/ko-task" continue <id> --add-review-rounds 3
   ```

2. **Other structured block:** `resume_next_step` is present and the block is
   not a review-cap block. Resume the stored step without inventing one:

   ```bash
   "$ORCHESTRA_DIR/bin/ko-task" continue <id>
   ```

Before either command, run `git status --porcelain`. Continue automatically
only when the worktree is clean or every reported change is clearly owned by
the blocked task according to its comments, stash metadata, and transcript.
If attribution is uncertain, ask the user. The CLI validates lifecycle,
branch, and repository policy, but only rejects a dirty worktree itself while
the orchestrator is idle. Never bypass a rejection with
`task set --status ready`.

After a successful recovery, say exactly what was resumed and continue
monitoring. If the command fails, report the failure and do not retry it
automatically.

### Do not recover automatically

Stop and ask for user direction when recovery would require any of these:

- Guessing `next_step` or interpreting an unstructured/legacy block
- Cleaning, stashing, discarding, committing, or editing worktree changes
- Supplying missing product decisions, credentials, or task requirements
- Recovering `hard-break`, stale/stopped orchestration, or database errors
- Clearing `stash_ref`, changing branches, changing agents, or rewriting task
  fields directly
- Repeating an automatic recovery for the same task and reason

Continue narrating other eligible work when possible. If the blocked-task gate
leaves nothing runnable, keep sampling state every 10 seconds but report the
blocker only once until the user responds or the evidence changes.

## Situation handling

| Situation | Action |
| --- | --- |
| Active task, new transcript text | Summarize the concrete new work |
| Active task, unchanged transcript | Say there is no new agent output |
| Phase transition | Name the new phase and reset transcript baseline |
| Review decision | Summarize the durable approval or rejection |
| Structured recoverable block | Recover once, report it, and keep watching |
| Ambiguous or unsafe block | Explain what evidence is missing and ask |
| No active, ready, or blocked work | Give a final idle sentence and stop |

Keep updates short, current-repo-only, and grounded in Kanban state plus fresh
transcript evidence.
