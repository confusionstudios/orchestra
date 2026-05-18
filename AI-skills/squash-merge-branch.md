Squash-merge a branch into master and commit using the latest available squash-merge notes.
Target branch: `master`.

Branch selection rules:
- If a branch name argument is provided (for example `/squash-merge-branch 2026-02-auv3/phase3` or `$squash-merge-branch 2026-02-auv3/phase3`), use it.
- If no argument is provided and current branch is not `master`, use the current branch as source and switch to `master`.
- If no argument is provided and current branch is `master`, ask which branch to merge and suggest the most recently updated local branch that is not `master`.

Steps:

1. Resolve the source branch using the rules above. Validate it exists locally.
2. Ensure target branch is `master`:
   - If currently not on `master`, run `git checkout master`.
   - If checkout fails, stop and report the error.
3. Resolve the input notes file from disk:
   - Look for `Orchestration/projects/1-ad-hoc-ai-chatter/squash-merge-notes.md`.
   - If no candidate file exists on disk, stop and report missing notes source.
4. Extract commit message from the selected notes file:
   - Expected format is:
     - first line: commit title
     - body sections: `## Why`, `## Work`, `## Other`
   - Use the full `squash-merge-notes.md` content as the commit message.
   - Validate required sections exist (`## Why`, `## Work`, `## Other`); if missing, stop and report.
5. Run the squash merge: `git merge --squash <source-branch>`.
   This stages all changes but does not commit.
6. Update the notes file and stage it as part of the same commit:
   - Overwrite it with an empty file, then `git add` it.
7. Read staged files (`git diff --cached --name-only`) and present them for explicit human confirmation against the notes intent.
8. Show the user:
   - source branch and target branch
   - selected notes file path
   - commit title and first few lines of body
   - staged file list summary
   Ask for confirmation before committing.
9. Once confirmed, commit:
    `git commit -m "<Title>" -m "<Body>"`
10. Run `git status` to confirm success. Report result and remind user to push when ready.

Rules:
- Do not push automatically.
- Do not delete the branch automatically.
- The commit message must come verbatim from the selected notes file; do not rewrite or summarize it.
- No AI references in the commit message.
- Always require explicit user confirmation immediately before commit.
- Stop if required commit-message sections are missing or extraction fails.
