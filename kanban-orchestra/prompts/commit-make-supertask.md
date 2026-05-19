# commit-make-supertask

You are the **sticky coder** for this supertask. Decompose it into an ordered
sequence of child tasks so reviewers can approve the plan. You are creating,
editing, or reordering child task records here — no code, no commits.

## Steps

1. **Catch up on prior feedback.** Run `task show-comments <id>` and read
   any rejection comments from earlier rounds.
2. **Inspect existing child tasks**, if any:
   ```
   task list --status ready
   ```
   Filter by branch to see all children of this supertask.
3. **Design the decomposition.** Each child task should produce exactly one
   landed commit. Define execution order via `--sequence-index` (lower
   indices run first; siblings auto-renumber at 100-step intervals).
4. **Create or update child tasks:**

   Add a new child:
   ```
   task add "<child title>" \
       --description "<markdown description>" \
       --parent <supertask-id> \
       --sequence-index <sort-key>
   ```
   Children inherit `--branch` from the supertask — omit it on add.

   Reorder a child:
   ```
   task set <child-id> --sequence-index <new-sort-key>
   ```

   Update a child's description:
   ```
   task set <child-id> --description "<new markdown description>"
   ```
5. **Verify the plan.** List children and confirm titles, descriptions, and
   order before continuing.
6. **Record your plan summary** as a fresh `commit-message` comment — this
   is the plan document reviewers will evaluate:
   ```
   cat <<'EOF' | task comment <id> --message-stdin --commit-message
   <plan summary: each child by sequence number, title, one-sentence purpose>
   EOF
   ```
   Always write a fresh `--commit-message` comment during the current run;
   the orchestrator detects it as your sign-off. End the summary with the
   canonical footer from `task get-commit-footer <id>` (e.g.
   `Task <id> (<attribution>)`).

## Rules

- Every child must have `--parent` pointing to this supertask.
- Children inherit branch from the supertask — leave `--branch` unset.
- Child descriptions are Markdown source. Use headings, bullets, and code spans
  where they make the task clearer.
- `sequence_index` controls execution: a child runs only when all siblings
  with lower indices are `done`.
- Leave child status as the default `ready` — the orchestrator gates
  execution.
- Supertasks themselves never land a commit. Stop after the plan summary.
- If you become blocked (e.g. the decomposition is unclear), leave a
  durable `task comment ... --comment` explaining the blocker before
  exiting; the orchestrator will stash any WIP.
