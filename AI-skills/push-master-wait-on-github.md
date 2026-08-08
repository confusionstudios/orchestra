Push local master to GitHub, creating a temporary check PR when needed.

Use this repo-local skill only in the Orchestra repository when the user asks
to push `master` and wait for GitHub checks.

## Preconditions

- Run from the Orchestra repo root.
- The current branch must be `master`.
- The worktree must be clean.
- Local `master` must be ahead of `origin/master`.
- Do not use this for feature branches or ordinary PR creation.

## Why The Temporary PR Exists

GitHub branch protection may reject a direct push to `master` before Actions
can create check runs for the new commit. In that case repeated direct pushes
will keep reporting required checks as expected or pending, because the target
SHA has no check runs at all.

When the target SHA has no checks, push a temporary branch at the exact same
commit and open a temporary PR to `master`. That gives GitHub Actions a ref on
which to run the required checks for the same SHA. Once those checks pass, retry
the direct `master` push.

## Workflow

1. Confirm state:
   ```bash
   git status --short --branch
   git rev-parse --abbrev-ref HEAD
   git log --oneline origin/master..HEAD
   ```
2. Capture the target SHA:
   ```bash
   head_sha="$(git rev-parse HEAD)"
   ```
3. Try the direct push:
   ```bash
   git push origin master
   ```
4. If the push succeeds, stop and report success.
5. If the push fails for a non-rule reason, stop and report the exact error.
6. If GitHub rejects the push with repository rule text saying required status
   checks are expected or pending, inspect check state for the target SHA:
   ```bash
   gh api "repos/confusionstudios/orchestra/commits/$head_sha/check-runs" \
     --jq '{total_count, check_runs: [.check_runs[] | {name, status, conclusion, html_url}]}'
   gh run list --all --limit 10 \
     --json databaseId,displayTitle,headBranch,headSha,status,conclusion,url,event
   ```
7. If check runs exist for the target SHA and are pending, wait briefly and
   retry the same direct push. Repeat until checks pass, fail, or GitHub accepts
   the push.
8. If any required check for the target SHA completes with failure, stop and
   report the failing run URL. Do not bypass branch protection.
9. If there are no check runs for the target SHA, create a temporary check PR:
   ```bash
   temp_branch="codex/master-check-${head_sha:0:7}"
   git push origin "HEAD:refs/heads/$temp_branch"
   gh pr create \
     --head "$temp_branch" \
     --base master \
     --title "Temporary Check Run For Master Push" \
     --body "$(cat <<EOF
Temporary PR to attach required GitHub checks to commit $head_sha before pushing master directly.

## Why

- Protected master requires successful checks attached to the exact commit before accepting the direct push.

## Work

- Run required GitHub checks for this exact SHA.
EOF
)"
   ```
10. Wait for the temporary PR checks for the target SHA:
    ```bash
    gh run list --all --limit 10 \
      --json databaseId,displayTitle,headBranch,headSha,status,conclusion,url,event
    ```
11. If the temporary PR checks pass, retry:
    ```bash
    git push origin master
    ```
12. After the direct push succeeds, delete the temporary branch:
    ```bash
    git push origin --delete "$temp_branch"
    ```
    GitHub may mark the temporary PR as merged automatically because `master`
    now contains the same commit; that is acceptable.
13. Verify local state and final master checks:
    ```bash
    git fetch origin master --quiet
    git status --short --branch
    gh run list --branch master --limit 5 \
      --json databaseId,displayTitle,headSha,status,conclusion,url
    ```

## Guardrails

- Do not force push.
- Do not change branch protection rules.
- Do not amend or rebase while waiting.
- Use a temporary branch/PR only to create required checks for the exact local
  `HEAD` that will be pushed to `master`.
- Delete the temporary branch after the direct master push succeeds.
- Keep updates short: current attempt number, check state, and next action.
