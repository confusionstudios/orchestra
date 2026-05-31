# AI Agent Command/Skill Docs

How each agent discovers and loads custom commands and skills:

- **Claude**: https://code.claude.com/docs/en/slash-commands
- **Gemini CLI**: https://geminicli.com/docs/cli/gemini-md/
- **Codex / Open Agent Standard skills**: https://developers.openai.com/codex/skills
- **Codex (AGENTS.md)**: https://developers.openai.com/codex/guides/agents-md

---

## Adding a Shared Orchestra Skill

All shared Orchestra skills live in `$ORCHESTRA_DIR/AI-skills/{skill-name}.md` as the canonical source. Agents get a thin `ko-{skill-name}` wrapper that points to the canonical file.

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

- `.claude/skills/ko-{skill-name}/SKILL.md`
- `.agents/skills/ko-{skill-name}/SKILL.md` (Open Agent Standard path used by Codex-, Gemini-, and Kilo-compatible agents)

To explicitly clean generated wrappers from obsolete output paths, run:

```bash
"$ORCHESTRA_DIR/bin/ko-sync-skills" --fix
```

Fix mode removes generated wrappers from paths such as
`.gemini/skills/{skill-name}/SKILL.md`, `.gemini/skills/ko-{skill-name}/SKILL.md`,
`.codex/skills/{skill-name}/SKILL.md`, and `.kilo/skills/{skill-name}/SKILL.md`.
Hand-edited or unknown files are left untouched.

Each wrapper uses the same thin shared format:

```markdown
---
name: ko-{skill-name}
description: {one-line description}
---

Follow the shared skill:

- Location: $ORCHESTRA_DIR/AI-skills/{skill-name}.md
- Least Seen at: /absolute/path/to/orchestra/AI-skills/{skill-name}.md
```
