#!/usr/bin/env python3
"""
smart_unblock.py - native blocked-task recovery for the orchestrator.

The orchestrator runs this loop as a background thread. Each cycle gathers
current evidence for every blocked task and hands that evidence to the
configured LLM agent, which decides why the task is blocked, whether recovery
is safe, and then either recovers the task or records an explanation for the
user. This module makes no recovery decision itself.

A repo-scoped flock serializes the loop and publishes live consultation
metadata so dispatch and `ko-task continue`/`set` can refuse a task while a
consultation is in flight.
"""

from __future__ import annotations

import fcntl
import glob
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
import db
import task as task_module

POLL_INTERVAL = 60
LOCK_FILE_NAME = "smart-unblock.lock"
STATE_FILE_NAME = "smart-unblock-state.json"

WATCHER_AUTHOR = "smart-unblock"
WATCHER_VERB = "smart-unblock"

# Bound one LLM consultation so a hung agent cannot wedge the watcher.
AGENT_TIMEOUT = 900

# Grace given to a consultation process group between SIGTERM and SIGKILL.
CONSULTATION_GRACE = 5.0

MAX_COMMENTS = 12
MAX_RUN_LOG = 25
MAX_TRANSCRIPT_LINES = 80


class WatcherAlreadyRunning(RuntimeError):
    """Raised when a second watcher tries to start for the same repo."""


# The lock file doubles as published consultation metadata so dispatch and
# `ko-task continue`/`set` can see which task is gated while a consultation
# is in flight.
_LOCK_STATE = {"handle": None, "fields": {}}
_LOCK_STATE_LOCK = threading.Lock()

# The consultation subprocess the current cycle is waiting on, if any.
_ACTIVE_CONSULTATION = {"proc": None}
_CONSULTATION_LOCK = threading.Lock()


# ── Paths ──────────────────────────────────────────────────────────────


def _runtime_root(db_path: str | None = None) -> Path:
    root = db.get_runtime_root(db_path)
    root.mkdir(parents=True, exist_ok=True)
    return root


def watcher_lock_path(db_path: str | None = None) -> Path:
    return _runtime_root(db_path) / LOCK_FILE_NAME


def watcher_state_path(db_path: str | None = None) -> Path:
    return _runtime_root(db_path) / STATE_FILE_NAME


def _repo_root(db_path: str | None = None) -> Path:
    return Path(db.get_db_path(db_path)).resolve().parent


# ── Lock and consultation lifecycle ────────────────────────────────────


def read_watcher_metadata(db_path: str | None = None) -> dict:
    """Read best-effort `key=value` metadata written by the running watcher."""
    try:
        lines = watcher_lock_path(db_path).read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return {}
    metadata = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key:
            metadata[key] = value
    return metadata


def _metadata_int(metadata: dict, key: str) -> int | None:
    value = metadata.get(key, "")
    return int(value) if value.isdigit() else None


def _probe_watcher_lock(db_path: str | None = None) -> tuple[bool, dict]:
    """Return ``(running, metadata)`` via a race-safe lock probe.

    Attempt a non-blocking exclusive lock. If the probe acquires it, no live
    watcher holds the flock — consultation metadata on disk is stale leftover
    from a crash or SIGKILL and must not gate dispatch/continue/set. If the
    probe gets ``BlockingIOError``, a live watcher holds the flock and the
    metadata is authoritative for the consultation gate.
    """
    lock_path = watcher_lock_path(db_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = lock_path.open("a+", encoding="utf-8")
    except OSError:
        return False, {}
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True, read_watcher_metadata(db_path)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        return False, read_watcher_metadata(db_path)
    finally:
        handle.close()


def watcher_status(db_path: str | None = None) -> dict:
    """Return the watcher's running state plus its recorded metadata."""
    lock_path = watcher_lock_path(db_path)
    running, metadata = _probe_watcher_lock(db_path)
    pid = metadata.get("pid")
    # Consultation fields are live only while the flock is held.
    consultation_task_id = (
        _metadata_int(metadata, "consultation_task_id") if running else None
    )
    consultation_pgid = (
        _metadata_int(metadata, "consultation_pgid") if running else None
    )
    return {
        "running": running,
        "pid": int(pid) if pid and pid.isdigit() else None,
        "consultation_pgid": consultation_pgid,
        "consultation_task_id": consultation_task_id,
        "agent": metadata.get("agent"),
        "interval": int(metadata["interval"]) if metadata.get("interval", "").isdigit() else None,
        "started_at": metadata.get("started_at"),
        "repo_root": str(_repo_root(db_path)),
        "lock_path": str(lock_path),
        "state_path": str(watcher_state_path(db_path)),
    }


def active_consultation_task_id(db_path: str | None = None) -> int | None:
    """Return the task id held under the shared consultation dispatch gate.

    Authoritative only while the watcher lock is actually held; stale lock-file
    bytes left after crash/SIGKILL do not keep the gate active.
    """
    return watcher_status(db_path).get("consultation_task_id")


def is_consultation_gated(task_id: int, db_path: str | None = None) -> bool:
    """True when ``task_id`` is held under the shared consultation dispatch gate.

    The gate covers main-queue dispatch and pinned-task continuation: while it
    is set, the task must not be marked ``running`` or advanced further.
    """
    consulting = active_consultation_task_id(db_path)
    return consulting is not None and consulting == int(task_id)


def find_dispatchable_task(conn, db_path: str | None = None):
    """Like ``db.find_ready_task``, but never returns a task under consultation.

    The consultation gate stays published from before the agent subprocess
    starts through post-consultation validation/rollback. Dispatch must honor
    it so a rogue ``ready`` mutation cannot be picked up mid-flight or in the
    window before rollback restores ``blocked``.
    """
    resolved = db_path or db.get_connection_db_path(conn)
    consulting = active_consultation_task_id(resolved)
    exclude = [consulting] if consulting is not None else None
    return db.find_ready_task(conn, exclude_ids=exclude)


def acquire_watcher_lock(db_path: str | None = None, *, agent: str, interval: int):
    """Take the repo-scoped watcher lock, or raise WatcherAlreadyRunning.

    The returned handle must stay open for the lifetime of the watcher.
    """
    lock_path = watcher_lock_path(db_path)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise WatcherAlreadyRunning(
            f"a smart-unblock watcher already holds {lock_path}"
        ) from exc
    fields = {
        "pid": str(os.getpid()),
        "agent": agent,
        "interval": str(interval),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with _LOCK_STATE_LOCK:
        _LOCK_STATE["handle"] = handle
        _LOCK_STATE["fields"] = fields
        _write_lock_metadata(handle, fields)
    return handle


def _write_lock_metadata(handle, fields: dict) -> None:
    """Rewrite the lock file with the given `key=value` metadata.

    Overwrite first and truncate after, so a concurrent probe never observes
    an empty lock file mid-update.
    """
    try:
        handle.seek(0)
        handle.write("".join(f"{key}={value}\n" for key, value in fields.items()))
        handle.truncate()
        handle.flush()
    except (OSError, ValueError):
        pass


def _set_lock_field(key: str, value) -> None:
    """Publish (or clear, when `value` is None) one metadata field."""
    with _LOCK_STATE_LOCK:
        handle = _LOCK_STATE["handle"]
        if handle is None:
            return
        if value is None:
            _LOCK_STATE["fields"].pop(key, None)
        else:
            _LOCK_STATE["fields"][key] = str(value)
        _write_lock_metadata(handle, _LOCK_STATE["fields"])


def release_watcher_lock(handle) -> None:
    """Drop the watcher lock and clear its metadata."""
    with _LOCK_STATE_LOCK:
        if _LOCK_STATE["handle"] is handle:
            _LOCK_STATE["handle"] = None
            _LOCK_STATE["fields"] = {}
    try:
        handle.seek(0)
        handle.truncate()
        handle.flush()
    except (OSError, ValueError):
        pass
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, ValueError):
        pass
    try:
        handle.close()
    except (OSError, ValueError):
        pass


def _own_process_group() -> int | None:
    try:
        return os.getpgid(0)
    except OSError:
        return None


def _group_is_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _signal_group(pgid: int, signum: int) -> None:
    """Signal a process group, ignoring groups that are already gone."""
    if not pgid or pgid <= 1 or pgid == _own_process_group():
        return
    try:
        os.killpg(pgid, signum)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _terminate_process(proc, *, grace: float = CONSULTATION_GRACE) -> None:
    """Tear down a consultation subprocess and everything it spawned."""
    if proc.poll() is not None:
        proc.wait()
        return
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None

    if pgid:
        _signal_group(pgid, signal.SIGTERM)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass

    if pgid:
        _signal_group(pgid, signal.SIGKILL)
    else:
        proc.kill()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass


def _register_consultation(proc) -> None:
    """Track (or clear) the consultation the watcher is currently waiting on."""
    with _CONSULTATION_LOCK:
        _ACTIVE_CONSULTATION["proc"] = proc

    pgid = None
    if proc is not None:
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = proc.pid
    _set_lock_field("consultation_pgid", pgid)


def terminate_active_consultation() -> bool:
    """Kill the in-flight consultation, if the watcher is inside one."""
    with _CONSULTATION_LOCK:
        proc = _ACTIVE_CONSULTATION["proc"]
    if proc is None:
        return False
    _terminate_process(proc)
    return True


# ── Repeated-block suppression state ───────────────────────────────────


def read_state(db_path: str | None = None) -> dict:
    try:
        raw = watcher_state_path(db_path).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def write_state(state: dict, db_path: str | None = None) -> None:
    path = watcher_state_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


# ── Evidence ───────────────────────────────────────────────────────────


def _git_output(args, repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def _is_watcher_transcript(path: str) -> bool:
    """Return whether a transcript was written by a smart-unblock consultation."""
    return f"-{WATCHER_VERB}-" in os.path.basename(path)


def _newest_transcript_tail(paths: list[str]) -> dict:
    """Return the newest of `paths` with a bounded tail of its contents."""
    if not paths:
        return {"path": "", "tail": ""}
    newest = max(paths, key=lambda p: (os.stat(p).st_mtime_ns, os.path.basename(p)))
    try:
        lines = Path(newest).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"path": newest, "tail": ""}
    return {"path": newest, "tail": "\n".join(lines[-MAX_TRANSCRIPT_LINES:])}


def _transcript_tails(task_id: int, db_path: str | None = None) -> tuple[dict, dict]:
    """Return the newest task transcript and the newest watcher transcript.

    The watcher's own consultation transcripts are kept separate so they can be
    offered to the LLM as prior context without disturbing the fingerprint.
    """
    task_dir = db.get_artifacts_root(db_path) / f"task-{task_id}"
    candidates = [p for p in glob.glob(str(task_dir / "*.log")) if os.path.isfile(p)]
    task_logs = [p for p in candidates if not _is_watcher_transcript(p)]
    watcher_logs = [p for p in candidates if _is_watcher_transcript(p)]
    return _newest_transcript_tail(task_logs), _newest_transcript_tail(watcher_logs)


def collect_block_evidence(conn, task_id: int, db_path: str | None = None) -> dict:
    """Gather the current repository evidence for one blocked task.

    Everything the watcher itself produced — its comments, its run-log entries,
    and its consultation transcripts — is separated into the `prior_unblock_*`
    keys, which are excluded from the fingerprint below. The LLM still sees its
    own earlier reasoning, but the watcher cannot retrigger itself.
    """
    repo_root = _repo_root(db_path)
    task = db.get_task(conn, task_id) or {}

    comments = db.get_comments(conn, task_id)
    prior_notes = [c for c in comments if c.get("author") == WATCHER_AUTHOR]
    other_comments = [c for c in comments if c.get("author") != WATCHER_AUTHOR]

    run_log = [r for r in db.get_run_log(conn, task_id) if r.get("author") != WATCHER_AUTHOR]
    transcript, watcher_transcript = _transcript_tails(task_id, db_path)

    return {
        "task": {k: v for k, v in task.items() if k != "updated_at"},
        "comments": other_comments[-MAX_COMMENTS:],
        "run_log": run_log[:MAX_RUN_LOG],
        "transcript": transcript,
        "stash_list": _git_output(["stash", "list"], repo_root),
        "worktree_status": _git_output(["status", "--porcelain"], repo_root),
        "current_branch": _git_output(["rev-parse", "--abbrev-ref", "HEAD"], repo_root),
        "prior_unblock_notes": prior_notes[-MAX_COMMENTS:],
        "prior_unblock_transcript": watcher_transcript,
        "repo_root": str(repo_root),
    }


# Evidence the watcher produced itself; reacting to it would loop forever.
SELF_AUTHORED_EVIDENCE_KEYS = ("prior_unblock_notes", "prior_unblock_transcript")


def evidence_fingerprint(evidence: dict) -> str:
    """Hash the parts of the evidence the watcher should react to."""
    material = {
        k: v for k, v in evidence.items() if k not in SELF_AUTHORED_EVIDENCE_KEYS
    }
    payload = json.dumps(material, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── LLM consultation ───────────────────────────────────────────────────


def build_unblock_prompt(evidence: dict, agent: str | None = None) -> str:
    """Render the decision prompt handed to the configured LLM.

    The agent never resumes the task itself. It records exactly one decision
    comment and stops; the watcher parses that comment from durable DB state
    and — only once it has verified the comment actually exists and names
    this agent — performs the resume itself. That keeps the status
    transition strictly downstream of a verified decision, so nothing can
    ever act on a task before its justification is visible.
    """
    task = evidence.get("task", {})
    task_id = task.get("id")
    repo_root = evidence.get("repo_root", "")
    agent_label = agent or "the configured smart-unblock agent"
    return f"""You are the Kanban Orchestra smart-unblock agent for one repository,
running as '{agent_label}'.

Task {task_id} is blocked. Decide why it is blocked and whether recovery is
safe right now. You do NOT resume the task yourself — the watcher does that,
and only after reading your decision comment back from the database. Your
only job is to record exactly one decision comment and then stop.

Repository root: {repo_root}
Task CLI: "$ORCHESTRA_DIR/bin/ko-task"

Collected evidence (JSON):

{json.dumps(evidence, indent=2, default=str)}

Investigate further with read-only commands when the evidence is not enough,
for example `ko-task show {task_id}`, `ko-task show-comments {task_id}`,
`ko-task show-run-log {task_id}`, `git status --porcelain`, `git stash list`,
and reading the transcript path above. Do not edit files, change branches,
stash, discard, or commit anything, and do not touch task status yourself —
no `ko-task continue`, no `ko-task set`.

If recovery is clearly safe and supported by this evidence, record a RESUME
decision, starting the comment by naming yourself as smart-unblock running as
'{agent_label}':

    cat <<'EOF' | "$ORCHESTRA_DIR/bin/ko-task" comment {task_id} --message-stdin --comment --author {WATCHER_AUTHOR}
    smart-unblock ({agent_label}): RESUME [+N] <what you are resuming and the evidence that made it safe>
    EOF

Include `+N` (e.g. `+1`) immediately after RESUME only when this task is
blocked at its review cap and needs N additional review round(s) granted to
proceed; leave it out for every other kind of block.

If recovery is not clearly safe — the block needs a human decision, the
worktree holds changes you cannot attribute, the evidence is ambiguous, or the
task needs product input — record a BLOCKED decision instead, again starting
the comment by naming yourself as smart-unblock running as '{agent_label}':

    cat <<'EOF' | "$ORCHESTRA_DIR/bin/ko-task" comment {task_id} --message-stdin --comment --author {WATCHER_AUTHOR}
    smart-unblock ({agent_label}): BLOCKED <why this task is blocked and what the user must decide>
    EOF

Leave exactly one comment, and always pass `--author {WATCHER_AUTHOR}` so the
watcher recognises the comment as its own and does not reconsider this block
because of it. A comment that does not start with `smart-unblock ({agent_label}):`
followed by RESUME or BLOCKED cannot be acted on and this task will simply be
reconsidered next cycle.
"""


def _wait_for_consultation(proc, timeout: int, stop_event) -> int | None:
    """Wait for the consultation, returning None if stopped or timed out."""
    deadline = time.monotonic() + timeout if timeout else None
    while True:
        if stop_event is not None and stop_event.is_set():
            return None
        try:
            return proc.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
        if deadline is not None and time.monotonic() >= deadline:
            return None


def invoke_unblock_agent(
    agent: str,
    prompt: str,
    task_id: int,
    db_path: str | None = None,
    *,
    timeout: int = AGENT_TIMEOUT,
    stop_event: threading.Event | None = None,
) -> dict:
    """Run the configured LLM on the prompt and capture its transcript.

    The agent gets its own process group and is tracked while it runs, so a
    watcher shutdown can take the consultation down with it instead of leaving
    an orphan that acts on the task after `stop` reported success.
    """
    cmd_template = config.resolve_agent_command(agent)
    if cmd_template is None:
        return {
            "agent": agent,
            "returncode": None,
            "error": f"unknown agent '{agent}'",
            "transcript_path": "",
        }

    cmd = [part.replace("{prompt}", prompt) for part in cmd_template]

    transcript_path = ""
    transcript_file = None
    try:
        path = db.new_agent_transcript_path(task_id, WATCHER_VERB, agent, db_path=db_path)
        transcript_file = path.open("w", encoding="utf-8")
        transcript_path = str(path)
    except (RuntimeError, OSError):
        transcript_file = None
        transcript_path = ""

    # Every child of this process -- and any grandchild it shells out to --
    # inherits this flag, so a rogue `ko-task continue`/`set` run anywhere in
    # the consultation's process tree is refused by the CLI itself. See
    # task.SMART_UNBLOCK_CONSULTATION_ENV_VAR. The lock-file consultation
    # gate is owned by poll_once for the whole invoke+validate+rollback
    # window so dispatch stays blocked even after this subprocess exits.
    consultation_env = dict(os.environ)
    consultation_env[task_module.SMART_UNBLOCK_CONSULTATION_ENV_VAR] = "1"

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(_repo_root(db_path)),
            stdin=subprocess.DEVNULL,
            stdout=transcript_file or subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            env=consultation_env,
        )
    except (OSError, ValueError) as exc:
        if transcript_file is not None:
            transcript_file.close()
        return {
            "agent": agent,
            "returncode": None,
            "error": f"could not launch agent: {exc}",
            "transcript_path": transcript_path,
        }

    _register_consultation(proc)
    try:
        returncode = _wait_for_consultation(proc, timeout, stop_event)
        if returncode is None:
            interrupted = stop_event is not None and stop_event.is_set()
            _terminate_process(proc)
            error = (
                "agent stopped with the watcher"
                if interrupted
                else f"agent timed out after {timeout}s"
            )
            return {
                "agent": agent,
                "returncode": None,
                "error": error,
                "interrupted": interrupted,
                "transcript_path": transcript_path,
            }
    finally:
        _register_consultation(None)
        if transcript_file is not None:
            transcript_file.close()

    return {
        "agent": agent,
        "returncode": returncode,
        "transcript_path": transcript_path,
    }


# ── Decision-comment verification ──────────────────────────────────────
#
# The prompt asks the agent to identify itself and to record a decision
# instead of touching task status, but the agent is an external LLM run
# through a shell command — nothing stops it from ignoring that instruction.
# These helpers verify, from durable DB state, that a decision comment
# naming the agent actually exists, and parse what it decided. The watcher
# is the only thing that ever moves a task off `blocked`, and only after
# this verification passes — so a missing, malformed, or self-continued
# decision is never trusted or silently treated as "handled" forever.

_RESUME_PATTERN = re.compile(r"^RESUME\b\s*(?:\+(\d+)\s*)?(.*)$", re.IGNORECASE | re.DOTALL)
_BLOCKED_PATTERN = re.compile(r"^BLOCKED\b\s*(.*)$", re.IGNORECASE | re.DOTALL)


def _decision_pattern(agent: str) -> "re.Pattern[str]":
    return re.compile(
        rf"^\s*{re.escape(WATCHER_AUTHOR)}\s*\(\s*{re.escape(agent)}\s*\)\s*:\s*(.*)$",
        re.IGNORECASE | re.DOTALL,
    )


def _new_comments_since(conn, task_id: int, before_ids: set) -> list[dict]:
    """Comments added to a task since `before_ids` was captured, oldest first."""
    return sorted(
        (c for c in db.get_comments(conn, task_id) if c.get("id") not in before_ids),
        key=lambda c: c["id"],
    )


def parse_decision(message: str, agent: str) -> dict | None:
    """Parse a comment into a decision if it identifies itself as this agent's.

    Returns `{"action": "resume", "add_review_rounds": int | None, "reason": str}`,
    `{"action": "blocked", "reason": str}`, or `None` if the message does not
    identify the agent or does not carry a recognised RESUME/BLOCKED verdict.
    """
    identity_match = _decision_pattern(agent).match(message or "")
    if not identity_match:
        return None
    rest = identity_match.group(1).strip()
    resume_match = _RESUME_PATTERN.match(rest)
    if resume_match:
        rounds = int(resume_match.group(1)) if resume_match.group(1) else None
        return {"action": "resume", "add_review_rounds": rounds, "reason": resume_match.group(2).strip()}
    blocked_match = _BLOCKED_PATTERN.match(rest)
    if blocked_match:
        return {"action": "blocked", "reason": blocked_match.group(1).strip()}
    return None


def find_decision_comment(comments: list[dict], agent: str) -> dict | None:
    """Return the first comment that carries a parseable decision from this agent."""
    for comment in comments:
        if comment.get("author") == WATCHER_AUTHOR and parse_decision(comment.get("message") or "", agent):
            return comment
    return None


def _apply_resume_decision(conn, task_id: int, decision: dict) -> str | None:
    """Resume a task after its RESUME decision comment was already verified.

    Runs in-process, synchronously, so the transition from `blocked` happens
    strictly downstream of the already-durable decision comment — the agent
    itself never touches task status, so there is no window in which the
    task can go `ready` before its justification is visible. Returns an
    error string on failure, or None on success.
    """
    try:
        task_module.continue_blocked_task(
            conn,
            task_id,
            add_review_rounds=decision.get("add_review_rounds"),
            next_step=None,
        )
        return None
    except task_module.ContinueTaskError as exc:
        return str(exc)


def _reject_agent_originated_continuation(conn, task_id: int, blocked_row: dict) -> bool:
    """Restore a blocked snapshot if a consultation mutated status without us.

    The CLI gates refuse the normal rogue-continue path, but an agent that
    clears those gates (or reaches the DB some other way) could still leave
    the task runnable. Rolling back here means the orchestrator can never
    dispatch on an unverified agent-originated continuation. Returns True
    when a rollback happened.
    """
    current = db.get_task(conn, task_id)
    if not current or current.get("status") == "blocked":
        return False
    db.update_task(
        conn,
        task_id,
        status="blocked",
        next_step=blocked_row.get("next_step"),
        block_reason=blocked_row.get("block_reason"),
        resume_next_step=blocked_row.get("resume_next_step"),
    )
    return True


# ── Poll cycle ─────────────────────────────────────────────────────────


def poll_once(
    conn,
    db_path: str | None = None,
    *,
    agent: str | None = None,
    stop_event: threading.Event | None = None,
) -> list[dict]:
    """Consider every blocked task once and return one result per task."""
    agent = agent or config.DEFAULT_UNBLOCKER
    state = read_state(db_path)
    blocked = db.list_tasks(conn, status="blocked", page_size=None)
    results = []

    for row in blocked:
        if stop_event is not None and stop_event.is_set():
            break
        task_id = row["id"]
        evidence = collect_block_evidence(conn, task_id, db_path)
        fingerprint = evidence_fingerprint(evidence)
        key = str(task_id)
        if state.get(key, {}).get("fingerprint") == fingerprint:
            results.append({"task_id": task_id, "action": "skipped-unchanged"})
            continue

        before_comment_ids = {c["id"] for c in db.get_comments(conn, task_id)}
        blocked_snapshot = dict(db.get_task(conn, task_id) or row)
        prompt = build_unblock_prompt(evidence, agent)
        # Hold the shared dispatch/status gate for the entire consultation
        # plus validation/rollback window. Clearing it when the subprocess
        # exits would leave a race where a rogue ready mutation is
        # dispatchable before we restore blocked.
        _set_lock_field("consultation_task_id", task_id)
        try:
            outcome = invoke_unblock_agent(
                agent, prompt, task_id, db_path, stop_event=stop_event
            )
            if outcome.get("interrupted"):
                # The consultation never reached a decision; reconsider after restart.
                # Also reject any mid-flight status mutation the agent may have made.
                if _reject_agent_originated_continuation(conn, task_id, blocked_snapshot):
                    db.add_run_log(
                        conn,
                        task_id,
                        (
                            f"smart-unblock interrupted consulting {agent}; rejected "
                            "agent-originated status change and restored blocked"
                        ),
                        verb=WATCHER_VERB,
                        author=WATCHER_AUTHOR,
                    )
                results.append({"task_id": task_id, "action": "interrupted", **outcome})
                break

            # A nonzero exit means the consultation itself did not complete
            # cleanly. Never act on anything it may have left behind, and never
            # fingerprint the evidence, so a flaky or failing consultation is
            # simply retried next cycle instead of being trusted or wedged.
            consult_failed = bool(outcome.get("error")) or outcome.get("returncode") != 0

            new_comments = _new_comments_since(conn, task_id, before_comment_ids)
            decision_comment = (
                find_decision_comment(new_comments, agent) if not consult_failed else None
            )
            decision = (
                parse_decision(decision_comment["message"], agent)
                if decision_comment
                else None
            )

            resume_error = None
            if consult_failed:
                action = "consult-failed"
                reason = outcome.get("error") or f"agent exited {outcome.get('returncode')}"
                message = f"smart-unblock could not consult {agent}: {reason}"
            elif decision is None:
                action = "unverified-decision"
                message = (
                    f"smart-unblock consulted {agent} on this block but left no comment "
                    f"identifying itself as smart-unblock ({agent}) with a RESUME/BLOCKED "
                    "verdict -- will reconsider"
                )
            elif decision["action"] == "blocked":
                action = "explained"
                message = (
                    f"smart-unblock consulted {agent}: left blocked -- {decision['reason']}"
                )
            else:
                # The decision comment is already durably recorded; only now does
                # the watcher itself perform the resume, so the transition is
                # always downstream of a verified decision.
                resume_error = _apply_resume_decision(conn, task_id, decision)
                if resume_error is None:
                    action = "recovered"
                    message = (
                        f"smart-unblock consulted {agent}: resumed the task -- "
                        f"{decision['reason']}"
                    )
                else:
                    action = "resume-failed"
                    message = (
                        f"smart-unblock consulted {agent}: verified a RESUME decision but "
                        f"could not resume the task: {resume_error} -- will reconsider"
                    )

            # Only a verified watcher-applied resume may leave the task runnable.
            # Anything else that mutated status during the consultation is rolled
            # back so the orchestrator cannot dispatch on an unverified transition.
            if action != "recovered" and _reject_agent_originated_continuation(
                conn, task_id, blocked_snapshot
            ):
                action = "rejected-agent-continuation"
                message = (
                    f"smart-unblock consulted {agent}: rejected agent-originated status "
                    "change and restored blocked -- will reconsider"
                )

            if outcome.get("transcript_path"):
                message += f". Transcript: {outcome['transcript_path']}"
            db.add_run_log(conn, task_id, message, verb=WATCHER_VERB, author=WATCHER_AUTHOR)

            # Only remember this evidence as "considered" once it produced a
            # verified decision that the watcher could act on. A failed
            # consultation, a missing/malformed decision, or a RESUME the
            # watcher could not apply must be reconsidered next cycle rather
            # than fingerprinted as handled forever.
            if action in ("explained", "recovered"):
                state[key] = {
                    "fingerprint": fingerprint,
                    "considered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "agent": outcome.get("agent"),
                    "returncode": outcome.get("returncode"),
                }
            else:
                state.pop(key, None)

            results.append({
                "task_id": task_id,
                "action": action,
                "decision_comment_id": decision_comment["id"] if decision_comment else None,
                **outcome,
            })
        finally:
            _set_lock_field("consultation_task_id", None)

    blocked_keys = {str(row["id"]) for row in blocked}
    state = {k: v for k, v in state.items() if k in blocked_keys}
    write_state(state, db_path)
    return results


def run_watcher(
    db_path: str | None = None,
    *,
    agent: str | None = None,
    interval: int = POLL_INTERVAL,
    max_cycles: int | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    """Hold the repo lock and poll for blocked tasks until stopped."""
    agent = agent or config.DEFAULT_UNBLOCKER
    stop_event = stop_event or threading.Event()
    handle = acquire_watcher_lock(db_path, agent=agent, interval=interval)

    cycles = 0
    try:
        while not stop_event.is_set():
            conn = db.connect(db_path)
            try:
                poll_once(conn, db_path, agent=agent, stop_event=stop_event)
            except Exception as exc:  # keep the watcher alive across bad cycles
                print(f"smart-unblock cycle failed: {exc}", flush=True)
            finally:
                conn.close()
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            stop_event.wait(interval)
    finally:
        # Never leave a consultation behind: a stopped loop must not leave an
        # agent that can still comment on or continue a task.
        terminate_active_consultation()
        release_watcher_lock(handle)
    return cycles
