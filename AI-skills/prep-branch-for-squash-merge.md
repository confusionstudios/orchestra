Produce a squash-merge commit message for the current working branch against the appropriate target branch, then overwrite `Orchestration/projects/1-ad-hoc-ai-chatter/squash-merge-notes.md` with it.

## Choose Target

1. Get the current branch name: `git branch --show-current`. If it is a long-lived branch such as `master`, `main`, or `develop`, stop and ask the user to switch to the working branch.
2. Determine the target branch before inspecting the branch contents:
   - Honor a target branch the user explicitly named.
   - Otherwise infer the target from repository context. For example, use `develop` when it exists and the working branch is clearly based on it; otherwise use the repository default branch.
   - If the likely target is ambiguous, ask the user. Do not silently assume `master`.

## Inspect And Draft

3. Get all commits on this branch vs the selected target: `git log {target}..HEAD --oneline`
4. Get all files changed vs the selected target: `git diff {target}...HEAD --name-only`
5. Get the full diff summary (stat only, no patch): `git diff {target}...HEAD --stat`
6. Look for planning documents: `ls Documentation/planning/` and read any `.md` files there to understand stated intent.
7. Read commit messages in full: `git log {target}..HEAD --format="%H %s%n%b"` to understand the work done.

Now synthesize. Do not list commits one by one. Group changes into coherent logical chunks based on what they collectively accomplish.

Write `Orchestration/projects/1-ad-hoc-ai-chatter/squash-merge-notes.md` with this exact structure (overwrite completely):

```
<Title Case commit title under 80 chars>

## What

<1-3 concise paragraphs: what this branch delivers and why it matters>

## Work

<concise bullets or short paragraphs describing major implementation chunks>

## Other

<related work that does not fit under Work; leave blank when none>

## Notes

<risks, follow-ups, or migration notes; leave blank when none>
```

Rules:
- This output is the commit message source for `squash-merge-branch`.
- It is prepared against the selected target branch.
- Keep it factual, concise, and human-readable.
- Keep the `## Other` and `## Notes` headings even when their content is blank.
- Do not add separate metadata blocks like "Squash merge title" or "Files Changed".
