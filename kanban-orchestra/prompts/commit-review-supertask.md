# commit-review-supertask

You are reviewing a supertask's decomposition plan. Review the plan only:
there is no code diff, build, or Git commit in this step.

## Workflow

1. Read the supertask goal, prior feedback, and the current plan summary in
   `## Supertask Reviewer Handoff`.
2. Inspect the live ordered children with
   `task list --parent <supertask-id>` and `task show <child-id>` as needed.
3. Check that the children completely cover the goal, each represents one
   clear landed commit, descriptions are actionable, sequencing is correct,
   and there are no gaps or duplicate scopes.
4. Record exactly one approval or rejection using the command from
   `## CLI Commands Available` and the current `review_round`.

Rejection feedback must identify concrete changes the planner can make. On
approval, the orchestrator moves the parent to `pending_subtasks`, allowing
its children to execute in sequence.
