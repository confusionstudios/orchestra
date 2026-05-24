Send the current work to a different AI reviewer, fix actionable findings, and repeat until approved.

Use this skill when the user asks for another AI to review the current on-disk diff, asks to review to convergence, or asks to get a second-model review before committing.

## Reviewer Routing

Pick the reviewer by the active assistant:

- Claude calls Codex.
- Codex calls Claude.
- Gemini calls Codex.

Use the repo's configured non-interactive CLI form when available:

```bash
repo_root="$(git rev-parse --show-toplevel)" || exit 1
cd "$repo_root" || exit 1

codex_review_help="$(codex exec review --help)"
printf '%s\n' "$codex_review_help" | rg -- "--uncommitted" >/dev/null &&
  printf '%s\n' "$codex_review_help" | rg -- "-o, --output-last-message" >/dev/null || {
  echo "Codex review CLI is unavailable or missing required flags."
  exit 1
}

# Claude or Gemini asking Codex to review
review_out="$(mktemp -t cross-review-codex.XXXXXX)"
trap 'rm -f "$review_out"' EXIT
perl -e 'alarm shift; exec @ARGV' 300 codex exec review --uncommitted -o "$review_out" "<review prompt>" || echo "Codex reviewer failed or timed out."
cat "$review_out"

# Codex asking Claude to review
perl -e 'alarm shift; exec @ARGV' 300 claude -p "<review prompt>" --output-format text \
  --allowedTools "Bash(git status*)" "Bash(git diff*)" "Bash(git log*)" "Bash(git show*)" "Bash(rg *)" Read Glob Grep \
  --disallowedTools Edit Write MultiEdit NotebookEdit
```

The `codex exec review --uncommitted -o` form is supported by the repo's installed Codex CLI. If `codex exec review --help` does not show both `--uncommitted` and `-o, --output-last-message`, stop and report the Codex review CLI as unavailable.

If the preferred reviewer CLI is unavailable, report the blocker and do not substitute the same model family as the reviewer unless the user explicitly approves.

The command examples assume a macOS/Linux shell with `perl` and `rg`. If those tools are unavailable, use an equivalent shell-level timeout and help-output check. Use a longer timeout when the diff is broad, schema-sensitive, or otherwise likely to require deeper context.

The Codex review subcommand gathers staged, unstaged, and untracked changes. Gemini should run the same Codex review command through its shell tool. Neither Codex nor Claude is mechanically prevented from editing files in every environment, so the prompt must explicitly say `Do not edit files`. Prefer read/review-specific CLI modes and deny edit tools where the reviewer CLI supports it.

If the active assistant is not Claude, Codex, or Gemini, stop and report that cross-review routing is undefined for the active assistant.

## Review Prompt

Ask the reviewer to inspect only the current on-disk changes and to avoid editing files:

```text
Review the current uncommitted changes in this repository, including staged, unstaged, and untracked files. Run `git status --short` and inspect the current on-disk changes before reviewing. Focus on bugs, behavioral regressions, safety issues, and missing tests. Do not edit files. Return findings first with file/line references; if no issues, say so clearly and mention residual risk. End with OUTCOME: approved, OUTCOME: rejected, or OUTCOME: blocked.
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
5. If the reviewer returns `OUTCOME: blocked`, stop and report the blocker, the command used, and the last reviewer output.
6. If the reviewer returns no `OUTCOME:` line, treat the pass as blocked and report the raw reviewer output.
7. If the reviewer returns actionable findings:
   - Fix only findings that are concrete defects, missing required validation, or clear behavioral regressions.
   - Ignore style-only, speculative, or unrelated suggestions unless they reveal a real defect.
   - Explain briefly when declining a reviewer note.
8. Run the relevant verification after each fix. Follow repo instructions for required tests.
9. Send the updated diff back to the same reviewer.
10. Repeat until the reviewer approves or blocks. If the loop has not converged after three reviewer passes, stop and report the remaining findings and verification status.

## Guardrails

- Do not let a reviewer process run indefinitely. If it produces no output for 5 minutes, interrupt it and retry once with a tighter prompt or a longer shell-level timeout for broad or schema-sensitive diffs.
- Do not commit unless the user asked to commit or the active workflow requires it.
- Do not stage unrelated files.
- If reviewer output conflicts with repo policy or user instructions, follow the higher-priority instruction and explain the conflict.
- Keep the final report short: reviewer outcome, fixes made, verification run, and commit hash if committed.
