Send the current work to the configured AI reviewer, fix actionable findings, and repeat until approved.

Use this skill when the user asks another AI to review the current on-disk diff,
asks to review to convergence, or asks for commit-review-style approval before
committing.

## Reviewer Routing

Pick the reviewer from the repo's agent configuration, not from the active
assistant's model family.

Priority:

1. If the user specifies a reviewer agent key, use that key.
2. Otherwise use the configured commit-review agent from
   `ORCHESTRA_DEFAULT_REVIEWER` when it names a valid registry key.
3. If the env var is unset or invalid, use the repo fallback exposed as
   `config.DEFAULT_REVIEWER`.

Resolve the default reviewer from the repo root:

```bash
repo_root="$(git rev-parse --show-toplevel)" || exit 1
orchestra_dir="${ORCHESTRA_DIR:-$repo_root}"
reviewer="$(
  PYTHONPATH="$orchestra_dir/shared_scripts:$orchestra_dir/kanban-orchestra/scripts" \
  "$orchestra_dir/bin/ko-python" - <<'PY'
import config
print(config.DEFAULT_REVIEWER)
PY
)"
PYTHONPATH="$orchestra_dir/shared_scripts" "$orchestra_dir/bin/ko-python" - "$reviewer" <<'PY'
import sys
from agent_registry import AGENT_CMD

reviewer = sys.argv[1]
if reviewer not in AGENT_CMD:
    raise SystemExit(f"unknown reviewer agent key: {reviewer}")
PY
```

Run only the command for the selected `$reviewer`.

For reviewers other than Codex, find the command template in
`$ORCHESTRA_DIR/shared_scripts/agent_registry.yaml` or through
`agent_registry.AGENT_CMD`, then replace the single `{prompt}` placeholder with
the review prompt.

Use the repo's configured non-interactive CLI form when available. Codex is the
only special case because `codex exec review --uncommitted` gathers the
uncommitted diff for review. All other reviewers must use the exact command
template for the selected key.

```bash
repo_root="$(git rev-parse --show-toplevel)" || exit 1
orchestra_dir="${ORCHESTRA_DIR:-$repo_root}"
cd "$repo_root" || exit 1

case "$reviewer" in
  codex)
    codex_review_help="$(codex exec review --help)"
    printf '%s\n' "$codex_review_help" | rg -- "--uncommitted" >/dev/null &&
      printf '%s\n' "$codex_review_help" | rg -- "-o, --output-last-message" >/dev/null || {
      echo "Codex review CLI is unavailable or missing required flags."
      exit 1
    }
    review_out="$(mktemp -t cross-review-codex.XXXXXX)"
    trap 'rm -f "$review_out"' EXIT
    perl -e 'alarm shift; exec @ARGV' 300 codex exec review --uncommitted -o "$review_out" "<review prompt>" || echo "Codex reviewer failed or timed out."
    cat "$review_out"
    ;;
  *)
    prompt_file="$(mktemp -t cross-review-prompt.XXXXXX)"
    trap 'rm -f "$prompt_file"' EXIT
    printf '%s' "<review prompt>" > "$prompt_file"
    PYTHONPATH="$orchestra_dir/shared_scripts" \
      perl -e 'alarm shift; exec @ARGV' 300 "$orchestra_dir/bin/ko-python" - "$reviewer" "$prompt_file" <<'PY'
import os
import sys
from pathlib import Path
from agent_registry import AGENT_CMD

reviewer = sys.argv[1]
prompt = Path(sys.argv[2]).read_text(encoding="utf-8")
cmd = [part.replace("{prompt}", prompt) for part in AGENT_CMD[reviewer]]
os.execvp(cmd[0], cmd)
PY
    ;;
esac
```

The `codex exec review --uncommitted -o` form is supported by the repo's installed Codex CLI. If `codex exec review --help` does not show both `--uncommitted` and `-o, --output-last-message`, stop and report the Codex review CLI as unavailable.

If the configured reviewer CLI is unavailable, report the blocker and do not
substitute a different reviewer unless the user explicitly approves.

The command examples assume a macOS/Linux shell with `perl` and `rg`. If those tools are unavailable, use an equivalent shell-level timeout and help-output check. Use a longer timeout when the diff is broad, schema-sensitive, or otherwise likely to require deeper context.

The Codex review subcommand gathers staged, unstaged, and untracked changes.
Neither Codex, Claude, Cursor, Kilo, nor Gemini is mechanically prevented from
editing files in every environment, so the prompt must explicitly say `Do not
edit files`. Prefer read/review-specific CLI modes and deny edit tools where
the reviewer CLI supports it.

## Review Prompt

Ask the reviewer to inspect only the current on-disk changes and to avoid
editing files. The reviewer may approve with non-blocking requested changes;
those requests do not require another approval pass unless the committer makes
substantive changes or chooses to ask for another review.

```text
Review the current uncommitted changes in this repository, including staged, unstaged, and untracked files. Run `git status --short` and inspect the current on-disk changes before reviewing. Focus on bugs, behavioral regressions, safety issues, and missing tests. Do not edit files. Return findings first with file/line references; if no issues, say so clearly and mention residual risk. End with OUTCOME: approved, OUTCOME: rejected, or OUTCOME: blocked.

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
7. If the reviewer returns no `OUTCOME:` line, treat the pass as blocked and report the raw reviewer output.
8. If the reviewer returns actionable findings with `OUTCOME: rejected`:
   - Fix only findings that are concrete defects, missing required validation, or clear behavioral regressions.
   - Ignore style-only, speculative, or unrelated suggestions unless they reveal a real defect.
   - Explain briefly when declining a reviewer note.
9. Run the relevant verification after each fix. Follow repo instructions for required tests.
10. Send the updated diff back to the same reviewer.
11. Repeat until the reviewer approves or blocks. If the loop has not converged after three reviewer passes, stop and report the remaining findings and verification status.

## Guardrails

- Do not let a reviewer process run indefinitely. If it produces no output for 5 minutes, interrupt it and retry once with a tighter prompt or a longer shell-level timeout for broad or schema-sensitive diffs.
- Do not commit unless the user asked to commit or the active workflow requires it.
- Do not stage unrelated files.
- If reviewer output conflicts with repo policy or user instructions, follow the higher-priority instruction and explain the conflict.
- Keep the final report short: reviewer outcome, fixes made, verification run, and commit hash if committed.
