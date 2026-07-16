Create and publish a GitHub PR for the current branch against the appropriate target branch.

## Choose Target

1. Determine the current branch: `git branch --show-current`. If it is a long-lived branch such as `master`, `main`, or `develop`, stop and tell the user to switch to a working branch.
2. Check for an existing PR for this branch: `gh pr list --head {branch} --json number,url,baseRefName`.
3. Determine the target branch before inspecting the branch contents:
   - Honor a target branch the user explicitly named.
   - If a PR already exists for the current branch, keep its existing target branch.
   - For a feature branch, infer the target from repository context. For example, use `develop` when it exists and the feature work is clearly based on it; otherwise use the repository default branch.
   - If the likely target is ambiguous, ask the user. Do not silently assume `master`.
4. Verify there are commits on the branch vs the selected target: `git log {target}..HEAD --oneline`. If empty, stop and tell the user there is nothing to PR.

## Inspect And Draft

5. Inspect the branch contents to draft a title and body:
   - `git log {target}..HEAD --reverse --pretty=format:'%s%n%n%b'` for commit history
   - `git diff {target}...HEAD --stat` for scope
6. Check for a repository PR template before drafting:
   - Prefer `.github/PULL_REQUEST_TEMPLATE.md` when present.
   - If `.github/PULL_REQUEST_TEMPLATE/` contains templates, choose the one that best matches the branch scope.
   - Use the repository template's headings and intent; remove placeholder comments and omit optional sections when they do not add value.
   - If no repository template exists, use the fallback format below.
7. Draft a PR title and body following the selected template or fallback format.

## Publish

8. Push the current branch: `git push -u origin HEAD`
9. Use the existing PR found above, if any:
   - If a PR exists: update it with `gh api repos/{owner}/{repo}/pulls/{number} -X PATCH -f title=... -f body=...`
   - If no PR exists: create it with `gh pr create --title ... --body ... --base {target}`
10. Report the PR URL and target branch to the user.

## PR Format

PR descriptions are Markdown — use standard Markdown formatting (headings, bullets, code spans, task lists).

### Title

- Title Case
- Under 70 characters
- Synthetic — convey the purpose, not a list of files

### Body

Sections, in order:

- **What** — The main story only: what changes for humans using or reviewing this branch. Start with one short sentence, then add at most one or two bullets for the core behavior. If a bullet is not part of that main story, move it to **Other**.
- **Why** — The motivation and impact of the main change.
- **Other** (optional) — Related or bundled changes that are not the main story. Put process/docs/tooling cleanup, supporting display changes, branch policy updates, tests, and implementation notes here when they support the branch but are not part of the primary user-facing change.
- **Review Notes** (optional) — Scan the commit log for follow-up/fix commits (subjects starting with `fix`, `address`, `review`, `cr`, or commits that revise earlier work in the same branch). Synthesize one bullet per distinct issue category that was identified and resolved during the branch's life. Omit entirely if no such commits exist.
- **Test Plan** — GitHub task-list checkboxes for verifying the PR.

### Style

- Prefer direct statements of what changed and why. Avoid contrastive filler like "instead of", "rather than", or "no longer" unless the comparison is the point.
- Only mention file paths if it adds value. The file changes are part of the PR.
- Keep sections short. If there is a single main story plus minor related changes, put only the main story sentence and its one or two core bullets under **What**, then put every secondary change under **Other**.

### Example

```
Knob Braking and Ramp Unified Under Physics Base Class

## What

KnobSlider and XYPad now delegate to the same braking path via the base class.

- Moved shared braking math into the base control class.
- Updated both controls to call the shared path on release.

## Why

The two controls had duplicated braking math, which made behavior drift likely.

## Other

- Renamed `applyFriction` to `applyBraking` so the name matches the behavior.

## Test Plan
- [ ] Drag a knob and release — verify braking curve matches previous behavior
- [ ] Drag XYPad and release — same check
- [ ] Confirm no regressions in automation playback
```
