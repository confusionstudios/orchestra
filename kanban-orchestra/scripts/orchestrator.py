#!/usr/bin/env python3
"""
orchestrator.py — Kanban Orchestra main loop.

Watches for ready tasks, picks one, launches agents, streams output,
updates state, and advances the state machine.

Sequential: one task at a time, exclusive repo access assumed.
"""

import os
import re
import select
import signal
import shlex
import subprocess
import sys
import threading
import time
import fcntl
import argparse
from collections import deque
from datetime import datetime
from pathlib import Path

# Allow importing db.py and config.py from same directory
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
import config
import repo_policy
import orchestrator_control
import active_agent_processes

# Import shared agent config from the orchestra repo
ORCHESTRA_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ORCHESTRA_ROOT / "shared_scripts"))
from shared_config import AGENT_CMD

# Re-export for backwards compatibility
DEFAULT_CODER = config.DEFAULT_CODER
DEFAULT_SUPER_PLANNER = config.DEFAULT_SUPER_PLANNER
DEFAULT_PLANNER = config.DEFAULT_PLANNER
DEFAULT_PLAN_REVIEWER = config.DEFAULT_PLAN_REVIEWER
DEFAULT_REVIEWER = config.DEFAULT_REVIEWER
DEFAULT_SUPER_REVIEWER = config.DEFAULT_SUPER_REVIEWER
MAX_REVIEW_ROUNDS = config.MAX_REVIEW_ROUNDS
MAX_PRIOR_COMMENTS = config.MAX_PRIOR_COMMENTS
POLL_INTERVAL = config.POLL_INTERVAL
HEARTBEAT_INTERVAL = config.HEARTBEAT_INTERVAL
STOP_AFTER_TASK_FILE = config.STOP_AFTER_TASK_FILE
DASHBOARD_START_REQUEST_FILE = config.DASHBOARD_START_REQUEST_FILE
MASTER_BRANCHES = {"master", "main"}
MASTER_TASKS_DISABLED_MESSAGE = (
    "Tasks on master/main are disabled by default. Use a feature branch, or add "
    "ALLOW_TASKS_ON_MASTER as a standalone line in AGENTS.md to explicitly opt in."
)
GITHUB_PR_URL_RE = re.compile(r"https://github\.com/[^\s/]+/[^\s/]+/pull/\d+")


_log_conn = None
_log_fh = None


def log(msg, task_id=None):
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = f"[{ts}]"
    if task_id:
        prefix += f" [task:{task_id}]"
    line = f"{prefix} {msg}"
    print(line, flush=True)
    if _log_fh is not None:
        try:
            print(line, file=_log_fh, flush=True)
        except Exception:
            pass
    if _log_conn is not None:
        try:
            db.add_run_log(_log_conn, task_id, msg, author="orchestrator")
        except Exception:
            pass


def format_task_ref(task):
    """Render a human-facing task reference for dashboards, prompts, and comments."""
    title = task.get("title")
    if title:
        return f"Task {task['id']}: {title}"
    return f"Task {task['id']}"


def _task_reviewer(task):
    """Return the code-review agent configured for a task."""
    return task.get("reviewer_agent") or DEFAULT_REVIEWER


# ── Heartbeat thread ─────────────────────────────────────────────────

_heartbeat_stop = threading.Event()
_heartbeat_thread = None
_singleton_lock_handle = None
_dashboard_process = None
_dashboard_metadata_path_for_process = None


class SingletonLockError(RuntimeError):
    """Raised when another orchestrator instance already holds the repo lock."""


def _singleton_lock_path(db_path=None, lock_path=None):
    """Resolve the singleton lock file path for the current workspace."""
    if lock_path is not None:
        return Path(lock_path).resolve()
    return db.get_lock_path(db_path)


def acquire_singleton_lock(db_path=None, lock_path=None):
    """Acquire the repo-scoped singleton lock or raise if it is already held."""
    global _singleton_lock_handle

    if _singleton_lock_handle is not None:
        raise RuntimeError("Singleton lock is already held by this process.")

    path = _singleton_lock_path(db_path=db_path, lock_path=lock_path)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        metadata = orchestrator_control.read_singleton_lock_metadata(lock_path=path)
        repo_root = metadata.get("repo_root") or str(path.parent)
        pid = metadata.get("pid")
        pid_detail = f" (PID {pid})" if pid else ""
        handle.close()
        raise SingletonLockError(
            f"Another Kanban orchestrator instance is already running for {repo_root}{pid_detail}."
        ) from exc

    identity = db.get_instance_identity(db_path, lock_path=path)
    handle.seek(0)
    handle.truncate()
    lock_fields = {
        "schema_version": "1",
        "role": "orchestrator",
        "pid": str(os.getpid()),
        "started_at": datetime.now().isoformat(),
        **identity,
    }
    handle.write("".join(f"{key}={value}\n" for key, value in lock_fields.items()))
    handle.flush()
    _singleton_lock_handle = handle
    return path


def release_singleton_lock():
    """Release the repo-scoped singleton lock if it is currently held."""
    global _singleton_lock_handle

    if _singleton_lock_handle is None:
        return

    try:
        _singleton_lock_handle.seek(0)
        _singleton_lock_handle.truncate()
        fcntl.flock(_singleton_lock_handle.fileno(), fcntl.LOCK_UN)
    finally:
        _singleton_lock_handle.close()
        _singleton_lock_handle = None


def _heartbeat_loop(db_path):
    """Background thread that updates last_heartbeat_at every HEARTBEAT_INTERVAL seconds."""
    conn = db.connect(db_path)
    try:
        while not _heartbeat_stop.wait(HEARTBEAT_INTERVAL):
            try:
                db.update_runtime(conn, last_heartbeat_at="CURRENT_TIMESTAMP")
            except Exception:
                pass  # Best-effort; don't crash the heartbeat thread
    finally:
        conn.close()


def start_heartbeat(db_path):
    global _heartbeat_thread
    _heartbeat_stop.clear()
    _heartbeat_thread = threading.Thread(target=_heartbeat_loop, args=(db_path,), daemon=True)
    _heartbeat_thread.start()


def stop_heartbeat():
    _heartbeat_stop.set()
    if _heartbeat_thread is not None:
        _heartbeat_thread.join(timeout=5)


def set_runtime_idle(conn, status_message="Waiting for ready tasks"):
    """Set runtime to idle state, clearing task/agent fields."""
    db.update_runtime(
        conn,
        status="idle",
        current_task_id=None,
        current_step="none",
        current_branch=None,
        review_round=None,
        active_agents=0,
        status_message=status_message,
        last_heartbeat_at="CURRENT_TIMESTAMP",
    )


def _dashboard_metadata_path(db_path=None):
    return db.get_runtime_root(db_path) / "dashboard.json"


def _dashboard_start_request_path(db_path=None):
    return db.get_runtime_root(db_path) / DASHBOARD_START_REQUEST_FILE


def request_dashboard_start(db_path=None):
    """Ask the running repo orchestrator to start its dashboard."""
    path = _dashboard_start_request_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text("start\n", encoding="utf-8")
    tmp.replace(path)
    return path


def _read_dashboard_start_request(db_path=None):
    path = _dashboard_start_request_path(db_path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        log(f"Could not read dashboard start request: {exc}")
        return False

    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "port":
            try:
                return int(value.strip())
            except ValueError:
                log(f"Ignoring invalid dashboard start request port: {value.strip()!r}")
                return True
    return True


def _clear_dashboard_start_request(db_path=None):
    try:
        _dashboard_start_request_path(db_path).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log(f"Could not clear dashboard start request: {exc}")


def _remove_dashboard_metadata(path=None, db_path=None):
    if path is None:
        path = _dashboard_metadata_path(db_path)
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log(f"Could not remove dashboard metadata: {exc}")


def start_dashboard(db_path=None, *, host="127.0.0.1", preferred_port=None):
    """Start the matching repo dashboard as a child of this orchestrator."""
    global _dashboard_metadata_path_for_process, _dashboard_process

    if _dashboard_process is not None and _dashboard_process.poll() is None:
        return _dashboard_process

    metadata_path = _dashboard_metadata_path(db_path)
    _remove_dashboard_metadata(metadata_path)
    dashboard_path = Path(__file__).resolve().parent / "dashboard.py"
    env = os.environ.copy()
    if preferred_port is not None:
        env["KO_DASH_PORT"] = str(preferred_port)
    env["KO_DASHBOARD_METADATA_PATH"] = str(metadata_path)
    stdout = _log_fh if _log_fh is not None else subprocess.DEVNULL
    process = subprocess.Popen(
        [sys.executable, str(dashboard_path)],
        cwd=str(Path(db.get_db_path(db_path)).resolve().parent),
        env=env,
        stdout=stdout,
        stderr=subprocess.STDOUT if _log_fh is not None else subprocess.DEVNULL,
    )
    _dashboard_process = process
    _dashboard_metadata_path_for_process = metadata_path
    log(f"Dashboard started with PID {process.pid}; metadata: {env['KO_DASHBOARD_METADATA_PATH']}")
    return process


def stop_dashboard():
    """Stop the dashboard process owned by this orchestrator, if any."""
    global _dashboard_metadata_path_for_process, _dashboard_process

    process = _dashboard_process
    metadata_path = _dashboard_metadata_path_for_process
    _dashboard_process = None
    _dashboard_metadata_path_for_process = None
    if process is None:
        if metadata_path is not None:
            _remove_dashboard_metadata(metadata_path)
        return
    if process.poll() is not None:
        _remove_dashboard_metadata(metadata_path)
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log(f"Dashboard process {process.pid} did not exit after SIGKILL")
    finally:
        _remove_dashboard_metadata(metadata_path)


def check_dashboard_process(conn, db_path=None):
    """Notice owned dashboard exit without stopping task processing."""
    global _dashboard_metadata_path_for_process, _dashboard_process
    process = _dashboard_process
    metadata_path = _dashboard_metadata_path_for_process
    if process is None:
        return

    exit_code = process.poll()
    if exit_code is None:
        return

    db.update_runtime(
        conn,
        status_message=f"Dashboard exited with code {exit_code}; dashboard stopped",
        last_heartbeat_at="CURRENT_TIMESTAMP",
    )
    log(f"Dashboard exited with code {exit_code}; dashboard stopped")
    _dashboard_process = None
    _dashboard_metadata_path_for_process = None
    _remove_dashboard_metadata(metadata_path)


def handle_dashboard_start_request(conn, db_path=None):
    """Consume an explicit dashboard-start request, if one exists."""
    request = _read_dashboard_start_request(db_path)
    if not request:
        return

    _clear_dashboard_start_request(db_path)
    if _dashboard_process is not None and _dashboard_process.poll() is None:
        log("Dashboard start requested; dashboard is already running")
        return

    preferred_port = request if type(request) is int else None
    process = start_dashboard(db_path, preferred_port=preferred_port)
    db.update_runtime(
        conn,
        status_message=f"Dashboard started with PID {process.pid} by explicit request",
        last_heartbeat_at="CURRENT_TIMESTAMP",
    )
    log(f"Dashboard started with PID {process.pid} by explicit request")


# ── Prompt assembly ────────────────────────────────────────────────────

def _prompts_dir():
    return Path(__file__).resolve().parent.parent / "prompts"


def _repo_root():
    """Return the git repo root, or a fallback string on failure."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return "(unknown — run: git rev-parse --show-toplevel)"


def _repo_root_for_subprocess():
    """Return a real repo-root cwd for child processes, or None if unavailable."""
    repo_root = _repo_root()
    if repo_root.startswith("("):
        return None
    return repo_root


def _master_tasks_allowed():
    try:
        return repo_policy.read_allow_tasks_on_master(_repo_root())
    except Exception:
        return False


def _task_on_disallowed_master_branch(task):
    return task.get("branch") in MASTER_BRANCHES and not _master_tasks_allowed()


def _build_reviewer_handoff(task, comments, skip_build_policy=False):
    """
    Build the reviewer handoff section injected into commit-review prompts.

    Provides repo root, task CLI path, the maker's recorded commit message,
    and the maker's recorded validation summary so reviewers start with
    complete context and do not need to rediscover the environment or rerun
    the maker's validation steps.

    skip_build_policy: when True, the repo has SKIP_BUILD_UNTIL_APPROVED enabled,
    so a deferred validation result is expected and normal.
    """
    repo_root = _repo_root()
    # Extract the most recent commit-message comment (the maker's summary)
    commit_msg_entries = [c for c in comments if c.get("kind") == "commit-message"]
    if commit_msg_entries:
        latest = commit_msg_entries[-1]["message"]
        maker_summary = f"**Maker's proposed commit message** (most recent):\n```\n{latest}\n```"
    else:
        maker_summary = "*(no commit-message comment recorded yet)*"

    # Extract the most recent validation comment for the current review round only.
    # Using the current round prevents stale round-N results from appearing in
    # a round-(N+1) review if the maker forgot to record fresh validation.
    current_round = task.get("review_round", 0)
    validation_entries = [
        c for c in comments
        if c.get("kind") == "validation" and c.get("review_round") == current_round
    ]
    if validation_entries:
        latest_validation = validation_entries[-1]["message"]
        validation_summary = (
            f"**Maker's validation summary** (most recent):\n```\n{latest_validation}\n```"
        )
    elif skip_build_policy:
        validation_summary = (
            "*(no full-build validation recorded — this repo has `SKIP_BUILD_UNTIL_APPROVED: true` "
            "in `AGENTS.md`, so the full build is intentionally deferred to Path B "
            "(post-approval finalization). The missing full-build result (e.g. test suite output) "
            "is expected here. The maker is still required to have recorded a deferred-validation "
            "comment explicitly stating that the full build was intentionally skipped — if that "
            "comment is present, the absence of a full-build result is correct and not a problem.)*"
        )
    else:
        validation_summary = "*(no validation comment recorded — maker may not have run the build)*"

    policy_note = (
        "\n> **Repo policy:** `SKIP_BUILD_UNTIL_APPROVED: true` — full-build validation is "
        "deferred to post-approval finalization (Path B). You are reviewing the diff without "
        "a full-build result. If the maker recorded a deferred-validation comment, that is "
        "expected and correct per repo policy.\n"
        if skip_build_policy else ""
    )

    return f"""## Reviewer Handoff

- **Repo root:** `{repo_root}`
- **Task CLI:** `task <subcommand>` (shorthand defined in shared context above; expands to `"$ORCHESTRA_DIR/bin/ko-task"`)
{policy_note}
{maker_summary}

{validation_summary}

**Your primary job** is to inspect `git diff --cached` and record an approval or rejection.
Do **not** rerun the maker's validation by default. Only run additional commands if the
diff or reported results give a specific reason to verify something — and prefer targeted
checks (e.g. `grep`, reading a single file) over full test reruns.
"""


def _latest_pr_metadata_comment(comments):
    """Return the most recent comment that appears to record GitHub PR metadata."""
    for comment in reversed(comments):
        message = comment.get("message") or ""
        if GITHUB_PR_URL_RE.search(message):
            return comment
    return None


def _build_pull_request_reviewer_handoff(task, comments):
    """Build the handoff section for pull-request-review prompts."""
    repo_root = _repo_root()
    latest = _latest_pr_metadata_comment(comments)
    if latest:
        metadata = f"**Maker's recorded PR metadata** (most recent):\n```\n{latest['message']}\n```"
    else:
        metadata = "*(no GitHub PR metadata comment with a PR URL recorded yet)*"

    return f"""## Pull Request Reviewer Handoff

- **Repo root:** `{repo_root}`
- **Task CLI:** `task <subcommand>` (shorthand defined in shared context above; expands to `"$ORCHESTRA_DIR/bin/ko-task"`)

{metadata}

**Your primary job** is to review the PR title and body quality, including whether
the branch summary accurately reflects the branch against `master`.
Do not perform implementation code review for this task kind.
"""


def _filter_comments_for_prompt(comments, verb):
    """
    Return a filtered, capped list of comments for the ## Prior Comments block.

    Filtering rules:
    - For reviewer verbs (commit-review, commit-review-supertask): exclude
      commit-message and validation kinds — both are already surfaced in the
      ## Reviewer Handoff section, so including them again would be redundant.
    - For all verbs: cap at MAX_PRIOR_COMMENTS, keeping the most recent entries.

    Returns (filtered_comments, total_before_cap) so callers can render a
    truncation note when comments were dropped.
    """
    is_reviewer = verb in ("commit-review", "commit-review-supertask", "commit-plan-review")
    if is_reviewer:
        filtered = [c for c in comments if c.get("kind") not in ("commit-message", "validation")]
    else:
        filtered = list(comments)

    # Always exclude plan-approval and plan-rejection from commit-review prompts
    # (they belong only to the planning phase and would be confusing noise in code review context)
    if verb in ("commit-review", "commit-review-supertask"):
        filtered = [c for c in filtered if c.get("kind") not in ("plan-approval", "plan-rejection")]

    total = len(filtered)
    if total > MAX_PRIOR_COMMENTS:
        filtered = filtered[-MAX_PRIOR_COMMENTS:]
    return filtered, total


def build_prompt(task, verb, agent_name, comments):
    """Assemble the full prompt from shared context + verb-specific prompt."""
    shared_path = _prompts_dir() / "shared-task-context.md"
    verb_path = _prompts_dir() / f"{verb}.md"

    shared_text = shared_path.read_text() if shared_path.exists() else ""
    verb_text = verb_path.read_text() if verb_path.exists() else ""

    # Conditionally prepend Path C (stash recovery) for commit-make
    if verb == "commit-make" and task.get("stash_ref"):
        path_c_path = _prompts_dir() / "commit-make-stash-recovery.md"
        if path_c_path.exists():
            verb_text = path_c_path.read_text() + "\n\n" + verb_text

    # Filter and cap comments for injection
    filtered_comments, total_comments = _filter_comments_for_prompt(comments, verb)
    comments_text = ""
    if filtered_comments:
        lines = []
        if total_comments > MAX_PRIOR_COMMENTS:
            lines.append(f"*(showing {MAX_PRIOR_COMMENTS} most recent of {total_comments} comments)*")
        for c in filtered_comments:
            lines.append(f"- [{c['kind']}] (round {c['review_round']}, {c['author'] or 'unknown'}): {c['message']}")
        comments_text = "\n".join(lines)

    # Determine role and visibility
    is_coder = verb in ("commit-make", "commit-make-supertask", "commit-plan", "pull-request-make")
    is_reviewer = verb in ("commit-review", "commit-review-supertask", "pull-request-review")
    is_plan_reviewer = verb == "commit-plan-review"
    is_supertask_verb = verb in ("commit-make-supertask", "commit-review-supertask")
    is_pull_request_verb = verb in ("pull-request-make", "pull-request-review")
    role = "coder" if is_coder else "reviewer"

    description = task["description"] or "(none)"

    # Build filtered task context. Task descriptions are authored as Markdown,
    # so keep the source block intact instead of flattening it into a list item.
    context_lines = [
        "## Task Context",
        f"- id: {task['id']}",
        f"- title: {task['title']}",
        "- description_markdown:",
        "  ```markdown",
        *[f"  {line}" for line in description.splitlines()],
        "  ```",
        f"- branch: {task['branch']}",
        f"- coder_agent: {task.get('coder_agent') or DEFAULT_CODER}",
        f"- reviewer_agent: {_task_reviewer(task)}",
        f"- review_round: {task['review_round']}",
    ]

    if not is_plan_reviewer:
        context_lines.append(f"- last_review_decision: {task['last_review_decision']}")

    if not is_plan_reviewer and not is_supertask_verb and not is_pull_request_verb:
        context_lines.append(f"- commit_hash: {task.get('commit_hash') or '(none)'}")

    if is_coder and not is_supertask_verb and not is_pull_request_verb:
        context_lines.append(f"- stash_ref: {task.get('stash_ref') or '(none)'}")

    # Show commit_plan only when relevant
    show_plan = False
    if is_plan_reviewer:
        show_plan = True
    elif verb == "commit-plan":
        show_plan = True
    elif verb == "commit-make" and task.get("last_review_decision") != "approve":
        show_plan = True

    if show_plan:
        context_lines.append(f"- commit_plan: {task.get('commit_plan') or '(none)'}")

    # Surface repo-level build policy so agents see it in task context
    try:
        skip_build_policy = repo_policy.read_skip_build_until_approved(_repo_root())
    except Exception:
        skip_build_policy = False
    if skip_build_policy:
        context_lines.append(
            "- skip_build_until_approved: yes "
            "(SKIP_BUILD_UNTIL_APPROVED marker detected in repo AGENTS.md — "
            "see Path A / Path B guidance below)"
        )

    try:
        allow_master_policy = repo_policy.read_allow_tasks_on_master(_repo_root())
    except Exception:
        allow_master_policy = False
    if task.get("branch") in MASTER_BRANCHES and allow_master_policy:
        context_lines.append(
            "- allow_tasks_on_master: yes "
            "(ALLOW_TASKS_ON_MASTER marker detected in repo AGENTS.md — "
            "this repo explicitly opts in to Kanban tasks on master/main)"
        )

    context_lines.extend([
        f"- assigned_agent: {agent_name}",
        f"- role: {role}",
        "",
        "## Prior Comments",
        comments_text or "(none)",
        "",
        "## CLI Commands Available"
    ])

    _t = 'task'
    if verb == "pull-request-make":
        context_lines.extend([
            f"- {_t} show {task['id']}",
            f"- {_t} show-comments {task['id']}",
            f"- {_t} log {task['id']} \"<message>\"",
            f"- cat <<'EOF' | {_t} comment {task['id']} --message-stdin --comment",
            f"- {_t} list [--status <status>] [--next-step <step>] [--branch <branch>]",
        ])
    elif is_coder:
        context_lines.extend([
            f"- {_t} show {task['id']}",
            f"- {_t} show-comments {task['id']}",
            f"- {_t} log {task['id']} \"<message>\"",
            f"- {_t} set {task['id']} --stash-ref <stash-ref>",
            f"- {_t} set {task['id']} --commit-plan \"<plan text>\"",
            f"- cat <<'EOF' | {_t} comment {task['id']} --message-stdin --comment",
            f"- cat <<'EOF' | {_t} comment {task['id']} --message-stdin --commit-message",
            f"- cat <<'EOF' | {_t} comment {task['id']} --message-stdin --done-without-commit",
            f"- {_t} get-commit-footer {task['id']}",
            f"- {_t} list [--status <status>] [--next-step <step>] [--branch <branch>]",
        ])
    elif is_reviewer:
        context_lines.extend([
            f"- {_t} show {task['id']}",
            f"- {_t} show-comments {task['id']}",
            f"- cat <<'EOF' | {_t} comment {task['id']} --message-stdin --approval --author {agent_name} --review-round {task['review_round']}",
            f"- cat <<'EOF' | {_t} comment {task['id']} --message-stdin --rejection --author {agent_name} --review-round {task['review_round']}",
        ])
    elif is_plan_reviewer:
        context_lines.extend([
            f"- {_t} show {task['id']}",
            f"- {_t} show-comments {task['id']}",
            f"- cat <<'EOF' | {_t} comment {task['id']} --message-stdin --plan-approval --author {agent_name}",
            f"- cat <<'EOF' | {_t} comment {task['id']} --message-stdin --plan-rejection --author {agent_name}",
        ])

    task_context = "\n".join(context_lines)

    # For reviewers, inject an explicit handoff section between task context and verb prompt
    if verb in ("pull-request-review",):
        reviewer_handoff = _build_pull_request_reviewer_handoff(task, comments)
        return f"{shared_text}\n\n{task_context}\n\n{reviewer_handoff}\n\n{verb_text}"

    if verb in ("commit-review", "commit-review-supertask"):
        reviewer_handoff = _build_reviewer_handoff(task, comments, skip_build_policy=skip_build_policy)
        return f"{shared_text}\n\n{task_context}\n\n{reviewer_handoff}\n\n{verb_text}"

    return f"{shared_text}\n\n{task_context}\n\n{verb_text}"


# ── Agent ping / ACK gate ─────────────────────────────────────────────

# Process-local ACK cache: (task_id, agent_name) entries mean the agent has
# already responded to a ping for this task in the current process lifetime.
# Not persisted — after an orchestrator restart all tasks may ping again.
_agent_ack_cache: set = set()

PING_PROMPT = "This is a ping. Respond with ACK."
PING_RETRY_INTERVAL = 60  # seconds between retries when agent does not respond


def ping_agent(agent_name, task_id):
    """Send a lightweight ping prompt. Returns True if any text response received."""
    if agent_name not in AGENT_CMD:
        log(f"Unknown agent '{agent_name}', cannot ping", task_id)
        return False

    cmd_template = AGENT_CMD[agent_name]
    cmd = [part.replace("{prompt}", PING_PROMPT) for part in cmd_template]

    log(f"Pre-flight ping: checking {agent_name} is responsive (task {task_id})", task_id)
    active_record_id = None
    try:
        proc_cwd = _repo_root_for_subprocess()
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0, start_new_session=True, cwd=proc_cwd,
        )
        active_record_id = active_agent_processes.register_active_agent(
            task_id=task_id,
            verb="ping",
            agent_name=agent_name,
            pid=proc.pid,
            db_path=db.get_db_path(),
        )
    except FileNotFoundError:
        log(f"Agent binary not found for '{agent_name}' during ping", task_id)
        return False

    # Read bytes with select so we ACK on the first available characters,
    # not on a newline boundary (line-iteration stalls without a newline).
    acked = False
    if proc.stdout:
        fd = proc.stdout.fileno()
        deadline = time.monotonic() + PING_RETRY_INTERVAL
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            readable, _, _ = select.select([proc.stdout], [], [], min(remaining, 5.0))
            if not readable:
                continue
            chunk = os.read(fd, 4096)
            if not chunk:  # EOF with no output
                break
            if chunk.strip():
                acked = True
                break
    # Terminate the ping subprocess as soon as we have a verdict.
    try:
        proc.kill()
    except OSError:
        pass
    proc.wait()
    if active_record_id:
        active_agent_processes.clear_active_agent(active_record_id, db_path=db.get_db_path())

    if acked:
        log(f"Pre-flight ping: {agent_name} responded — proceeding with task {task_id}", task_id)
    else:
        log(
            f"Pre-flight ping: {agent_name} produced no output — "
            f"agent may be out of tokens or unavailable (task {task_id})",
            task_id,
        )
    return acked


def ensure_agent_acked(agent_name, task_id, conn):
    """
    Ensure the agent has ACKed for this task before running a real step.

    Cached process-locally per (task_id, agent_name). On cache hit, returns
    immediately. On miss, pings and retries every minute until ACK is received.
    The task stays pinned and running throughout the retry loop. Do not call
    for steps that will be skipped.
    """
    cache_key = (task_id, agent_name)
    if cache_key in _agent_ack_cache:
        return

    while True:
        if ping_agent(agent_name, task_id):
            _agent_ack_cache.add(cache_key)
            return

        log(
            f"STALLED — {agent_name} did not respond to pre-flight ping for task {task_id}. "
            f"Orchestrator is waiting; no other tasks will run until the agent responds. "
            f"Retrying in {PING_RETRY_INTERVAL}s.",
            task_id,
        )
        db.update_runtime(
            conn,
            status_message=(
                f"STALLED: waiting for {agent_name} to acknowledge ping for task {task_id}. "
                f"Agent may be out of tokens or unavailable. Retrying every minute."
            ),
        )
        time.sleep(PING_RETRY_INTERVAL)


# ── Agent execution ────────────────────────────────────────────────────

def _summarize_transcript_tail(lines, max_chars=240):
    """Return a short single-line summary from the transcript tail."""
    tail = [line.strip() for line in lines if line and line.strip()]
    if not tail:
        return ""
    summary = " | ".join(tail)
    if len(summary) > max_chars:
        return summary[: max_chars - 3] + "..."
    return summary


def run_agent(agent_name, prompt, task_id, conn, verb, cancel_event=None, proc_registry=None):
    """
    Launch an agent subprocess, capture full output to a transcript, return exit code.

    cancel_event: threading.Event — if set, abort reading and kill the subprocess.
    proc_registry: dict — if provided, register the Popen object under agent_name
                   so the caller can kill it on interrupt.
    """
    if agent_name not in AGENT_CMD:
        log(f"Unknown agent '{agent_name}', skipping", task_id)
        return 1

    cmd_template = AGENT_CMD[agent_name]
    cmd = [part.replace("{prompt}", prompt) for part in cmd_template]
    transcript_path = None
    proc_cwd = _repo_root_for_subprocess()

    log(f"Launching {agent_name} for {verb}", task_id)

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True, cwd=proc_cwd,
        )
    except FileNotFoundError:
        log(f"Agent binary not found for '{agent_name}'", task_id)
        db.add_run_log(conn, task_id, f"Agent binary not found: {agent_name}", verb=verb, author="orchestrator")
        return 127

    try:
        transcript_path = db.new_agent_transcript_path(
            task_id,
            verb,
            agent_name,
            db_path=db.get_db_path(),
        )
    except RuntimeError:
        transcript_path = None

    launch_message = f"Launching {agent_name} for {verb}"
    if transcript_path is not None:
        launch_message += f". Transcript: {transcript_path}"
    db.add_run_log(conn, task_id, launch_message, verb=verb, author="orchestrator")

    active_record_id = active_agent_processes.register_active_agent(
        task_id=task_id,
        verb=verb,
        agent_name=agent_name,
        pid=proc.pid,
        db_path=db.get_db_path(),
    )

    if proc_registry is not None:
        proc_registry[agent_name] = proc

    transcript_tail = deque(maxlen=3)
    transcript_line_count = 0

    if transcript_path is not None:
        with transcript_path.open("w", encoding="utf-8") as transcript:
            transcript.write(f"# agent: {agent_name}\n")
            transcript.write(f"# verb: {verb}\n")
            transcript.write(f"# command: {shlex.join(cmd)}\n")
            if proc_cwd:
                transcript.write(f"# cwd: {proc_cwd}\n")
            transcript.write("\n")

            for raw_line in proc.stdout or []:
                if cancel_event and cancel_event.is_set():
                    break
                transcript.write(raw_line)
                if raw_line and not raw_line.endswith("\n"):
                    transcript.write("\n")
                line = raw_line.rstrip("\n")
                if line:
                    transcript_line_count += 1
                    transcript_tail.append(line)
    else:
        for raw_line in proc.stdout or []:
            if cancel_event and cancel_event.is_set():
                break
            line = raw_line.rstrip("\n")
            if line:
                transcript_line_count += 1
                transcript_tail.append(line)

    # If cancelled, kill the subprocess process group
    if cancel_event and cancel_event.is_set():
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
        if active_record_id:
            active_agent_processes.clear_active_agent(active_record_id, db_path=db.get_db_path())
        cancel_message = f"{agent_name} cancelled during {verb}"
        if transcript_path is not None:
            cancel_message += f". Partial transcript: {transcript_path}"
        db.add_run_log(conn, task_id, cancel_message, verb=verb, author="orchestrator")
        return -1

    proc.wait()
    if active_record_id:
        active_agent_processes.clear_active_agent(active_record_id, db_path=db.get_db_path())
    log(f"{agent_name} exited with code {proc.returncode}", task_id)
    tail_summary = _summarize_transcript_tail(transcript_tail)
    if proc.returncode == 0:
        completion_message = (
            f"{agent_name} completed {verb} with exit code 0"
            f" after {transcript_line_count} transcript line"
            f"{'' if transcript_line_count == 1 else 's'}"
        )
        if transcript_path is not None:
            completion_message += f". Transcript: {transcript_path}"
    else:
        completion_message = f"{agent_name} failed {verb} with exit code {proc.returncode}"
        if tail_summary:
            completion_message += f". Tail: {tail_summary}"
        if transcript_path is not None:
            completion_message += f". Full transcript: {transcript_path}"
    db.add_run_log(conn, task_id, completion_message, verb=verb, author="orchestrator")
    return proc.returncode


def get_head_commit_hash():
    """Return the hash of HEAD on the current branch, or None if git fails."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None


def is_worktree_dirty():
    """Return True if the worktree has any uncommitted changes (staged, unstaged, or untracked)."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        )
        return bool(result.stdout.strip())
    except subprocess.CalledProcessError:
        return False


def stash_task_wip(task_id, conn):
    """
    Stage all changes and stash them as orchestrator-preserved task WIP.
    Records the stash ref in the DB. Returns the stash ref string, or None on failure.
    """
    try:
        subprocess.run(["git", "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "stash", "push", "--include-untracked"],
            check=True, capture_output=True,
        )
        result = subprocess.run(
            ["git", "stash", "list"],
            capture_output=True, text=True, check=True,
        )
        first_line = result.stdout.split("\n")[0].strip()
        stash_ref = first_line.split(":")[0].strip() if first_line else ""
        if stash_ref:
            db.update_task(conn, task_id, stash_ref=stash_ref)
            return stash_ref
        return None
    except subprocess.CalledProcessError:
        return None


def mark_blocked(task_id, conn, comment, runtime_status, log_message=None, preserve_wip=False):
    """
    Block a task with a consistent state transition.

    When preserve_wip is True, any dirty worktree is treated as task-created WIP:
    stage it, stash it, record stash_ref, and leave a durable note.
    """
    if preserve_wip and is_worktree_dirty():
        stash_ref = stash_task_wip(task_id, conn)
        if stash_ref:
            db.add_comment(
                conn, task_id,
                f"blocked with task WIP: orchestrator stashed changes as {stash_ref}.",
                kind="comment", author="orchestrator",
            )
        else:
            db.add_comment(
                conn, task_id,
                "blocked with task WIP but stash failed; manual recovery needed.",
                kind="comment", author="orchestrator",
            )

    db.update_task(conn, task_id, status="blocked", next_step="none")
    db.add_comment(conn, task_id, comment, kind="comment", author="orchestrator")
    db.update_runtime(conn, status_message=runtime_status)
    if log_message:
        log(log_message, task_id)


def ensure_branch(task, conn):
    """Switch to the task's branch, creating if needed. Returns True on success."""
    branch = task.get("branch")
    if not isinstance(branch, str) or not branch.strip():
        db.add_comment(
            conn,
            task["id"],
            "Branch error: task has no branch set. Assign a branch before re-queueing.",
            kind="comment",
            author="orchestrator",
        )
        return False
    branch = branch.strip()
    if not re.match(r'^[A-Za-z0-9._/][A-Za-z0-9._/ -]*$', branch):
        db.add_comment(
            conn, task["id"],
            f"Branch error: invalid branch name '{branch}'.",
            kind="comment", author="orchestrator",
        )
        return False
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            subprocess.run(["git", "checkout", branch], capture_output=True, text=True, check=True)
            return True

        current = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        if current in ("master", "main"):
            subprocess.run(
                ["git", "checkout", "-b", branch],
                capture_output=True, text=True, check=True,
            )
            return True
        else:
            db.add_comment(
                conn, task["id"],
                f"Cannot create branch '{branch}': currently on '{current}', base is ambiguous.",
                kind="comment", author="orchestrator",
            )
            return False

    except subprocess.CalledProcessError as e:
        db.add_comment(
            conn, task["id"],
            f"Branch error: {e.stderr or e.stdout or str(e)}",
            kind="comment", author="orchestrator",
        )
        return False


# ── State machine transitions ─────────────────────────────────────────

def handle_commit_make(task, conn):
    """Execute a commit-make step. Returns (success, done_without_commit, needs_rereview).

    needs_rereview is True when a SKIP_BUILD_UNTIL_APPROVED repo's Path B deferred build
    changed the staged diff. The orchestrator re-enters commit-review in that case instead
    of finalizing.
    """
    agent = task.get("coder_agent") or DEFAULT_CODER
    db.update_task(conn, task["id"], coder_agent=agent)
    ensure_agent_acked(agent, task["id"], conn)

    # Read repo policy once; drives Path A validation enforcement and Path B gating.
    try:
        skip_build_policy = repo_policy.read_skip_build_until_approved(_repo_root())
    except Exception:
        skip_build_policy = False

    comments = db.get_comments(conn, task["id"])
    prompt = build_prompt(task, "commit-make", agent, comments)

    db.update_runtime(
        conn,
        current_step="commit-make",
        active_agents=1,
        status_message=f"{agent} building task '{task['title']}'",
    )

    db.add_comment(conn, task["id"],
                   f"Starting commit-make with coder '{agent}'.",
                   kind="comment", author="orchestrator")

    # On Path A (initial build/rework), the coder must write a fresh commit-message
    # comment during the current run. A clean exit without a new one means the agent
    # quit silently (quota exceeded, banner-only crash, reused an older comment, etc.).
    # Skip this check on Path B (finalization after approval) because that pass reuses
    # the latest existing commit-message comment.
    is_finalization = task["last_review_decision"] == "approve"
    if not is_finalization:
        comments_before = db.get_comments(conn, task["id"])
        commit_msg_count_before = sum(1 for c in comments_before if c["kind"] == "commit-message")
        # When the repo opts into deferred build validation, the coder must also write a
        # fresh validation comment explicitly stating deferral — so reviewers know the
        # absence of a full-build result is intentional, not an oversight.
        validation_count_before = sum(1 for c in comments_before if c["kind"] == "validation") if skip_build_policy else None
    else:
        head_before = get_head_commit_hash()
        # Capture the highest comment ID before the agent runs so we can scope the
        # DONE_WITHOUT_COMMIT check to signals written during this Path B run only.
        comments_before = db.get_comments(conn, task["id"])
        max_comment_id_before = max((c["id"] for c in comments_before), default=0)

    exit_code = run_agent(agent, prompt, task["id"], conn, "commit-make")
    conn.commit()

    if exit_code != 0:
        log(f"commit-make failed with exit code {exit_code}", task["id"])
        return False, False, False

    if not is_finalization:
        comments_after = db.get_comments(conn, task["id"])
        commit_msg_count_after = sum(1 for c in comments_after if c["kind"] == "commit-message")
        if commit_msg_count_after <= commit_msg_count_before:
            log(
                f"commit-make: {agent} exited 0 but wrote no new commit-message comment for this run; "
                "treating as failure",
                task["id"],
            )
            db.add_run_log(conn, task["id"],
                           f"{agent} exited 0 without writing a new commit-message comment for this run",
                           verb="commit-make", author="orchestrator")
            return False, False, False
        # Enforce deferred validation comment when the repo opts in.
        if skip_build_policy:
            validation_count_after = sum(1 for c in comments_after if c["kind"] == "validation")
            if validation_count_after <= validation_count_before:
                log(
                    f"commit-make Path A: {agent} exited 0 with SKIP_BUILD_UNTIL_APPROVED active "
                    "but wrote no fresh validation comment stating deferral; treating as failure",
                    task["id"],
                )
                db.add_run_log(
                    conn, task["id"],
                    f"{agent} exited 0 on Path A with SKIP_BUILD_UNTIL_APPROVED active "
                    "but wrote no fresh validation comment",
                    verb="commit-make", author="orchestrator",
                )
                return False, False, False
    else:
        # Path B: require either a new commit, an explicit DONE_WITHOUT_COMMIT, or a
        # deferred-build-changed signal (all scoped to this run via id > max_comment_id_before).
        # deferred-build-changed is only meaningful when the repo has opted into the policy.
        head_after = get_head_commit_hash()
        new_commit_created = head_after and head_after != head_before
        comments_after = db.get_comments(conn, task["id"])
        done_without_commit = any(
            c["kind"] == "done-without-commit" and c["id"] > max_comment_id_before
            for c in comments_after
        )
        deferred_build_changed = skip_build_policy and any(
            c["kind"] == "deferred-build-changed" and c["id"] > max_comment_id_before
            for c in comments_after
        )
        if not new_commit_created and not done_without_commit and not deferred_build_changed:
            # Resilience: the agent may have narrated the --deferred-build-changed CLI
            # call in text rather than executing it.  If the deferred-build policy is
            # active and the worktree is dirty, that is strong evidence of exactly this
            # mistake.  Emit the signal on the agent's behalf and re-enter review rather
            # than stashing and blocking.
            if skip_build_policy and is_worktree_dirty():
                subprocess.run(["git", "add", "."], check=True, capture_output=True)
                db.add_comment(
                    conn, task["id"],
                    "Orchestrator recovered from agent narration error: agent exited 0 "
                    "with a dirty worktree but did not execute --deferred-build-changed. "
                    "Changes staged; re-entering review.",
                    kind="deferred-build-changed",
                    author="orchestrator",
                )
                log(
                    f"commit-make Path B: {agent} narrated --deferred-build-changed instead "
                    "of executing it; orchestrator staged changes and emitted signal",
                    task["id"],
                )
                deferred_build_changed = True
            else:
                log(
                    f"commit-make Path B: {agent} exited 0 but created no new commit and "
                    "did not signal DONE_WITHOUT_COMMIT or DEFERRED_BUILD_CHANGED; treating as failure",
                    task["id"],
                )
                db.add_run_log(
                    conn, task["id"],
                    f"{agent} exited 0 on Path B without a new commit, DONE_WITHOUT_COMMIT, "
                    "or DEFERRED_BUILD_CHANGED",
                    verb="commit-make", author="orchestrator",
                )
                return False, False, False
        # A real commit wins over any signal: finalize normally.
        if new_commit_created:
            return True, False, False
        # Deferred build changed the diff: re-enter review.
        if deferred_build_changed:
            return True, False, True
        # Done without commit.
        return True, True, False

    return True, False, False


def handle_commit_review(task, conn):
    """
    Execute commit-review with the task's configured reviewer agent.
    Returns ('approve'|'reject'|'error').
    """
    reviewer = _task_reviewer(task)
    ensure_agent_acked(reviewer, task["id"], conn)
    comments = db.get_comments(conn, task["id"])

    db.update_runtime(
        conn,
        current_step="commit-review",
        active_agents=1,
        review_round=task["review_round"],
        status_message=f"Round {task['review_round']}: {reviewer} reviewing",
    )

    db.add_comment(conn, task["id"],
                   f"Starting commit-review round {task['review_round']} with reviewer: {reviewer}.",
                   kind="comment", author="orchestrator")

    prompt = build_prompt(task, "commit-review", reviewer, comments)
    exit_code = run_agent(reviewer, prompt, task["id"], conn, "commit-review")

    if exit_code != 0:
        db.add_comment(conn, task["id"],
                       f"Reviewer '{reviewer}' could not complete review round {task['review_round']} "
                       f"due to an operational error (exit code {exit_code}).",
                       kind="comment", author="orchestrator")
        return "error"

    # Look for any approval or rejection comment for this round.
    # The CLI persists the reviewer identity on the comment itself, so the
    # orchestrator should honor the recorded round decision rather than
    # assuming a single hardcoded reviewer name.
    round_comments = db.get_comments(conn, task["id"])
    current_round = task["review_round"]
    reviewer_decisions = [
        c for c in round_comments
        if c["review_round"] == current_round
        and c["kind"] in ("approval", "rejection")
    ]

    if not reviewer_decisions:
        log(
            f"Reviewer '{reviewer}' exited 0 but left no decision for round {current_round}",
            task["id"],
        )
        return "reject"

    decision = reviewer_decisions[-1]["kind"]
    if decision == "approval":
        db.add_comment(conn, task["id"],
                       f"Review round {task['review_round']} approved by {reviewer}.",
                       kind="comment", author="orchestrator")
        return "approve"
    return "reject"


def handle_pull_request_make(task, conn):
    """Execute pull-request-make. Returns True when fresh PR metadata was recorded."""
    agent = task.get("coder_agent") or DEFAULT_CODER
    db.update_task(conn, task["id"], coder_agent=agent)
    ensure_agent_acked(agent, task["id"], conn)
    comments = db.get_comments(conn, task["id"])
    max_comment_id_before = max((c["id"] for c in comments), default=0)
    prompt = build_prompt(task, "pull-request-make", agent, comments)

    db.update_runtime(
        conn,
        current_step="pull-request-make",
        active_agents=1,
        status_message=f"{agent} preparing PR metadata for '{task['title']}'",
    )

    db.add_comment(
        conn,
        task["id"],
        f"Starting pull-request-make with maker '{agent}'.",
        kind="comment",
        author="orchestrator",
    )

    exit_code = run_agent(agent, prompt, task["id"], conn, "pull-request-make")
    conn.commit()

    if exit_code != 0:
        log(f"pull-request-make failed with exit code {exit_code}", task["id"])
        return False

    comments_after = db.get_comments(conn, task["id"])
    new_metadata_comments = [
        c for c in comments_after
        if c["id"] > max_comment_id_before and GITHUB_PR_URL_RE.search(c.get("message") or "")
    ]
    if not new_metadata_comments:
        log(
            f"pull-request-make: {agent} exited 0 but wrote no fresh PR metadata comment with a GitHub PR URL; treating as failure",
            task["id"],
        )
        db.add_run_log(
            conn,
            task["id"],
            f"{agent} exited 0 without writing a fresh PR metadata comment containing a GitHub PR URL",
            verb="pull-request-make",
            author="orchestrator",
        )
        return False

    return True


def handle_pull_request_review(task, conn):
    """
    Execute pull-request-review with the task's configured reviewer agent.
    Returns ('approve'|'reject'|'error').
    """
    reviewer = _task_reviewer(task)
    ensure_agent_acked(reviewer, task["id"], conn)
    comments = db.get_comments(conn, task["id"])

    db.update_runtime(
        conn,
        current_step="pull-request-review",
        active_agents=1,
        review_round=task["review_round"],
        status_message=f"Round {task['review_round']}: {reviewer} reviewing PR metadata",
    )

    db.add_comment(
        conn,
        task["id"],
        f"Starting pull-request-review round {task['review_round']} with reviewer: {reviewer}.",
        kind="comment",
        author="orchestrator",
    )

    prompt = build_prompt(task, "pull-request-review", reviewer, comments)
    exit_code = run_agent(reviewer, prompt, task["id"], conn, "pull-request-review")

    if exit_code != 0:
        db.add_comment(
            conn,
            task["id"],
            f"Reviewer '{reviewer}' could not complete pull-request-review round {task['review_round']} "
            f"due to an operational error (exit code {exit_code}).",
            kind="comment",
            author="orchestrator",
        )
        return "error"

    round_comments = db.get_comments(conn, task["id"])
    current_round = task["review_round"]
    reviewer_decisions = [
        c for c in round_comments
        if c["review_round"] == current_round
        and c["kind"] in ("approval", "rejection")
    ]

    if not reviewer_decisions:
        log(
            f"Reviewer '{reviewer}' exited 0 but left no PR review decision for round {current_round}",
            task["id"],
        )
        return "reject"

    decision = reviewer_decisions[-1]["kind"]
    if decision == "approval":
        db.add_comment(
            conn,
            task["id"],
            f"Pull request review round {task['review_round']} approved by {reviewer}.",
            kind="comment",
            author="orchestrator",
        )
        return "approve"
    return "reject"


def handle_commit_make_supertask(task, conn):
    """Execute a commit-make-supertask (planning) step. Returns True on success."""
    agent = task.get("coder_agent") or DEFAULT_SUPER_PLANNER
    db.update_task(conn, task["id"], coder_agent=agent)
    ensure_agent_acked(agent, task["id"], conn)
    comments = db.get_comments(conn, task["id"])
    prompt = build_prompt(task, "commit-make-supertask", agent, comments)

    db.update_runtime(
        conn,
        current_step="commit-make-supertask",
        active_agents=1,
        status_message=f"{agent} planning '{task['title']}'",
    )

    db.add_comment(conn, task["id"],
                   f"Starting commit-make-supertask with coder '{agent}'.",
                   kind="comment", author="orchestrator")

    comments_before = db.get_comments(conn, task["id"])
    commit_msg_count_before = sum(1 for c in comments_before if c["kind"] == "commit-message")

    exit_code = run_agent(agent, prompt, task["id"], conn, "commit-make-supertask")
    conn.commit()

    if exit_code != 0:
        log(f"commit-make-supertask failed with exit code {exit_code}", task["id"])
        return False

    comments_after = db.get_comments(conn, task["id"])
    commit_msg_count_after = sum(1 for c in comments_after if c["kind"] == "commit-message")
    if commit_msg_count_after <= commit_msg_count_before:
        log(
            f"commit-make-supertask: {agent} exited 0 but wrote no new commit-message comment; "
            "treating as failure",
            task["id"],
        )
        db.add_run_log(conn, task["id"],
                       f"{agent} exited 0 without writing a new commit-message comment",
                       verb="commit-make-supertask", author="orchestrator")
        return False

    return True


def handle_commit_review_supertask(task, conn):
    """
    Execute commit-review-supertask (plan review) with the single reviewer agent.
    Returns ('approve'|'reject'|'error').
    """
    reviewer = DEFAULT_SUPER_REVIEWER
    ensure_agent_acked(reviewer, task["id"], conn)
    comments = db.get_comments(conn, task["id"])

    db.update_runtime(
        conn,
        current_step="commit-review-supertask",
        active_agents=1,
        review_round=task["review_round"],
        status_message=f"Round {task['review_round']}: {reviewer} reviewing plan",
    )

    db.add_comment(conn, task["id"],
                   f"Starting commit-review-supertask round {task['review_round']} with reviewer: {reviewer}.",
                   kind="comment", author="orchestrator")

    prompt = build_prompt(task, "commit-review-supertask", reviewer, comments)
    exit_code = run_agent(reviewer, prompt, task["id"], conn, "commit-review-supertask")

    if exit_code != 0:
        db.add_comment(conn, task["id"],
                       f"Reviewer '{reviewer}' could not complete supertask review round "
                       f"{task['review_round']} due to an operational error (exit code {exit_code}).",
                       kind="comment", author="orchestrator")
        return "error"

    round_comments = db.get_comments(conn, task["id"])
    current_round = task["review_round"]
    reviewer_decisions = [
        c for c in round_comments
        if c["review_round"] == current_round
        and c["kind"] in ("approval", "rejection")
        and c["author"] == reviewer
    ]

    if not reviewer_decisions:
        log(f"Reviewer '{reviewer}' exited 0 but left no decision for round {current_round}", task["id"])
        return "reject"

    decision = reviewer_decisions[-1]["kind"]
    if decision == "approval":
        db.add_comment(conn, task["id"],
                       f"Supertask review round {task['review_round']} approved by {reviewer}.",
                       kind="comment", author="orchestrator")
        return "approve"
    return "reject"


def commit_make_requires_clean_worktree(task):
    """
    Return True when commit-make should block on pre-existing dirtiness.

    Fresh commit-make pickup must start from a clean repo so unrelated local
    changes do not contaminate the task. Once the same task has already gone
    through review, its own staged work is expected to remain in the worktree
    for rework or finalization.
    """
    return task.get("last_review_decision") == "none"


def handle_commit_plan(task, conn):
    """Execute a commit-plan step. Returns True on success."""
    # Always uses DEFAULT_PLANNER rather than task-level coder_agent. This is intentional to ensure
    # consistent commit planning across all tasks - agent-specific override is not supported.
    agent = DEFAULT_PLANNER
    db.update_task(conn, task["id"], coder_agent=agent)
    ensure_agent_acked(agent, task["id"], conn)
    comments = db.get_comments(conn, task["id"])
    prompt = build_prompt(task, "commit-plan", agent, comments)

    db.update_runtime(
        conn,
        current_step="commit-plan",
        active_agents=1,
        status_message=f"{agent} drafting plan for '{task['title']}'",
    )

    db.add_comment(conn, task["id"],
                   f"Starting commit-plan with coder '{agent}'.",
                   kind="comment", author="orchestrator")

    # Clear any stale commit_plan before running the agent so the post-run
    # check reliably detects whether the agent wrote a *new* plan.
    db.update_task(conn, task["id"], commit_plan=None)

    exit_code = run_agent(agent, prompt, task["id"], conn, "commit-plan")

    if exit_code != 0:
        log(f"commit-plan failed with exit code {exit_code}", task["id"])
        return False

    # Verify the agent persisted a plan on the task record
    refreshed = db.get_task(conn, task["id"])
    if not refreshed or not refreshed.get("commit_plan"):
        log(
            f"commit-plan: {agent} exited 0 but wrote no commit_plan on the task; "
            "treating as failure",
            task["id"],
        )
        db.add_run_log(conn, task["id"],
                       f"{agent} exited 0 without writing commit_plan",
                       verb="commit-plan", author="orchestrator")
        return False

    return True


def handle_commit_plan_review(task, conn):
    """
    Execute commit-plan-review with the single reviewer agent.
    Returns ('approve'|'reject'|'error').

    The decision is determined by any new approval or rejection comment written
    by the reviewer during this run — not filtered by review_round, so review_round
    is not incremented for planning rejections.
    """
    reviewer = DEFAULT_PLAN_REVIEWER
    ensure_agent_acked(reviewer, task["id"], conn)
    comments = db.get_comments(conn, task["id"])

    db.update_runtime(
        conn,
        current_step="commit-plan-review",
        active_agents=1,
        review_round=task["review_round"],
        status_message=f"{reviewer} reviewing plan for '{task['title']}'",
    )

    db.add_comment(
        conn, task["id"],
        f"Starting commit-plan-review with reviewer: {reviewer}.",
        kind="comment", author="orchestrator",
    )
    # Capture the ID of the start comment so we can find decisions written after it
    all_comments_before = db.get_comments(conn, task["id"])
    start_comment_id = all_comments_before[-1]["id"] if all_comments_before else 0

    prompt = build_prompt(task, "commit-plan-review", reviewer, comments)
    exit_code = run_agent(reviewer, prompt, task["id"], conn, "commit-plan-review")

    if exit_code != 0:
        db.add_comment(conn, task["id"],
                       f"Reviewer '{reviewer}' could not complete commit-plan-review "
                       f"due to an operational error (exit code {exit_code}).",
                       kind="comment", author="orchestrator")
        return "error"

    # Find any new plan-approval or plan-rejection comment written during this run.
    # Uses distinct kinds so plan decisions never contaminate the commit-review round.
    all_after = db.get_comments(conn, task["id"])
    reviewer_decisions = [
        c for c in all_after
        if c["id"] > start_comment_id
        and c["kind"] in ("plan-approval", "plan-rejection")
        and c["author"] == reviewer
    ]

    if not reviewer_decisions:
        log(f"Reviewer '{reviewer}' exited 0 but left no plan decision", task["id"])
        return "reject"

    decision = reviewer_decisions[-1]["kind"]
    if decision == "plan-approval":
        db.add_comment(conn, task["id"],
                       f"Plan approved by {reviewer}.",
                       kind="comment", author="orchestrator")
        return "approve"
    return "reject"


def _check_parent_completion(task_id, conn):
    """If task has a parent supertask and all siblings are done, mark parent done."""
    task = db.get_task(conn, task_id)
    if not task:
        return
    parent_id = task.get("parent_task_id")
    if not parent_id:
        return
    parent = db.get_task(conn, parent_id)
    if not parent or parent["status"] != "pending_subtasks":
        return
    children = db.get_child_tasks(conn, parent_id)
    if all(c["status"] == "done" for c in children):
        db.update_task(conn, parent_id, status="done", next_step="none")
        db.add_comment(conn, parent_id,
                       "All child tasks done. Supertask complete.",
                       kind="comment", author="orchestrator")
        log(f"Supertask {parent_id} complete (all children done)", task_id)


def _finalize_commit(task, conn, done_without_commit=False):
    """Path B: record commit hash (if any), set status=done, and log completion."""
    task_id = task["id"]

    if done_without_commit:
        db.update_task(conn, task_id, status="done", next_step="none")
        db.add_comment(conn, task_id,
                       "Task complete: finalized without a new commit (DONE_WITHOUT_COMMIT).",
                       kind="comment", author="orchestrator")
        db.update_runtime(conn, status_message=f"Task {task_id} done (no-commit finalization)")
        log("Task done (no-commit finalization)", task_id)
    else:
        new_hash = get_head_commit_hash()
        if new_hash:
            db.update_task(conn, task_id, commit_hash=new_hash)
            log(f"Recorded commit_hash {new_hash[:8]}", task_id)
        db.update_task(conn, task_id, status="done", next_step="none")
        db.add_comment(conn, task_id,
                       f"Task complete: commit finalized and pushed to branch '{task['branch']}'.",
                       kind="comment", author="orchestrator")
        db.update_runtime(conn, status_message=f"Task {task_id} done (commit finalized)")
        log("Task done (commit finalized)", task_id)
    _check_parent_completion(task_id, conn)


def _finalize_pull_request(task, conn):
    """Mark a pull request task done after metadata review approval or skip."""
    task_id = task["id"]
    db.update_task(conn, task_id, status="done", next_step="none")
    db.add_comment(
        conn,
        task_id,
        "Task complete: pull request metadata approved.",
        kind="comment",
        author="orchestrator",
    )
    db.update_runtime(conn, status_message=f"Pull request task {task_id} done")
    log("Pull request task done", task_id)


def _requeue_for_review_after_deferred_build(task, conn):
    """Re-enter commit-review after a SKIP_BUILD_UNTIL_APPROVED Path B build changed the staged diff.

    The coder staged the build-modified diff and signaled deferred-build-changed.
    Increment review_round and route to commit-review so the reviewer sees the updated diff.
    """
    task_id = task["id"]
    new_round = task["review_round"] + 1
    db.update_task(conn, task_id,
                   status="ready", next_step="commit-review",
                   review_round=new_round, last_review_decision="none")
    db.add_comment(
        conn, task_id,
        f"Deferred full build (SKIP_BUILD_UNTIL_APPROVED) changed the staged diff. "
        f"Re-entering review at round {new_round} so the updated diff is reviewed before landing.",
        kind="comment", author="orchestrator",
    )
    db.update_runtime(
        conn,
        status_message=f"Deferred build changed diff; task {task_id} re-entering review round {new_round}",
    )
    log(f"Deferred build changed staged diff; re-entering review at round {new_round}", task_id)


def _approve_commit(task, conn):
    """Advance task to Path B (finalization) after approval or skip."""
    task_id = task["id"]
    db.update_task(conn, task_id,
                   status="ready", next_step="commit-make",
                   last_review_decision="approve")
    db.update_runtime(conn, status_message="Review approved; returning to commit-make for finalization")
    log("All reviewers approved, queued for finalization", task_id)


def _queue_follow_up_if_needed(task, conn):
    """Queue the follow-up task directly after *task* if one was declared.

    Idempotent: if the follow-up is already ready (from a prior commit-make
    run), the requeue is skipped.  Preserves follow_up_task_id on the current
    task so the one-follow-up limit remains durable across review cycles.

    Returns the follow-up task id if it was queued during this call, else None.
    """
    task_id = task["id"]
    follow_up_id = task.get("follow_up_task_id")
    if not follow_up_id:
        return None
    follow_up = db.get_task(conn, follow_up_id)
    if not follow_up:
        log(f"WARNING: follow_up_task_id {follow_up_id} not found; skipping requeue", task_id)
        return None
    if follow_up["status"] != "none":
        log(f"Follow-up task {follow_up_id} already queued; skipping re-queue", task_id)
        return None
    db.update_task(conn, follow_up_id, status="ready", next_step="commit-make")
    try:
        db.reposition_task(conn, follow_up_id, after_id=task_id)
    except ValueError as e:
        log(f"WARNING: could not reposition follow-up {follow_up_id}: {e}", task_id)
    db.add_comment(conn, task_id,
                   f"Follow-up task {follow_up_id} ('{follow_up['title']}') queued after this task.",
                   kind="comment", author="orchestrator")
    db.add_comment(conn, follow_up_id,
                   f"Queued after task {task_id} by follow-up mechanism.",
                   kind="comment", author="orchestrator")
    log(f"Follow-up task {follow_up_id} queued after current task", task_id)
    return follow_up_id


def _approve_supertask_plan(task, conn):
    """Advance supertask to pending_subtasks after approval or skip."""
    task_id = task["id"]
    db.update_task(conn, task_id,
                   status="pending_subtasks", next_step="none",
                   last_review_decision="approve")
    db.update_runtime(conn, status_message=f"Supertask {task_id} plan approved; children now active")
    log("Supertask plan approved, status=pending_subtasks", task_id)


def _approve_plan(task, conn):
    """Advance task to commit-make after plan approval or skip."""
    task_id = task["id"]
    db.update_task(conn, task_id,
                   status="ready", next_step="commit-make",
                   last_review_decision="none")
    db.update_runtime(conn, status_message=f"Plan approved; task {task_id} queued for commit-make")
    log("Plan approved, queued for commit-make", task_id)


def advance(task, conn):
    """
    Execute the task's next_step and advance the state machine.
    Returns True if the task was processed without unrecoverable error.
    """
    task_id = task["id"]
    step = task["next_step"]
    kind = task.get("kind", "task")

    # Enforce step/kind cross-contamination rules
    _TASK_ONLY_STEPS = {"commit-make", "commit-review", "commit-plan", "commit-plan-review"}
    _SUPERTASK_ONLY_STEPS = {"commit-make-supertask", "commit-review-supertask"}
    _PULL_REQUEST_ONLY_STEPS = {"pull-request-make", "pull-request-review"}
    if kind == "task" and step in _SUPERTASK_ONLY_STEPS:
        mark_blocked(
            task_id,
            conn,
            f"Normal task cannot use supertask step '{step}'. Fix next_step before re-queueing.",
            f"Blocked: task {task_id} has supertask step '{step}'",
            log_message=f"Blocked: normal task has supertask step '{step}'",
            preserve_wip=False,
        )
        return False
    if kind == "task" and step in _PULL_REQUEST_ONLY_STEPS:
        mark_blocked(
            task_id,
            conn,
            f"Normal task cannot use pull request step '{step}'. Fix next_step before re-queueing.",
            f"Blocked: task {task_id} has pull request step '{step}'",
            log_message=f"Blocked: normal task has pull request step '{step}'",
            preserve_wip=False,
        )
        return False
    if kind == "supertask" and step in _TASK_ONLY_STEPS:
        mark_blocked(
            task_id,
            conn,
            f"Supertask cannot use task-only step '{step}'. Fix next_step before re-queueing.",
            f"Blocked: supertask {task_id} has task-only step '{step}'",
            log_message=f"Blocked: supertask has task-only step '{step}'",
            preserve_wip=False,
        )
        return False
    if kind == "supertask" and step in _PULL_REQUEST_ONLY_STEPS:
        mark_blocked(
            task_id,
            conn,
            f"Supertask cannot use pull request step '{step}'. Fix next_step before re-queueing.",
            f"Blocked: supertask {task_id} has pull request step '{step}'",
            log_message=f"Blocked: supertask has pull request step '{step}'",
            preserve_wip=False,
        )
        return False
    if kind == "pull_request" and step not in _PULL_REQUEST_ONLY_STEPS:
        mark_blocked(
            task_id,
            conn,
            f"Pull request task cannot use step '{step}'. Fix next_step before re-queueing.",
            f"Blocked: pull request task {task_id} has invalid step '{step}'",
            log_message=f"Blocked: pull request task has invalid step '{step}'",
            preserve_wip=False,
        )
        return False

    # Supertask planning/review steps and task planning steps skip branch switching.
    is_supertask_step = step in ("commit-make-supertask", "commit-review-supertask")
    is_plan_step = step in ("commit-plan", "commit-plan-review")

    if not is_supertask_step and not is_plan_step:
        # Only the first commit-make pickup requires a clean worktree. Rework
        # after review rejection and finalization after approval both expect
        # the task's own staged changes to still be present.
        if (
            step == "commit-make"
            and commit_make_requires_clean_worktree(task)
            and is_worktree_dirty()
        ):
            mark_blocked(
                task_id,
                conn,
                "commit-make blocked at pickup: worktree was already dirty. "
                "Resolve the dirty state before re-queueing. No stash was created.",
                f"Blocked: dirty worktree at pickup for task {task_id}",
                log_message="Blocked at pickup: worktree dirty before commit-make started",
                preserve_wip=False,
            )
            return False

        # Switch to task branch
        if not ensure_branch(task, conn):
            mark_blocked(
                task_id,
                conn,
                "Branch issue blocked the task; resolve the branch problem and re-queue.",
                f"Blocked: branch issue on task {task_id}",
                log_message="Blocked: branch issue",
                preserve_wip=False,
            )
            return False

    if step == "commit-make-supertask":
        success = handle_commit_make_supertask(task, conn)
        if not success:
            mark_blocked(
                task_id,
                conn,
                "commit-make-supertask failed; marked blocked for human triage.",
                f"Blocked: commit-make-supertask failed for task {task_id}",
                log_message="Blocked: commit-make-supertask failed",
                preserve_wip=False,
            )
            return False

        if db.should_skip_step(conn, task_id, "commit-review-supertask"):
            _approve_supertask_plan(task, conn)
        else:
            db.update_task(conn, task_id,
                           status="ready", next_step="commit-review-supertask",
                           last_review_decision="none")
            db.update_runtime(conn, status_message=f"Supertask {task_id} plan built, queued for review")
            log("Supertask plan built, queued for review", task_id)
        return True

    elif step == "commit-review-supertask":

        outcome = handle_commit_review_supertask(task, conn)

        if outcome == "error":
            mark_blocked(
                task_id,
                conn,
                f"Reviewer '{DEFAULT_SUPER_REVIEWER}' failed supertask review round {task['review_round']}; "
                "task blocked for human triage.",
                f"Blocked: reviewer failed on supertask {task_id}",
                log_message="Blocked: reviewer failed on supertask",
                preserve_wip=False,
            )
            return False

        if outcome == "approve":
            _approve_supertask_plan(task, conn)
        else:
            new_round = task["review_round"] + 1
            if new_round >= MAX_REVIEW_ROUNDS:
                db.update_task(
                    conn, task_id,
                    review_round=new_round, last_review_decision="reject",
                )
                mark_blocked(
                    task_id,
                    conn,
                    f"Blocked: reached max review rounds ({MAX_REVIEW_ROUNDS})",
                    f"Supertask {task_id} blocked after {MAX_REVIEW_ROUNDS} review rounds",
                    log_message=f"Supertask blocked after {MAX_REVIEW_ROUNDS} review rounds",
                    preserve_wip=False,
                )
            else:
                db.update_task(conn, task_id,
                               status="ready", next_step="commit-make-supertask",
                               review_round=new_round, last_review_decision="reject")
                db.add_comment(conn, task_id,
                               f"Supertask review round {task['review_round']} rejected; "
                               f"returning to commit-make-supertask for round {new_round}.",
                               kind="comment", author="orchestrator")
                db.update_runtime(conn, status_message=f"Supertask review rejected; returning to commit-make-supertask round {new_round}")
                log(f"Supertask rejected, advancing to round {new_round}", task_id)
        return True

    elif step == "commit-plan":
        if db.should_skip_step(conn, task_id, "commit-plan"):
            _approve_plan(task, conn)
            return True

        success = handle_commit_plan(task, conn)
        if not success:
            mark_blocked(
                task_id,
                conn,
                "commit-plan failed; marked blocked for human triage.",
                f"Blocked: commit-plan failed for task {task_id}",
                log_message="Blocked: commit-plan failed",
                preserve_wip=False,
            )
            return False

        if db.should_skip_step(conn, task_id, "commit-plan-review"):
            _approve_plan(task, conn)
        else:
            db.update_task(conn, task_id,
                           status="ready", next_step="commit-plan-review")
            db.update_runtime(conn, status_message=f"Task {task_id} plan drafted, queued for plan review")
            log("Plan drafted, queued for plan review", task_id)
        return True

    elif step == "commit-plan-review":
        if db.should_skip_step(conn, task_id, "commit-plan-review"):
            _approve_plan(task, conn)
            return True

        outcome = handle_commit_plan_review(task, conn)

        if outcome == "error":
            mark_blocked(
                task_id,
                conn,
                f"Reviewer '{DEFAULT_PLAN_REVIEWER}' failed commit-plan-review; task blocked for human triage.",
                f"Blocked: plan reviewer failed on task {task_id}",
                log_message="Blocked: plan reviewer failed",
                preserve_wip=False,
            )
            return False

        if outcome == "approve":
            _approve_plan(task, conn)
        else:
            # Plan rejected — return to commit-plan; do NOT increment review_round.
            # Clear commit_plan so the planner cannot reuse the stale rejected plan.
            db.update_task(conn, task_id,
                           status="ready", next_step="commit-plan", commit_plan=None)
            db.add_comment(conn, task_id,
                           "Plan rejected; returning to commit-plan for revision.",
                           kind="comment", author="orchestrator")
            db.update_runtime(conn, status_message=f"Plan rejected; task {task_id} returning to commit-plan")
            log("Plan rejected, returning to commit-plan", task_id)
        return True

    elif step == "pull-request-make":
        success = handle_pull_request_make(task, conn)
        if not success:
            agent = task.get("coder_agent") or DEFAULT_CODER
            mark_blocked(
                task_id,
                conn,
                f"pull-request-make failed for maker '{agent}'; marked blocked for human triage.",
                f"Blocked: pull-request-make failed for task {task_id}",
                log_message="Blocked: pull-request-make failed",
                preserve_wip=False,
            )
            return False

        if db.should_skip_step(conn, task_id, "pull-request-review"):
            _finalize_pull_request(task, conn)
        else:
            db.update_task(
                conn,
                task_id,
                status="ready",
                next_step="pull-request-review",
                last_review_decision="none",
            )
            db.update_runtime(conn, status_message=f"Pull request task {task_id} metadata queued for review")
            log("Pull request metadata queued for review", task_id)
        return True

    elif step == "pull-request-review":
        outcome = handle_pull_request_review(task, conn)

        if outcome == "error":
            reviewer = _task_reviewer(task)
            mark_blocked(
                task_id,
                conn,
                f"Reviewer '{reviewer}' failed pull-request-review round {task['review_round']}; "
                "task blocked for human triage.",
                f"Blocked: PR reviewer failed on task {task_id}",
                log_message="Blocked: PR reviewer failed",
                preserve_wip=False,
            )
            return False

        if outcome == "approve":
            db.update_task(conn, task_id, last_review_decision="approve")
            _finalize_pull_request(task, conn)
        else:
            new_round = task["review_round"] + 1
            if new_round >= MAX_REVIEW_ROUNDS:
                db.update_task(
                    conn, task_id,
                    review_round=new_round, last_review_decision="reject",
                )
                mark_blocked(
                    task_id,
                    conn,
                    f"Blocked: reached max pull request review rounds ({MAX_REVIEW_ROUNDS})",
                    f"Pull request task {task_id} blocked after {MAX_REVIEW_ROUNDS} review rounds",
                    log_message=f"Blocked after {MAX_REVIEW_ROUNDS} pull request review rounds",
                    preserve_wip=False,
                )
            else:
                db.update_task(
                    conn,
                    task_id,
                    status="ready",
                    next_step="pull-request-make",
                    review_round=new_round,
                    last_review_decision="reject",
                )
                db.add_comment(
                    conn,
                    task_id,
                    f"Pull request review round {task['review_round']} rejected; "
                    f"returning to pull-request-make for round {new_round}.",
                    kind="comment",
                    author="orchestrator",
                )
                db.update_runtime(conn, status_message=f"PR metadata rejected; returning to pull-request-make round {new_round}")
                log(f"Pull request metadata rejected, advancing to round {new_round}", task_id)
        return True

    elif step == "commit-make":
        success, done_without_commit, needs_rereview = handle_commit_make(task, conn)
        if not success:
            agent = task.get("coder_agent") or DEFAULT_CODER
            mark_blocked(
                task_id,
                conn,
                f"commit-make failed for coder '{agent}'; marked blocked for human triage.",
                f"Blocked: commit-make failed for task {task_id}",
                log_message="Blocked: commit-make failed",
                preserve_wip=True,
            )
            return False

        # Re-fetch task to pick up agent-set fields (e.g. follow_up_task_id)
        task = db.get_task(conn, task_id)

        if task["last_review_decision"] == "approve":
            if needs_rereview:
                # Deferred build (SKIP_BUILD_UNTIL_APPROVED) changed the diff: re-enter review.
                new_round = task["review_round"] + 1
                if new_round >= MAX_REVIEW_ROUNDS:
                    mark_blocked(
                        task_id,
                        conn,
                        f"Blocked: deferred build changed diff and max review rounds ({MAX_REVIEW_ROUNDS}) reached.",
                        f"Task {task_id} blocked after deferred build diff change at round limit",
                        log_message="Blocked: deferred build diff change at round limit",
                        preserve_wip=True,
                    )
                    return False
                _requeue_for_review_after_deferred_build(task, conn)
            else:
                _finalize_commit(task, conn, done_without_commit=done_without_commit)
        elif db.should_skip_step(conn, task_id, "commit-review"):
            # Advance first so the current task has status=ready, which
            # db.reposition_task requires for the anchor argument.
            _approve_commit(task, conn)
            # Queue the follow-up (if any) directly after the now-ready task.
            _queue_follow_up_if_needed(task, conn)
        else:
            # Path A: code built, move to review
            db.update_task(conn, task_id,
                           status="ready", next_step="commit-review",
                           last_review_decision="none")
            queued_id = _queue_follow_up_if_needed(task, conn)
            if queued_id:
                db.update_runtime(conn, status_message=f"Task {task_id} commit built; follow-up {queued_id} queued after it")
            else:
                db.update_runtime(conn, status_message=f"Task {task_id} commit built, queued for review")
            log("Commit built, queued for review", task_id)
        return True
    elif step == "commit-review":
        outcome = handle_commit_review(task, conn)

        if outcome == "error":
            reviewer = _task_reviewer(task)
            mark_blocked(
                task_id,
                conn,
                f"Reviewer '{reviewer}' failed review round {task['review_round']}; task blocked for human triage.",
                f"Blocked: reviewer failed on task {task_id}",
                log_message="Blocked: reviewer failed",
                preserve_wip=False,
            )
            return False

        if outcome == "approve":
            _approve_commit(task, conn)
        else:
            new_round = task["review_round"] + 1
            if new_round >= MAX_REVIEW_ROUNDS:
                db.update_task(
                    conn, task_id,
                    review_round=new_round, last_review_decision="reject",
                )
                mark_blocked(
                    task_id,
                    conn,
                    f"Blocked: reached max review rounds ({MAX_REVIEW_ROUNDS})",
                    f"Task {task_id} blocked after {MAX_REVIEW_ROUNDS} review rounds",
                    log_message=f"Blocked after {MAX_REVIEW_ROUNDS} review rounds",
                    preserve_wip=True,
                )
            else:
                db.update_task(conn, task_id,
                               status="ready", next_step="commit-make",
                               review_round=new_round, last_review_decision="reject")
                db.add_comment(conn, task_id,
                               f"Review round {task['review_round']} rejected; coder returning to commit-make for round {new_round}.",
                               kind="comment", author="orchestrator")
                db.update_runtime(conn, status_message=f"Review rejected; returning to commit-make round {new_round}")
                log(f"Rejected, advancing to round {new_round}", task_id)
        return True

    else:
        log(f"Unexpected next_step '{step}'", task_id)
        mark_blocked(
            task_id,
            conn,
            f"Unexpected next_step '{step}'",
            f"Blocked: unexpected next_step on task {task_id}",
            preserve_wip=False,
        )
        return False


# ── Main loop ──────────────────────────────────────────────────────────

def init_runtime(conn):
    """Write the initial runtime row on startup, overwriting any stale prior row."""
    db.upsert_runtime(
        conn,
        status="idle",
        pid=os.getpid(),
        started_at="CURRENT_TIMESTAMP",
        last_heartbeat_at="CURRENT_TIMESTAMP",
        current_task_id=None,
        current_step="none",
        current_branch=None,
        review_round=None,
        active_agents=0,
        status_message="Starting up",
    )


def recover_running_tasks(conn):
    """
    On startup, reset any tasks stuck in 'running' from a previous orchestrator
    instance. These are invisible to find_ready_task and would be orphaned forever.

    For commit-review tasks, advance review_round before re-queuing — matching the
    KeyboardInterrupt handler — so stale reviewer votes from the interrupted round are
    not mixed with fresh votes in the new run.
    """
    rows = conn.execute(
        "SELECT * FROM tasks WHERE status = 'running'"
    ).fetchall()
    for row in rows:
        task = db._row_to_task(row)
        if task["next_step"] in ("commit-review", "commit-review-supertask", "pull-request-review"):
            new_round = task["review_round"] + 1
            db.update_task(conn, task["id"], status="ready", review_round=new_round, last_review_decision="none")
            db.add_comment(
                conn, task["id"],
                f"Task was stuck in 'running' state ({task['next_step']}) at orchestrator startup. "
                f"Advanced to review round {new_round} to avoid mixing stale votes.",
                kind="comment", author="orchestrator",
            )
            log(f"Recovered stuck {task['next_step']} task (advanced to round {new_round}): '{task['title']}'", task["id"])
        elif task["next_step"] == "commit-plan-review":
            # For plan review, reset to commit-plan so the plan can be re-reviewed
            # cleanly without mixing stale approval/rejection comments.
            # Also clear commit_plan so a stale plan cannot be re-submitted as-is.
            db.update_task(conn, task["id"], status="ready", next_step="commit-plan",
                           commit_plan=None)
            db.add_comment(
                conn, task["id"],
                "Task was stuck in 'running' state (commit-plan-review) at orchestrator startup. "
                "Reset to commit-plan to allow clean re-review.",
                kind="comment", author="orchestrator",
            )
            log(f"Recovered stuck commit-plan-review task (reset to commit-plan): '{task['title']}'", task["id"])
        elif task["next_step"] == "commit-plan":
            # For interrupted commit-plan runs, clear commit_plan so the planner
            # must write a fresh plan rather than reusing the stale one.
            db.update_task(conn, task["id"], status="ready", commit_plan=None)
            db.add_comment(
                conn, task["id"],
                "Task was stuck in 'running' state (commit-plan) at orchestrator startup. "
                "Reset to ready; commit_plan cleared so the planner writes a fresh plan.",
                kind="comment", author="orchestrator",
            )
            log(f"Recovered stuck commit-plan task (cleared commit_plan): '{task['title']}'", task["id"])
        else:
            db.update_task(conn, task["id"], status="ready")
            db.add_comment(
                conn, task["id"],
                "Task was stuck in 'running' state at orchestrator startup (previous instance likely crashed). Reset to 'ready'.",
                kind="comment", author="orchestrator",
            )
            log(f"Recovered stuck running task: '{task['title']}'", task["id"])


def update_runtime_after_task(conn, task_id, succeeded):
    """Transition runtime state after a task finishes or becomes blocked."""
    if succeeded:
        set_runtime_idle(conn)
        return

    next_ready = db.find_ready_task(conn)
    if next_ready:
        db.update_runtime(
            conn,
            status="running",
            current_task_id=None,
            current_step="none",
            current_branch=None,
            review_round=None,
            active_agents=0,
            status_message=f"Task {task_id} blocked; continuing to next ready task",
            last_heartbeat_at="CURRENT_TIMESTAMP",
        )
        log(f"Task blocked; continuing to queued task {next_ready['id']}", task_id)
        return

    # No queued follow-up work: go idle but keep the blocker message visible.
    rt = db.get_runtime(conn)
    msg = rt["status_message"] if rt else f"Task {task_id} blocked"
    set_runtime_idle(conn, status_message=msg)


def process_pinned_task(task, conn):
    """
    Drive a picked task until it reaches a terminal state.

    Non-terminal transitions requeue the same task as ready with a new next_step.
    Keep reloading and continuing that task locally instead of returning to the
    global ready queue, so no other task can leapfrog between phases.
    """
    current = task
    task_id = task["id"]

    while True:
        if _task_on_disallowed_master_branch(current):
            db.update_task(conn, task_id, status="blocked", next_step="none")
            db.add_comment(
                conn,
                task_id,
                MASTER_TASKS_DISABLED_MESSAGE,
                kind="comment",
                author="orchestrator",
            )
            log(f"Blocked ready task on protected branch '{current['branch']}'", task_id)
            update_runtime_after_task(conn, task_id, False)
            return False

        db.update_task(conn, task_id, status="running")
        current = db.get_task(conn, task_id)

        try:
            db.update_runtime(
                conn,
                status="running",
                current_task_id=task_id,
                current_step=current["next_step"],
                current_branch=current.get("branch"),
                review_round=current["review_round"],
                active_agents=0,
                status_message=f"Picked up task {task_id}: {current['title']}",
            )
        except Exception as e:
            # Roll back to ready so the task isn't stuck in running state
            db.update_task(conn, task_id, status="ready")
            raise RuntimeError(f"Runtime update failed at pickup for task {task_id}: {e}") from e

        db.add_run_log(conn, task_id,
                       f"Starting {current['next_step']}",
                       verb=current["next_step"], author="orchestrator")

        try:
            succeeded = advance(current, conn)
        except KeyboardInterrupt:
            # Ctrl-C during processing — recover task, then re-raise
            # so main() can run the stopping→stopped shutdown path.
            log("Interrupted!", task_id)
            if current["next_step"] in ("commit-review", "commit-review-supertask", "pull-request-review"):
                new_round = current["review_round"] + 1
                db.update_task(conn, task_id,
                               status="ready", next_step=current["next_step"],
                               review_round=new_round, last_review_decision="none")
                log(f"Review interrupted, advancing to round {new_round}", task_id)
            elif current["next_step"] == "commit-plan-review":
                # Reset to commit-plan so plan review starts fresh.
                # Also clear commit_plan so a stale plan cannot be re-submitted as-is.
                db.update_task(conn, task_id, status="ready", next_step="commit-plan",
                               commit_plan=None)
                log("Plan review interrupted, reset to commit-plan", task_id)
            elif current["next_step"] == "commit-plan":
                # Clear commit_plan so the planner must write a fresh plan
                # rather than reusing whatever stale value may be on the record.
                db.update_task(conn, task_id, status="ready", commit_plan=None)
                log("commit-plan interrupted, commit_plan cleared", task_id)
            else:
                db.update_task(conn, task_id, status="ready")
                log("Recoverable interrupt, task re-queued", task_id)
            raise
        except Exception as e:
            log(f"Unrecoverable error: {e}", task_id)
            db.update_task(conn, task_id, status="blocked", next_step="none")
            db.add_comment(conn, task_id,
                           f"Orchestrator error: {e}",
                           kind="comment", author="orchestrator")
            # Orchestrator-level failure: status='error' per spec,
            # but clear task/agent fields so dashboard doesn't show
            # phantom active work.
            db.update_runtime(
                conn,
                status="error",
                current_task_id=None,
                current_step="none",
                current_branch=None,
                review_round=None,
                active_agents=0,
                status_message=f"Orchestrator error on task {task_id}: {e}",
            )
            return False

        current = db.get_task(conn, task_id)

        # Propagate blocked child task to its parent supertask
        if current["status"] == "blocked" and current.get("parent_task_id"):
            parent_id = current["parent_task_id"]
            parent = db.get_task(conn, parent_id)
            if parent and parent["status"] not in ("done", "blocked"):
                db.update_task(conn, parent_id, status="blocked")
                db.add_comment(conn, parent_id,
                               f"Child task {task_id} blocked; supertask blocked.",
                               kind="comment", author="orchestrator")
                log(f"Child task blocked; supertask {parent_id} also blocked", task_id)

        if current["status"] == "ready" and current["next_step"] != "none":
            log(f"Continuing pinned task at step={current['next_step']}", task_id)
            continue

        update_runtime_after_task(conn, task_id, succeeded)
        return succeeded


def main_loop(conn, *, db_path=None):
    """Poll for ready tasks and process them one at a time."""
    log("Kanban Orchestra started. Polling for ready tasks...")

    # Initialize runtime and start heartbeat
    init_runtime(conn)
    recover_running_tasks(conn)
    start_heartbeat(db.get_db_path())
    set_runtime_idle(conn)

    stop_file = Path(db.get_db_path()).parent / STOP_AFTER_TASK_FILE
    blocked_gate_logged_task_ids = set()

    while True:
        check_dashboard_process(conn, db_path)
        handle_dashboard_start_request(conn, db_path)
        if stop_file.exists():
            log(f"Found {STOP_AFTER_TASK_FILE} — stopping cleanly. Deleting file.")
            stop_file.unlink()
            db.update_runtime(conn, status="stopping", active_agents=0,
                              status_message="Stop-after-task requested")
            stop_heartbeat()
            db.update_runtime(conn, status="stopped",
                              status_message="Stopped")
            break

        gated_tasks = db.list_ready_tasks_blocked_by_blocked_gate(conn)
        current_gated_ids = {gated_task["id"] for gated_task in gated_tasks}
        blocked_gate_logged_task_ids.intersection_update(current_gated_ids)
        for gated_task in gated_tasks:
            if gated_task["id"] in blocked_gate_logged_task_ids:
                continue
            log(
                "Skipping ready task because another task is blocked and "
                "allow_when_blocked is false",
                gated_task["id"],
            )
            blocked_gate_logged_task_ids.add(gated_task["id"])

        task = db.find_ready_task(conn)
        if not task:
            time.sleep(POLL_INTERVAL)
            continue

        task_id = task["id"]
        blocked_gate_logged_task_ids.discard(task_id)
        log(f"Picked up: '{task['title']}' (step={task['next_step']})", task_id)

        process_pinned_task(task, conn)


_shutting_down = False


def main(argv=None):
    global _shutting_down, _log_fh

    if argv is None:
        argv = []

    parser = argparse.ArgumentParser(description="Run the Kanban Orchestra orchestrator.")
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Run only the orchestrator worker. The default starts the matching repo dashboard too.",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=None,
        help="Preferred dashboard port. If unavailable, the dashboard chooses the next free port.",
    )
    args = parser.parse_args(argv)
    db_path = db.get_db_path()
    log_path = db.get_orchestrator_log_path(db_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _log_fh = log_path.open("a", encoding="utf-8", buffering=1)

    def handle_sigint(signum, frame):
        global _shutting_down
        if _shutting_down:
            # Second Ctrl-C: hard kill process group
            log("Force shutdown.")
            os.killpg(os.getpgrp(), signal.SIGKILL)
        else:
            _shutting_down = True
            log("Ctrl-C received. Press again to force quit.")
            raise KeyboardInterrupt

    os.setpgrp()
    signal.signal(signal.SIGINT, handle_sigint)

    conn = None
    try:
        log(f"Logging to {log_path}")
        lock_path = acquire_singleton_lock(db_path=db_path)
        log(f"Acquired singleton lock: {lock_path}")
        if is_worktree_dirty():
            log("Refusing to start: git worktree is dirty. Commit, stash, or clean it before starting the orchestrator.")
            return 2
        if not args.no_dashboard:
            start_dashboard(db_path, preferred_port=args.dashboard_port)

        conn = db.connect(db_path)
        global _log_conn
        _log_conn = conn
        main_loop(conn, db_path=db_path)
    except SingletonLockError as e:
        log(str(e))
        if not args.no_dashboard:
            request_path = request_dashboard_start(db_path)
            log(f"Requested dashboard start via {request_path}")
            return 0
        return 1
    except KeyboardInterrupt:
        log("Shutting down gracefully.")
        # Set runtime to stopping, then stopped
        try:
            db.update_runtime(conn, status="stopping", active_agents=0,
                              status_message="Shutting down")
        except Exception:
            pass
        stop_heartbeat()
        try:
            db.update_runtime(conn, status="stopped",
                              status_message="Stopped")
        except Exception:
            pass
        # Kill child processes in our process group
        try:
            os.killpg(os.getpgrp(), signal.SIGTERM)
        except ProcessLookupError:
            pass
    finally:
        stop_dashboard()
        stop_heartbeat()
        release_singleton_lock()
        if conn is not None:
            conn.close()
        if _log_fh is not None:
            _log_fh.close()
            _log_fh = None
        release_singleton_lock()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
