# Orchestra

Orchestra is a local orchestration harness for AI-agent development workflows.
It coordinates work in a git repository through a SQLite-backed task queue,
prompt templates, shell wrappers, and a small dashboard.

The project is for developers who already run coding agents from local CLIs
and want a more structured loop around planning, implementation, review, and
one-task-one-commit delivery. Orchestra does not provide hosted model access,
API keys, subscriptions, or accounts for Codex, Claude, Gemini, Kilo, or any
other model provider.

## What It Does

- Tracks development work in a local Kanban task database.
- Dispatches task prompts to configured agent CLIs on your machine.
- Supports separate coder and reviewer agents per task.
- Preserves review feedback and validation notes in task comments.
- Provides a local browser dashboard for queue and runtime visibility.
- Includes an older feature-phase workflow for larger planned efforts.

Orchestra is local-first tooling, not a hosted service. It assumes you are
comfortable with git, shell commands, and reviewing agent-generated changes.

## Core Concepts

**Orchestra checkout** is this repository. Set `ORCHESTRA_DIR` to its path and
put `bin/` on `PATH` to use the `ko-*` wrapper commands.

**Work repo** is the git repository where development happens. Kanban state is
stored in that repo as `kanban-orchestra.db`, with runtime files under
`.kanban-orchestra/`.

**Agent CLIs** are external commands configured in
`shared_scripts/shared_config.py`. The default config includes examples for
`claude`, `codex`, `gemini`, and `kilo`, but those binaries, logins, model
accounts, and billing arrangements are entirely user-provided.

**Task lifecycle** is usually:

```text
commit-plan -> commit-plan-review -> commit-make -> commit-review -> finalize
```

Planning and review steps can be skipped per task when a smaller workflow is
appropriate.

## Requirements

Required:

- macOS, Linux, or another Unix-like shell environment.
- Git.
- Python 3 with `venv` support.
- Python packages from `requirements.txt`.
- At least one configured agent CLI on `PATH` if you want the orchestrator to
  run tasks automatically.

Optional:

- A browser for the local dashboard.
- `gh` for GitHub-oriented helper workflows.
- Homebrew and the scripts in `shared_scripts/` if you want the author's
  preferred macOS helper tools.
- Additional document/media tools such as `pandoc` or `ffmpeg` only for
  workflows that explicitly use them.

## Setup

Clone the repo and build its checkout-local Python environment:

```bash
git clone https://github.com/dr2050/orchestra.git
cd orchestra
./shared_scripts/bootstrap-python-env.sh
export ORCHESTRA_DIR="$PWD"
export PATH="$ORCHESTRA_DIR/bin:$PATH"
```

For a shared machine, you can choose a stable shared checkout path instead:

```bash
export ORCHESTRA_DIR=/Users/Shared/orchestra
"$ORCHESTRA_DIR/shared_scripts/bootstrap-python-env.sh"
export PATH="$ORCHESTRA_DIR/bin:$PATH"
```

That path is only an example. Any clone path works as long as `ORCHESTRA_DIR`
points at it.

## Configure Agent CLIs

Review `shared_scripts/shared_config.py` before running the orchestrator. It
maps agent names to command lines, for example:

- `codex` -> `codex exec ...`
- `claude`, `haiku`, `sonnet`, `opus` -> `claude ...`
- `gemini` -> `gemini ...`
- `kilo` -> `kilo ...`

Install and authenticate only the CLIs you intend to use. If your available
agents differ from the defaults, either edit the shared config for your
machine or set task-level/default agent choices:

```bash
export ORCHESTRA_DEFAULT_CODER=codex
export ORCHESTRA_DEFAULT_REVIEWER=codex
```

You can also pass `--coder-agent` and `--reviewer-agent` when adding a task.

### Agent Permissions

Orchestra is a local automation harness for trusted local worktrees. It is not
a sandbox, permission boundary, or security isolation layer.

Configured agent commands may read and write files that are available to your
local user. Run the orchestrator only in repos and branches that you are
willing to let automation edit, and review the staged diff before landing
changes.

Automatic task execution expects non-interactive agent CLI modes, such as
`--yolo` or `--dangerously-skip-permissions`, because agents cannot complete
queued work if every command or file edit waits for a permission prompt. Review
and adjust `shared_scripts/shared_config.py` for your machine and trust model
before running the orchestrator.

Branch safety is a separate workflow control: tasks on `master` or `main` are
blocked by default unless the work repo opts in with `ALLOW_TASKS_ON_MASTER`.

## Quickstart

Run these commands from a separate work repo, not from the Orchestra checkout:

```bash
cd /path/to/work-repo
ko-kanban
```

Create a small task on the current branch:

```bash
ko-task add "Improve README" \
  --description "Make the setup instructions clearer." \
  --branch "$(git branch --show-current)" \
  --skip commit-plan
```

Kanban tasks on `master` or `main` are disabled by default. Use a feature
branch for normal work. Repos that intentionally queue work on `master` or
`main` can opt in by adding `ALLOW_TASKS_ON_MASTER` as a standalone line in
their root `AGENTS.md`.

Inspect the queue, note the task ID, then mark the task ready:

```bash
ko-task list
ko-task set <task-id> --status ready
```

Start the orchestrator:

```bash
ko-orchestrator
```

In another terminal, start the dashboard:

```bash
ko-dashboard
```

Open `http://127.0.0.1:8427` to watch task state and orchestrator output.

The orchestrator expects a clean worktree before it starts. It will create,
stage, review, and finalize commits according to the task lifecycle and the
agents you configured.

## Common Commands

```bash
ko-task list
ko-task show <task-id>
ko-task show-comments <task-id>
ko-task log <task-id> "short progress note"
ko-task get-commit-footer <task-id>
ko-get-update
```

Feature-phase commands are still available for larger planned efforts:

```bash
ko-feature-orchestrator
ko-feature-dashboard
```

See [feature-phase-orchestration/README.md](feature-phase-orchestration/README.md)
for that older workflow.

## Testing

From the Orchestra checkout:

```bash
bin/ko-test
```

With no arguments, this runs the unit tests for the Kanban and feature-phase
orchestration scripts.

## Known Limitations

- This is local-first orchestration around local git checkouts and local agent
  CLIs. It is not multi-user infrastructure.
- Agent behavior depends on your installed CLI versions, local auth state,
  model access, and provider limits.
- Some helper scripts reflect macOS/Homebrew-oriented workflows.
- Task state is stored in SQLite files inside the work repo.
- The orchestrator intentionally refuses to start on a dirty worktree.
- The feature-phase pipeline predates the Kanban task queue and is less central
  to day-to-day use.
- This is a small open-source project extracted from personal tooling; expect
  rough edges and read diffs carefully.

## License

Orchestra is available under the MIT License. See [LICENSE](LICENSE).
