Understand Orchestra agent aliases/specs, labels, commands, and default role env vars.

Use this skill when you need to identify available Orchestra agents, choose an
agent alias or provider/model spec for a role, explain which model/tool it
invokes, or call another agent from this repo.

## Source Of Truth

The canonical registry is:

```text
$ORCHESTRA_DIR/shared_scripts/agent_registry.yaml
```

Do not infer agent identity from model self-reporting. Use registry keys and
labels from that file. The Python loader exposes the same data. Use the repo
virtualenv, because the loader depends on the repo's Python dependencies:

```bash
orchestra_dir="${ORCHESTRA_DIR:-$(git rev-parse --show-toplevel)}"
PYTHONPATH="$orchestra_dir/shared_scripts" "$orchestra_dir/bin/ko-python" - <<'PY'
from agent_registry import AGENTS, AGENT_PROVIDERS, resolve_agent_command, resolve_agent_label
for key in AGENTS:
    print(f"{key}: {resolve_agent_label(key)} -> {resolve_agent_command(key)}")
print("Dynamic providers:", ", ".join(AGENT_PROVIDERS))
PY
```

## Current Role Defaults

Orchestra role defaults are environment-driven:

```text
ORCHESTRA_DEFAULT_SUPER_PLANNER
ORCHESTRA_DEFAULT_SUPER_REVIEWER
ORCHESTRA_DEFAULT_PLANNER
ORCHESTRA_DEFAULT_PLAN_REVIEWER
ORCHESTRA_DEFAULT_CODER
ORCHESTRA_DEFAULT_REVIEWER
```

`ORCHESTRA_DEFAULT_REVIEWER` is the default commit-review agent. If the user
asks for the configured commit-review reviewer, use that value when it names a
valid fixed alias or provider/model spec such as `cursor:<model>`. If it is
unset or invalid, use the repo fallback from `kanban-orchestra/scripts/config.py`.

Resolve defaults from the active environment like this:

```bash
orchestra_dir="${ORCHESTRA_DIR:-$(git rev-parse --show-toplevel)}"
PYTHONPATH="$orchestra_dir/shared_scripts:$orchestra_dir/kanban-orchestra/scripts" "$orchestra_dir/bin/ko-python" - <<'PY'
import config
for name in (
    "DEFAULT_SUPER_PLANNER",
    "DEFAULT_SUPER_REVIEWER",
    "DEFAULT_PLANNER",
    "DEFAULT_PLAN_REVIEWER",
    "DEFAULT_CODER",
    "DEFAULT_REVIEWER",
):
    key = getattr(config, name)
    print(f"{name}={key} ({config.get_agent_display_label(key)})")
PY
```

## Calling An Agent

When you need to call an agent, resolve the command through
`agent_registry.resolve_agent_command(agent_spec)`, then replace the single
`{prompt}` placeholder with the prompt text. This supports fixed aliases and
provider/model specs such as `cursor:claude-opus-4-8-high`. Keep the call
non-interactive, run it from the repo root, and include task-specific context
in the prompt.

Skill-specific instructions override the generic registry command. In
particular, `cross-review-converge` uses `codex exec review --uncommitted` for
Codex review so the reviewer receives staged, unstaged, and untracked changes.

For reviews, explicitly say:

```text
Do not edit files. Return findings first. End with OUTCOME: approved,
OUTCOME: rejected, or OUTCOME: blocked.
```

If an approval includes non-blocking requested changes, the reviewer should add
`NON_BLOCKING_REQUESTS:` after `OUTCOME: approved`. Mandatory changes require
`OUTCOME: rejected`.

If the user names a specific agent alias or provider/model spec, use that
value. Otherwise use the role default that matches the work, especially
`ORCHESTRA_DEFAULT_REVIEWER` for commit-review or convergence review.

## Common Local Keys

The exact set can change, so check the registry before relying on this list.
At the time this skill was written, useful keys included:

- `codex`
- `opus`
- `antigravity`
- `cursor-composer-2.5`
- `cursor-opus-4.6`
- `cursor-opus-4.7`
- `kilo-opus-4.6`
- `kilo-opus-4.7`
- `kilo-sonnet-4.6`
- `cursor:<model>` for dynamic Cursor Agent model specs
- `kilo:<model>` for dynamic Kilo model specs
