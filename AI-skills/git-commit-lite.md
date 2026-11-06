Make a one-line git commit from current conversation context.

Use this when the user asks for a lightweight commit and explicitly wants speed
over the normal diff-reading commit workflow.

This is intentionally the exception to the normal commit format: it records
only a title and never a body, `WORK`, `WHY`, `OTHER`, or `NOTES` section.

Rules:

- Do not inspect the diff.
- Do not run tests.
- Do not ask for confirmation.
- Use the current conversation and recent work memory to choose the subject.
- Stage the current repo changes with `git add .`.
- Commit with exactly one `git commit -m "<subject>"`.
- Use a concise Title Case subject under 80 characters.
- Do not include a commit body.
- Do not add co-author trailers.
- Do not put push status in the commit subject.

If there are no changes to stage, stop and say there is nothing to commit.

After that one-line commit succeeds, attempt one normal push unless `AGENTS.md` has a standalone `NO_AUTOMATIC_GIT_PUSH` line. A prose mention does not opt out. An explicit user request to push overrides the opt-out only.

Use the same safety checks as a normal commit:

- Stay on the attached branch you just committed, with exactly one same-name remote upstream.
- Require one fetch URL and at most one push URL on that same remote. Resolve the effective push destination with `git remote get-url --push` (the push URL when configured, otherwise the fetch URL, including one push rewrite). Do not follow `pushRemote`, `remote.pushDefault`, a glob, a force refspec, mirror configuration, or `push.default=matching` to a broader destination.
- Confirm the branch exists at that push destination and that its current tip is an ancestor of the new commit. `git ls-remote <remote>` reads the fetch URL, so query the resolved push URL. If a further fetch rewrite would point that query at a different URL, skip the push unless the resolved destination can be read directly. Do not create a remote branch, set an upstream, force-push, or push tags.
- Push only `refs/heads/<branch>:refs/heads/<branch>` to the configured remote name, noninteractively, without `--force` or `--set-upstream`.

Keep the repository's configured transport. Do not print credentials or private URLs. If the push is skipped or fails, leave the local commit in place and tell the user the commit hash and a concise reason.
