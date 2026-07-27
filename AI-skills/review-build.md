**Note**: This is the ad-hoc manual workflow, not the orchestrated pipeline. It reads and writes handoff notes in the shared per-user ad-hoc state directory and is used outside the orchestrator.

Review the latest build handoff by reading `$ADHOC_STATE_DIR/build-notes.md`, inspecting the code changes, and writing `$ADHOC_STATE_DIR/review-notes.md`.

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

1. Read `$ADHOC_STATE_DIR/build-notes.md`.
2. Inspect the referenced code changes (or current changed files if the note is incomplete).
3. Set:
   - `Reviewed By` to the active assistant name (for example Claude or Codex)
   - `Model` to the most specific model identity known, including reasoning effort or mode when available, else `unknown`
4. Overwrite `$ADHOC_STATE_DIR/review-notes.md` with this structure:

```
# Review Notes

**Status**: Ready for builder — [approved | rejected | blocked]
**Reviewed By**: <assistant-name>
**Model**: <specific-model-name-and-reasoning-effort-or-unknown>

## Scope Reviewed
<files reviewed, one per line>

## Problems
<actionable defects only; if none, write `- No behavioural regressions detected in scoped changes.`>

## Notes
<non-blocking observations, confirmations, compatibility notes; omit if empty>

## Outcome
OUTCOME: [approved | rejected | blocked]
```

5. Update the `**Status**` line in `$ADHOC_STATE_DIR/build-notes.md` to:
   - `**Status**: Processed by reviewer`

Rules:
- Keep findings focused on regressions and correctness risks.
- Do not propose unrelated feature work.
- Use repository-real file casing.
