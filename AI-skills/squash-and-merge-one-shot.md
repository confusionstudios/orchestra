Synthesize a squash-merge commit message in memory and immediately squash-merge a working branch into the appropriate target branch — no intermediate files written.

## Choose Source And Target

- If a source branch argument is provided, use it.
- If no source branch is provided and the current branch is not a long-lived branch such as `master`, `main`, or `develop`, use the current branch as source.
- If no source branch is provided and the current branch is long-lived, ask which working branch to merge and suggest the most recently updated local working branch.
- Honor a target branch the user explicitly named.
- Otherwise infer the target from repository context. For example, use `develop` when it exists and the source branch is clearly based on it; otherwise use the repository default branch.
- If the likely target is ambiguous, ask the user. Do not silently assume `master`.

## Steps

1. Resolve source and target branches using the rules above. Validate they exist locally and are different.
2. Collect branch context in memory:
   - `git log {target}..{source} --oneline` — commit list
   - `git log {target}..{source} --format="%H %s%n%b"` — full commit messages
   - `git diff {target}...{source} --stat` — change summary
   - `git diff {target}...{source} --name-only` — file list
3. Synthesize a commit message in memory using this exact structure:

   ```
   <Title Case commit title under 80 chars>

   What

   <1-3 concise paragraphs: what this branch delivers and why it matters>

   Work

   <concise bullets (use • not -) or short paragraphs describing major implementation chunks>

   Other

   <related work that does not fit under Work; leave blank when none>

   Notes

   <risks, follow-ups, or migration notes; leave blank when none>
   ```

   Rules:
   - Do not list commits one by one. Group changes into coherent logical chunks.
   - Do not describe changes by file or method name. Write what was done and why at a feature/behavior level. Git has the file-level changes, you don't need to repeat them.
   - Keep it factual, concise, and human-readable. Keep it positive, avoid saying what was NOT done.
   - Keep the `Other` and `Notes` headings even when their content is blank.
   - No AI references.

4. Switch to the target branch: `git checkout <target-branch>`. Stop and report if checkout fails.
5. Run squash merge: `git merge --squash <source-branch>`.
6. Show the user for explicit confirmation:
   - Source branch and target branch
   - Full synthesized commit message
   Ask: "Proceed with commit?" Do not continue until confirmed.
8. Once confirmed, commit using the synthesized message verbatim.
9. Run `git status` to confirm success. Remind user to push when ready.

## Rules

- Do not push automatically.
- Do not delete the source branch automatically.
- Always require explicit user confirmation immediately before commit.
- No AI references in the commit message.
