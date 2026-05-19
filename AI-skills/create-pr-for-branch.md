Create and publish a GitHub PR for the current branch, targeting master.

Steps:

1. Determine the current branch: `git branch --show-current`. If it is `master`, stop and tell the user to switch to a feature branch.
2. Verify there are commits on the branch vs master: `git log master..HEAD --oneline`. If empty, stop and tell the user there is nothing to PR.
3. Inspect the branch contents to draft a title and body:
   - `git log master..HEAD --reverse --pretty=format:'%s%n%n%b'` for commit history
   - `git diff master...HEAD --stat` for scope
4. Draft a PR title and body following the format below.
5. Push the current branch: `git push -u origin HEAD`
6. Check if a PR already exists for this branch: `gh pr list --head {branch} --json number,url`
   - If a PR exists: update it with `gh api repos/{owner}/{repo}/pulls/{number} -X PATCH -f title=... -f body=...`
   - If no PR exists: create it with `gh pr create --title ... --body ... --base master`
7. Report the PR URL to the user.

## PR Format

PR descriptions are Markdown — use standard Markdown formatting (headings, bullets, code spans, task lists).

### Title

- Title Case
- Under 70 characters
- Synthetic — convey the purpose, not a list of files

### Body

Open with a 1–2 sentence summary of the purpose and result. Synthesize; do not enumerate the diff or repeat changes line by line.

Sections, in order:

- **WORK** — Markdown bullets summarizing what changed. Each bullet is a distinct logical change.
- **WHY** — Markdown bullets explaining the motivation for each work item.
- **DETAIL** (optional) — Only when implementation context adds value a reviewer wouldn't get from the diff alone.
- **ALSO** (optional) — Tangential changes bundled in the same PR.
- **Review** (optional) — Scan the commit log for follow-up/fix commits (subjects starting with `fix`, `address`, `review`, `cr`, or commits that revise earlier work in the same branch). Synthesize one bullet per distinct issue category that was identified and resolved during the branch's life. Omit entirely if no such commits exist.
- **Test Plan** — GitHub task-list checkboxes for verifying the PR.

### Style

- Prefer direct statements of what changed and why. Avoid contrastive filler like "instead of", "rather than", or "no longer" unless the comparison is the point.
- Only mention file paths if it adds value. The file changes are part of the PR.
- Keep sections short. If a section would be one trivial bullet, fold it into the opening summary.

### Example

```
Knob Braking and Ramp Unified Under Physics Base Class

Shared ramp and braking logic now live in `PhysicsEngineConcreteBase`, eliminating duplication between KnobSlider and XYPad.

## WORK
- KnobSlider and XYPad delegate to the same braking path via the base class
- Renamed `applyFriction` to `applyBraking` so the name matches the behavior

## WHY
- DRY — identical braking math was copy-pasted across two subclasses
- Naming was misleading (friction ≠ braking in the physics model)

## Test Plan
- [ ] Drag a knob and release — verify braking curve matches previous behavior
- [ ] Drag XYPad and release — same check
- [ ] Confirm no regressions in automation playback
```
