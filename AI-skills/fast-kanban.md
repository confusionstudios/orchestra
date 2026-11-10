Queue a tiny, bounded Kanban change with Codex coding, Opus review, and lightweight timing.

Use for a small, low-impact change that already has an implementation pattern,
such as one display-label override beside an existing equivalent. Use the normal
Kanban workflow when the work needs design, a migration, broad changes, or new
coverage to establish correctness. Fast means a smaller assignment, not weaker
repository safeguards.

## Queue the Smallest Change

Follow [kanban.md](kanban.md) for queue context, branch policy, admission and
execution. Inspect the existing pattern before writing the ticket. Reuse the
current appropriate branch; ask before creating a branch. Do not interrupt an
active worker, bypass blocked-work gates, or clear unrelated dirty files.

Explicitly select `codex` as coder and `opus` as reviewer through the effective
registry. Retain their configured models, role patches and host launchers. Do
not change shared defaults. Skip planning, keep commit review enabled, and start
at `commit-make`. If either selected route is unavailable, report the blocker
rather than substituting an agent.

Keep the description to a few sentences: the exact change, the existing pattern
to reuse, the focused check, and the scope boundary. For example:

> Show the support account's friendly name using the existing administrator
> display-name override. Make the smallest change; no adjacent refactoring or
> new test coverage. Run PHP lint and the existing local rendering check.
> Finalize the commit; deployment is a separate operation.

```bash
"$ORCHESTRA_DIR/bin/ko-task" add "<Title Case Title>" \
  --branch "<current-appropriate-branch>" \
  --coder-agent codex --reviewer-agent opus \
  --skip commit-plan --description "<short bounded description>"
"$ORCHESTRA_DIR/bin/ko-task" set <id> \
  --next-step commit-make --remove-skip commit-review
"$ORCHESTRA_DIR/bin/ko-task" set <id> --status ready
```

The coder should reuse the implementation pattern, change only what the request
requires, and avoid adjacent refactoring. Add no new test coverage by default:
use the relevant lint/syntax check and a focused existing test or local behavior
check. Modify an existing assertion only if the change requires it. Repository
required checks still apply, including mandatory suites before staging. If the
change exposes substantive logic or risk that needs more validation, narrow or
reclassify the work rather than hiding that need to keep the task fast.

Use the normal review and finalization path. Commit locally after approval;
follow the repository's normal automatic-push policy and report its result.
Neither queueing this task nor committing/pushing authorizes deployment or
publication. Those need separate authorization.

## Measure From Existing Evidence

Queue promptly. Do not add hooks, instrumentation, extra agent calls, or a
background watcher just to measure speed. Report pickup in the usual status
format. Do not camp on completion unless the user asks; collect timing when
completion is observed or at the next status check.

Use `ko-task show <id>`, `show-run-log <id>`, task comments, and the relevant
phase artifacts under this queue's runtime directory (reported by
`ko-get-update`). Capture one compact timing comment after completion, using
existing timestamps; a short phase-completion comment is enough when an actual
boundary otherwise has no timestamp. Never create a comment per command.

Record these intervals, citing the task/artifact timestamps used:

- **Queue wait:** ready admission to first pickup. Separate an intentional hold
  between creation and ready admission; show later requeue waits separately.
- **Implementation/verification:** coder build/rework runs, including their
  checks. Include retries rather than hiding them in one successful pass.
- **Review:** actual reviewer runs, with round count.
- **Finalization:** the approval-to-finalizer handoff excluded; count the coder's
  finalization run through the local commit.
- **Boundary delays:** gaps between phase runs, including implementation to
  review and approval to finalizer. Name material blockers or waits.
- **Total wall time:** ticket creation to local commit. Report later push and
  lifecycle completion separately if relevant; do not equate `done_at` with
  commit time when they differ.

Normalize timestamps to a common timezone before subtracting. Use process/run
timestamps where available; file mtimes alone do not prove agent execution
duration. Mark missing boundaries as unknown or estimates and explain the
source; do not invent precision. Avoid double-counting overlapping intervals
and identify unexplained remainder. Timing is evidence, not a speed promise.

Keep the final report short: result, commit/push status, validation, and one
timing line: `Queue … · make/check … · review … · finalize … · boundaries … ·
total … to local commit.` Use measured values and label unknown intervals.
