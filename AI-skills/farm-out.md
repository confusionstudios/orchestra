Delegate ad-hoc implementation to another agent while the current agent reviews and converges.

Use this when the user asks to farm out, delegate, hand off, spin up another
agent, ask another agent or subagent, or have Composer/Cursor/Claude (or
another provider/model) do the implementation while the current agent stays
owner and drives review/convergence.

This is an ad-hoc delegation workflow. It is not Kanban Orchestra tasks and not
the orchestrator pipeline.

The current agent stays owner/reviewer. The delegate implements; you inspect the
resulting diff, run verification, and loop until converged or blocked.

## Delegate Selection

1. If the user names an agent alias or provider/model spec, use it.
2. Otherwise use the configured default coding agent:
   `ORCHESTRA_DEFAULT_CODER`, or the Orchestra fallback `config.DEFAULT_CODER`.
3. If the delegate CLI is unavailable, report the blocker. Do not substitute
   another agent unless the user approves.

## Agent Command Resolution

Require `$ORCHESTRA_DIR`. Do not fall back to the current worktree as the
Orchestra checkout. Resolve commands through the shared agent registry at
`$ORCHESTRA_DIR/shared_scripts/agent_registry.py` (with `agent_registry.yaml`).
Set `repo_root` from `git rev-parse --show-toplevel` when in a git repo.

Resolve spec: `USER_SPEC` -> `ORCHESTRA_DEFAULT_CODER` -> `config.DEFAULT_CODER`,
then the command:

```bash
# USER_SPEC=opus  # optional user override
: "${ORCHESTRA_DIR:?ORCHESTRA_DIR is not set}"
repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || true
ko_python="$ORCHESTRA_DIR/bin/ko-python"
if [[ ! -x "$ko_python" ]]; then
  echo "error: Orchestra Python wrapper missing: $ko_python" >&2
  exit 1
fi

PYTHONPATH="$ORCHESTRA_DIR/shared_scripts:$ORCHESTRA_DIR/kanban-orchestra/scripts" \
"$ko_python" - <<'PY'
import os
from agent_registry import resolve_agent_command
from config import DEFAULT_CODER

spec = (
    os.environ.get("USER_SPEC")
    or os.environ.get("ORCHESTRA_DEFAULT_CODER")
    or DEFAULT_CODER
)
cmd = resolve_agent_command(spec)
if not cmd:
    raise SystemExit(f"no command for spec: {spec!r}")
print(" ".join(cmd))  # substitute {prompt}, run from repo root
PY
```

Replace the single `{prompt}` placeholder and run from `$repo_root` when set.

If the registry or `ko-python` is unavailable, report the blocker. Do not assume
a default alias such as `sonnet` is resolvable outside the registry. The
delegate may edit files.

## Pre-Delegation Worktree Check

Before delegating:

```bash
git status --short
```

- Do not silently mix unrelated dirty changes into the delegate's scope.
- If the worktree is dirty, record the pre-delegation state in your notes and
  delegate prompt (paths, staged/unstaged/untracked). After delegation, review
  only changes attributable to the delegated task; distinguish them from
  pre-existing dirt.
- If unrelated dirty files would confuse review, ask the user whether to stash,
  commit separately, or narrow scope before delegating.

## Delegate Prompt

Give the delegate a self-contained implementation brief:

- User goal and acceptance criteria
- Relevant files, constraints, and repo conventions
- Required verification (tests, commands)
- Explicit permission to edit files and use normal tool access
- **Do not commit.** **Do not stage** unless the user/operator explicitly
  requested staging. Leave all changes on disk for the current agent to review.
- Instruction to stop and report blockers instead of guessing

Run the delegate non-interactively when possible. Use a practical shell timeout
for large tasks.

## Review And Convergence Loop

After each delegate run:

1. Inspect the on-disk diff (`git status --short`, `git diff`, untracked files).
2. Judge against the original goal: correctness, regressions, safety, tests.
3. Run relevant verification per repo instructions.
4. If converged (goal met, verification passes, no material defects), stop.
5. If defects are fixable:
   - Prefer sending a focused follow-up prompt to the same delegate with
     concrete findings.
   - You may apply small fixes yourself when faster; stay owner/reviewer.
6. If the delegate reports `OUTCOME: blocked` or an equivalent hard blocker,
   stop and report it with the command used and last output.
7. Repeat until converged. If not converged after three full delegate-to-review
   cycles, stop and report remaining defects, verification status, and what
   was tried.

Do not commit unless the user asked to commit.

## Final Response

Keep it short:

- Delegate used and cycles run
- What changed (files/behavior)
- Verification run and results
- Converged, remaining issues, or blocker
- Pre-existing dirty state called out if it affected review
