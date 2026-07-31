Send the current work to the configured AI reviewer, fix actionable findings, and repeat until approved.

Use this skill when the user asks another AI to review the current on-disk diff,
asks to review to convergence, or asks for commit-review-style approval before
committing.

## Reviewer Routing

Pick the reviewer from the repo's agent configuration, not from the active
assistant's model family.

Priority:

1. If the user specifies a reviewer agent alias or provider/model spec, use it.
2. Otherwise use the configured commit-review agent from
   `ORCHESTRA_DEFAULT_REVIEWER` when it names a valid fixed alias or
   provider/model spec such as `cursor:<model>`.
3. If the env var is unset or invalid, use the Orchestra fallback exposed as
   `config.DEFAULT_REVIEWER`.

Require `$ORCHESTRA_DIR`. Do not fall back to the current worktree as the
Orchestra checkout. Resolve the default reviewer:

```bash
: "${ORCHESTRA_DIR:?ORCHESTRA_DIR is not set}"
repo_root="$(git rev-parse --show-toplevel)" || exit 1
reviewer="$(
  PYTHONPATH="$ORCHESTRA_DIR/shared_scripts:$ORCHESTRA_DIR/kanban-orchestra/scripts" \
  "$ORCHESTRA_DIR/bin/ko-python" - <<'PY'
import config
print(config.DEFAULT_REVIEWER)
PY
)"
PYTHONPATH="$ORCHESTRA_DIR/shared_scripts" "$ORCHESTRA_DIR/bin/ko-python" - "$reviewer" <<'PY'
import sys
from agent_registry import is_valid_agent_spec

reviewer = sys.argv[1]
if not is_valid_agent_spec(reviewer):
    raise SystemExit(f"unknown reviewer agent alias or provider/model spec: {reviewer}")
PY
```

Run only the review command for the selected `$reviewer`.

Resolve the command through
`agent_registry.resolve_review_agent_command(reviewer)`, then replace the
single `{prompt}` placeholder with the review prompt. The shared resolver uses
review-specific forms only when they are known to be sufficiently permissive,
such as Codex `exec review --uncommitted`, and falls back to the normal agent
command for providers without a review-specific template. Do not use restrictive
ASK/read-only modes for agent CLIs; they tend to block necessary tool access.

```bash
: "${ORCHESTRA_DIR:?ORCHESTRA_DIR is not set}"
repo_root="$(git rev-parse --show-toplevel)" || exit 1
cd "$repo_root" || exit 1

prompt_file="$(mktemp -t cross-review-prompt.XXXXXX)"
trap 'rm -f "$prompt_file"' EXIT
printf '%s' "<review prompt>" > "$prompt_file"
PYTHONPATH="$ORCHESTRA_DIR/shared_scripts" \
  perl -e 'alarm shift; exec @ARGV' 300 "$ORCHESTRA_DIR/bin/ko-python" - "$reviewer" "$prompt_file" <<'PY'
import os
import sys
from pathlib import Path
from agent_registry import resolve_review_agent_command

reviewer = sys.argv[1]
prompt = Path(sys.argv[2]).read_text(encoding="utf-8")
cmd_template = resolve_review_agent_command(reviewer)
if cmd_template is None:
    raise SystemExit(f"unknown reviewer agent alias or provider/model spec: {reviewer}")
cmd = [part.replace("{prompt}", prompt) for part in cmd_template]
os.execvp(cmd[0], cmd)
PY
```

If the configured reviewer CLI is unavailable, report the blocker and do not
substitute a different reviewer unless the user explicitly approves.

The command example assumes a macOS/Linux shell with `perl`. If it is unavailable, use an equivalent shell-level timeout. Use a longer timeout when the diff is broad, schema-sensitive, or otherwise likely to require deeper context.

The Codex review subcommand gathers staged, unstaged, and untracked changes.
Neither Codex, Claude, Cursor, Kilo, nor Antigravity is mechanically prevented from
editing files in every environment, so the prompt must explicitly say `Do not
edit files`. Do not compensate with restrictive ASK/read-only modes for agent
CLIs; use normal permissive agent commands and rely on explicit review
instructions.

## Review Prompt

Ask the reviewer to inspect only the current on-disk changes and to avoid
editing files. The reviewer may approve with non-blocking requested changes;
those requests do not require another approval pass unless the committer makes
substantive changes or chooses to ask for another review.

Before constructing the prompt, collect untracked files and append their paths
to it. `git diff` does not include untracked files, so the reviewer needs an
explicit list to inspect them directly:

```bash
git status --short
git ls-files --others --exclude-standard
```

```text
Review the current uncommitted changes in this repository, including staged, unstaged, and untracked files. Run `git status --short` and inspect the current on-disk changes before reviewing. Focus on bugs, behavioral regressions, safety issues, and missing tests. Do not edit files. Return findings first with file/line references; if no issues, say so clearly and mention residual risk. End with OUTCOME: approved, OUTCOME: rejected, or OUTCOME: blocked.

Untracked files that are in scope and must be read directly:
<explicit newline-separated path list, or "None">

If you approve but want non-blocking follow-up changes before commit, add a `NON_BLOCKING_REQUESTS:` section after `OUTCOME: approved`. Use this only for changes the committer may apply without another approval pass. Use `OUTCOME: rejected` for mandatory changes.
```

Add one sentence of task-specific context when it would materially improve the review, such as the user request or the intended behavior.

## Convergence Loop

1. Confirm the repo state:
   ```bash
   git status --short
   ```
2. If `git status --short` is empty, stop and report that there is no on-disk diff to review.
3. Run the reviewer command from the repo root.
4. If the reviewer returns `OUTCOME: approved`, treat the review loop as converged.
5. If an approved review includes `NON_BLOCKING_REQUESTS:`, read those requests
   before committing. You may apply small, directly requested changes without
   another review pass when the approved behavior stays intact. Request another
   review if the edits become substantive.
6. If the reviewer returns `OUTCOME: blocked`, stop and report the blocker, the command used, and the last reviewer output.
7. If the review response is blank, send the same reviewer a short
   non-destructive health check such as `Reply with exactly PONG.`
   - If it does not return the requested literal reply, stop and report the
     reviewer as blocked.
   - If it does reply, retry once with a bounded, numbered checklist. Name all
     untracked target paths again. Ask for `yes` or `no` plus path/line evidence
     for each question, and require a final `FINAL: approved`,
     `FINAL: rejected`, or `FINAL: blocked` line.
     Use this shape, adding task-specific questions when needed:
     ```text
     Read the current on-disk diff and these untracked files directly:
     <explicit newline-separated path list, or "None">

     Do not edit files. Answer each question with yes or no and brief path/line
     evidence:
     1. Does the current change implement the requested behavior?
     2. Is there a concrete bug, regression, safety issue, or missing required
        validation?
     3. Are the required tests present and appropriate for real logic?

     End with exactly one line: FINAL: approved, FINAL: rejected, or
     FINAL: blocked.
     ```
   - Treat `FINAL: approved` as convergence, `FINAL: rejected` as actionable
     findings, and `FINAL: blocked` as blocked. A second blank response is
     blocked. Do not substitute another reviewer without user approval.
8. If the reviewer returns non-blank output with no `OUTCOME:` line, treat the
   pass as blocked and report the raw reviewer output.
9. If the reviewer returns actionable findings with `OUTCOME: rejected` or
   `FINAL: rejected`:
   - Fix only findings that are concrete defects, missing required validation, or clear behavioral regressions.
   - Ignore style-only, speculative, or unrelated suggestions unless they reveal a real defect.
   - Explain briefly when declining a reviewer note.
10. Run the relevant verification after each fix. Follow repo instructions for required tests.
11. Send the updated diff back to the same reviewer.
12. Repeat until the reviewer approves or blocks. If the loop has not converged after three reviewer passes, stop and report the remaining findings and verification status.

## Guardrails

- Do not let a reviewer process run indefinitely. If it produces no output for 5 minutes, interrupt it and retry once with a tighter prompt or a longer shell-level timeout for broad or schema-sensitive diffs.
- Do not commit unless the user asked to commit or the active workflow requires it.
- Do not stage unrelated files.
- If reviewer output conflicts with repo policy or user instructions, follow the higher-priority instruction and explain the conflict.
- Keep the final report short: reviewer outcome, fixes made, verification run, and commit hash if committed.
