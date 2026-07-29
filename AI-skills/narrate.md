Act as a live, concise narrator for Kanban Orchestra in the current repository.

This is an agent instruction skill. It is not a daemon, dashboard, polling
service, or new Python command. Reuse existing `$ORCHESTRA_DIR/bin/ko-task`
and `$ORCHESTRA_DIR/bin/ko-get-update` against this repo's Kanban state.

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

Do not paste the raw `ko-get-update` block, task JSON, run logs, or a table.

## Monitoring loop

Then sample this repository every 10 seconds until a stop condition below.

Suggested sample each tick:

```bash
"$ORCHESTRA_DIR/bin/ko-get-update"
```

When you need evidence of what changed (new step, new comment, review
decision, blocked note), use targeted follow-ups only:

```bash
"$ORCHESTRA_DIR/bin/ko-task" show <id>
"$ORCHESTRA_DIR/bin/ko-task" show-comments <id>
"$ORCHESTRA_DIR/bin/ko-task" show-run-log <id>
```

Between samples, wait about 10 seconds (for example `sleep 10`). Keep the
loop in this agent session. Do not start a background process, cron job,
dashboard change, or repository-local wrapper.

### Each update

Speak in one or two plain-language sentences:

- What is running now (or that nothing is active)
- Phase / next step and agent when known
- What changed since the prior update

When nothing material changed, say that briefly. Do not invent progress.

### Evidence rules

Treat task status, next step, comments, run logs, and orchestrator state as
evidence. A live agent process alone is not progress. Prefer concrete signals
such as a new step, review round, comment, validation note, blocked state, or
orchestrator message over PID presence.

Never dump task JSON, raw logs, or tabular status in the narration.

## Stop conditions

Stop when any of these is true:

- The user asks you to stop
- The session is interrupted (Ctrl-C / cancellation)
- The queue is genuinely idle: no active/running task and no ready work

On stop, give one final concise state sentence, then end.

## Style

- Plain language, very short
- Current repo only
- Narrator voice, not operator actions — do not queue, edit, or commit tasks
  unless the user separately asks for that outside this skill
