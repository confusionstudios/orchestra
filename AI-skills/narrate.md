Act as a live, concise narrator for Kanban Orchestra in the current repository.

This is an agent instruction skill. It is not a daemon, dashboard, polling
service, or new Python command. Reuse existing `$ORCHESTRA_DIR/bin/ko-task`
and `$ORCHESTRA_DIR/bin/ko-get-update` against this repo's Kanban state.
Do not mutate Kanban tasks, comments, status, or files as part of narration.

## When to use

- The user wants a running plain-language feed of what Kanban is doing here
- They ask to narrate, watch, or monitor the current-repo Kanban queue live

## Prerequisites

1. Resolve the repo root with `git rev-parse --show-toplevel`. If that fails,
   stop and say they must run from inside a git repository.
2. Require `$ORCHESTRA_DIR` and that `"$ORCHESTRA_DIR/bin/ko-get-update"` and
   `"$ORCHESTRA_DIR/bin/ko-task"` exist. If not, stop and say the Orchestra
   tooling environment is not configured.
3. Require `$(git rev-parse --show-toplevel)/kanban-orchestra.db`. If missing,
   stop and say this repo does not have Kanban Orchestra initialized.
4. Confirm this is the only repo you monitor. Do not follow other checkouts.

A running orchestrator is preferred, but narration may still report stopped or
stale state honestly. "Running Kanban Orchestra" here means the DB exists and
status can be read; say clearly when the orchestrator itself is not running.

## Opening report

On invocation, immediately sample state and give one short plain-language
summary of the current active/queued situation. Cover:

- Whether the orchestrator looks running, idle, stopped, stale, or in error
- The active task, if any: id, title, step/phase, and coder/reviewer when known
- Ready queue depth (or "nothing ready")
- If an active transcript exists, one grounded clause about what the agent is
  doing now (see Live agent output)

Do not paste the raw `ko-get-update` block, task JSON, run logs, transcript
dumps, or a table.

## Monitoring loop

Then sample this repository every 10 seconds until a stop condition below.

Keep the loop in this agent session. Do not use `tail -f`, start a background
process, cron job, dashboard change, or repository-local wrapper.

Between samples, wait about 10 seconds (for example `sleep 10`).

### Each sample

1. Read queue/runtime state:

```bash
"$ORCHESTRA_DIR/bin/ko-get-update"
```

2. From that output (or a follow-up `ko-task show <id>`), note the active task
   id and current step/phase when present.
3. Resolve and sample the newest matching agent transcript for that task/step
   (recipe below). Compare path, size, mtime, and/or visible tail content with
   the prior sample.
4. When phase, review decision, blocked note, or validation needs confirmation,
   use targeted follow-ups only:

```bash
"$ORCHESTRA_DIR/bin/ko-task" show <id>
"$ORCHESTRA_DIR/bin/ko-task" show-comments <id>
"$ORCHESTRA_DIR/bin/ko-task" show-run-log <id>
```

### Each update

Speak in one or two plain-language sentences:

- What is running now (or that nothing is active)
- Phase / next step and agent when known
- What the agent is concretely doing, based on new transcript evidence when
  available — or that there is no new agent output while the phase is unchanged

When nothing material changed, say that briefly. Do not invent progress.

## Live agent output

Primary evidence for "what is happening now" is new visible text in the active
phase's agent transcript under:

```text
$(git rev-parse --show-toplevel)/.kanban-orchestra/artifacts/task-<id>/
```

Match the dashboard selection rule in
`kanban-orchestra/scripts/dashboard.py` (`_latest_agent_transcript_for_step`):

- Require a real active task id and a current step that is not empty/`none`
- Glob `task-<id>/*-<step>-*.log`
- Choose `max` by `(st_mtime_ns, basename)` — nanosecond mtime first, then
  lexicographically greatest basename on ties (do not use `ls -t`)
- Do not use transcripts from other steps of the same task

### Compact shell recipe

Identify active task/step from `ko-get-update` (ACTIVE TASK `#<id>` and
`step: <step>`), then select and bound-tail the matching transcript:

```bash
REPO="$(git rev-parse --show-toplevel)"
# Set TASK_ID and STEP from the current ko-get-update sample.
ART="$REPO/.kanban-orchestra/artifacts/task-${TASK_ID}"
# Same key as dashboard._latest_agent_transcript_for_step:
# max((st_mtime_ns, path.name)).
TRANSCRIPT="$(
  python3 -c '
import glob, os, sys
art, step = sys.argv[1:3]
cands = [p for p in glob.glob(os.path.join(art, "*-%s-*.log" % step)) if os.path.isfile(p)]
print(max(cands, key=lambda p: (os.stat(p).st_mtime_ns, os.path.basename(p))) if cands else "", end="")
' "$ART" "$STEP"
)"
if [ -z "${TRANSCRIPT}" ]; then
  echo "NO_TRANSCRIPT"
else
  # Bound the read; prefer a modest tail over the whole file.
  stat -f 'path=%N size=%z mtime=%m' "$TRANSCRIPT" 2>/dev/null \
    || stat -c 'path=%n size=%s mtime=%Y' "$TRANSCRIPT"
  tail -n 80 "$TRANSCRIPT"
fi
```

Do not follow the file with `tail -f`. Re-run the select + `tail` each ~10s
sample. Keep prior `TRANSCRIPT` path/size/mtime (or a short hash of the tail)
in session memory to detect change.

### How to narrate transcript evidence

- Prefer concrete new lines: inspecting a path, running a command, awaiting
  review, writing a validation note, reporting an error.
- Distinguish agent-reported intention ("I will inspect X") from a completed
  result ("Inspected X; found Y"). Label intention as intention when that is
  all the transcript shows.
- Summarize in plain language. Never dump raw logs, JSON, or long quotes.
- Do not turn vague model chatter, planning filler, or speculative wording
  into claimed facts.
- When only header metadata exists (`# agent:`, `# verb:`, `# command:`) and
  no body yet, say the phase transcript exists but has no agent body yet.

### Situation handling

| Situation | What to say |
| --- | --- |
| No active task | Orchestrator idle / no active task; mention ready depth if useful |
| Active task, no matching transcript yet | Name task + phase; say no agent output file for this phase yet |
| Transcript exists but empty / headers only | Phase known; waiting for agent body output |
| Transcript unchanged since last sample | Briefly: no new agent output; preserve current phase/state |
| New transcript lines | Terse grounded summary of the concrete new work |
| Phase / step transition | Note the new phase; reset transcript baseline; select the new step's file |
| Review approval / rejection comments | Prefer `show-comments` / update text; narrate the decision, not raw comment blobs |
| Agent exit / error in transcript or run log | Say the agent exited or failed, with the concrete signal available |
| Queue idle and complete | Final idle sentence, then stop |

Secondary evidence (step changes, review rounds, validation comments, blocked
notes, orchestrator messages) still matters for phase/state. A live agent PID
alone is not progress. New transcript text is the primary signal for the
agent's current work.

## Stop conditions

Stop when any of these is true:

- The user asks you to stop
- The session is interrupted (Ctrl-C / cancellation)
- The queue is genuinely idle: no active/running task and no ready work

On stop, give one final concise state sentence, then end.

## Style

- Plain language, very short
- Current repo only
- Grounded in transcript + Kanban state; honest fallbacks when evidence is thin
- Narrator voice, not operator actions — do not queue, edit, or commit tasks
  unless the user separately asks for that outside this skill
