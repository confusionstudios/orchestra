# commit-make-build

You are the **sticky coder** for this task. You are building or reworking the
commit before review approval.

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

## Build Or Rework The Commit

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
     is deferred to finalization by repo policy.
5. **Record validation.** Add a fresh validation comment summarising what you
   ran and the outcome:
   ```
   cat <<'EOF' | task comment <id> --message-stdin --validation
   <command and result, e.g. 'python3 -m pytest -q: 42 passed'>
   EOF
   ```
   When you skipped the build, state that explicitly here — e.g. `"Purely
   additive change — no build step"` or `"Full build deferred by
   SKIP_BUILD_UNTIL_APPROVED policy; will run during finalization after approval."`
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
8. **Pre-exit checklist.** Before your final response, run `task
   show-comments <id>` and confirm both fresh records from this run exist:
   - a `validation` comment for step 5
   - a `commit-message` comment for step 7

   An ordinary `--comment` is useful for blockers or extra notes, but it does
   not satisfy the commit-message requirement. If the latest commit message
   was recorded with `--comment`, immediately write the same message again
   with `--commit-message` before exiting.
9. Stop here. The orchestrator routes the task to review next.
