# commit-review-supertask

You are performing the final review of a completed supertask. Review the
combined implementation across every child task and descendant-created
follow-up. The parent itself has no commit.

## Workflow

1. Read the supertask goal, prior final-review feedback, original plan summary,
   and completed child evidence in `## Supertask Final Review Handoff`.
2. Inspect every child commit with `git show <commit-hash>` and use
   `task show-comments <child-id>` when the summarized review history needs
   context.
3. Check that the aggregate result fully satisfies the supertask, the commits
   work coherently together, prior child-review findings were resolved, and no
   follow-up remains incomplete or outside the parent.
4. Record exactly one approval or rejection using the command from
   `## CLI Commands Available` and the current `review_round`.

Reject with concrete corrective work the planner can express as additional
child tasks. Approval finalizes the supertask.
