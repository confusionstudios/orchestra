Review recently done Kanban Orchestra tasks for whether orchestration added value.

Use this when the user asks whether recent Kanban tasks were worth running
through the orchestra, or whether reviewer/committer handoff added useful
signal.

## Question To Answer

The operative question is not "did the task finish?" It is:

Did Kanban Orchestra add value compared with doing the work ad hoc?

Pay special attention to whether the reviewer added real value:

- Found a concrete bug, regression, missing validation, or unclear outcome.
- Forced a useful correction before commit.
- Added comments the committer reasonably used during finalization.
- Approved without useful signal, creating process cost but little value.

## Workflow

1. Identify recent done tasks:
   ```bash
   "$ORCHESTRA_DIR/bin/ko-task" list --status done
   ```
   Review only as many as the user requested. If no count is given, inspect the
   most recent 5 done tasks.
2. For each task, inspect task details, comments, and run log:
   ```bash
   "$ORCHESTRA_DIR/bin/ko-task" show <id>
   "$ORCHESTRA_DIR/bin/ko-task" show-comments <id>
   "$ORCHESTRA_DIR/bin/ko-task" show-run-log <id>
   ```
3. Look for evidence of reviewer value:
   - Rejection comments and whether later work addressed them.
   - Approval comments with non-blocking requests or useful final guidance.
   - Validation comments before and after review.
   - Finalization changes caused by reviewer comments.
   - Repeated agent runs, stalls, blocked states, or noisy handoff that outweighed the benefit.
4. If needed, inspect the related commit with `git show --stat <hash>` and
   `git show --name-only <hash>`, but do not review the implementation from
   scratch unless the task evidence is ambiguous.

## Output

Start with the answer. Then give compact evidence.

For each reviewed task, report:

- Task id and title.
- Verdict: `worth it`, `mixed`, or `not worth it`.
- Reviewer value: `high`, `some`, `none`, or `unclear`.
- One or two concrete reasons from comments/run logs.

End with a short recommendation:

- Keep using Kanban for this kind of task.
- Use Kanban only when review is likely to catch real issues.
- Do it ad hoc next time.

Do not produce a long transcript summary. Quote only short snippets when they
prove the judgment.
