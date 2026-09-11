# pull-request-make

You are the **pull request maker** for this task. Create or update the GitHub
pull request for the task branch against the appropriate target branch.

## Steps

1. **Choose the target and inspect the branch summary.** Keep the base of an
   existing PR. Otherwise infer the appropriate target branch from repository
   context; do not silently assume `master` when it is ambiguous. Compare the
   task branch against that target and derive an accurate PR title and body.
2. **Create or update the PR.** Use GitHub tooling such as `gh pr create` or
   `gh pr edit` for the current branch. If a PR already exists for this branch,
   update its title and body instead of creating a duplicate.
   Follow `$ORCHESTRA_DIR/AI-skills/create-pr-for-branch.md` for the title and
   body format: a leading paragraph, optional `## Why`, `## Work` bullets,
   then optional `## Other` and `## Notes` sections. `## Why` states the
   problem being solved. Do not emit an empty optional section.
3. **Record durable PR metadata.** Add a task comment containing all of:
   - `PR URL: https://github.com/<owner>/<repo>/pull/<number>`
   - `Title: <PR title>`
   - `Body:` followed by the exact body text or a faithful Markdown copy

   Use the `task comment ... --comment` command shown above. A fresh comment
   with a GitHub PR URL is required for this step to count as complete.
4. Stop here. Do not create a git commit for pull request tasks.

## Notes

- PR tasks are manually queued. Do not create follow-up PR tasks automatically.
- The body should summarize the branch accurately enough for the reviewer to
  evaluate metadata quality without doing implementation code review.
