Make a one-line git commit from current conversation context.

Use this when the user asks for a lightweight commit and explicitly wants speed
over the normal diff-reading commit workflow.

This is intentionally the exception to the normal commit format: it records
only a title and never a body, `Work`, `Other`, or `Notes` section.

Rules:

- Do not inspect the diff.
- Do not run tests.
- Do not ask for confirmation.
- Use the current conversation and recent work memory to choose the subject.
- Stage the current repo changes with `git add .`.
- Commit with exactly one `git commit -m "<subject>"`.
- Use a concise Title Case subject under 80 characters.
- Do not include a commit body.
- Do not add co-author trailers.

If there are no changes to stage, stop and say there is nothing to commit.
