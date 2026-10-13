Create a native Codex worktree chat from a clean checkout with a user-owned `.worktreeinclude` manifest. Use when the user asks to start Codex in a fresh worktree, open a Codex worktree thread, or isolate work on an existing or new branch.

The Codex app owns the chat and worktree. Do not run `git worktree add`, start a CLI-only session as a substitute, stash, commit, discard changes, or create `.worktreeinclude`. The new worktree must own a named branch and a newly created Kanban database so `close-worktree` can import it later. Fleet discovers that database from the primary checkout; do not register the worktree separately.

Helper:

```bash
prep() { "$ORCHESTRA_DIR/bin/ko-python" "$ORCHESTRA_DIR/AI-skills/create-codex-worktree/scripts/worktree_prep.py" "$@"; }
```

## Required Inputs

The target branch is required. Ask for it; do not invent one.

This skill invocation authorizes the helper to fill mode, base, and parking when the checkout identifies exactly one value. Explicit values still win.

- Mode: `existing` when the target branch exists, otherwise `new`
- Parking branch, for `existing`: the invoking checkout's current branch when that branch is different from the target. When the checkout holds the target, the single long-lived branch among `develop`, `main`, and `master` that exists and differs from the target. Ask when zero or several qualify.
- Base branch, for `new`: the branch currently checked out in the invoking checkout. Ask when that checkout is detached.

## Preflight

Resolve the invoking repository root and stop before any mutation unless it is clean and `.worktreeinclude` is a regular file there. An empty or comment-only manifest is valid.

```bash
current_root="$(git rev-parse --show-toplevel)" || exit 1
prep preflight --root "$current_root"
```

| Failure | Required response |
| --- | --- |
| Tracked, staged, or untracked changes | Stop and report that the worktree must be made clean. Do not modify it. |
| `.worktreeinclude` is absent or is not a regular file | Stop and report that the user must create and review it. Do not create it. |
| Target branch was not supplied | Stop and ask for the branch. |
| Parking or base is ambiguous | Stop and ask for that value. |
| Target branch is checked out in another worktree | Stop and report the owner. Do not move that checkout. |

## Prepare The Invoking Checkout

Omit `--mode`, `--parking`, and `--base` when the user did not state them. Append a flag to the command below only for a value the user supplied. The helper prints the resolved mode, branch, parking branch, and base.

Existing branch: if the invoking checkout currently holds that branch, the helper parks it on the resolved parking branch. Create the Codex worktree from the target branch.

New branch: the helper switches the invoking checkout to the resolved base. Create the Codex worktree from that base. Do not create the branch in the invoking checkout.

```bash
prepare_output="$(prep prepare --root "$current_root" --branch "$target_branch")" || exit 1
printf '%s\n' "$prepare_output"
resolved_mode="$(printf '%s\n' "$prepare_output" | sed -n 's/^mode: //p')"
resolved_base="$(printf '%s\n' "$prepare_output" | sed -n 's/^base: //p')"
case "$resolved_mode" in
  existing)
    revision="$target_branch"
    ;;
  new)
    if [ -z "$resolved_base" ]; then
      echo "prep prepare did not report a base branch" >&2
      exit 1
    fi
    revision="$resolved_base"
    ;;
  *)
    echo "prep prepare did not report mode existing or new" >&2
    exit 1
    ;;
esac
```

## Create The Codex Chat

Ask the Codex app for one new user-visible worktree chat from `$revision`. Do not create a Git worktree yourself. If this runtime cannot spawn a native Codex app worktree chat, stop and report that; do not fall back to `git worktree add` or `codex exec`.

Send this bootstrap as the new chat's first message, including the `prep` helper definition above. Fill `<resolved_mode>` with the captured `$resolved_mode`. Continue with the user's requested work after it succeeds:

```text
prep() { "$ORCHESTRA_DIR/bin/ko-python" "$ORCHESTRA_DIR/AI-skills/create-codex-worktree/scripts/worktree_prep.py" "$@"; }
Invoking checkout: <current_root>
Mode: <resolved_mode>
Target branch: <target_branch>
1. Confirm this chat's worktree path and the invoking checkout path.
2. Run: prep attach --root "<worktree_path>" --mode <resolved_mode> --branch <target_branch>
3. Run: prep verify-copy --source <current_root> --dest "<worktree_path>"
4. Run: prep verify-kanban-absent --root "<worktree_path>"
5. Run: prep bootstrap-kanban --root "<worktree_path>"
   Require that helper stdout to include "Status: created kanban database".
6. Run: prep start-instance --root "<worktree_path>"
   Require stdout to include a dashboard URL for this worktree only.
7. Report the Codex thread, worktree path, branch, Kanban database path, and dashboard URL.
8. Continue with: <user's requested work>
```

`.worktreeinclude` is the complete source for ignored local files. Codex copies ignored matches from that file as one ordered `.gitignore`-style ruleset, including directory, root-anchored, and negation entries, and skips source symlinks. Verify that copy against the same ordered exclude-from selection intersected with standard ignored files; tracked files that also match a pattern arrive through Git and must not be compared. An empty approved manifest reports `copied: none`. Stop on failure. Do not copy `.git`, `.kanban-orchestra/`, Kanban databases, SQL dumps, locks, `agents.local.yaml`, or root auth files (`auth.json`, `credentials.json`, `credentials.yml`, `.netrc`).

`start-instance` runs `ko-fleet start` and `ko-fleet status` with the new worktree path as the only selector. The primary checkout must already be a Fleet primary. Do not add the worktree to Fleet config, and do not start or stop the primary instance. Re-running the command reports an instance that is already running.

## Report

Return the created Codex thread, worktree path, branch, Kanban database path, and dashboard URL.

## Guardrails

- Never stash, commit, discard, switch, or create files to pass preflight.
- Never invent a target branch. Use the documented mode, base, and parking defaults only when they are unambiguous.
- Never copy Kanban database, auth, lock, or runtime state into the new worktree.
- Never leave the new worktree in a detached HEAD; it must own the named branch.
- Never start a Fleet instance other than the new worktree.
