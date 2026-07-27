**Note**: This is the ad-hoc manual workflow, not the orchestrated pipeline. It writes build-notes.md into the shared per-user ad-hoc state directory and is used outside the orchestrator.

Prepare a build handoff by writing `build-notes.md` into the shared ad-hoc state directory. Overwrite it completely every time.

This workflow is agent-agnostic and reusable across CLI tools.

## Ad Hoc State Location

Resolve the shared handoff directory before writing notes:

```bash
ADHOC_STATE_DIR="$("$ORCHESTRA_DIR/bin/ko-adhoc-state-dir" --ensure)"
```

That helper is the single source of truth:

1. If `$ORCH_ADHOC_STATE_DIR` is set, use that directory (exact path; caller owns isolation).
2. Otherwise use `$XDG_STATE_HOME/orchestra/adhoc/<repo-key>/<worktree-key>/`, or `~/.local/state/orchestra/adhoc/<repo-key>/<worktree-key>/` when `XDG_STATE_HOME` is unset.
3. `<repo-key>` is a stable hash of this repository's absolute `git-common-dir`. `<worktree-key>` is a stable hash of this worktree's absolute `git-dir`. Every affected skill in one worktree resolves the same directory; linked worktrees and distinct repositories do not share state.

Do not write under `Orchestration/projects/` or any other worktree-local Orchestration path.

## Steps

1. Get the current branch with `git branch --show-current`.
2. Check whether changes are committed or only on disk with `git status --short`.
3. Ask the user (or infer from context) for the proposed commit message if one is not already known.
4. Get changed files:
   - If committed branch work is being summarized: `git diff --name-only master...HEAD`
   - If on-disk work is being summarized: `git diff --name-only`
5. Set:
   - `Prepared By` to the active assistant name (for example Claude or Codex)
   - `Model` to the most specific model identity known, including reasoning effort or mode when available, else `unknown`

Write `$ADHOC_STATE_DIR/build-notes.md` with this exact structure:

```
# Build Notes

**Status**: Waiting for review — code changes [on disk only, not yet committed | committed to branch]
**Prepared By**: <assistant-name>
**Model**: <specific-model-name-and-reasoning-effort-or-unknown>
**Review focus**: Refactor only — no new behaviour. Review for regressions only.
**Branch**: `<branch-name>`

---

## Proposed Commit — <commit title>

**Title**: <commit title>

**Body**:

<commit body>

---

## Files Changed
<list of changed files, one per line, use repository-real casing>

## Next Action
Reviewer should run `review-build`, write `review-notes.md`, and set the final outcome.
```

Keep it factual and concise. No extra commentary.
