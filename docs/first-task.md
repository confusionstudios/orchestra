# Your First Orchestra Task

This walkthrough installs Orchestra, creates a throwaway Git repo, and runs one
small commit task through coding, review, and a landed commit. It uses plain
terminal commands and a single coding-agent CLI. It does not need Fleet,
Tailscale, the installed agent skills, changes to your shell profile, or a
reboot.

The examples use Codex. A Claude Code variant is noted where it differs.

Orchestra runs the agent in a non-interactive, permission-skipping mode as your
user. A throwaway repo keeps Git history tidy, but it is not a sandbox. Read
[SECURITY.md](../SECURITY.md) before pointing Orchestra at anything you care
about.

## Before You Start

- Python 3.10+ and Git, with `user.name` and `user.email` configured
- the Codex CLI installed and signed in, so that `codex exec` works
  non-interactively (or Claude Code, for the variant below)

## 1. Install Orchestra

Pick a location that does not already exist. `git clone` refuses to write into
a non-empty directory.

```bash
git clone https://github.com/confusionstudios/orchestra.git ~/src/orchestra
export ORCHESTRA_DIR="$HOME/src/orchestra"
"$ORCHESTRA_DIR/shared_scripts/bootstrap-python-env.sh"
```

The `export` lasts only for this terminal. Re-run it in any other terminal you
use below, or call the wrappers by full path.

## 2. Create A Throwaway Repo

Use a new directory. If `mkdir` fails because it already exists, stop and
choose another name before continuing.

```bash
mkdir ~/orchestra-first-task && cd ~/orchestra-first-task
git init
git switch -c first-task

cat > .gitignore <<'EOF'
# Orchestra runtime state
kanban-orchestra.db
kanban-orchestra.db-journal
kanban-orchestra.db-shm
kanban-orchestra.db-wal
kanban-orchestra.sql
kanban-orchestra.lock
.kanban-orchestra/
EOF

cat > greet.py <<'EOF'
def greet(name):
    return "Hello, " + name
EOF

git add .gitignore greet.py
git commit -m "Initial commit"
git status --short
```

The last command should print nothing. Three details matter:

- **Feature branch.** Tasks on `master` or `main` are disabled by default, so
  the task runs on `first-task`.
- **Initial commit.** Orchestra works on a branch with existing history, not
  an empty repo.
- **Ignored runtime state.** Orchestra keeps its task database, logs, and
  agent preferences in the work repo, and the coder stages its work with
  `git add .`. Ignoring these files keeps them out of your commits and keeps
  `git status` clean.

## 3. Use One CLI For Every Role

Out of the box, Orchestra uses Claude for coding, planning, and unblocking, and
Codex for review. With only one CLI, point every role at it in the repo's own
preferences file, which the `.gitignore` above already covers:

```bash
mkdir .kanban-orchestra
cat > .kanban-orchestra/agents.yaml <<'EOF'
version: 1
defaults:
  coder: {agent: codex}
  reviewer: {agent: codex}
  planner: {agent: codex}
  plan_reviewer: {agent: codex}
  super_planner: {agent: codex}
  super_reviewer: {agent: codex}
  unblocker: {agent: codex}
EOF
```

For Claude Code only, replace each `codex` with `sonnet`.

`ORCHESTRA_DEFAULT_*` environment variables take priority over this file. If
you have set any, unset them in this terminal. If `KANBAN_DB` is set, unset it
too, so the wrappers use this repo's database. Then confirm the effective
choices:

```bash
"$ORCHESTRA_DIR/bin/ko-task" agents | python3 -c \
  'import json, sys; print({r: (d["agent"], d["source"]) for r, d in json.load(sys.stdin)["defaults"].items()})'
```

Every role should show `codex` (or `sonnet` for Claude Code). A source of
`environment` means an `ORCHESTRA_DEFAULT_*` variable is still overriding
the file.

## 4. Queue A Task

A good task says what to change, what to leave alone, and how to check it:

```bash
"$ORCHESTRA_DIR/bin/ko-task" add "Add a greeting test" \
  --branch first-task \
  --description 'Add `test_greet.py` using only the standard library `unittest` module. Assert that `greet("Ada")` returns `Hello, Ada`. Do not change `greet.py`. Validate with `python3 -m unittest -v`.'
```

This prints the new task as JSON, with `"id": 1` and `"status": "none"`. A task
in `none` is a draft. New tasks skip commit review by default, so turn review
on and mark the task ready to run:

```bash
"$ORCHESTRA_DIR/bin/ko-task" set 1 --remove-skip commit-review --status ready
"$ORCHESTRA_DIR/bin/ko-get-update"
```

The update lists task 1 under `READY` and reminds you that the orchestrator has
not started yet.

In everyday use you would ask your coding agent to do this through the Kanban
skill, which `ko-install-global-skills` installs. The commands above are what
that skill runs.

## 5. Run the Orchestrator

From the repo root:

```bash
"$ORCHESTRA_DIR/bin/ko-orchestrator"
```

Leave it running. It picks up task 1, has the coder write and stage the change,
sends the staged diff to the reviewer, and creates the commit once the review
approves. A small task like this usually takes a few minutes.

From a second terminal in the same directory, check progress at any time:

```bash
"$ORCHESTRA_DIR/bin/ko-get-update"
```

While the orchestrator is running, this also prints the local dashboard URL.

## 6. Inspect the Result

When the update shows task 1 under `RECENTLY DONE`:

```bash
git log --oneline
git show --stat HEAD
python3 -m unittest -v
git status --short
"$ORCHESTRA_DIR/bin/ko-task" show-comments 1
```

You should see a second commit that adds `test_greet.py`, a passing test, a
clean worktree, and the comments that record the coder's validation and the
reviewer's decision. The dashboard shows the same history.

Press Ctrl-C once in the orchestrator terminal to stop it.

## If It Gets Stuck

- **The update says `waiting-dirty`.** Something untracked or modified is in
  the worktree. Run `git status --short`, then commit or remove it. Queued work
  starts once the tree is clean.
- **The task is `blocked`.** Read `ko-task show-comments 1` for the reason. A
  missing or signed-out CLI is the usual cause on a first run. The running
  orchestrator may reassess and resume it on its own. Otherwise, how to resume
  depends on the recorded reason: `ko-task continue 1` works only when the task
  recorded its resume step, a review-cap block needs `--add-review-rounds N`,
  and other blocks need `--next-step <step>`. See the operator rules in the
  [README](../README.md#operating-model).
- **The roles are not what you expected.** Re-run the `ko-task agents` check
  from step 3. Agent choices are recorded when a task is added. For a task that
  has not started, `ko-task set 1 --coder-agent codex --reviewer-agent codex`
  replaces the two main choices. Restart the orchestrator after editing the
  preferences file.

## Cleaning Up

Stop the orchestrator and delete `~/orchestra-first-task`, which holds this
walkthrough's task database, logs, and preferences. The walkthrough does not
change your shell profile or shared agent preferences; the agent CLI keeps its
usual session state wherever it normally does.

If something was confusing or broke along the way, that is useful to hear. See
[CONTRIBUTING.md](../CONTRIBUTING.md) for what to include.
