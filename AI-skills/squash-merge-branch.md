Squash-merge a working branch into the appropriate target branch and commit using the latest available squash-merge notes.

## Ad Hoc State Location

Resolve the shared handoff directory before reading or clearing notes:

```bash
ADHOC_STATE_DIR="$("$ORCHESTRA_DIR/bin/ko-adhoc-state-dir" --ensure)"
```

That helper is the single source of truth:

1. If `$ORCH_ADHOC_STATE_DIR` is set, use that directory (exact path; caller owns isolation).
2. Otherwise use `$XDG_STATE_HOME/orchestra/adhoc/<repo-key>/<worktree-key>/`, or `~/.local/state/orchestra/adhoc/<repo-key>/<worktree-key>/` when `XDG_STATE_HOME` is unset.
3. `<repo-key>` is a stable hash of this repository's absolute `git-common-dir`. `<worktree-key>` is a stable hash of this worktree's absolute `git-dir`. Every affected skill in one worktree resolves the same directory; linked worktrees and distinct repositories do not share state.

Do not write under `Orchestration/projects/` or any other worktree-local Orchestration path.

## Choose Source And Target

- If a source branch argument is provided (for example `/squash-merge-branch 2026-02-auv3/phase3` or `$squash-merge-branch 2026-02-auv3/phase3`), use it.
- If no source branch is provided and the current branch is not a long-lived branch such as `master`, `main`, or `develop`, use the current branch as source.
- If no source branch is provided and the current branch is long-lived, ask which working branch to merge and suggest the most recently updated local working branch.
- Honor a target branch the user explicitly named.
- Otherwise infer the target from repository context. For example, use `develop` when it exists and the source branch is clearly based on it; otherwise use the repository default branch.
- If the likely target is ambiguous, ask the user. Do not silently assume `master`.

Steps:

1. Resolve the source branch using the rules above. Validate it exists locally.
2. Resolve the target branch using the rules above. Validate it exists locally and is not the source branch.
3. Ensure the target branch is checked out:
   - If currently not on the target, run `git checkout <target-branch>`.
   - If checkout fails, stop and report the error.
4. Resolve the input notes file from disk:
   - Look for `$ADHOC_STATE_DIR/squash-merge-notes.md`.
   - If no candidate file exists on disk, stop and report missing notes source.
5. Extract commit message from the selected notes file:
   - Expected format is:
     - first line: commit title
     - body sections: `## What`, `## Work`, `## Other`, `## Notes`
   - Use the full `squash-merge-notes.md` content as the commit message.
   - Validate required headings exist (`## What`, `## Work`, `## Other`, `## Notes`); `## Other` and `## Notes` may have blank content. Stop and report if a required heading is missing.
6. Run the squash merge: `git merge --squash <source-branch>`.
   This stages all changes but does not commit.
7. Clear the notes file in the ad-hoc state directory (truncate or delete `$ADHOC_STATE_DIR/squash-merge-notes.md`). Do not `git add` it; the notes live outside the worktree.
8. Read staged files (`git diff --cached --name-only`) and present them for explicit human confirmation against the notes intent.
9. Show the user:
   - source branch and target branch
   - selected notes file path
   - commit title and first few lines of body
   - staged file list summary
   Ask for confirmation before committing.
10. Once confirmed, commit:
    `git commit -m "<Title>" -m "<Body>"`
11. Run `git status` to confirm success. Report result and remind user to push when ready.

Rules:
- Do not push automatically.
- Do not delete the branch automatically.
- The commit message must come verbatim from the selected notes file; do not rewrite or summarize it.
- No AI references in the commit message.
- Always require explicit user confirmation immediately before commit.
- Stop if required commit-message sections are missing or extraction fails.
