# Orchestra

Orchestra turns your local AI coding agents into an autonomous development
team. You describe tasks in plain language; Orchestra queues them, assigns
agents to code and review, runs rejection cycles on the staged diffs, and
lands approved commits — while you do something else.

<img src="docs/agent-with-kanban-skill-with-orchestrator-dashboard.png" alt="Kanban Agent & Dashboard" />

## How It Works

Orchestra is a Python-based local task queue backed by SQLite. Each task is
scoped to one git commit. The orchestrator prompts a coding agent to make the
change, routes the diff through a review agent, and repeats until the reviewer
approves or the retry budget is exhausted. The approved commit is then
finalized on the branch.

The primary interface is your AI agent's **Kanban skill**, not a CLI you drive
by hand. You install the skill into your agent, then talk to it: *"Queue a
task to fix X."* The skill handles bootstrapping, task creation, status
checks, and all wrapper commands.

## Supported Agents

Orchestra shells out to local agent CLIs. It ships with built-in support for:

| Key | Agent CLI | Notes |
|-----|-----------|-------|
| `claude` / `sonnet` / `haiku` / `opus` | [Claude Code](https://claude.ai/code) | Default coder and reviewer |
| `codex` | [OpenAI Codex CLI](https://github.com/openai/codex) | |
| `gemini` | [Gemini CLI](https://github.com/google-gemini/gemini-cli) | |
| `kilo` | [Kilo Code](https://kilocode.ai) | |

You can add or change agent commands in
`shared_scripts/shared_config.py`. Orchestra does not provide API keys,
accounts, or billing — install and authenticate each CLI yourself.

## Prerequisites

- **Python 3.10+**
- **Git**
- **macOS or Linux** (Windows is untested)
- At least one supported agent CLI installed and authenticated

## Getting Started

1. **Clone the repo:**

   ```bash
   git clone https://github.com/dr2050/orchestra.git /path/to/orchestra
   ```

2. **Set `ORCHESTRA_DIR`** in your shell startup file (`.zshrc`, `.bashrc`, or
   equivalent):

   ```bash
   export ORCHESTRA_DIR="/path/to/orchestra"
   ```

3. **Bootstrap the Python environment:**

   ```bash
   "$ORCHESTRA_DIR/shared_scripts/bootstrap-python-env.sh"
   ```

4. **Sync skills into a work repo:**

   ```bash
   cd /path/to/work-repo
   "$ORCHESTRA_DIR/bin/ko-sync-skills"
   ```

5. **Start the dashboard** for the work repo:

   ```bash
   "$ORCHESTRA_DIR/bin/ko-ui"
   ```

   Use the Browser button in `orchestra-ui` to open the HTML dashboard.

6. **Talk to your agent.** In the work repo, invoke the Kanban skill with a
   plain-language request:

   > "Using the Kanban skill, queue a task to fix the broken pagination query."

   The skill handles the rest.

### Tips

* Orchestra will not launch against a dirty worktree. Commit or stash before
  starting.
* The agent running the Kanban skill should not modify the worktree itself —
  queued runs block if uncommitted changes appear between tasks.
* Set a task to `none` status while you're still editing it. Change it to
  `ready` when it should be picked up.
* The Kanban skill can adjust task details before launch: add/remove skipped
  steps, swap the coding or review agent, change retry limits. Ask it.

## Agent Configuration

Default agent roles can be overridden with environment variables. Set each one
to a key from `shared_scripts/shared_config.py`:

```bash
export ORCHESTRA_DEFAULT_CODER=haiku
export ORCHESTRA_DEFAULT_REVIEWER=codex
export ORCHESTRA_DEFAULT_PLANNER=sonnet
export ORCHESTRA_DEFAULT_PLAN_REVIEWER=codex
export ORCHESTRA_DEFAULT_SUPER_PLANNER=sonnet
export ORCHESTRA_DEFAULT_SUPER_REVIEWER=codex
```

## Source Layout

| Path | Contents |
|------|----------|
| `AI-skills/` | Canonical skill instructions |
| `kanban-orchestra/scripts/` | Task queue, dashboard, orchestrator, and CLI |
| `kanban-orchestra/prompts/` | Prompts injected into task agents |
| `bin/` | Thin wrappers that run through the checkout-local venv |
| `shared_scripts/` | Setup and helper scripts |

Agent-local skill folders (`.claude/`, `.codex/`, `.gemini/`, `.agents/`) are
generated artifacts and gitignored.

## Operating Model

Kanban state lives in the **work repo**, not in the Orchestra checkout:

- `kanban-orchestra.db` — task state (SQLite)
- `.kanban-orchestra/` — runtime files and logs
- Tasks target the work repo's current branch unless overridden

The orchestrator expects a clean worktree. It refuses to launch when dirty and
blocks rather than continuing through unexpected uncommitted changes.

## Policies

Orchestra is not a sandbox or permission boundary. Agent commands run as your
local user and can read or write any files available to that user.

Automatic task execution requires non-interactive agent CLI modes. If an agent
needs a permission prompt for every command, queued work will not complete
reliably.

Tasks on `master` or `main` are blocked by default. To opt in, add
`ALLOW_TASKS_ON_MASTER` to the work repo's root `AGENTS.md`.

## Development

To work on Orchestra itself, skip the orchestration workflow and treat it as a
normal Python repo:

```bash
export ORCHESTRA_DIR="/path/to/orchestra"
"$ORCHESTRA_DIR/shared_scripts/bootstrap-python-env.sh"
"$ORCHESTRA_DIR/bin/ko-test"
```

## License

MIT. See [LICENSE](LICENSE).
