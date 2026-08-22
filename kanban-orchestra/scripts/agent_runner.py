#!/usr/bin/env python3
"""
agent_runner.py - subprocess-facing agent runtime helpers.

Owns agent ping, ACK gating, transcript writing, and active-agent process
metadata for Kanban Orchestra agent subprocesses.
"""

import hashlib
import os
import select
import shlex
import signal
import subprocess
import time
from collections import deque

import active_agent_processes
import config
import db


def _default_log(msg, task_id=None):
    print(msg, flush=True)


log = _default_log


def _default_repo_root():
    """Return the git repo root, or a fallback string on failure."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "(unknown — run: git rev-parse --show-toplevel)"


_repo_root_func = _default_repo_root


def set_log_function(log_func):
    """Use the orchestrator logger for subprocess runtime messages."""
    global log
    log = log_func


def set_repo_root_function(repo_root_func):
    """Use the orchestrator repo-root resolver for subprocess cwd selection."""
    global _repo_root_func
    _repo_root_func = repo_root_func


def _repo_root_for_subprocess():
    """Return a real repo-root cwd for child processes, or None if unavailable."""
    repo_root = _repo_root_func()
    if repo_root.startswith("("):
        return None
    return repo_root


# Process-local ACK cache: (task_id, agent_name, purpose) entries mean the agent
# has already responded to that probe for this task in the current process
# lifetime. "run" and "review" are distinct — a shallow run ping cannot prove
# the review/tool path is usable. Not persisted: after an orchestrator restart
# all tasks may ping again.
_agent_ack_cache: set = set()

PING_PROMPT = "This is a ping. Respond with ACK."
REVIEW_READY_PREFIX = "KO-REVIEW-READY:"
PING_RETRY_INTERVAL = 60  # seconds between retries when agent does not respond
_REVIEW_PROBE_GRACE_SECONDS = 0.05


def review_ping_prompt(task_id):
    """Build a reviewer probe whose expected digest is not in the prompt text."""
    return (
        f"This is a reviewer readiness probe for task {task_id}. "
        "Inspect git diff --cached in this worktree. "
        "Compute the lowercase SHA-256 hex digest of that command's exact stdout. "
        "Record a durable task comment on this task via the task CLI whose message "
        f"contains {REVIEW_READY_PREFIX} immediately followed by that digest. "
        "Echoing this prompt or printing a capability claim does not satisfy the probe."
    )


def cached_diff_digest(cwd=None):
    """Return the SHA-256 hex digest of `git diff --cached` stdout."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached"],
            capture_output=True,
            cwd=cwd or _repo_root_for_subprocess(),
        )
        payload = result.stdout or b""
    except Exception:
        payload = b""
    return hashlib.sha256(payload).hexdigest()


def review_ready_token(digest=None, *, cwd=None):
    """Return the expected reviewer-ready token for the current cached diff."""
    if digest is None:
        digest = cached_diff_digest(cwd=cwd)
    return f"{REVIEW_READY_PREFIX}{digest}"


def _comment_watermark(conn, task_id):
    row = conn.execute(
        "SELECT MAX(id) FROM comments WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return row[0] or 0


def _new_comment_has_ready_token(conn, task_id, token, after_id):
    """True when a comment newer than after_id contains the ready token."""
    conn.commit()
    row = conn.execute(
        """SELECT 1 FROM comments
           WHERE task_id = ? AND id > ? AND instr(message, ?) > 0
           LIMIT 1""",
        (task_id, after_id, token),
    ).fetchone()
    return row is not None


def _probe_comment_conn(conn):
    if conn is not None:
        return conn, False
    return db.connect(db.get_db_path()), True


def _resolve_command_template(agent_name, *, use_review_command=False):
    if use_review_command:
        return config.resolve_review_agent_command(agent_name)
    return config.resolve_agent_command(agent_name)


def ping_agent(agent_name, task_id, *, use_review_command=False, purpose=None, conn=None):
    """Send a ping prompt. Returns True if the agent produced a usable response.

    A shallow CLI banner or any-text response is enough for ordinary run pings.
    Reviewer readiness requires a durable task comment that contains a token
    derived from `git diff --cached`. Echoed prompt text, a bare ACK, or a
    prior run-ping cache hit cannot prove that review/tool path is usable.
    """
    purpose = purpose or ("review" if use_review_command else "run")
    cmd_template = _resolve_command_template(agent_name, use_review_command=use_review_command)
    if cmd_template is None:
        log(f"Unknown agent '{agent_name}', cannot ping", task_id)
        return False

    prompt = review_ping_prompt(task_id) if purpose == "review" else PING_PROMPT
    cmd = [part.replace("{prompt}", prompt) for part in cmd_template]
    expected_token = None
    comment_conn = None
    close_comment_conn = False
    watermark = 0
    if purpose == "review":
        expected_token = review_ready_token()
        try:
            comment_conn, close_comment_conn = _probe_comment_conn(conn)
            watermark = _comment_watermark(comment_conn, task_id)
        except Exception:
            comment_conn = None
            close_comment_conn = False

    if purpose == "review":
        log(
            f"Pre-flight reviewer probe: checking {agent_name} review/tool path "
            f"(task {task_id})",
            task_id,
        )
    else:
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
        if close_comment_conn and comment_conn is not None:
            comment_conn.close()
        return False

    def _review_comment_ready():
        if comment_conn is None or expected_token is None:
            return False
        try:
            return _new_comment_has_ready_token(
                comment_conn, task_id, expected_token, watermark,
            )
        except Exception:
            return False

    # Drain stdout so the child cannot block on a full pipe. Run pings ACK on
    # the first available characters (not a newline). Reviewer readiness never
    # treats stdout as evidence — echoed prompt text can contain instructions.
    acked = False
    eof = False
    deadline = time.monotonic() + PING_RETRY_INTERVAL
    stdout = proc.stdout
    fd = stdout.fileno() if stdout else None
    try:
        while time.monotonic() < deadline:
            if purpose == "review" and _review_comment_ready():
                acked = True
                break
            if eof:
                if purpose == "review":
                    time.sleep(_REVIEW_PROBE_GRACE_SECONDS)
                    acked = _review_comment_ready()
                break
            if fd is None:
                eof = True
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            readable, _, _ = select.select([stdout], [], [], min(remaining, 1.0))
            if not readable:
                continue
            chunk = os.read(fd, 4096)
            if not chunk:
                eof = True
                continue
            if purpose != "review" and chunk.strip():
                acked = True
                break
    finally:
        if close_comment_conn and comment_conn is not None:
            comment_conn.close()
    # Terminate the ping subprocess as soon as we have a verdict.
    try:
        proc.kill()
    except OSError:
        pass
    proc.wait()
    if active_record_id:
        active_agent_processes.clear_active_agent(active_record_id, db_path=db.get_db_path())

    if acked:
        if purpose == "review":
            log(
                f"Pre-flight reviewer probe: {agent_name} acknowledged the review/tool path "
                f"— proceeding with task {task_id}",
                task_id,
            )
        else:
            log(f"Pre-flight ping: {agent_name} responded — proceeding with task {task_id}", task_id)
    else:
        if purpose == "review":
            log(
                f"Pre-flight reviewer probe: {agent_name} did not acknowledge the review/tool path "
                f"— agent may be out of tokens, missing tools, or unavailable (task {task_id})",
                task_id,
            )
        else:
            log(
                f"Pre-flight ping: {agent_name} produced no output — "
                f"agent may be out of tokens or unavailable (task {task_id})",
                task_id,
            )
    return acked


def ensure_agent_acked(agent_name, task_id, conn, *, use_review_command=False, purpose=None):
    """
    Ensure the agent has ACKed for this task before running a real step.

    Cached process-locally per (task_id, agent_name, purpose). Reviewer
    readiness is a distinct purpose from an ordinary run ping: a shallow CLI
    ping, echoed probe text, or a bare ACK cannot by itself prove the
    review/tool path is usable. Do not call for steps that will be skipped.

    Returns True when the agent has acknowledged. Ordinary run pings retry
    every minute until ACK is received and keep the task pinned. Reviewer
    readiness is single-shot: a failed probe returns False immediately so
    the caller can apply the bounded review-infrastructure retry policy
    instead of stalling forever.
    """
    purpose = purpose or ("review" if use_review_command else "run")
    cache_key = (task_id, agent_name, purpose)
    if cache_key in _agent_ack_cache:
        return True

    while True:
        if purpose == "review" or use_review_command:
            acked = ping_agent(
                agent_name, task_id,
                use_review_command=use_review_command,
                purpose="review",
                conn=conn,
            )
        else:
            acked = ping_agent(agent_name, task_id)
        if acked:
            _agent_ack_cache.add(cache_key)
            return True
        if purpose == "review" or use_review_command:
            return False

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



def _summarize_transcript_tail(lines, max_chars=240):
    """Return a short single-line summary from the transcript tail."""
    tail = [line.strip() for line in lines if line and line.strip()]
    if not tail:
        return ""
    summary = " | ".join(tail)
    if len(summary) > max_chars:
        return summary[: max_chars - 3] + "..."
    return summary


def _format_agent_command_for_transcript(cmd, prompt):
    """Render the launched command without replaying the full prompt body."""
    if not prompt:
        return shlex.join(cmd)
    prompt_summary = (
        f"<prompt: {len(prompt)} chars; "
        f"sha256={hashlib.sha256(prompt.encode('utf-8')).hexdigest()[:12]}>"
    )
    redacted_cmd = [
        part.replace(prompt, prompt_summary) if prompt in part else part
        for part in cmd
    ]
    return shlex.join(redacted_cmd)


def run_agent(
    agent_name,
    prompt,
    task_id,
    conn,
    verb,
    cancel_event=None,
    proc_registry=None,
    *,
    use_review_command=False,
):
    """
    Launch an agent subprocess, capture full output to a transcript, return exit code.

    cancel_event: threading.Event — if set, abort reading and kill the subprocess.
    proc_registry: dict — if provided, register the Popen object under agent_name
                   so the caller can kill it on interrupt.
    """
    cmd_template = _resolve_command_template(agent_name, use_review_command=use_review_command)
    if cmd_template is None:
        log(f"Unknown agent '{agent_name}', skipping", task_id)
        return 1

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
            transcript.write(f"# command: {_format_agent_command_for_transcript(cmd, prompt)}\n")
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
