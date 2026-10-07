Understand Orchestra agent aliases, commands, and effective role preferences.

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

Shared Git-ignored overrides live at
`$ORCHESTRA_DIR/shared_scripts/agents.local.yaml`. Its `version: 1`
and `aliases` and `defaults` mappings can redefine names and role choices for
every work repo using that installation, without a Git commit or shell edit.
Repo-local aliases and agents take precedence; repo role entries replace shared role entries.
Explicit task choices and `ORCHESTRA_DEFAULT_*` exports take priority over
file-based role defaults. Remove persistent exports to use shared defaults.
Use `ko-task agents` to inspect the source and resolved command. Restart workers
and dashboards after editing either layer;
existing task snapshots retain their selected commands.

Registry `aliases` are Orchestra semantic names. They target an existing fixed
key or `provider:model` spec and do not define their own commands. `grok` is
the current preferred Cursor Grok model.

Use `"$ORCHESTRA_DIR/bin/ko-task" models refresh [provider]` to update local
CLI model metadata explicitly. `models status` shows freshness, the last
successful refresh, and a concise failure reason. `models list [provider]`
shows exact IDs, labels, capability metadata, and baseline/discovery sources.
The generated cache is `.kanban-orchestra/model-cache.json` in the launched
work repository; it is ignored by Git and scoped to the local configuration
and authentication context. A missing or corrupt cache uses baseline choices;
refresh to recover. Failed refreshes keep the last good list. A configured
model absent from the latest listing remains selectable and is flagged by
`ko-task agents`; listing absence does not prove the CLI will reject it.
Execution errors, including missing binaries and rejected models, fail the
task without changing the selected provider or model.

Explicit `provider:<exact-model-id>` works for Codex, Claude, Cursor,
Kilo, and Antigravity without a registry entry. Each launched work repository
may set `.kanban-orchestra/agents.yaml`; use `"$ORCHESTRA_DIR/bin/ko-task" agents`
to inspect the effective registry and role defaults. The local file is ignored
by Git. Restart the worker and dashboard after editing local preferences; admitted
tasks keep their saved command snapshots. Cache listings are read on demand
and reflect a refresh without a restart. Ordinary task runs
do not query provider CLIs for models.

Preview migration of literal shell defaults with
`"$ORCHESTRA_DIR/bin/ko-migrate-agent-defaults"`; `--write` saves them into the
shared ignored file without executing or editing the shell file. Existing
exports retain priority until optionally removed and cleared manually.

## Current Role Defaults

Resolve role defaults in this order: explicit task/user choice, an explicit
`ORCHESTRA_DEFAULT_*` environment value, repo-local role entry, shared role
entry, then the product fallback. Environment overrides remain optional:

```text
ORCHESTRA_DEFAULT_SUPER_PLANNER
ORCHESTRA_DEFAULT_SUPER_REVIEWER
ORCHESTRA_DEFAULT_PLANNER
ORCHESTRA_DEFAULT_PLAN_REVIEWER
ORCHESTRA_DEFAULT_CODER
ORCHESTRA_DEFAULT_REVIEWER
ORCHESTRA_DEFAULT_UNBLOCKER
```

`ORCHESTRA_DEFAULT_REVIEWER` is the default commit-review agent. If the user
asks for the configured commit-review reviewer, use that value when it names a
valid fixed alias, registry alias, or provider/model spec such as
`cursor:<model>`. If unset, use the repo-local reviewer role, then the shared
reviewer role, then the product fallback. An explicitly invalid value is an error.

Resolve effective defaults for the launched work repository like this:

```bash
"$ORCHESTRA_DIR/bin/ko-task" agents
```

## Calling An Agent

When you need to call an agent, resolve the command through
`agent_registry.configure(work_repo_root)` followed by
`choice = agent_registry.effective().role(role, explicit_spec)` and
`agent_registry.effective().command(choice["agent"], patch=choice["patch"])`, then replace the
single `{prompt}` placeholder. This retains role-specific model, reasoning,
and option settings. For a direct spec with no role patch,
`resolve_agent_command(agent_spec)` also works. This supports fixed aliases,
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
staged, unstaged, and untracked changes. Ad-hoc review explicitly chooses the
review template; Kanban review steps use the saved normal command unless a
caller explicitly requests the review template.

For reviews, explicitly say:

```text
Do not edit files. Return findings first. End with OUTCOME: approved,
OUTCOME: rejected, or OUTCOME: blocked.
```

If an approval includes non-blocking requested changes, the reviewer should add
`NON_BLOCKING_REQUESTS:` after `OUTCOME: approved`. Mandatory changes require
`OUTCOME: rejected`.

If the user names a specific agent alias or provider/model spec, use that
value. Otherwise use the role default that matches the work, using the effective
`reviewer` role for commit-review or convergence review.

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
- `grok` — preferred Cursor Grok model; inspect the effective alias target and overrides
- `cursor-composer-2.5`
- `cursor-grok-4.5`
- `cursor-opus-4.6`
- `cursor-opus-4.7`
- `kilo-opus-4.6`
- `kilo-opus-4.7`
- `kilo-sonnet-4.6`
- `cursor:<exact-model-id>` for dynamic Cursor Agent model specs; use `agent --list-models` for live account IDs
- `kilo:<model>` for dynamic Kilo model specs
