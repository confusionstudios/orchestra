Log concise development work notes to the configured Obsidian developer journal.

Use this when the user asks to log work, record what happened, write a work
journal entry, update the developer journal, save a session summary, or persist
work notes to Obsidian.

## Journal Location

The journal directory is private local configuration. It must be set with:

```bash
export ORCH_DEVLOG_DIR="/path/to/Developer/Journal"
```

Do not hardcode a personal Obsidian path in repo files, commits, or shared
instructions. If `$ORCH_DEVLOG_DIR` is unset or unavailable, report that and do
not log elsewhere unless the user explicitly asks.

The journal uses Sunday-start weekly Markdown notes named `YYYY-MM-DD - Week.md`,
with daily headings named `# YYYY-MM-DD - Weekday`.

## Project Name

Resolve the project label from:

1. `--project`, when explicitly passed.
2. `$ORCH_DEVLOG_PROJECT`, when set.
3. Otherwise a readable name derived from the current Git repo root directory
   (hyphens/underscores become spaces; each word is title-cased). Example:
   `orchestra` -> `Orchestra`, `midi-designer3` -> `Midi Designer3`.

Prefer `$ORCH_DEVLOG_PROJECT` when the derived directory name would be unclear
or ugly. Do not read `.orchestra-skill-sync` for the project name.

## Workflow

1. Identify the work to log from the current conversation and tool results.
2. Limit the log scope to the current user-requested work or session. Do not
   scan old git history, old journal entries, prior unrelated tasks, or repo
   activity outside the active conversation unless the user explicitly asks for
   a date range or historical summary.
3. Write for a human scanning the journal later, not for code review. Default
   to one short outcome-focused sentence per project, but use multiple nested
   bullets when one project has several distinct outcomes worth preserving.
4. Summarize what got done and why it matters. Avoid file lists, command lists,
   commit hashes, branch names, and exhaustive verification details unless one
   is the point of the entry.
5. Mention blockers or failed verification only when they materially affect
   what happened next.
6. Use backticks for literal things only when truly needed. Project labels are
   the exception: make them bold.
7. Start every bullet with the project first. Do not prefix bullets with
   timestamps. For a single entry for a project, bold the project name and use a
   colon:

```markdown
- **project-name**: Entry text.
```

8. When a day has multiple entries for the same project, or when a single
   project entry needs multiple bullets, group them under one bold project
   bullet with no colon, and indent the entries:

```markdown
- **project-name**
  - First entry.
  - Second entry.
```

9. Do not invent details. If the user provides exact wording, preserve it unless
   they ask for cleanup.
10. Append the entry to the current day heading in the current week note.
    Create the week note or daily heading if absent.
11. Never overwrite or reorganize existing journal content.

## Logging Helper

Use the bundled helper from the Orchestra checkout:

```bash
"$ORCHESTRA_DIR/AI-skills/devlog/scripts/log_work.py" "Moved non-Kanban shared skills into Orchestra."
```

For multi-line entries:

```bash
printf '%s\n' "Moved non-Kanban shared skills into Orchestra." "Kept private Obsidian paths in local environment." |
  "$ORCHESTRA_DIR/AI-skills/devlog/scripts/log_work.py" --stdin
```

Multiple input lines become multiple nested bullets under the project:

```markdown
- **Orchestra**
  - Moved non-Kanban shared skills into Orchestra.
  - Kept private Obsidian paths in local environment.
```

If the helper is not available, implement the same behavior directly:

- Compute the current local date.
- Compute the Sunday that starts that week.
- Resolve the project label from `--project`, `$ORCH_DEVLOG_PROJECT`, or a
  readable name derived from the current Git repo root directory.
- Open `$ORCH_DEVLOG_DIR/<sunday> - Week.md`.
- Ensure a daily heading exists.
- Append a compact project-first bullet for one-line entries:

```markdown
- **project-name**: Entry text.
```

- If the entry has multiple lines, or the same project already has an entry for
  that day, convert it to or append under a grouped project bullet:

```markdown
- **project-name**
  - First entry.
  - Second entry.
```

Report the path updated after logging.
