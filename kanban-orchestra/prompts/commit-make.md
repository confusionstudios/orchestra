# commit-make

You are the **sticky coder** for this task. Your first action is to read
`last_review_decision` in the task context above — it determines which path
you follow.

## Path A — `last_review_decision` is not `approve`

You are building (or reworking) the commit.

1. **Catch up on prior feedback.** Run `task show-comments <id>` and read any
   rejection comments or human notes from earlier rounds.
2. **Implement the change** described in the title and description. On a
   rework, address every reviewer comment from the latest round. If you get
   blocked after editing files, leave a durable `task comment ... --comment`
   explaining the blocker and exit non-zero — the orchestrator will stage
   and stash any uncommitted changes for you.
3. **Optional: declare a follow-up task.** If implementation reveals related
   work that belongs in its own commit:
   ```
   task follow-up <id> --description "<markdown description of the follow-up>"
   ```
   This auto-numbers the current task `1/2` (if not already numbered) and
   creates the follow-up as `2/2` (or extends an existing `n/x`). The
   orchestrator queues the follow-up immediately after this task. Call at
   most once per task. Follow-up descriptions are Markdown source; use
   headings, bullets, and code spans where they make the task clearer.
4. **Run the build.** Check `AGENTS.md` (and any file it references, e.g.
   `Orchestration/project-instructions.md`) for the documented build command
   and run that exact command — use it verbatim, not a lighter substitute.
   Fix any failures before continuing. Stream progress with
   `task log <id> "<msg>"`.

   Skip the build only when one of these applies:
   - The change is purely additive (config, skill files, prompts) and no
     build command is documented.
   - `skip_build_until_approved: yes` is in the task context — the full build
     is deferred to Path B by repo policy.
5. **Record validation.** Add a fresh validation comment summarising what you
   ran and the outcome:
   ```
   cat <<'EOF' | task comment <id> --message-stdin --validation
   <command and result, e.g. 'python3 -m pytest -q: 42 passed'>
   EOF
   ```
   When you skipped the build, state that explicitly here — e.g. `"Purely
   additive change — no build step"` or `"Full build deferred by
   SKIP_BUILD_UNTIL_APPROVED policy; will run on Path B after approval."`
   The deferral comment is required when the policy is active so reviewers
   know the missing build output is intentional.
6. **Stage everything** with `git add .` so reviewers see the diff via
   `git diff --cached`.
7. **Write the commit message.** Record it as a fresh comment on this run:
   ```
   cat <<'EOF' | task comment <id> --message-stdin --commit-message
   <commit message body>
   EOF
   ```
   Always write a fresh `--commit-message` comment during the current run,
   even if older ones exist from earlier attempts — the orchestrator detects
   the new comment as your sign-off.

   Each round's message must stand alone: write it as if the review
   conversation never happened, describing the full task end-to-end. Follow
   the format in `$ORCHESTRA_DIR/AI-skills/git-commit.md`. End with the
   canonical footer from:
   ```
   task get-commit-footer <id>
   ```
   That returns `Task <id> (<attribution>)` — use the exact string as the
   last line.
8. Stop here. The orchestrator routes you to Path B for finalization once
   reviewers approve.

---

## Path B — `last_review_decision` is `approve`

Reviewers have approved. You are finalizing the commit.

1. **Keep the approved diff as-is.** The staged diff is what reviewers signed
   off on.
2. **Reuse the approved commit message.** Find the most recent
   `commit-message` entry in `task show-comments <id>`. Reusing the existing
   comment is valid on Path B only, after review approval.
3. **Confirm the canonical footer.** Run `task get-commit-footer <id>` and
   ensure the message ends with that exact `Task <id> (<attribution>)` line.
   Replace any bare `Task <id>` trailer that is missing the attribution.
4. **Run the deferred build (only if `skip_build_until_approved: yes`).**
   This is the deferred validation step.
   - If the build passes without modifying staged files: continue to step 5.
   - If the build modifies files (artifacts, formatters, fixes), stage them
     with `git add .`, then signal another review round:
     ```
     cat <<'EOF' | task comment <id> --message-stdin --deferred-build-changed
     <what the build changed>
     EOF
     ```
     Exit without committing. The orchestrator re-enters review.
5. **Finalize — pick exactly one path:**

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
