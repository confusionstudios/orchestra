Merge a linked worktree branch, import its Kanban history, and remove the worktree.

Use this when the user asks to finish a linked Git worktree after its branch is
ready to land, including when that branch was already squash-merged through a
pull request. The current checkout is the target checkout. The user must name
the source worktree as a path or an unambiguous shorthand such as `6300`; they
may also name a target branch.

This is a destructive closeout workflow. Stop and report rather than guessing,
forcing a merge, deleting a worktree, or continuing after a failed check.

## Validate The Worktree

1. Resolve the current target checkout, then resolve the user-provided source
   worktree from a path or shorthand through
   `git worktree list --porcelain`:
   ```bash
   target_root="$(git rev-parse --show-toplevel)" || exit 1
   source_identifier="<source-worktree-path-or-shorthand>"
   if [ -d "$source_identifier" ]; then
     source_abs="$(cd -P -- "$source_identifier" && pwd)" || exit 1
   else
     source_abs=""
   fi
   source_matches="$(
     git -C "$target_root" worktree list --porcelain |
       awk -v id="$source_identifier" -v abs="$source_abs" '
         $1 == "worktree" {
           path = substr($0, 10)
           if (abs != "" && path == abs) { print path; next }
           if (path == id) { print path; next }
           n = split(path, parts, "/")
           for (i = 1; i <= n; i++) if (parts[i] == id) { print path; next }
         }
       '
   )"
   match_count="$(printf '%s\n' "$source_matches" | awk 'NF { n++ } END { print n+0 }')"
   test "$match_count" -eq 1 || exit 1
   source_root="$(cd -P -- "$source_matches" && pwd)" || exit 1
   ```
   Match only `worktree` path lines, and only as a resolved existing
   directory, an exact path, or a complete path component such as `.../6300`
   or `.../6300/...`. An existing directory that is not a listed worktree is
   not a match; keep looking for an unambiguous shorthand. Stop on zero or
   multiple matches. Do not search `HEAD` or `branch` lines, and do not treat
   `6300` as a match for `16300` or `foo-6300`.
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
   the target HEAD before closeout:
   ```bash
   target_head="$(git -C "$target_root" rev-parse "$target_branch")" || exit 1
   ```
   Do not continue on a dirty checkout, and do not treat a dirty worktree as
   equivalent to any landed commit.
6. Confirm the source Kanban database exists and its orchestrator is stopped:
   ```bash
   test -f "$source_root/kanban-orchestra.db" || exit 1
   "$ORCHESTRA_DIR/bin/ko-get-update" --db "$source_root/kanban-orchestra.db" || exit 1
   ```
   Confirm the `INSTANCE:` path is `$source_root`. Treat the source
   orchestrator as safely stopped only when that source update shows no live
   process, no active task, and no running dashboard for the source worktree.
   Check all three independently. A `stale` or `stopped` status line is not
   enough.

   Stop if `ORCHESTRATOR:` is `running`, `starting`, `stopping`, `hard-break`,
   or `error`. `idle`, `stopped`, `stale`, and `no runtime row` may continue
   only if the three checks below also pass.

   - No live process: if a `  processes:` line is present, it must be
     `  processes: none found`. If a fleet summary is present (`This repo (`),
     it must say `is stopped.`, not `is running.`. Any `processes:` value that
     lists a PID is a live process. `ORCHESTRATOR: no runtime row` omits
     `  processes:`; that omission is not proof. Read the `INSTANCE` `lock:`
     file and stop if it names a `role=orchestrator` PID for which `kill -0`
     succeeds.
   - No active task: `ORCHESTRATOR: no runtime row` (no `ACTIVE TASK:`
     section), or `ACTIVE TASK: none (orchestrator idle)`. Any
     `ACTIVE TASK: #` line is an active task.
   - No running dashboard: every printed `Dashboard:` value must be
     `not running`. A runtime row prints indented `  Dashboard: ...`. A fleet
     summary, if present, also has a `Dashboard:` value. Any `Dashboard:`
     value with a URL means the dashboard is running.
     `ORCHESTRATOR: no runtime row` omits the runtime `  Dashboard:` line;
     that omission is not proof. Read
     `$source_root/.kanban-orchestra/dashboard.json` and stop if it names a
     `role=dashboard` PID for which `kill -0` succeeds.

   Do not import a database that an orchestrator may still write, and do not
   remove a running worktree.

## Prove Already Landed Or Squash Merge

1. Switch the target checkout to the selected target branch. Stop if checkout
   fails:
   ```bash
   git -C "$target_root" switch "$target_branch"
   ```
2. Prove whether the source worktree is already landed by exact tree
   equivalence with a commit reachable from the selected local target branch:
   ```bash
   source_tree="$(git -C "$source_root" log -1 --format=%T HEAD)" || exit 1
   test -n "$source_tree" || exit 1
   landed_commit="$(
     git -C "$target_root" log --format='%H %T' "$target_branch" -- |
       awk -v t="$source_tree" '$2 == t { print $1; exit }'
   )"
   ```
   Search only the selected local target branch. Take the most recent matching
   commit when more than one has that tree. Never infer equivalence from commit
   messages, partial diffs, pull-request state, remote refs, or a dirty
   checkout.
3. If `landed_commit` is non-empty, record it as the landed commit, set
   `landed_via=already-landed`, and skip the squash merge. Do not create another
   commit.
4. If exact tree equivalence cannot be proven, follow the existing squash-merge
   workflow. Non-equivalent trees are not already-landed; they still squash-merge,
   and that merge must succeed before removal:
   - Read `$ORCHESTRA_DIR/AI-skills/squash-and-merge-one-shot.md`. Use its
     branch-context collection and commit-message structure, but skip its normal
     user confirmation step. This skill invocation is the authorization to make
     the squash commit.
   - Squash-merge the exact source branch and stop on any conflict:
     ```bash
     git -C "$target_root" merge --squash "$source_branch"
     ```
   - Commit the staged squash result with the synthesized message. The message
     must use: title, one leading paragraph, an optional non-empty `WHY` section,
     `WORK` bullets, and optional non-empty `OTHER` and `NOTES` sections. `WHY`
     explains the motivation, not the implementation. Record the new commit as
     the landed commit and set `landed_via=squash`.

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

1. Confirm the recorded landed commit on the selected target branch and that
   the target checkout is clean:
   ```bash
   test -z "$(git -C "$target_root" status --short)" || exit 1
   ```
   - For `landed_via=already-landed`:
     ```bash
     git -C "$target_root" merge-base --is-ancestor "$landed_commit" "$target_branch" || exit 1
     test "$(git -C "$target_root" log -1 --format=%T "$landed_commit")" = "$source_tree" || exit 1
     test "$(git -C "$target_root" rev-parse HEAD)" = "$target_head" || exit 1
     ```
   - For `landed_via=squash`:
     ```bash
     test "$(git -C "$target_root" rev-parse HEAD)" != "$target_head" || exit 1
     test "$(git -C "$target_root" rev-parse HEAD)" = "$landed_commit" || exit 1
     ```
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
   `source_root`, then report the source branch, target branch, landed commit,
   `landed_via`, imported task count and ID map, and successful worktree
   removal. For `landed_via=squash`, report the newly created squash commit.
   For `landed_via=already-landed`, report the previously landed equivalent
   commit and that no new commit was created.

## Guardrails

- Do not use this for an ordinary branch that is not checked out in a linked
  worktree.
- Do not merge, import, or remove anything when the source worktree is dirty,
  detached, running an orchestrator, or missing its Kanban database.
- Do not treat a stale orchestrator as stopped unless the source worktree has
  no orchestrator process, no active task, and no running dashboard. Do not
  infer those from omitted `ko-get-update` fields.
- Do not import before a landed commit is recorded, either a proven
  already-landed equivalent commit or a newly created squash commit.
- Do not delete the source worktree manually with filesystem commands.
- Do not push unless the user separately asks.
