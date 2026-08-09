Merge a linked worktree branch, import its Kanban history, and remove the worktree.

Use this when the user asks to finish a linked Git worktree after its branch is
ready to land. The current checkout is the target checkout. The user must name
the source worktree path; they may also name a target branch.

This is a destructive closeout workflow. Stop and report rather than guessing,
forcing a merge, deleting a worktree, or continuing after a failed check.

## Validate The Worktree

1. Resolve the current target checkout and the user-provided source worktree:
   ```bash
   target_root="$(git rev-parse --show-toplevel)" || exit 1
   source_root="$(cd -P -- "<source-worktree-path>" && pwd)" || exit 1
   ```
2. Require both paths to belong to the same Git repository. Compare their
   absolute common Git directories with:
   ```bash
   git -C "$target_root" rev-parse --path-format=absolute --git-common-dir
   git -C "$source_root" rev-parse --path-format=absolute --git-common-dir
   ```
   Stop if they differ, if the paths are the same checkout, or if the source is
   not listed by `git -C "$target_root" worktree list --porcelain`.
3. Read the source branch from the source worktree:
   ```bash
   source_branch="$(git -C "$source_root" symbolic-ref --quiet --short HEAD)" || exit 1
   source_head="$(git -C "$source_root" rev-parse HEAD)" || exit 1
   test "$(git -C "$target_root" rev-parse "$source_branch")" = "$source_head" || exit 1
   ```
   Stop if it is detached, empty, or does not resolve from `target_root` to
   `source_head`. The branch checked out in that worktree is the only merge
   source.
4. Choose the target branch:
   - Honor an explicit user choice.
   - Otherwise use the branch currently checked out in `target_root` when it is
     a long-lived integration branch such as `develop`, `main`, or `master`.
   - If that does not identify a clear target, stop and ask. Do not silently
     switch to or assume `master`.
5. Require the target and source branches to differ, both checkouts to be
   clean (`git status --short`), and the target branch to exist locally. Record
   the target HEAD before merging:
   ```bash
   target_head="$(git -C "$target_root" rev-parse "$target_branch")" || exit 1
   ```
6. Confirm the source Kanban database exists and its orchestrator is stopped:
   ```bash
   test -f "$source_root/kanban-orchestra.db" || exit 1
   (cd "$source_root" && "$ORCHESTRA_DIR/bin/ko-get-update")
   ```
   Stop if that update reports a running or active orchestrator. Do not import
   a database that an orchestrator may still write, and do not remove a running
   worktree.

## Squash Merge Without Message Confirmation

1. Switch the target checkout to the selected target branch. Stop if checkout
   fails:
   ```bash
   git -C "$target_root" switch "$target_branch"
   ```
2. Read `$ORCHESTRA_DIR/AI-skills/squash-and-merge-one-shot.md`. Use its
   branch-context collection and commit-message structure, but skip its normal
   user confirmation step. This skill invocation is the authorization to make
   the squash commit.
3. Squash-merge the exact source branch and stop on any conflict:
   ```bash
   git -C "$target_root" merge --squash "$source_branch"
   ```
4. Commit the staged squash result with the synthesized message. The message
   must use: title, one leading paragraph, an optional non-empty `WHY` section,
   `WORK` bullets, and optional non-empty `OTHER` and `NOTES` sections. `WHY`
   explains the motivation, not the implementation. Record the
   landed commit hash.

## Import Kanban History

1. Count source tasks before importing, then import from the target checkout:
   ```bash
   source_task_count="$(
     "$ORCHESTRA_DIR/bin/ko-python" -c \
       'import sqlite3, sys; print(sqlite3.connect(sys.argv[1]).execute("SELECT COUNT(*) FROM tasks").fetchone()[0])' \
       "$source_root/kanban-orchestra.db"
   )"
   cd "$target_root"
   "$ORCHESTRA_DIR/bin/ko-task" import-worktree "$source_root"
   ```
2. Require the JSON result to report `imported_count` equal to
   `source_task_count`, and retain its `id_map`. Stop if the import fails or the
   counts differ. Do not remove the worktree after a failed import.
3. Remember the import semantics when reporting: target task IDs remain
   authoritative; imported tasks receive fresh IDs; done tasks stay done;
   unfinished tasks become `none`; and source branch metadata is retained in
   import comments.

## Verify And Remove

1. Confirm the squash commit landed on the selected target branch, changed
   `HEAD` from `target_head`, and left the target checkout clean.
2. Confirm `"$ORCHESTRA_DIR/bin/ko-task" list --status done` and
   `"$ORCHESTRA_DIR/bin/ko-task" list --status none` can read the target
   database after import. Inspect the import result and at least one imported
   task/comment when tasks were imported.
3. Remove the source with normal Git worktree removal only:
   ```bash
   git -C "$target_root" worktree remove "$source_root"
   git -C "$target_root" worktree prune
   ```
   Never pass `--force`. Stop and report if removal fails.
4. Verify `git -C "$target_root" worktree list --porcelain` no longer lists
   `source_root`, then report the source branch, target branch, squash commit,
   imported task count and ID map, and successful worktree removal.

## Guardrails

- Do not use this for an ordinary branch that is not checked out in a linked
  worktree.
- Do not merge, import, or remove anything when the source worktree is dirty,
  detached, running an orchestrator, or missing its Kanban database.
- Do not import before the squash commit succeeds.
- Do not delete the source worktree manually with filesystem commands.
- Do not push unless the user separately asks.
