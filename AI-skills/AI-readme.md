# AI Agent Command/Skill Docs

How each agent discovers and loads custom commands and skills:

- **Claude**: https://code.claude.com/docs/en/slash-commands
- **Antigravity**: use the Open Agent Standard skill path through `agy`
- **Codex / Open Agent Standard skills**: https://developers.openai.com/codex/skills
- **Codex (AGENTS.md)**: https://developers.openai.com/codex/guides/agents-md

---

## Adding a Shared Orchestra Skill

All shared Orchestra skills live in `$ORCHESTRA_DIR/AI-skills/{skill-name}.md`
as the canonical source. Agents load them through thin user-level wrappers
installed once per machine.

Wrappers use two prefixes:

- `orch-kb-{skill-name}` for Kanban Orchestra skills.
- `orch-adhoc-{skill-name}` for general shared workflow skills.

### 1. Write the canonical skill

Create `$ORCHESTRA_DIR/AI-skills/{skill-name}.md` with the instructions the agent should follow.

### 2. Start the skill file with a one-line summary

The installer reads `$ORCHESTRA_DIR/AI-skills/{skill-name}.md` directly. It uses
the first non-empty line of the file as the wrapper description, so keep that
opening line short and descriptive.

### 3. Install wrappers once per machine

```bash
"$ORCHESTRA_DIR/bin/ko-install-global-skills"
```

This creates or refreshes thin wrappers under:

- `~/.claude/skills/orch-kb-{skill-name}/SKILL.md` or `~/.claude/skills/orch-adhoc-{skill-name}/SKILL.md`
- `~/.codex/skills/orch-kb-{skill-name}/SKILL.md` or `~/.codex/skills/orch-adhoc-{skill-name}/SKILL.md`

Wrappers reference the canonical skill through `$ORCHESTRA_DIR`. They do not
copy skill text into work repos. After the one-time install, every worktree and
repo on the machine uses the same user-level skills. Re-run the installer after
adding, changing, or deleting canonical skills.

Operational skills resolve Orchestra tooling only through `$ORCHESTRA_DIR`; they
do not treat the current worktree as an Orchestra checkout fallback.

`AI-readme.md` is excluded from installation. Existing wrappers that the
installer can identify as generated Orchestra wrappers are updated in place or
removed when their canonical skill was deleted. Unrecognized or hand-edited
skills under the same names are left untouched.

Fleet (`ko-fleet`) starts and stops orchestrators and dashboards for configured
repos. It does not install, sync, or distribute skills.

To verify installed wrappers without writing:

```bash
"$ORCHESTRA_DIR/bin/ko-install-global-skills" --check
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
