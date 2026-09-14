Create a native Codex worktree chat from a clean checkout with a user-owned `.worktreeinclude` manifest. Use when the user asks to start Codex in a fresh worktree, open a Codex worktree thread, or isolate work on an existing or new branch.

The Codex app owns the chat and worktree. Do not run `git worktree add`, start a CLI-only session as a substitute, stash, commit, discard changes, or create `.worktreeinclude`. The new worktree must own a named branch and a newly created Kanban database so `close-worktree` can import it later.

Helper:

```bash
prep() { "$ORCHESTRA_DIR/bin/ko-python" "$ORCHESTRA_DIR/AI-skills/create-codex-worktree/scripts/worktree_prep.py" "$@"; }
```

## Required Inputs

Stop and ask for any value the user did not explicitly state. Do not choose a default.

- Mode: `existing` or `new`
- Target branch
- Parking branch, when mode is `existing`
- Base branch, when mode is `new`

## Preflight

Resolve the invoking repository root and stop before any mutation unless it is clean and `.worktreeinclude` is a regular file there:

```bash
current_root="$(git rev-parse --show-toplevel)" || exit 1
prep preflight --root "$current_root"
```

| Failure | Required response |
| --- | --- |
| Tracked, staged, or untracked changes | Stop and report that the worktree must be made clean. Do not modify it. |
| `.worktreeinclude` is absent or is not a regular file | Stop and report that the user must create and review it. Do not create it. |
| Mode, branch, or required base/parking branch was not supplied | Stop and ask for the missing value. |

## Prepare The Invoking Checkout

Existing branch: if the invoking checkout currently holds that branch, park it on the user-selected parking branch, then create the Codex worktree from the existing branch.

```bash
prep prepare --root "$current_root" --mode existing \
  --branch "$target_branch" --parking "$parking_branch"
revision="$target_branch"
```

New branch: switch the invoking checkout to the user-selected base, then create the Codex worktree from that base. Create the new branch only inside the Codex worktree.

```bash
prep prepare --root "$current_root" --mode new \
  --branch "$target_branch" --base "$base_branch"
revision="$base_branch"
```

## Create The Codex Chat

Ask the Codex app for one new user-visible worktree chat from `$revision`. Do not create a Git worktree yourself. If this runtime cannot spawn a native Codex app worktree chat, stop and report that; do not fall back to `git worktree add` or `codex exec`.

Send this bootstrap as the new chat's first message, including the `prep` helper definition above. Continue with the user's requested work after it succeeds:

```text
prep() { "$ORCHESTRA_DIR/bin/ko-python" "$ORCHESTRA_DIR/AI-skills/create-codex-worktree/scripts/worktree_prep.py" "$@"; }
Invoking checkout: <current_root>
Mode: existing|new
Target branch: <target_branch>
1. Confirm this chat's worktree path and the invoking checkout path.
2. Run: prep attach --root "<worktree_path>" --mode <existing|new> --branch <target_branch>
3. Run: prep verify-copy --source <current_root> --dest "<worktree_path>"
4. Run: prep verify-kanban-absent --root "<worktree_path>"
5. Run: prep bootstrap-kanban --root "<worktree_path>"
   Require that helper stdout to include "Status: created kanban database".
6. Report the Codex thread, worktree path, branch, and Kanban database path.
7. Continue with: <user's requested work>
```

`.worktreeinclude` is the complete source for ignored local files. Codex copies ignored matches from that file as one ordered `.gitignore`-style ruleset, including directory, root-anchored, and negation entries, and skips source symlinks. Verify that copy against the same ordered exclude-from selection intersected with standard ignored files; tracked files that also match a pattern arrive through Git and must not be compared. Stop on failure. Do not copy `.git`, `.kanban-orchestra/`, Kanban databases, SQL dumps, or locks.

## Report

Return the created Codex thread, worktree path, branch, and Kanban database path.

## Guardrails

- Never stash, commit, discard, switch, or create files to pass preflight.
- Never invent `existing`/`new`, a branch name, or a base/parking branch.
- Never copy Kanban database or runtime state into the new worktree.
- Never leave the new worktree in a detached HEAD; it must own the named branch.
