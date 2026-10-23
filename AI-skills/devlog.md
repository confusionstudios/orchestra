Log concise development work notes to the configured Obsidian developer journal.

Use when asked to log work, record progress, update the journal, or save a
session summary. Write to Obsidian, not Notion or a new repo document.

## Where To Write

- `$ORCH_DEVLOG_DIR` is the private journal directory. Never hardcode its personal
  path in shared instructions. If unset, report that; do not choose a substitute.
- Use a weekly file named `YYYY-MM-DD - Week.md`, where the date is the **Sunday
  starting that week**, not today's date. Example: Friday 2026-10-23 belongs in
  `2026-10-18 - Week.md`.
- Within that file, use daily headings: `# 2026-10-23 - Friday`. Use the user's
  local date; date historical work under the day it happened when known.
- Open the expected week file directly. Listing an iCloud vault can be blocked
  even when a particular note is accessible. A failed listing alone is not proof
  that the journal is unavailable.

## What To Write

Record human-readable outcomes from the requested conversation or period:
what shipped, what changed, why it matters, and material blockers. Do not turn
entries into file inventories, command transcripts, commit lists, or test reports.
Do not investigate unrelated history unless the user requests a historical recap.
Preserve exact user wording when requested; do not invent accomplishments.

Start with the bold project name. One outcome uses `- **Project**: Outcome.`
Several outcomes use one project bullet with indented entries:

```markdown
# 2026-10-23 - Friday

- **Hogwarts Owl Dispatch**
  - Released 3.2.1, fixing a crash when assigning a parcel to an owl on sick leave.
  - Published the 3.3 beta with searchable delivery routes and a Hogsmeade postcode filter.
- **Shire Pantry Planner**
  - Added a second-breakfast slot so meal reminders stop overlapping elevenses.
  - Fixed seed-cake quantities doubling when inviting a wizard to tea.
- **Gotham Bat-Signal Scheduler**: Restored dusk alerts after the clock-tower time change.
```

Append under the correct day and existing project group. Preserve other agents'
entries and unrelated content. Create the week file or day heading if missing;
replace existing notes only when explicitly asked. Avoid duplicate entries.

## Helper

```sh
"$ORCHESTRA_DIR/AI-skills/devlog/scripts/log_work.py" --project "Project" "Outcome."
```

Use `--date YYYY-MM-DD` for a historical day. For several outcomes, pass one per
line through stdin with `--stdin`. Project defaults to `$ORCH_DEVLOG_PROJECT`,
then a readable name derived from the current Git repo.

If writing directly, use the same layout and preserve existing content. Never
truncate a note you cannot read; create a missing file without overwriting an
existing one. Verify the saved entry and report the week filename. If access
fails, report the specific limitation without claiming the note was updated.
