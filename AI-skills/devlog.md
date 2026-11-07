Log concise weekly development outcomes to the configured Google Docs devlog.

Use when asked to log work, record progress, or update the development journal.

Read `~/.config/agent-devlog.json` for `folder_url` and `documents` (a map from
`YYYY H1` / `YYYY H2` to document URLs). Keep private destinations out of shared
skills. If unconfigured, ask where to write; do not fall back to Obsidian.
If Drive/Docs tools are unavailable, report that rather than claiming a write.

Use the Google Drive/Docs tools. Choose the half-year from the work's local date:
January–June is H1, July–December is H2. Documents are named `YYYY H1 Devlog` or
`YYYY H2 Devlog`. If the document is not mapped, list the configured folder and
reuse it by title; create it there only if missing. Prefer direct folder listing
when global Drive search misses shared files.

Native Google Docs structure (Markdown equivalent):

```markdown
# 2026-10-25 - Week
## Hogwarts Owl Dispatch
- Released parcel tracking for Hogsmeade deliveries ([PR #28](https://github.com/hogwarts/owl-dispatch/pull/28)).
- Fixed duplicate owl assignments.
```

Weeks start on Sunday. Projects use Heading 2, with ordinary bullets below.
Use `$ORCH_DEVLOG_PROJECT` when set, otherwise the project's readable name.
Reuse the existing week/project section; merge repeated progress into the final
outcome. No daily headings, session reports, command inventories, or invented
accomplishments. Keep useful blockers and link PR mentions to their actual URLs.

Write each full description as ordinary, unlinked text, followed by a short,
meaningful clickable label in parentheses. Only the label is linked; the
description and parentheses stay unlinked. Use labels such as `PR #28`,
`Commit abc1234`, `Review report`, or `Alpha download`. When reformatting,
preserve link destinations and the substance of descriptions.

Read before editing, preserve unrelated entries, make scoped changes, and verify
the result. Keep entries short. Obsidian notes are historical archives; the old
Obsidian helper is not used for this workflow.
