Review the current on-disk git diff and report approval or actionable defects.

Use this skill when the user asks for a direct review of uncommitted work on disk, without the ad-hoc `build-notes.md` / `review-notes.md` handoff.

## Steps

1. Confirm the repo state:
   ```bash
   git status --short
   ```
2. Inspect the on-disk diff:
   ```bash
   git diff
   ```
3. If there are staged changes, inspect them separately so no reviewed work is missed:
   ```bash
   git diff --cached
   ```
4. Skim only the context needed to evaluate the diff. Prefer targeted reads of touched files over broad repo exploration.
5. Evaluate the diff.

   Check:
   - Whether the changes accomplish the user-stated intent, if one was provided
   - Behavioral regressions against the existing codebase
   - Logic errors, broken edge cases, missing migrations/config updates, or inconsistent call sites
   - Test coverage gaps when the change introduces behavior that should be verified
   - Any contradiction between the diff and reported validation

   Do not check:
   - Style-only preferences
   - Hypothetical improvements unrelated to the diff
   - Pre-existing issues outside the changed scope
6. Run tests only when the diff or repo policy gives a specific reason. Prefer targeted tests. If repository instructions specify a required review/test command, run it unless there is a practical blocker.
7. Report findings first, ordered by severity. Include file and line references where possible.

## Output

Use this structure:

```text
## Findings
- [severity] path/to/file:line - Actionable defect or risk. Explain why it matters and what needs to change.

## Open Questions
- Question or assumption, if any.

## Verification
- Commands run, or `Not run: <reason>`.

## Outcome
OUTCOME: [approved | rejected | blocked]

NON_BLOCKING_REQUESTS:
- Optional follow-up changes the builder may apply without another approval pass.
```

If there are no findings, say:

```text
## Findings
- No behavioral regressions detected in the reviewed diff.
```

## Decision Rules

- Use `approved` only when no actionable defects are found.
- With `approved`, optionally include `NON_BLOCKING_REQUESTS:` for comments or
  follow-up changes that are not required for approval.
- Use `rejected` when the diff has a concrete defect or missing required validation that the builder can fix.
- Use `blocked` when the diff cannot be reviewed because required context, files, or commands are unavailable.
- Keep rejection feedback specific enough that the builder can act without another clarification round.
- Do not edit files while using this skill unless the user explicitly asks you to fix the issues.
