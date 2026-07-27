**Note**: This is the ad-hoc manual workflow, not the orchestrated pipeline. It reads and writes handoff notes in the shared per-user ad-hoc state directory and is used outside the orchestrator.

Respond to a reviewer handoff by reading `$ADHOC_STATE_DIR/review-notes.md` and acting on its findings.

## Ad Hoc State Location

Resolve the shared handoff directory before reading or writing notes:

```bash
ADHOC_STATE_DIR="$("$ORCHESTRA_DIR/bin/ko-adhoc-state-dir" --ensure)"
```

That helper is the single source of truth:

1. If `$ORCH_ADHOC_STATE_DIR` is set, use that directory (exact path; caller owns isolation).
2. Otherwise use `$XDG_STATE_HOME/orchestra/adhoc/<repo-key>/<worktree-key>/`, or `~/.local/state/orchestra/adhoc/<repo-key>/<worktree-key>/` when `XDG_STATE_HOME` is unset.
3. `<repo-key>` is a stable hash of this repository's absolute `git-common-dir`. `<worktree-key>` is a stable hash of this worktree's absolute `git-dir`. Every affected skill in one worktree resolves the same directory; linked worktrees and distinct repositories do not share state.

Do not write under `Orchestration/projects/` or any other worktree-local Orchestration path.

## Steps

1. Read `$ADHOC_STATE_DIR/review-notes.md`.
2. Confirm the file is ready for builder action by checking `**Status**` is one of:
   - `Ready for builder — rejected`
   - `Ready for builder — approved`
   If not, stop and tell the user the review is not ready yet.
3. If status is `Ready for builder — approved`:
   - invoke the local git-commit helper (`/git-commit` or `$git-commit`)
4. If status is `Ready for builder — rejected`:
   - fix each listed issue
   - build with `fastlane alpha` to verify
   - invoke prep-for-review (`/prep-for-review` or `$prep-for-review`) for another review round
   - do not commit
5. After handling the review, update the `**Status**` line in `$ADHOC_STATE_DIR/review-notes.md` to:
   - `**Status**: Builder processed`

Keep all updates append-only except replacing the single status line.
