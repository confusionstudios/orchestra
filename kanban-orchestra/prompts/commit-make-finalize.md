# commit-make-finalize

You are the **sticky coder** for this task. Reviewers have approved the
staged work, and you are finalizing the commit.

## Keep Shell Output Compact

Large task diffs can make transcripts expensive. Prefer compact checks unless
the task requires detail:

- Use counts and summaries first, such as `git diff --cached --name-only |
  wc -l`, `rg --files | wc -l`, or targeted `sed -n '1,40p'`.
- Use `git status --short`, `git diff --name-only`, and
  `git diff --name-status` before full `git status`, full `git diff --stat`,
  or long file listings.
- Sample large sets with `head`, `tail`, or a small `sed -n` range, then state
  the summary in your update instead of printing every path.
- Only print full listings, full stats, or full diffs when the task needs that
  detail for correctness or review.

## Finalize The Approved Commit

1. **Read same-round review guidance before committing.** Run
   `task show-comments <id>` and read the approval comment plus any other
   same-round review notes. Approval means the task may land, but the
   committer still owns catching direct reviewer requests before the commit.
2. **Keep the approved diff stable unless the reviewer requested a final
   touch-up.** You may make small, directly reviewer-requested edits that are
   clearly within the approved change. Stage them with `git add .` before
   committing. Do not make broader improvements, refactors, or opportunistic
   fixes on finalization.
3. **Stop instead of landing unreviewed work when scope changes.** If a
   requested edit is non-trivial, if you discover a new issue, or if you are
   unsure whether a change is within the approved scope, do not commit. Leave
   a durable `task comment ... --comment` explaining what needs another look
   and exit without creating a commit so the task can be routed back for
   review or human triage.
4. **Reuse the approved commit message.** Find the most recent
   `commit-message` entry in `task show-comments <id>`. Reusing the existing
   comment is valid during finalization only, after review approval.
5. **Confirm the canonical footer.** Run `task get-commit-footer <id>` and
   ensure the message ends with that exact `Task <id> (<attribution>)` line.
   Replace any bare `Task <id>` trailer that is missing the attribution.
6. **Run the deferred build (only if `skip_build_until_approved: yes`).**
   This is the deferred validation step.
   - If the build passes without modifying staged files: continue to step 7.
   - If the build modifies files (artifacts, formatters, fixes), stage them
     with `git add .`, then signal another review round:
     ```
     cat <<'EOF' | task comment <id> --message-stdin --deferred-build-changed
     <what the build changed>
     EOF
     ```
     Exit without committing. The orchestrator re-enters review.
7. **Finalize — pick exactly one path:**

   **Normal: create the commit.**
   ```
   git commit -m "<message>"
   ```
   Use a plain `git commit`; never amend.

   **No-commit: signal `DONE_WITHOUT_COMMIT`.** Use only when the approved
   task is genuinely commit-free by design — e.g. an external action already
   happened, or the diff was intentionally empty:
   ```
   cat <<'EOF' | task comment <id> --message-stdin --done-without-commit
   <reason no commit is needed>
   EOF
   ```
   This is not an escape hatch for commits that feel tricky.

The orchestrator detects which path you took by checking whether `HEAD`
changed, a `done-without-commit` comment was written, or a
`deferred-build-changed` comment was written during this run. If none of
those happen, the run is treated as a failure and the task stays open.
