Understand Orchestra agent aliases/specs, labels, commands, and default role env vars.

Use this skill when you need to identify available Orchestra agents, choose an
agent alias or provider/model spec for a role, explain which model/tool it
invokes, or call another agent from this repo.

## Source Of Truth

The canonical registry is:

```text
$ORCHESTRA_DIR/shared_scripts/agent_registry.yaml
```

Do not infer agent identity from model self-reporting. Inspect effective keys,
labels, and commands for the launched work repository with:

```bash
"$ORCHESTRA_DIR/bin/ko-task" agents
```

## Registry Aliases vs Live Model IDs

Registry `aliases` are Orchestra semantic names. They target an existing fixed
key or `provider:model` spec and do not define their own commands. `grok` is
the current preferred Cursor Grok model.

`agent --list-models` is authoritative for live, account-specific Cursor model
IDs. Explicit `provider:<exact-model-id>` works for Codex, Claude, Cursor,
Kilo, and Antigravity without a registry entry. Each launched work repository
may set `.kanban-orchestra/agents.yaml`; use `"$ORCHESTRA_DIR/bin/ko-task" agents`
to inspect the effective registry and role defaults. The local file is ignored
by Git. Restart the worker and dashboard after editing it; admitted tasks keep
their saved command snapshots.

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
valid fixed alias, registry alias, or provider/model spec such as
`cursor:<model>`. If unset, use the local reviewer role default, then the
Orchestra fallback. An explicitly invalid value is an error.

Resolve effective defaults for the launched work repository like this:

```bash
"$ORCHESTRA_DIR/bin/ko-task" agents
```

## Calling An Agent

When you need to call an agent, resolve the command through
`agent_registry.configure(work_repo_root)` followed by
`agent_registry.resolve_agent_command(agent_spec)`, then replace the single
`{prompt}` placeholder with the prompt text. This supports fixed aliases,
registry aliases such as `grok`, and provider/model specs such as
`cursor:claude-opus-4-8-high`. Keep the call
non-interactive, run it from the repo root, and include task-specific context
in the prompt. Cursor specs resolve through `remote-control-cursor run` so the
agent executes in the signed-in GUI session. Do not feed resolver source code
through stdin; agent CLIs may consume inherited stdin as additional prompt
content. Use `python -c`, a script file, or another invocation that leaves the
child process stdin clean.

Skill-specific instructions override the generic registry command. In
particular, `cross-review-converge` uses `codex exec review {prompt}` for Codex
review. Current Codex cannot combine `--uncommitted` with a custom prompt; the
prompt (including an explicit untracked-file list) is what scopes the review to
staged, unstaged, and untracked changes.

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

## Verify Agent Output

A zero exit status proves that an agent CLI ran; it does not prove that the
agent returned a usable response. Before relying on an unfamiliar route, send
a tiny non-destructive ping and require a literal reply.

For reviews, explicitly name untracked target files. `git diff` does not show
them, so a reviewer must read those paths directly.

If a broad review is blank but the ping succeeds, keep the selected reviewer
and retry with a bounded numbered checklist that requires `yes` or `no` answers
and path/line evidence. A blank response is never approval. If the bounded
retry is also blank, report the reviewer as blocked; do not silently substitute
another agent.

## Common Local Keys

The exact set can change, so check the registry before relying on this list.
At the time this skill was written, useful keys included:

- `haiku` — Claude Haiku
- `sonnet` / `claude` — Claude Sonnet
- `codex`
- `opus` — Claude Opus
- `fable` — Claude Fable
- `antigravity`
- `grok` — current preferred Cursor Grok model (`cursor:cursor-grok-4.6-high`)
- `cursor-composer-2.5`
- `cursor-grok-4.5`
- `cursor-opus-4.6`
- `cursor-opus-4.7`
- `kilo-opus-4.6`
- `kilo-opus-4.7`
- `kilo-sonnet-4.6`
- `cursor:<exact-model-id>` for dynamic Cursor Agent model specs; use `agent --list-models` for live account IDs
- `kilo:<model>` for dynamic Kilo model specs
