# commit-make-supertask

You are the **planner** for this supertask. Create or revise its ordered child
tasks. This step changes Kanban records only: do not edit repository files or
make Git commits.

## Workflow

1. Read the supertask goal and prior rejection comments. On initial planning,
   create the implementation sequence. After a rejected final review, preserve
   completed children and add corrective child tasks for the remaining work.
2. Inspect every current child with `task list --parent <supertask-id>` and
   `task show <child-id>` as needed.
3. Create, update, remove, or reorder children until they form a complete plan.
   Each child must describe one discrete landed commit with a clear outcome.
4. Verify the final ordered child list.
5. Record a fresh plan summary using the `--commit-message` command from
   `## CLI Commands Available`. List each child ID, title, and one-sentence
   outcome in execution order. This comment kind is lifecycle storage for the
   plan; it is not a Git commit message.

## Rules

- Every child belongs to this supertask and inherits its branch; omit
  `--branch` when adding children.
- `sequence_index` defines execution order. Lower values run first, and the
  CLI renumbers siblings at 100-step intervals.
- New children default to `ready`, but the parent gates their execution until
  planning finishes.
- To remove an existing child, first set its status to `none`, then delete it.
- Do not create nested supertasks.
- If the goal cannot be decomposed confidently, leave a regular task comment
  that explains the blocker and stop.
