# Agent Configuration Upgrade

The tracked registry remains the baseline. Shared and repo-local preferences
allow aliases and role defaults to change without a Git commit. Existing
`ORCHESTRA_DEFAULT_*` exports still work and take priority over file defaults.

## Pull And Restart

1. Let active work finish, then stop the instances using this installation.
   For Fleet-managed repositories, use `"$ORCHESTRA_DIR/bin/ko-fleet" stop`.
   Stop separately launched instances through their original launcher.
2. Pull the branch you use for updates. Use `develop` for integration updates;
   use `master` after the release PR has merged. Follow the README setup steps
   if the Python environment needs bootstrapping.
3. Run `"$ORCHESTRA_DIR/bin/ko-install-global-skills"` to refresh installed skill
   wrappers. Their content points to the canonical documents in this checkout.
4. Start Fleet-managed repositories with `"$ORCHESTRA_DIR/bin/ko-fleet" start`.
   Restart separately launched instances through their original launcher.
5. Check `"$ORCHESTRA_DIR/bin/ko-fleet" status` and run
   `"$ORCHESTRA_DIR/bin/ko-get-update"` from each work repository of interest.

Dirty repositories now start with a live dashboard and heartbeat, and wait
without changing files or dispatching tasks. Queued ready work starts when the
worktree becomes clean. A reboot is not required to upgrade or verify this.

## Optional Local Preferences

Shared preferences live at
`$ORCHESTRA_DIR/shared_scripts/agents.local.yaml` and apply to repositories
using that installation. For example:

```yaml
version: 1
aliases:
  grok: cursor:grok-4.7-high
defaults:
  coder:
    agent: grok
```

Use the exact model ID accepted by your installed CLI. The example alias is
local configuration, not a promise that every account has that model.

Repo preferences live at `.kanban-orchestra/agents.yaml` in the work repository.
They may define agents, aliases, and defaults; the shared file supports aliases
and defaults, plus installation-local launchers. Repo aliases take precedence over shared aliases. A repo role
entry replaces the shared entry for that role.

Role selection is explicit task/user choice, environment override, repo role,
shared role, then product fallback. Explicit choices and environment overrides
suppress file-based role patches while still resolving effective aliases.
Inspect the result from the work repository:

```bash
"$ORCHESTRA_DIR/bin/ko-task" agents
```

Restart workers and dashboards after changing preference files. Tasks already
admitted retain their command snapshots; explicitly editing a task agent
replaces that role's snapshot. Legacy tasks acquire snapshots at dispatch.
Invalid legacy choices block with an explanation for the operator.

Both preference files are ignored by Git. Pulling an update does not copy them
between computers. Local preferences are optional; the tracked baseline works
without them.

## Optional Local Launchers

The public defaults are `claude` for Claude execution/discovery and `agent` for
Cursor execution (`agent --list-models` for discovery). To opt into a local
launcher, the operator may add this to the shared ignored
`$ORCHESTRA_DIR/shared_scripts/agents.local.yaml`:

```yaml
version: 1
launchers:
  claude: local-claude-launcher
  cursor:
    run: [local-cursor-launcher, run]
    models: [local-cursor-launcher, models]
```

Each provider entry is optional. Claude retains its single executable string.
Cursor requires exactly two nonempty argument lists: `run` is the prefix for
normal and review execution; `models` is the complete model-listing command.
They may name different executables. Each first element must be an executable
name on `PATH` or an absolute path (spaces supported); `~` is expanded there.
Relative executable paths, shell command strings, and placeholders are unsupported.
Arguments are passed literally without a shell. Cursor run prefixes receive the
resolved CLI arguments unchanged; models prefixes receive no extra arguments.
Claude launchers must accept Claude CLI arguments unchanged.
No helper is selected merely because it is installed.
An explicitly configured launcher that is unavailable fails with an error;
there is no fallback to a public CLI. This setting is supported only in the shared
file, keeping repository preferences independent of machine-specific paths.

These settings cover fixed Claude and Cursor keys, semantic aliases such as
`grok`, `claude:model`, `cursor:model`, role patches, normal workers, ad-hoc
review resolution, and model discovery.
Models, flags, and the single prompt argument are retained. Provider identity
remains the canonical provider. Workers use the work repository as their working
directory and empty stdin. Claude discovery sends its initialization protocol to the selected
launcher. Cursor discovery invokes the configured `models` prefix with empty
stdin. Its cache scope includes both Cursor prefixes, and the effective registry
fingerprint includes launcher configuration. Invalid or missing configured
commands fail clearly; failed refreshes preserve last-good metadata on disk,
with metadata from a different scope hidden until a successful refresh.
Cursor scope also tracks metadata (never contents) for the login keychain and
native CLI `auth.json`: `~/.cursor/auth.json` on macOS,
`$XDG_CONFIG_HOME/cursor/auth.json` (default `~/.config/cursor/auth.json`) on
Linux, and `%APPDATA%/Cursor/auth.json` on Windows. An explicit
`CURSOR_CONFIG_DIR` additionally tracks `<CURSOR_CONFIG_DIR>/auth.json` for
launchers using that directory; it does not replace native credential tracking.
Changing the launcher invalidates discovery metadata from the previous route; ordinary resolution
never queries the provider.

Restart workers and dashboards after an operator changes preferences. Existing
tasks retain their saved executable and arguments, including snapshots that
still name a previously configured launcher. Explicitly reselect a task's agent
to replace that role's snapshot; changing the launcher does not rewrite admitted tasks.

For a harmless resolver-only check from a work repository, run:

```bash
"$ORCHESTRA_DIR/bin/ko-task" agents
```

Inspect the resolved executable, prefix, and model arguments. This does not
launch an agent or perform discovery. Run discovery separately only when ready
to use the launcher's authentication context.

## Optional Shell-Default Migration

Preview literal role exports from `~/.zshrc`:

```bash
"$ORCHESTRA_DIR/bin/ko-migrate-agent-defaults"
```

Add `--write` to save a conflict-free preview into shared preferences. The tool
reads shell text; it never executes or changes `~/.zshrc`. Unsupported shell
expressions, commented configuration, and conflicts require manual review.
Repeated writes of the same choices are idempotent.

Exports continue to override file preferences. To use file defaults, optionally
remove the exports yourself and clear them from the environment that launches
workers, including persistent terminal or tmux sessions, before restarting.

## Optional Model Discovery

From a work repository:

```bash
"$ORCHESTRA_DIR/bin/ko-task" models refresh
"$ORCHESTRA_DIR/bin/ko-task" models status
"$ORCHESTRA_DIR/bin/ko-task" models list
```

Refresh may be limited to a provider. The ignored, per-repo
`.kanban-orchestra/model-cache.json` stores advisory discovery metadata scoped
to the local configuration and authentication context. Listings are read on
demand, so refreshing the cache does not require a restart.

Missing CLIs or failed listings are reported and preserve the last good list.
Without a usable cache, baseline choices remain available. Ordinary task runs
do not discover models. A configured model absent from a listing remains
selectable; execution failures report the error without silently substituting
another model. Discovery does not install CLIs or supply credentials.
