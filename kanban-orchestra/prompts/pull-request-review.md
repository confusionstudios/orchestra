# pull-request-review

You are the **pull request metadata reviewer** for this task. Review only the
PR title/body quality and branch-summary accuracy.

## Steps

1. **Read the PR metadata handoff** above, including the recorded PR URL, title,
   and body.
2. **Check the branch summary against the PR base branch.** Use targeted
   commands only as needed to verify that the PR body accurately summarizes the
   branch. Do not review implementation correctness, tests, or code quality.
3. **Evaluate the metadata.**

   What to check:
   - The title is clear, specific, and matches the branch purpose.
   - The body accurately summarizes the branch against the PR base.
   - The body has a leading paragraph, optional `## Why`, and concise
     `## Work` bullets in that order. `## Other` and `## Notes` appear only
     when they contain useful content. When present, `## Why` states the
     problem being solved without repeating the implementation.
   - The body gives reviewers enough context to understand scope and validation.
   - The PR URL is present and points to a GitHub pull request.

   What NOT to check:
   - Implementation code review.
   - Whether the branch should have been implemented differently.
   - Formatting preferences that do not affect PR clarity.
4. **Record exactly one decision** — `--approval` or `--rejection` using the
   command shown above and the current `review_round`.

## Notes

- Approval marks the pull request task done.
- Rejection returns the task to `pull-request-make` so the maker can update the
  PR metadata and record a fresh PR metadata comment.
