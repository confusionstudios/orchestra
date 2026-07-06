# AI Agent Command/Skill Docs

How each agent discovers and loads custom commands and skills:

- **Claude**: https://code.claude.com/docs/en/slash-commands
- **Antigravity**: use the Open Agent Standard skill path through `agy`
- **Codex / Open Agent Standard skills**: https://developers.openai.com/codex/skills
- **Codex (AGENTS.md)**: https://developers.openai.com/codex/guides/agents-md

---

## Adding a Shared Orchestra Skill

All shared Orchestra skills live in `$ORCHESTRA_DIR/AI-skills/{skill-name}.md`
as the canonical source. Agents get a thin wrapper that points to the canonical
file.

Wrappers use two prefixes:

- `orch-kb-{skill-name}` for Kanban Orchestra skills.
- `orch-adhoc-{skill-name}` for general shared workflow skills.

### 1. Write the canonical skill

Create `$ORCHESTRA_DIR/AI-skills/{skill-name}.md` with the instructions the agent should follow.

### 2. Start the skill file with a one-line summary

The wrapper sync script reads `$ORCHESTRA_DIR/AI-skills/{skill-name}.md` directly. It uses the first non-empty line of the file as the wrapper description, so keep that opening line short and descriptive.

### 3. Sync wrappers into the target repo

From the repo that should receive the wrappers:

```bash
"$ORCHESTRA_DIR/bin/ko-sync-skills"
```

This creates or refreshes:

- `.claude/skills/orch-kb-{skill-name}/SKILL.md` or `.claude/skills/orch-adhoc-{skill-name}/SKILL.md`
- `.agents/skills/orch-kb-{skill-name}/SKILL.md` or `.agents/skills/orch-adhoc-{skill-name}/SKILL.md` (Open Agent Standard path used by Codex-, Antigravity-, Kilo-, and other compatible agents)

To explicitly repair generated wrapper policy and clean old generated wrappers
from current output paths, run:

```bash
"$ORCHESTRA_DIR/bin/ko-sync-skills" --fix
```

Fix mode ensures narrow `.gitignore` entries exist for generated
`orch-kb-*` and `orch-adhoc-*` wrapper directories, removes generated
unprefixed wrappers from current `.claude/skills` and `.agents/skills` paths,
removes all legacy `ko-*` wrapper directories from those same current paths,
and removes current generated wrappers from git tracking when run in a git
repo. Current generated wrappers under `.claude/skills/orch-*` and
`.agents/skills/orch-*` remain on disk as ignored local generated files.
Hand-edited or unknown non-`ko-*` files are left untouched.

### 4. Register repos for ad-hoc sync

To opt a repo into future bulk syncs from the Orchestra checkout:

```bash
"$ORCHESTRA_DIR/bin/ko-sync-skills" --register /path/to/client-repo --project-name "MIDI Designer"
```

Registration writes two things:

- A repo-local `.orchestra-skill-sync` marker. This is safe to commit and says
  the repo accepts shared Orchestra skill sync. When `--project-name` is passed,
  the marker also stores the human display name used by the devlog skill.
- A private machine-local path entry in
  `~/.config/orchestra/skill-sync.repos`, or in the path named by
  `$ORCHESTRA_SKILL_SYNC_REPOS`.

From the Orchestra checkout, sync every registered repo:

```bash
"$ORCHESTRA_DIR/bin/ko-sync-skills" --registered
```

Registered sync runs fix mode and then normal sync for each repo whose path is
still valid and whose repo root still contains `.orchestra-skill-sync`. Missing
paths or repos without the marker are skipped rather than guessed.

To opt out:

```bash
"$ORCHESTRA_DIR/bin/ko-sync-skills" --unregister /path/to/client-repo
```

Each wrapper uses the same thin shared format:

```markdown
---
name: orch-kb-{skill-name}
description: {one-line description}
---

Follow the shared skill:

- Location: $ORCHESTRA_DIR/AI-skills/{skill-name}.md
- Least Seen at: /absolute/path/to/orchestra/AI-skills/{skill-name}.md
```
