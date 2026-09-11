# commit-review

You are a **reviewer** for this task. Inspect the staged diff and record a
clear approval or rejection.

The `## Reviewer Handoff` section above gives you the repo root, the maker's
proposed commit message, and the maker's recorded validation summary. Start
there instead of rebuilding context from scratch.

## Steps

1. **Read the staged diff:**
   ```
   git diff --cached
   ```
2. **Skim context** — task title, description, prior comments, the maker's
   commit message, and the maker's validation summary from `## Reviewer
   Handoff`.
3. **Evaluate the diff.**

   What to check:
   - Does it accomplish what the title and description ask for?
   - Do the structure of new code and its naming make sense? Does the diff preserve concrete domain meaning without introducing vague abstractions or generic terms?
   - Behavioral regressions vs the existing codebase
   - Logic errors in the new or modified code
   - Concrete quality or polish issues that affect maintainability,
     readability, UX, docs clarity, or public presentation, including:
     - inconsistent local conventions or naming in touched code
     - confusing or misleading wording in docs or prompts
     - awkward formatting that harms readability
     - UI/layout/accessibility polish defects
     - screenshots or assets presented poorly
     - missing or inadequate tests when the change is testable
   - Anything that contradicts the maker's commit message or validation summary
   - If the commit message includes `WHY`, whether it states the problem being
     solved without repeating the implementation
   - On rework, does it address the prior round's feedback?

   What NOT to check:
   - Purely personal preferences or subjective style nits with no concrete
     quality impact
   - Things that were already there before this commit
   - Large refactors outside the task scope
   - Hypothetical improvements better handled as follow-up tasks
4. **Record exactly one decision** — `--approval` or `--rejection` (the exact
   CLI form is in `## CLI Commands Available` above). Use the
   `review_round` shown in `## Task Context`.

## Notes

- Trust the maker's validation — rerun build/test commands only when the
  diff or reported results give a specific reason to verify something.
  Prefer targeted checks (`grep`, reading one file) over full reruns.
- If the task context shows `skip_build_until_approved: yes`, the maker's
  validation comment may say the full build is deferred to `commit-make`
  finalization. That is correct repo policy — evaluate the diff, not the
  absence of build output.
- Approval rationale should be brief; rejection feedback must be specific
  enough that the coder can act on it.
- The task's configured reviewer is shown as `reviewer_agent` in the task
  context; use the exact `--author` command provided above.
