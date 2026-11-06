You will make a git commit.
- Read the files and ignore your memory of what changed.
- Git Commit Message Format:
   - **Title**: Use Title Case For Commit Message Titles. Synthetic. Less than 80 characters.
   - **Leading paragraph**: After the title, write one short paragraph about the main change.
   - **WHY** (optional): Explain the motivation for this commit when it adds useful context. Do not repeat the implementation. Omit the heading when empty.
   - **WORK**: Add a `WORK` section with concise Unicode `•` bullets for the main implementation work.
   - **OTHER** (optional): Add an `OTHER` section only for related work that does not belong in the main story. Omit the heading when empty.
   - **NOTES** (optional): Add a `NOTES` section only for useful process detail, such as validation, migration, follow-up, or review context. Omit the heading when empty.
   - **Wording**: Prefer direct statements of what changed and why. Avoid contrastive filler like `instead of`, `rather than`, or `no longer` unless the comparison is the important point.
- SYNTHESIZE. The commit subject and opening sentences should tell the reader what changed at a high level and why.
- SUMMARIZE the purpose. Do NOT repeat the code changes line by line or enumerate the diff. We already have the code and file list.
- Only mention file paths if it adds value. The file changes are part of the commit.
- Do not mention Claude/Codex/Gemini as code author unless specifically asked. The human is the author. THERE IS NO CO-AUTHOR, Claude!
- Do NOT wait for confirmation. Just commit immediately.

## Push After A Successful Commit

After the commit succeeds, attempt one normal push when it is safe. Tell the user what happened. Do not put push status in the commit message.

Skip the automatic push when `AGENTS.md` contains a standalone `NO_AUTOMATIC_GIT_PUSH` line. Prose that merely mentions that name does not opt out. If the user explicitly asked to push, that request overrides the opt-out and the safety checks below still apply.

Push only when all of these are true:

- `HEAD` is attached and is the branch you just committed.
- That branch has exactly one upstream, and the upstream is the same branch name on a remote. A `.` or other local-only upstream is not eligible.
- The remote has exactly one fetch URL and at most one push URL. Resolve the effective push destination with `git remote get-url --push`. That is the push URL when one is configured, otherwise the fetch URL, after one `pushInsteadOf` or `insteadOf` rewrite. Keep that resolved destination; do not replace it with another host or credential.
- `branch.<name>.pushRemote` and `remote.pushDefault` do not point at a different remote. Stop when URLs, push refspecs, or the remote listing are multiple or ambiguous.
- The configured push refspec is absent or exactly this one branch. Do not push when it uses a glob, a force prefix, mirror configuration, or `push.default=matching`.
- The branch already exists on the effective push destination, and that destination's current tip is an ancestor of the new commit. `git ls-remote <remote>` reads the fetch URL, so query the resolved push URL before pushing. If a further fetch `insteadOf` would send that query to a different URL, skip the push unless the resolved destination can be read directly. Do not create a remote branch or set an upstream.
- The command is one bounded noninteractive push of `refs/heads/<branch>:refs/heads/<branch>` to the configured remote name. Do not pass `--force`, `--force-with-lease`, `--all`, `--mirror`, `--tags`, `--follow-tags`, or `--set-upstream`.

Use the configured remote name for the push so Git keeps the repository's transport and credential helper. Do not print credentials or private URLs.

If the push is skipped or fails, keep the local commit. Tell the user the full commit hash and a concise reason. Authentication, network, timeout, and server rejection do not undo the commit.

## Example

Physics Engine: Knob Braking and Ramp Unified Under Base Class

Shared ramp and braking logic now live in `PhysicsEngineConcreteBase`.

WHY
• Divergent release behavior made braking inconsistent across control types.

WORK
• KnobSlider and XYPad now delegate to the same braking path.

OTHER
• Renamed `applyFriction` to `applyBraking` so the shared behavior reads the same way it behaves.
