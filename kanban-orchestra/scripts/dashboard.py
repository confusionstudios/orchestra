#!/usr/bin/env python3
"""
Kanban Orchestra Dashboard — v1.

Serves an overview page and per-task detail pages with live SSE refresh.
Overview remains read-only; task detail pages can edit stored title and
description source text.

Routes:
  GET /              — overview (health card, current task, ready queue,
                       icebox, blocked tasks, recent done)
  GET /task/{id}     — task detail (metadata, edit form, comments, run log)
  POST /task/{id}/edit — update task title/description source text
  GET /events        — SSE stream for overview fragments
  GET /events/{id}   — SSE stream for task-detail fragments
"""

import errno
import json
import os
import re
import socket as _socket
import sqlite3
import sys
import time
from collections import deque
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from markdown_it import MarkdownIt

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
import db


app = FastAPI(title="Kanban Orchestra Dashboard")

_FAVICON = Path(__file__).resolve().parent.parent / "favicon.ico"

# ── Helpers ────────────────────────────────────────────────────────────

STALE_SECONDS = 60  # heartbeat older than this → "stale"
_REVIEW_ROUND_DISPLAY_RE = re.compile(r"\b((?:[Rr]eview round)|(?:[Rr]ound)) (\d+)\b")


def _esc(v) -> str:
    """Escape a value for HTML; treat None as empty string."""
    return escape(str(v)) if v is not None else ""


_md = MarkdownIt("commonmark", {"html": False})


def _md_html(text: str | None) -> str:
    """Render Markdown text to safe HTML. Returns empty string for None/empty."""
    if not text:
        return ""
    return _md.render(text)


def _normalize_description_source(text: str | None) -> str | None:
    """Store blank descriptions as NULL and normalize browser newlines."""
    if text is None:
        return None
    normalized = text.replace("\r\n", "\n")
    if not normalized.strip():
        return None
    return normalized


def _display_review_round_text(text: str | None) -> str:
    """Convert review-round labels in user-facing strings from 0-based to 1-based."""
    if not text:
        return ""

    def repl(match: re.Match[str]) -> str:
        return f"{match.group(1)} {int(match.group(2)) + 1}"

    return _REVIEW_ROUND_DISPLAY_RE.sub(repl, text)


def _age(dt_str: str | None) -> str:
    """Return a human-readable age string for a UTC datetime string."""
    if not dt_str:
        return "unknown"
    try:
        dt = _parse_utc_datetime(dt_str)
        if dt is None:
            return dt_str
        secs = int((datetime.now(timezone.utc) - dt).total_seconds())
        if secs < 0:
            secs = 0
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return dt_str


def _parse_utc_datetime(dt_str: str | None) -> datetime | None:
    """Parse a timestamp and normalize it to a UTC-aware datetime."""
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _client_timestamp(dt_str: str | None) -> str | None:
    """Return an ISO8601 UTC timestamp that browser JS can parse reliably."""
    dt = _parse_utc_datetime(dt_str)
    if dt is None:
        return None
    return dt.isoformat().replace("+00:00", "Z")


def _server_tz_label() -> str:
    """Return the server local timezone abbreviation, e.g. 'EDT'."""
    return datetime.now().astimezone().strftime("%Z") or "local time"


def _abbreviate_home(path: Path) -> str:
    """Render paths under the user's home directory with a leading ~."""
    home = Path.home().resolve()
    try:
        if path == home:
            return "~"
        return f"~/{path.relative_to(home)}"
    except ValueError:
        return str(path)


def _running_directory_display() -> str:
    """Return the resolved work repo directory shown on the dashboard."""
    return _abbreviate_home(db.get_repo_root())


def _display_timestamp(dt_str: str | None) -> str:
    """Render a stable server-side timestamp in YYYY-MM-DD HH:MM format (server local timezone)."""
    if not dt_str:
        return "unknown"
    dt = _parse_utc_datetime(dt_str)
    if dt is None:
        return dt_str
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def _absolute_timestamp_html(dt_str: str | None, *, css_class: str = "") -> str:
    """Render a timestamp directly from the server without client rewriting."""
    class_attr = f' class="timestamp-absolute{(" " + _esc(css_class)) if css_class else ""}"'
    return f"<span{class_attr}>{_esc(_display_timestamp(dt_str))}</span>"


def _live_age(dt_str: str | None, *, css_class: str = "") -> str:
    """Render an age label with machine-readable timestamp for client refresh."""
    age_text = _age(dt_str)
    client_ts = _client_timestamp(dt_str)
    class_attr = f' class="{_esc(css_class)}"' if css_class else ""
    if client_ts is None:
        return f"<span{class_attr}>{_esc(age_text)}</span>"
    return (
        f'<span{class_attr} data-relative-time="true" '
        f'data-timestamp="{_esc(client_ts)}">{_esc(age_text)}</span>'
    )


def _is_stale(last_heartbeat: str | None) -> bool:
    dt = _parse_utc_datetime(last_heartbeat)
    if dt is None:
        return True
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    return secs > STALE_SECONDS


def _short_hash(h: str | None) -> str:
    if not h:
        return ""
    return h[:8]


def _kind_glyph(task: dict) -> str:
    """Return a tight unicode glyph indicating supertask (⊕) or subtask (↳), else ''."""
    if task.get("kind") == "supertask":
        return "⊕"
    if task.get("parent_task_id") is not None:
        return "↳"
    return ""


def _task_ref(task: dict) -> str:
    """Render a human-friendly task reference for UI surfaces."""
    title = task.get("title")
    if title:
        return f"Task {task['id']}: {title}"
    return f"Task {task['id']}"


def _task_kind_label(task: dict) -> str:
    if task.get("kind") == "supertask":
        return "supertask"
    if task.get("parent_task_id") is not None:
        return "child task"
    return "task"


def _format_skips(skips: list[str] | None) -> str:
    """Return a stable comma-separated skips label for UI/CLI surfaces."""
    if not skips:
        return ""
    return ", ".join(skips)


def _get_task_reviewers(conn, task_id: int) -> list[str]:
    """Return distinct reviewer names from approval comments for a task."""
    rows = conn.execute(
        "SELECT DISTINCT author FROM comments "
        "WHERE task_id = ? AND kind = 'approval' AND author IS NOT NULL "
        "ORDER BY id",
        (task_id,),
    ).fetchall()
    return [r["author"] for r in rows]


def _task_reviewer(task: dict | None) -> str:
    """Return the configured code-reviewer agent for display."""
    if not task:
        return ""
    return task.get("reviewer_agent") or config.DEFAULT_REVIEWER


def _open_conn() -> sqlite3.Connection | None:
    """Return a connection to the DB, or None if the DB doesn't exist."""
    path = Path(db.get_db_path())
    if not path.exists():
        return None
    return db.connect(str(path))


def _get_prev_next_task_ids(conn, task_id: int) -> tuple[int | None, int | None]:
    """Return (prev_id, next_id) for the tasks adjacent to task_id ordered by id."""
    if conn is None:
        return None, None
    rows = conn.execute("SELECT id FROM tasks ORDER BY id").fetchall()
    ids = [r["id"] for r in rows]
    try:
        idx = ids.index(task_id)
    except ValueError:
        return None, None
    prev_id = ids[idx - 1] if idx > 0 else None
    next_id = ids[idx + 1] if idx < len(ids) - 1 else None
    return prev_id, next_id


STATUS_BADGE_CLASS = {
    "none":     "badge-none",
    "ready":    "badge-ready",
    "running":  "badge-running",
    "done":     "badge-done",
    "blocked":  "badge-blocked",
    "pending_subtasks": "badge-pending-subtasks",
    "idle":     "badge-idle",
    "starting": "badge-starting",
    "stopping": "badge-stopping",
    "stopped":  "badge-stopped",
    "hard-break": "badge-hard-break",
    "error":    "badge-error",
    "stale":    "badge-stale",
}


RUNTIME_STATUSES_WITHOUT_HEARTBEAT_STALE = {"starting", "stopping", "stopped", "hard-break"}


def _runtime_display_status(status: str, last_heartbeat: str | None) -> str:
    if _is_stale(last_heartbeat) and status not in RUNTIME_STATUSES_WITHOUT_HEARTBEAT_STALE:
        return "stale"
    return status


COMMENT_KIND_CLASS = {
    "approval":       "comment-approval",
    "rejection":      "comment-rejection",
    "commit-message": "comment-commit-msg",
    "comment":        "comment-general",
}


def _supertask_children_progress(conn, supertask_id: int) -> dict:
    """Return child-task progress details for a supertask."""
    children = db.get_child_tasks(conn, supertask_id) if conn else []
    total = len(children)
    done = sum(1 for child in children if child.get("status") == "done")
    blocked = sum(1 for child in children if child.get("status") == "blocked")
    running = sum(1 for child in children if child.get("status") == "running")
    ready = sum(1 for child in children if child.get("status") == "ready")
    return {
        "children": children,
        "total": total,
        "done": done,
        "blocked": blocked,
        "running": running,
        "ready": ready,
    }


def _child_ready_state(conn, task: dict) -> dict:
    """Describe whether a child task is actually runnable or still gated."""
    parent_id = task.get("parent_task_id")
    if parent_id is None:
        return {"bucket": "runnable", "reason": "independent task", "parent": None, "blocking_task": None}

    parent = db.get_task(conn, parent_id) if conn else None
    if parent is None:
        return {"bucket": "gated", "reason": "parent supertask missing", "parent": None, "blocking_task": None}

    if parent.get("status") != "pending_subtasks":
        step = parent.get("next_step") or ""
        if parent.get("status") == "ready" and step == "commit-make-supertask":
            reason = "waiting for supertask planning"
        elif parent.get("status") == "ready" and step == "commit-review-supertask":
            reason = "waiting for supertask plan review"
        elif parent.get("status") == "blocked":
            reason = "blocked by parent supertask"
        else:
            reason = f"waiting for parent supertask ({parent.get('status') or 'unknown'})"
        return {"bucket": "gated", "reason": reason, "parent": parent, "blocking_task": None}

    siblings = db.get_child_tasks(conn, parent_id) if conn else []
    for sibling in siblings:
        if sibling["id"] == task["id"]:
            break
        if sibling.get("status") != "done":
            return {
                "bucket": "gated",
                "reason": f"waiting for Task {sibling['id']}",
                "parent": parent,
                "blocking_task": sibling,
            }

    return {"bucket": "runnable", "reason": "runnable now", "parent": parent, "blocking_task": None}


def _ready_work_buckets(conn, current_task_id: int | None = None) -> tuple[list[dict], list[dict]]:
    """Split status=ready tasks into actually runnable work and gated child work."""
    tasks = db.list_tasks(conn, status="ready", page_size=None)
    runnable = []
    gated = []
    for task in tasks:
        if current_task_id is not None and task.get("id") == current_task_id:
            continue
        if task.get("parent_task_id") is None:
            runnable.append(task)
            continue
        state = _child_ready_state(conn, task)
        if state["bucket"] == "runnable":
            runnable.append(task)
        else:
            gated.append({**task, "_ready_state": state})
    runnable.sort(key=_ready_queue_sort_key)
    gated.sort(key=_ready_queue_sort_key)
    return runnable, gated


def _ready_queue_sort_key(task: dict) -> tuple:
    """Keep ready-task ordering aligned with orchestrator pickup order."""
    parent_id = task.get("parent_task_id")
    if parent_id is None:
        return (
            0,
            task.get("sequence_index") is None,
            task.get("sequence_index") or 0,
            task["id"],
        )
    return (
        1,
        parent_id,
        task.get("sequence_index") is None,
        task.get("sequence_index") or 0,
        task["id"],
    )


def _child_queue_state_label(conn, task: dict) -> str:
    """Return a stable user-facing queue/execution label for child tasks."""
    status = task.get("status") or ""
    if task.get("parent_task_id") is None:
        return status or "independent task"
    if status == "running":
        return "active now"
    if status in ("done", "blocked", "pending_subtasks"):
        return status
    state = _child_ready_state(conn, task)
    return state["reason"]


def _hierarchy_summary_html(task: dict, conn) -> str:
    """Render hierarchy/progress details for task detail pages."""
    if conn is None:
        return ""

    if task.get("kind") == "supertask":
        progress = _supertask_children_progress(conn, task["id"])
        total = progress["total"]
        if total == 0:
            body = '<p class="muted">No child tasks yet. This supertask is only represented by its planning record.</p>'
        else:
            rows = []
            for idx, child in enumerate(progress["children"], start=1):
                queue_state = _child_queue_state_label(conn, child)
                rows.append(
                    f"""<tr>
                      <td>{idx}</td>
                      <td><a href="/task/{_esc(child['id'])}">#{_esc(child['id'])}</a></td>
                      <td>{_esc(child.get('title') or '')}</td>
                      <td><span class="badge {STATUS_BADGE_CLASS.get(child.get('status', 'none'), 'badge-none')}">{_esc(child.get('status') or '')}</span></td>
                      <td>{_esc(child.get('next_step') or '')}</td>
                      <td class="muted">{_esc(queue_state)}</td>
                    </tr>"""
                )
            summary = (
                f"<p><strong>Children:</strong> {progress['done']}/{total} done"
                f" &nbsp; <strong>Running:</strong> {progress['running']}"
                f" &nbsp; <strong>Ready:</strong> {progress['ready']}"
                f" &nbsp; <strong>Blocked:</strong> {progress['blocked']}</p>"
            )
            body = (
                f"{summary}"
                "<table class='hierarchy-table'>"
                "<thead><tr><th class='col-count'>#</th><th class='col-id'>ID</th><th>Title</th><th class='col-agent'>Status</th><th class='col-step'>Next Step</th><th class='col-state'>Queue State</th></tr></thead>"
                f"<tbody>{''.join(rows)}</tbody></table>"
            )
        return f"""
        <div class="task-hierarchy">
          <h3 class="task-plan-heading">Hierarchy</h3>
          <p class="muted">This is a supertask. It never lands a commit; it sequences child execution.</p>
          {body}
        </div>"""

    parent_id = task.get("parent_task_id")
    if parent_id is None:
        return ""

    parent = db.get_task(conn, parent_id)
    if parent is None:
        return """
        <div class="task-hierarchy">
          <h3 class="task-plan-heading">Hierarchy</h3>
          <p class="muted">Parent supertask not found.</p>
        </div>"""

    siblings = db.get_child_tasks(conn, parent_id)
    total = len(siblings)
    position = next((i for i, sibling in enumerate(siblings, start=1) if sibling["id"] == task["id"]), None)
    queue_state = _child_queue_state_label(conn, task)

    return f"""
    <div class="task-hierarchy">
      <h3 class="task-plan-heading">Hierarchy</h3>
      <p><strong>Parent supertask:</strong> <a href="/task/{_esc(parent['id'])}">{_esc(_task_ref(parent))}</a></p>
      <p><strong>Sequence:</strong> {position or '?'} of {total}</p>
      <p><strong>Queue state:</strong> {_esc(queue_state)}</p>
    </div>"""


# ── Fragment renderers ─────────────────────────────────────────────────


def render_health_card(runtime: dict | None) -> str:
    """Render the orchestrator health card fragment."""
    if runtime is None:
        return """
        <div class="card" id="health-card">
          <h2>Orchestrator</h2>
          <p><span class="badge badge-stopped">no runtime row</span></p>
          <p class="muted">No orchestrator_runtime entry found. Start the orchestrator first.</p>
        </div>"""

    status = runtime.get("status") or "unknown"
    display_status = _runtime_display_status(status, runtime.get("last_heartbeat_at"))
    badge_cls = STATUS_BADGE_CLASS.get(display_status, "badge-none")
    hb_age = _live_age(runtime.get("last_heartbeat_at"), css_class="heartbeat-age")
    lines = [
        '<div class="card" id="health-card">',
        f'  <h2>Orchestrator <span class="badge {badge_cls}">{_esc(display_status)}</span></h2>',
        f'  <p class="muted">Heartbeat: {hb_age}</p>',
    ]

    if runtime.get("started_at"):
        lines.append(f'  <p class="muted">Started: {_absolute_timestamp_html(runtime.get("started_at"))}</p>')

    if runtime.get("status_message"):
        lines.append(f'  <p>{_esc(_display_review_round_text(runtime["status_message"]))}</p>')

    if runtime.get("current_task_id"):
        tid = runtime["current_task_id"]
        step = runtime.get("current_step") or ""
        branch = runtime.get("current_branch") or ""
        rround = runtime.get("review_round")
        lines.append(f'  <p><strong>Task:</strong> <a href="/task/{_esc(tid)}">#{_esc(tid)}</a></p>')
        if step:
            lines.append(f'  <p><strong>Step:</strong> {_esc(step)}</p>')
        if branch:
            lines.append(f'  <p><strong>Branch:</strong> <code>{_esc(branch)}</code></p>')
        if rround is not None:
            lines.append(f'  <p><strong>Review round:</strong> {_esc(rround + 1)}</p>')
        if step in ("commit-review", "commit-review-supertask", "commit-plan-review"):
            lines.append('  <p><strong>Review status:</strong> in progress</p>')

    lines.append("</div>")
    return "\n".join(lines)


_LOG_ERROR_KEYWORDS = ("error", "exception", "traceback", "critical", "fatal", "fail")
_LOG_WARN_KEYWORDS = ("warn", "warning", "deprecated", "retry", "retrying", "timeout")
_LOG_DONE_KEYWORDS = ("success", "done", "complete", "completed")


def _log_line_class(message: str | None) -> str:
    lower = (message or "").lower()
    if any(keyword in lower for keyword in _LOG_ERROR_KEYWORDS):
        return "log-row-error"
    if any(keyword in lower for keyword in _LOG_WARN_KEYWORDS):
        return "log-row-warning"
    if "picked up" in lower:
        return "log-row-picked-up"
    if any(keyword in lower for keyword in _LOG_DONE_KEYWORDS):
        return "log-row-done"
    return ""


def _log_class_attr(message: str | None, base_class: str) -> str:
    classes = [base_class]
    line_class = _log_line_class(message)
    if line_class:
        classes.append(line_class)
    return f'class="{" ".join(classes)}"'


def render_current_task_card(runtime: dict | None, conn) -> str:
    """Render the active task card (with a small run log snippet)."""
    if runtime is None or not runtime.get("current_task_id"):
        return """
        <div class="card" id="current-task-card">
          <h2>Current Task</h2>
          <p class="muted">Orchestrator is idle — no active task.</p>
        </div>"""

    tid = runtime["current_task_id"]
    task = db.get_task(conn, tid) if conn else None
    if not task:
        return f"""
        <div class="card" id="current-task-card">
          <h2>Current Task</h2>
          <p class="muted">Task #{_esc(tid)} not found in database.</p>
        </div>"""

    step = runtime.get("current_step") or task.get("next_step") or ""
    branch = task.get("branch") or ""
    coder = task.get("coder_agent") or ""
    reviewer = _task_reviewer(task)
    rround = task.get("review_round", 0)
    skips = _format_skips(task.get("skips"))
    hierarchy_bits = []
    if conn and task.get("kind") == "supertask":
        progress = _supertask_children_progress(conn, tid)
        if progress["total"] == 0:
            hierarchy_bits.append("<strong>Kind:</strong> supertask plan in progress")
            hierarchy_bits.append("<strong>Children:</strong> none yet")
        else:
            hierarchy_bits.append(
                f"<strong>Kind:</strong> supertask ({progress['done']}/{progress['total']} children done)"
            )
    elif conn and task.get("parent_task_id") is not None:
        parent = db.get_task(conn, task["parent_task_id"])
        if parent:
            siblings = db.get_child_tasks(conn, parent["id"])
            position = next((i for i, sibling in enumerate(siblings, start=1) if sibling["id"] == tid), None)
            hierarchy_bits.append(f'<strong>Supertask:</strong> <a href="/task/{_esc(parent["id"])}">{_esc(_task_ref(parent))}</a>')
            if position is not None:
                hierarchy_bits.append(f"<strong>Position:</strong> {position} of {len(siblings)}")
        hierarchy_bits.append(f"<strong>Queue state:</strong> {_esc(_child_queue_state_label(conn, task))}")
    else:
        hierarchy_bits.append(f"<strong>Kind:</strong> {_esc(_task_kind_label(task))}")

    hierarchy_html = ""
    if hierarchy_bits:
        hierarchy_html = f"<p>{' &nbsp; '.join(hierarchy_bits)}</p>"

    # Fetch last 10 run log entries
    run_log_rows = []
    if conn:
        rows = conn.execute(
            "SELECT message, created_at FROM run_log WHERE task_id = ? ORDER BY id DESC LIMIT 10",
            (tid,),
        ).fetchall()
        run_log_rows = list(reversed(rows))

    log_html = ""
    if run_log_rows:
        entries = "\n".join(
            f'<div {_log_class_attr(r["message"], "log-row")}><span class="log-ts">{_absolute_timestamp_html(r["created_at"], css_class="log-timestamp")}</span> {_esc(r["message"] or "")}</div>'
            for r in run_log_rows
        )
        log_html = f'<div class="log-snippet" data-stick-to-bottom="true">{entries}</div>'
    else:
        log_html = '<p class="muted">No run log entries yet.</p>'

    return f"""
    <div class="card" id="current-task-card">
      <h2>Current Task</h2>
      <p><strong><a href="/task/{_esc(tid)}">{_esc(_task_ref(task))}</a></strong></p>
      <p><strong>Branch:</strong> <code>{_esc(branch)}</code> &nbsp;
         <strong>Coder:</strong> {_esc(coder)} &nbsp;
         <strong>Reviewer:</strong> {_esc(reviewer)} &nbsp;
         <strong>Step:</strong> {_esc(step)} &nbsp;
         <strong>Review round:</strong> {_esc(rround + 1)}</p>
      {f'<p><strong>Skips:</strong> {_esc(skips)}</p>' if skips else ''}
      {hierarchy_html}
      <h3>Recent Run Log</h3>
      {log_html}
    </div>"""


def render_ready_queue(conn, runtime: dict | None = None) -> str:
    """Render ready work, separating runnable tasks from gated child tasks."""
    if conn is None:
        return '<div class="card" id="ready-queue"><h2>Ready Work</h2><p class="muted">Database not available.</p></div>'
    current_task_id = runtime.get("current_task_id") if runtime else None
    runnable, gated = _ready_work_buckets(conn, current_task_id=current_task_id)
    if not runnable and not gated:
        return '<div class="card" id="ready-queue"><h2>Ready Work</h2><p class="muted">No ready work.</p></div>'

    runnable_html = '<p class="muted">No tasks are currently runnable.</p>'
    if runnable:
        rows = "\n".join(
            f"""<tr>
              <td><a href="/task/{_esc(t['id'])}">#{_esc(t['id'])}</a></td>
              <td>{_kind_glyph(t) and f'<span class="kind-glyph">{_kind_glyph(t)}</span> ' or ''}{_esc(t['title'])}</td>
              <td><code>{_esc(t['branch'] or '')}</code></td>
              <td>{_esc(t['coder_agent'] or '')}</td>
              <td>{_esc(_task_reviewer(t))}</td>
              <td>{_esc(t['next_step'] or '')}</td>
              <td>{_esc(_format_skips(t.get('skips'))) or '<span class="muted">-</span>'}</td>
            </tr>"""
            for t in runnable
        )
        runnable_html = (
            "<table>"
            "<thead><tr><th class='col-id'>ID</th><th>Title</th><th class='col-branch'>Branch</th><th class='col-agent'>Coder</th><th class='col-agent'>Reviewer</th><th class='col-step'>Next Step</th><th class='col-skips'>Skips</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )

    gated_html = ""
    if gated:
        rows = "\n".join(
            f"""<tr>
              <td><a href="/task/{_esc(t['id'])}">#{_esc(t['id'])}</a></td>
              <td>{_kind_glyph(t) and f'<span class="kind-glyph">{_kind_glyph(t)}</span> ' or ''}{_esc(t['title'])}</td>
              <td>{f'<a href="/task/{_esc(t["_ready_state"]["parent"]["id"])}">#{_esc(t["_ready_state"]["parent"]["id"])}</a>' if t["_ready_state"].get("parent") else '<span class="muted">missing</span>'}</td>
              <td><code>{_esc(t['branch'] or '')}</code></td>
              <td class="muted">{_esc(t['_ready_state']['reason'])}</td>
              <td>{_esc(_format_skips(t.get('skips'))) or '<span class="muted">-</span>'}</td>
            </tr>"""
            for t in gated
        )
        gated_html = (
            "<table>"
            "<thead><tr><th class='col-id'>ID</th><th>Title</th><th class='col-id'>Supertask</th><th class='col-branch'>Branch</th><th>Why Not Yet</th><th class='col-skips'>Skips</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )

    return f"""
    <div class="card" id="ready-queue">
      <h2>Ready Work</h2>
      <h3>Runnable Now</h3>
      {runnable_html}
      {f'<h3>Queued Behind Supertask</h3>{gated_html}' if gated_html else ''}
    </div>"""


def render_active_supertasks(conn) -> str:
    """Render supertasks currently holding the queue via pending_subtasks."""
    if conn is None:
        return '<div class="card" id="active-supertasks"><h2>Active Supertasks</h2><p class="muted">Database not available.</p></div>'

    tasks = db.list_tasks(conn, status="pending_subtasks", page_size=None)
    if not tasks:
        return '<div class="card" id="active-supertasks"><h2>Active Supertasks</h2><p class="muted">No active supertasks.</p></div>'

    rows_html = []
    for task in tasks:
        progress = _supertask_children_progress(conn, task["id"])
        rows_html.append(f"""<tr>
          <td><a href="/task/{_esc(task['id'])}">#{_esc(task['id'])}</a></td>
          <td>{_kind_glyph(task) and f'<span class="kind-glyph">{_kind_glyph(task)}</span> ' or ''}{_esc(task['title'])}</td>
          <td><code>{_esc(task.get('branch') or '')}</code></td>
          <td>{_esc(progress['done'])}/{_esc(progress['total'])}</td>
          <td>{_esc(progress['ready'])}</td>
          <td>{_esc(progress['running'])}</td>
          <td>{_esc(progress['blocked'])}</td>
          <td>{_esc(_format_skips(task.get('skips'))) or '<span class="muted">-</span>'}</td>
        </tr>""")

    return f"""
    <div class="card" id="active-supertasks">
      <h2>Active Supertasks</h2>
      <table>
        <thead><tr><th class='col-id'>ID</th><th>Title</th><th class='col-branch'>Branch</th><th class='col-count'>Done</th><th class='col-count'>Ready</th><th class='col-count'>Running</th><th class='col-count'>Blocked</th><th class='col-skips'>Skips</th></tr></thead>
        <tbody>{"".join(rows_html)}</tbody>
      </table>
    </div>"""


def render_icebox(conn) -> str:
    """Render the parked backlog fragment for status='none' tasks."""
    if conn is None:
        return '<div class="card" id="icebox"><h2>Icebox</h2><p class="muted">Database not available.</p></div>'
    tasks = db.list_tasks(conn, status="none", page_size=None)
    if not tasks:
        return '<div class="card" id="icebox"><h2>Icebox</h2><p class="muted">No parked tasks.</p></div>'

    rows = "\n".join(
        f"""<tr>
          <td><a href="/task/{_esc(t['id'])}">#{_esc(t['id'])}</a></td>
          <td>{_kind_glyph(t) and f'<span class="kind-glyph">{_kind_glyph(t)}</span> ' or ''}{_esc(t['title'])}</td>
          <td><code>{_esc(t['branch'] or '')}</code></td>
          <td>{_esc(t['coder_agent'] or '')}</td>
          <td>{_esc(_task_reviewer(t))}</td>
          <td class="muted">{_esc(_age(t.get('updated_at') or t.get('created_at')))}</td>
        </tr>"""
        for t in tasks
    )
    return f"""
    <div class="card" id="icebox">
      <h2>Icebox</h2>
      <table>
        <thead><tr><th class='col-id'>ID</th><th>Title</th><th class='col-branch'>Branch</th><th class='col-agent'>Coder</th><th class='col-agent'>Reviewer</th><th class='col-age'>Updated</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def render_blocked_tasks(conn) -> str:
    """Render the blocked tasks fragment."""
    if conn is None:
        return '<div class="card" id="blocked-tasks"><h2>Blocked Tasks</h2><p class="muted">Database not available.</p></div>'
    tasks = db.list_tasks(conn, status="blocked", page_size=None)
    if not tasks:
        return '<div class="card" id="blocked-tasks"><h2>Blocked Tasks</h2><p class="muted">No blocked tasks.</p></div>'

    rows_html = []
    for t in tasks:
        # Fetch most recent comment for blocker summary
        last_comment = conn.execute(
            "SELECT message FROM comments WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (t["id"],),
        ).fetchone()
        summary = last_comment["message"] if last_comment else "No comments."
        # Truncate long summaries
        if len(summary) > 120:
            summary = summary[:117] + "..."
        age = _age(t.get("updated_at") or t.get("created_at"))
        rows_html.append(f"""<tr>
          <td><a href="/task/{_esc(t['id'])}">#{_esc(t['id'])}</a></td>
          <td>{_kind_glyph(t) and f'<span class="kind-glyph">{_kind_glyph(t)}</span> ' or ''}{_esc(t['title'])}</td>
          <td><code>{_esc(t['branch'] or '')}</code></td>
          <td>{_esc(t.get('coder_agent') or '') or '<span class="muted">-</span>'}</td>
          <td>{_esc(_task_reviewer(t))}</td>
          <td class="muted">{_esc(age)}</td>
          <td class="muted">{_esc(summary)}</td>
          <td>{_esc(_format_skips(t.get('skips'))) or '<span class="muted">-</span>'}</td>
        </tr>""")

    return f"""
    <div class="card" id="blocked-tasks">
      <h2>Blocked Tasks</h2>
      <table>
        <thead><tr><th class='col-id'>ID</th><th>Title</th><th class='col-branch'>Branch</th><th class='col-agent'>Coder</th><th class='col-agent'>Reviewer</th><th class='col-age'>Updated</th><th>Last Note</th><th class='col-skips'>Skips</th></tr></thead>
        <tbody>{"".join(rows_html)}</tbody>
      </table>
    </div>"""


def render_recently_done(conn) -> str:
    """Render the recently done tasks fragment with incremental reveal."""
    if conn is None:
        return '<div class="card" id="recently-done"><h2>Recently Done</h2><p class="muted">Database not available.</p></div>'
    rows_raw = [dict(r) for r in conn.execute(
        "SELECT id, title, branch, commit_hash, kind, parent_task_id, coder_agent, reviewer_agent FROM tasks "
        "WHERE status = 'done' ORDER BY updated_at DESC, id DESC"
    ).fetchall()]
    for row in rows_raw:
        row["reviewers"] = _get_task_reviewers(conn, row["id"])
    if not rows_raw:
        return '<div class="card" id="recently-done"><h2>Recently Done</h2><p class="muted">No completed tasks yet.</p></div>'

    initial_visible = 5
    increment = 10
    rows = "\n".join(
        f"""<tr data-show-more-row data-row-index="{idx}"{" hidden" if idx >= initial_visible else ""}>
          <td><a href="/task/{_esc(r['id'])}">#{_esc(r['id'])}</a></td>
          <td>{_kind_glyph(r) and f'<span class="kind-glyph">{_kind_glyph(r)}</span> ' or ''}{_esc(r['title'])}</td>
          <td><code>{_esc(r['branch'] or '')}</code></td>
          <td>{f'<code>{_esc(_short_hash(r["commit_hash"]))}</code>' if r['commit_hash'] else '-'}</td>
          <td>{_esc(r.get('coder_agent') or '') or '<span class="muted">-</span>'}</td>
          <td>{_esc(_task_reviewer(r))}</td>
          <td>{_esc(", ".join(r.get('reviewers') or [])) or '<span class="muted">-</span>'}</td>
        </tr>"""
        for idx, r in enumerate(rows_raw)
    )
    total_rows = len(rows_raw)
    controls_html = ""
    if total_rows > initial_visible:
        controls_html = (
            f'<div class="show-more-controls" data-show-more-controls>'
            f'<button type="button" class="button-secondary show-more-button" '
            f'data-show-more-button>Show More</button>'
            f'<span class="muted show-more-summary" data-show-more-summary>'
            f'Showing {initial_visible} of {total_rows}'
            f'</span>'
            f"</div>"
        )
    return f"""
    <div class="card" id="recently-done" data-show-more-root data-initial-visible="{initial_visible}" data-visible-count="{initial_visible}" data-increment="{increment}" data-total-rows="{total_rows}">
      <h2>Recently Done</h2>
      <table>
        <thead><tr><th class='col-id'>ID</th><th>Title</th><th class='col-branch'>Branch</th><th class='col-commit'>Commit</th><th class='col-agent'>Coder</th><th class='col-agent'>Configured Reviewer</th><th class='col-agent'>Approver</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      {controls_html}
    </div>"""


def _render_commit_plan(plan: str | None) -> str:
    if not plan:
        return ""
    return (
        "<div class='task-plan'>"
        "<h3 class='task-plan-heading'>Plan</h3>"
        f"<div class='task-plan-body'>{_md_html(plan)}</div>"
        "</div>"
    )


def render_task_header(task: dict, conn=None, edit_error: str | None = None, *, prev_id: int | None = None, next_id: int | None = None) -> str:
    """Render the task detail header with inline title/description editing."""
    status_cls = STATUS_BADGE_CLASS.get(task.get("status", "none"), "badge-none")
    kind_glyph = _kind_glyph(task)
    supertask_badge = f'<span class="kind-glyph kind-glyph-header">{kind_glyph}</span>' if kind_glyph else ""
    tid = task["id"]
    title = task.get("title") or ""
    description_source = task.get("description") or ""
    skips = _format_skips(task.get("skips"))
    title_editor_id = f"task-title-editor-{tid}"
    description_editor_id = f"task-description-editor-{tid}"
    title_display_attr = " hidden" if edit_error else ""
    title_form_hidden = "" if edit_error else " hidden"
    description_html = (
        "<div class='task-description-body'>"
        f"{_md_html(description_source)}"
        "</div>"
        if description_source
        else "<p class='muted empty-description'>Add a description.</p>"
    )
    error_html = f'<p class="form-error">{_esc(edit_error)}</p>' if edit_error else ""
    prev_btn = f'<a class="task-nav-btn" href="/task/{_esc(prev_id)}">&#8592; #{_esc(prev_id)}</a>' if prev_id is not None else '<span></span>'
    next_btn = f'<a class="task-nav-btn" href="/task/{_esc(next_id)}">#{_esc(next_id)} &#8594;</a>' if next_id is not None else '<span></span>'
    return f"""
    <div class="card" id="task-header">
      <div class="task-nav-row">{prev_btn}{next_btn}</div>
      <div class="inline-edit-block title-inline-edit" id="{title_editor_id}">
        <div class="inline-edit-display task-title-display" data-inline-display{title_display_attr}>
          <h2 class="inline-edit-heading" role="button" tabindex="0" onclick="activateInlineEditor('{title_editor_id}')" onkeydown="handleInlineEditorKey(event, '{title_editor_id}')">
            <span class="badge {status_cls}">{_esc(task.get('status', ''))}</span>
            {supertask_badge}
            {_esc(title)}
          </h2>
        </div>
        <form class="edit-form inline-edit-form title-edit-form"{title_form_hidden} data-inline-form action="/task/{tid}/edit" method="post">
          <label class="field-label" for="task-title">Title</label>
          <input class="text-input inline-title-input" id="task-title" name="title" type="text" value="{_esc(title)}" required>
          <textarea class="preserve-field" name="description" aria-hidden="true" tabindex="-1">{_esc(description_source)}</textarea>
          {error_html}
          <div class="edit-actions inline-edit-actions">
            <button type="submit">Save</button>
            <button type="button" class="button-secondary" onclick="cancelInlineEditor('{title_editor_id}')">Cancel</button>
          </div>
        </form>
      </div>
      <p class="muted task-ref">{_esc(_task_ref(task))}</p>
      <div class="inline-edit-block description-inline-edit" id="{description_editor_id}">
        <div class="task-description inline-edit-display" data-inline-display role="button" tabindex="0" onclick="activateInlineEditor('{description_editor_id}')" onkeydown="handleInlineEditorKey(event, '{description_editor_id}')">
          {description_html}
        </div>
        <form class="edit-form inline-edit-form description-edit-form" hidden data-inline-form action="/task/{tid}/edit" method="post">
          <input type="hidden" name="title" value="{_esc(title)}">
          <label class="field-label" for="task-description">Description Source (Markdown)</label>
          <textarea class="text-area" id="task-description" name="description" rows="12" spellcheck="false">{_esc(description_source)}</textarea>
          <div class="edit-actions inline-edit-actions">
            <button type="submit">Save</button>
            <button type="button" class="button-secondary" onclick="cancelInlineEditor('{description_editor_id}')">Cancel</button>
          </div>
        </form>
      </div>
      <table class="meta-table">
        <tr><th>Branch</th><td><code>{_esc(task.get('branch') or 'unset')}</code></td>
            <th>Coder</th><td>{_esc(task.get('coder_agent') or '')}</td></tr>
        <tr><th>Reviewer</th><td>{_esc(_task_reviewer(task))}</td>
            <th>Next step</th><td>{_esc(task.get('next_step', ''))}</td></tr>
        <tr><th>Review round</th><td>{_esc(task.get('review_round', 0) + 1)}</td>
            <th>Last decision</th><td>{_esc(task.get('last_review_decision', ''))}</td></tr>
        <tr><th>Skips</th><td>{_esc(skips) or '-'}</td>
            <th>Commit</th><td>{f'<code>{_esc(_short_hash(task.get("commit_hash")))}</code>' if task.get('commit_hash') else '-'}</td></tr>
        <tr><th>Created</th><td>{_absolute_timestamp_html(task.get('created_at'))}</td>
            <th>Updated</th><td>{_absolute_timestamp_html(task.get('updated_at'))}</td></tr>
      </table>
      {_hierarchy_summary_html(task, conn)}
      {_render_commit_plan(task.get('commit_plan'))}
    </div>"""


def render_task_runtime_panel(task: dict, runtime: dict | None) -> str:
    """Render the live runtime panel on the task detail page."""
    tid = task["id"]
    if runtime is None or runtime.get("current_task_id") != tid:
        status = task.get("status", "")
        status_cls = STATUS_BADGE_CLASS.get(status, "badge-none")
        return f"""
        <div class="card" id="task-runtime-panel">
          <h2>Current State</h2>
          <p><span class="badge {status_cls}">{_esc(status)}</span> This task is not the active task.</p>
        </div>"""

    step = runtime.get("current_step") or ""
    msg = _display_review_round_text(runtime.get("status_message"))
    hb = _live_age(runtime.get("last_heartbeat_at"), css_class="heartbeat-age")
    health = _runtime_display_status(runtime.get("status", ""), runtime.get("last_heartbeat_at"))
    badge_cls = STATUS_BADGE_CLASS.get(health, "badge-running")

    review_html = ""
    if step in ("commit-review", "commit-review-supertask", "commit-plan-review"):
        review_html = '<p><strong>Review status:</strong> in progress</p>'

    return f"""
    <div class="card" id="task-runtime-panel">
      <h2>Current State <span class="badge {badge_cls}">{_esc(health)}</span></h2>
      <p><strong>Step:</strong> {_esc(step)}</p>
      {review_html}
      {"<p>" + _esc(msg) + "</p>" if msg else ""}
      <p class="muted">Heartbeat: {hb}</p>
    </div>"""


def render_comments_panel(task_id: int, conn) -> str:
    """Render the permanent comments panel."""
    comments = db.get_comments(conn, task_id)
    if not comments:
        return '<div class="card" id="comments-panel"><h2>Comments</h2><p class="muted">No comments yet.</p></div>'

    entries = []
    for c in comments:
        kind = c.get("kind", "comment")
        cls = COMMENT_KIND_CLASS.get(kind, "comment-general")
        author = c.get("author") or "—"
        verb = c.get("verb") or ""
        rr = c.get("review_round")
        meta_parts = [f"Round {rr + 1}" if rr is not None else None, verb or None]
        meta = " · ".join(p for p in meta_parts if p)
        entries.append(f"""
        <div class="comment {cls}">
          <div class="comment-meta">
            <span class="comment-kind">{_esc(kind)}</span>
            <span class="comment-author">{_esc(author)}</span>
            {f'<span class="muted">{_esc(meta)}</span>' if meta else ""}
            {_absolute_timestamp_html(c.get("created_at"), css_class="comment-timestamp")}
          </div>
          <pre class="comment-body">{_esc(_display_review_round_text(c.get('message')))}</pre>
        </div>""")

    return f"""
    <div class="card" id="comments-panel">
      <h2>Comments</h2>
      {"".join(entries)}
    </div>"""


def render_run_log_panel(task_id: int, conn) -> str:
    """Render the run log panel."""
    run_log = db.get_run_log(conn, task_id)
    if not run_log:
        return '<div class="card" id="run-log-panel"><h2>Run Log</h2><p class="muted">No run log entries.</p></div>'

    entries = "\n".join(
        f'<div {_log_class_attr(r.get("message"), "log-row")}><span class="log-ts">{_absolute_timestamp_html(r.get("created_at"), css_class="log-timestamp")}</span>'
        f'<span class="log-author">{_esc(r.get("author") or "")}</span>'
        f' {_esc(r.get("message") or "")}</div>'
        for r in reversed(run_log)
    )
    return f"""
    <div class="card" id="run-log-panel">
      <h2>Run Log</h2>
      <div class="run-log" data-stick-to-bottom="true">{entries}</div>
    </div>"""


def _connection_db_path(conn) -> str | None:
    """Return the backing SQLite path for an open connection."""
    if conn is None:
        return None
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None:
        return None
    return row["file"] if hasattr(row, "keys") else row[2]


def _tail_text_file(path: Path, limit: int = 200) -> list[str]:
    """Return the last limit lines from a text file."""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return list(deque((line.rstrip("\n") for line in handle), maxlen=limit))


def _slice_from_last_orchestrator_start(lines: list[str]) -> list[str]:
    """Return only the most recent orchestrator run from a tailed log."""
    start_marker = "Kanban Orchestra started. Polling for ready tasks..."
    for index in range(len(lines) - 1, -1, -1):
        if start_marker in lines[index]:
            return lines[index:]
    return lines


def render_global_run_log_panel(conn) -> str:
    """Render the persisted orchestrator stdout tail."""
    log_path = db.get_orchestrator_log_path(_connection_db_path(conn))
    lines = _tail_text_file(log_path, limit=200)
    lines = _slice_from_last_orchestrator_start(lines)
    if not lines:
        return '<div class="card" id="global-run-log-panel"><h2>Orchestrator Output</h2><p class="muted">No orchestrator output yet.</p></div>'

    entries = "\n".join(f'<div {_log_class_attr(line, "log-output-line")}>{_esc(line)}</div>' for line in lines)
    return f"""
    <div class="card" id="global-run-log-panel">
      <h2>Orchestrator Output</h2>
      <div class="run-log" data-stick-to-bottom="true">{entries}</div>
    </div>"""


# ── CSS + Layout ───────────────────────────────────────────────────────

COMMON_CSS = """
:root {
  --bg: #000000;
  --panel: #0c0c0c;
  --ink: #d0d0d0;
  --muted: #585858;
  --accent: #00cc44;
  --accent-dim: #007a28;
  --border: #222222;
  --green: #00cc44;
  --red: #ff4444;
  --blue: #4499ff;
  --orange: #ff8800;
}

* { box-sizing: border-box; }
[hidden] { display: none !important; }

body {
  margin: 0;
  font-family: "Menlo", "Monaco", "Courier New", monospace;
  background: var(--bg);
  color: var(--ink);
  font-size: 13.5px;
  line-height: 1.55;
}

main {
  max-width: 900px;
  margin: 0 auto;
  padding: 24px 20px 64px;
}

nav {
  background: #050505;
  border-bottom: 1px solid var(--accent);
  padding: 8px 20px;
  display: flex;
  align-items: center;
  gap: 16px;
}

nav > a:first-child::before {
  content: "$ ";
  color: var(--muted);
}

nav a {
  font-family: "Menlo", "Monaco", "Courier New", monospace;
  color: var(--accent);
  text-decoration: none;
  font-weight: bold;
}

nav a:hover { color: #ffffff; text-decoration: none; }

h1 { margin: 0 0 8px; font-size: 1.6rem; color: var(--accent); text-shadow: 0 0 18px rgba(0,204,68,0.28); }
h2 { margin: 0 0 12px; font-size: 1.1rem; border-bottom: 1px solid var(--border); padding-bottom: 6px; color: var(--accent); }
h3 { margin: 12px 0 6px; font-size: 0.85rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
p  { margin: 4px 0 8px; }

.lede { color: var(--muted); margin: 0 0 20px; font-size: 0.88rem; }

.card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: 2px;
  padding: 18px 20px;
  margin-bottom: 16px;
}

a { color: var(--accent); }
a:hover { color: #ffffff; text-decoration: none; }

code {
  font-family: "Menlo", "Monaco", "Courier New", monospace;
  font-size: 0.9em;
  background: #1a1a1a;
  border-radius: 2px;
  padding: 1px 4px;
  color: #88c0d0;
}

.task-description {
  margin: 8px 0 12px;
  line-height: 1.6;
}

.inline-edit-block {
  margin-bottom: 10px;
}

.inline-edit-display {
  border-radius: 2px;
  transition: background-color 140ms ease, box-shadow 140ms ease;
}

.inline-edit-display:hover,
.inline-edit-display:focus,
.inline-edit-display:focus-within {
  background: rgba(0, 204, 68, 0.07);
  box-shadow: inset 0 0 0 1px rgba(0, 204, 68, 0.25);
  outline: none;
}

.inline-edit-heading {
  cursor: pointer;
}

.task-title-display {
  border-radius: 2px;
}

.task-title-display h2 {
  margin-bottom: 0;
}

.task-description.inline-edit-display {
  cursor: pointer;
  padding: 10px 12px;
}

.task-description-body > :first-child {
  margin-top: 0;
}

.empty-description {
  margin: 0;
}

.task-description p { margin: 4px 0 8px; }
.task-description ul, .task-description ol { margin: 4px 0 8px; padding-left: 24px; }
.task-description code { background: #1a1a1a; color: #88c0d0; border-radius: 2px; padding: 1px 4px; }

.task-plan { margin-top: 16px; border-top: 1px solid var(--border); padding-top: 12px; }
.task-hierarchy { margin-top: 16px; border-top: 1px solid var(--border); padding-top: 12px; }
.task-plan-heading { font-size: 0.78em; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin: 0 0 8px; }
.task-plan-body p { margin: 4px 0 8px; }
.task-plan-body ul, .task-plan-body ol { margin: 4px 0 8px; padding-left: 24px; }
.task-plan-body code { background: #1a1a1a; color: #88c0d0; border-radius: 2px; padding: 1px 4px; }

.edit-form {
  display: grid;
  gap: 10px;
}

.field-label {
  font-weight: 600;
  color: var(--muted);
  font-size: 0.85em;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.text-input,
.text-area {
  width: 100%;
  border: 1px solid var(--border);
  border-radius: 2px;
  padding: 10px 12px;
  background: #111111;
  color: var(--ink);
  font: inherit;
}

.text-input:focus,
.text-area:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 1px var(--accent);
}

.text-area {
  min-height: 220px;
  font-family: "Menlo", "Monaco", "Courier New", monospace;
  font-size: 0.88em;
  line-height: 1.5;
  resize: vertical;
}

.edit-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

.edit-actions button {
  border: 1px solid var(--accent);
  background: var(--accent);
  color: #000000;
  border-radius: 2px;
  padding: 8px 14px;
  font: inherit;
  font-weight: 700;
  cursor: pointer;
  text-transform: uppercase;
  font-size: 0.82em;
  letter-spacing: 0.04em;
}

.edit-actions button:hover {
  background: #ffffff;
  border-color: #ffffff;
  color: #000000;
}

.button-secondary {
  background: transparent !important;
  color: var(--muted) !important;
  border-color: var(--border) !important;
}

.button-secondary:hover {
  color: var(--ink) !important;
  border-color: var(--ink) !important;
}

.show-more-controls {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-top: 12px;
  flex-wrap: wrap;
}

.show-more-button {
  border: 1px solid var(--border);
  border-radius: 2px;
  padding: 7px 12px;
  font: inherit;
  font-weight: 600;
  cursor: pointer;
  background: transparent;
  color: var(--muted);
  text-transform: uppercase;
  font-size: 0.78em;
  letter-spacing: 0.04em;
}

.show-more-button:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.preserve-field {
  display: none;
}

.form-error {
  color: var(--red);
  font-weight: 600;
}

pre.comment-body {
  font-family: "Menlo", "Monaco", "Courier New", monospace;
  font-size: 0.82em;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 4px 0 0;
  background: #111111;
  border-radius: 2px;
  padding: 8px 10px;
  border-left: 3px solid var(--border);
  color: var(--ink);
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.88rem;
}

th, td {
  text-align: left;
  padding: 5px 8px;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}

th { color: var(--muted); font-weight: normal; text-transform: uppercase; font-size: 0.76em; letter-spacing: 0.05em; }

.meta-table th { width: 100px; }

/* Data table column width classes */
#recently-done table,
#ready-queue table,
#active-supertasks table,
#icebox table,
#blocked-tasks table,
.hierarchy-table { table-layout: fixed; }

.col-id     { width: 46px; }
.col-branch { width: 108px; }
.col-commit { width: 72px; }
.col-agent  { width: 84px; }
.col-step   { width: 126px; }
.col-age    { width: 72px; }
.col-skips  { width: 72px; }
.col-count  { width: 52px; }
.col-state  { width: 114px; }

.muted { color: var(--muted); font-size: 0.88em; }
.task-ref  { font-family: monospace; font-size: 0.82em; }

/* Status badges */
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 2px;
  font-size: 0.75rem;
  font-family: "Menlo", "Monaco", monospace;
  font-weight: 700;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}

.badge-none     { background: #1a1a1a; color: #555555; border: 1px solid #333333; }
.badge-ready    { background: #001a0a; color: var(--green); border: 1px solid var(--green); }
.badge-running  { background: #001020; color: var(--blue); border: 1px solid var(--blue); }
.badge-done     { background: #002210; color: #66ffaa; border: 1px solid var(--green); }
.badge-blocked  { background: #1a0000; color: var(--red); border: 1px solid var(--red); }
.badge-pending-subtasks { background: #1a0f00; color: var(--orange); border: 1px solid var(--orange); }
.badge-idle     { background: #1a1a1a; color: var(--muted); border: 1px solid #333333; }
.badge-starting { background: #001020; color: var(--blue); border: 1px solid var(--blue); }
.badge-stopping { background: #1a0d00; color: var(--orange); border: 1px solid var(--orange); }
.badge-stopped  { background: #1a1a1a; color: #555555; border: 1px solid #333333; }
.badge-hard-break { background: #1a0d00; color: var(--orange); border: 1px solid var(--orange); }
.badge-error    { background: #1a0000; color: var(--red); border: 1px solid var(--red); }
.badge-stale    { background: #1a0000; color: var(--red); border: 1px solid var(--red); }
.kind-glyph { font-size: 0.85em; opacity: 0.6; }
.kind-glyph-header { font-size: 1em; opacity: 0.75; }

/* Comments */
.comment {
  padding: 10px 12px;
  border-radius: 2px;
  margin-bottom: 10px;
  border-left: 4px solid var(--border);
  background: #0f0f0f;
}

.comment-general    { border-left-color: var(--border); }
.comment-approval   { border-left-color: var(--green); background: #050f08; }
.comment-rejection  { border-left-color: var(--red); background: #0f0505; }
.comment-commit-msg { border-left-color: var(--blue); background: #050a12; }

.comment-meta {
  font-size: 0.78em;
  color: var(--muted);
  margin-bottom: 4px;
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: baseline;
}

.comment-kind   { font-weight: bold; text-transform: uppercase; font-size: 0.72em; letter-spacing: 0.07em; }
.comment-author { color: var(--accent); }

.task-nav-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  min-height: 32px;
}

.task-nav-btn {
  border: 1px solid var(--border);
  border-radius: 2px;
  padding: 5px 14px;
  font: inherit;
  font-size: 0.82em;
  background: transparent;
  color: var(--muted);
  text-decoration: none;
  display: inline-block;
  line-height: 1.4;
}

.task-nav-btn:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.timestamp-absolute { font-weight: 600; }

/* Run log */
.run-log, .log-snippet {
  font-family: "Menlo", "Monaco", monospace;
  font-size: 0.78em;
  background: #050505;
  color: #a0a0a0;
  border-radius: 2px;
  padding: 10px 12px;
  max-height: 360px;
  overflow-y: auto;
  line-height: 1.6;
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent-dim);
}

.log-snippet { max-height: 200px; }

.log-row { display: flex; gap: 8px; align-items: flex-start; }
.log-ts { color: var(--muted); flex-shrink: 0; min-width: 210px; }
.log-author { color: #5588cc; flex-shrink: 0; }
.log-output-line { white-space: pre-wrap; word-break: break-word; }
.log-row-error, .log-output-line.log-row-error { color: var(--red); }
.log-row-warning, .log-output-line.log-row-warning { color: var(--orange); }
.log-row-picked-up, .log-output-line.log-row-picked-up { color: var(--blue); }
.log-row-done, .log-output-line.log-row-done { color: var(--green); }
.run-log .timestamp-absolute, .log-snippet .timestamp-absolute { color: #a0a0a0; }
"""


def _page_shell(title: str, body: str, nav_extra: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_esc(title)}</title>
  <link rel="icon" type="image/x-icon" href="/favicon.ico">
  <style>{COMMON_CSS}</style>
</head>
<body>
  <nav>
    <a href="/">Kanban Orchestra</a>
    {nav_extra}
  </nav>
  <main>
    {body}
  </main>
  <script>
    (() => {{
      function formatRelativeAge(timestamp) {{
        const then = Date.parse(timestamp);
        if (Number.isNaN(then)) return null;

        let secs = Math.floor((Date.now() - then) / 1000);
        if (secs < 0) secs = 0;
        if (secs < 60) return `${{secs}}s ago`;
        if (secs < 3600) return `${{Math.floor(secs / 60)}}m ago`;
        if (secs < 86400) return `${{Math.floor(secs / 3600)}}h ago`;
        return `${{Math.floor(secs / 86400)}}d ago`;
      }}

      function updateRelativeTimes(root = document) {{
        root.querySelectorAll("[data-relative-time]").forEach((el) => {{
          const nextText = formatRelativeAge(el.dataset.timestamp);
          if (nextText) el.textContent = nextText;
        }});
      }}

      function hydrateShowMore(root = document) {{
        const cards = root.matches?.("[data-show-more-root]")
          ? [root]
          : Array.from(root.querySelectorAll("[data-show-more-root]"));
        cards.forEach((card) => {{
          const rows = Array.from(card.querySelectorAll("[data-show-more-row]"));
          if (!rows.length) return;

          const initialVisible = Number(card.dataset.initialVisible || "5");
          const requestedVisible = Number(card.dataset.visibleCount || String(initialVisible));
          const totalRows = Number(card.dataset.totalRows || rows.length);
          const visibleCount = Math.max(0, Math.min(requestedVisible, totalRows));

          rows.forEach((row, index) => {{
            const showRow = index < visibleCount;
            row.hidden = !showRow;
            row.style.display = showRow ? "" : "none";
          }});

          const controls = card.querySelector("[data-show-more-controls]");
          const summary = card.querySelector("[data-show-more-summary]");
          if (summary) {{
            summary.textContent = `Showing ${{Math.min(visibleCount, totalRows)}} of ${{totalRows}}`;
          }}
          if (controls) {{
            controls.hidden = visibleCount >= totalRows;
          }}

          window.__recentlyDoneVisibleCount = visibleCount;
        }});
      }}

      function stickLogsToBottom(root = document) {{
        const logs = root.matches?.("[data-stick-to-bottom]")
          ? [root]
          : Array.from(root.querySelectorAll("[data-stick-to-bottom]"));
        logs.forEach((log) => {{
          log.scrollTop = log.scrollHeight;
        }});
      }}

      window.showMoreRows = function(button) {{
        const card = button.closest("[data-show-more-root]");
        if (!card) return;
        const currentVisible = Number(card.dataset.visibleCount || "5");
        const increment = Number(card.dataset.increment || "10");
        const totalRows = Number(card.dataset.totalRows || "0");
        const nextVisible = Math.min(currentVisible + increment, totalRows);
        card.dataset.visibleCount = String(nextVisible);
        window.__recentlyDoneVisibleCount = nextVisible;
        hydrateShowMore(card);
      }};
      window.hydrateShowMore = hydrateShowMore;
      window.stickLogsToBottom = stickLogsToBottom;
      document.addEventListener("click", (event) => {{
        const button = event.target.closest("[data-show-more-button]");
        if (!button) return;
        event.preventDefault();
        window.showMoreRows(button);
      }});

      updateRelativeTimes();
      hydrateShowMore();
      stickLogsToBottom();
      window.setInterval(updateRelativeTimes, 1000);
    }})();
  </script>
</body>
</html>"""


def _task_detail_response(
    task_id: int,
    conn,
    *,
    form_task: dict | None = None,
    edit_error: str | None = None,
    status_code: int = 200,
):
    task = db.get_task(conn, task_id)
    if task is None:
        body = f'<div class="card"><p class="muted">Task #{task_id} not found.</p></div>'
        return HTMLResponse(_page_shell("Task Not Found", body), status_code=404)

    runtime = db.get_runtime(conn)
    prev_id, next_id = _get_prev_next_task_ids(conn, task_id)
    header_html = render_task_header(form_task or task, conn, edit_error=edit_error, prev_id=prev_id, next_id=next_id)
    runtime_html = render_task_runtime_panel(task, runtime)
    comments_html = render_comments_panel(task_id, conn)
    log_html = render_run_log_panel(task_id, conn)

    body = f"""
    <div id="task-header-wrap">{header_html}</div>
    <div id="task-runtime-wrap">{runtime_html}</div>
    <div id="task-comments-wrap">{comments_html}</div>
    <div id="task-log-wrap">{log_html}</div>
    <p class="muted tz-note">Times shown in server local time ({_server_tz_label()}).</p>
    <script>
      const source = new EventSource("/events/{task_id}");

      function swap(id, html) {{
        const el = document.getElementById(id);
        if (el) {{
          el.innerHTML = html;
          if (window.stickLogsToBottom) window.stickLogsToBottom(el);
        }}
      }}

      function headerHasOpenEditor() {{
        return document.querySelector("#task-header-wrap [data-inline-form]:not([hidden])");
      }}

      function activateInlineEditor(containerId) {{
        const root = document.getElementById(containerId);
        if (!root) return;
        const display = root.querySelector("[data-inline-display]");
        const form = root.querySelector("[data-inline-form]");
        if (!display || !form) return;
        display.hidden = true;
        form.hidden = false;
        const field = form.querySelector("input:not([type='hidden']), textarea:not(.preserve-field)");
        if (field) {{
          field.focus();
          if (typeof field.select === "function") field.select();
        }}
      }}

      function cancelInlineEditor(containerId) {{
        const root = document.getElementById(containerId);
        if (!root) return;
        const display = root.querySelector("[data-inline-display]");
        const form = root.querySelector("[data-inline-form]");
        if (!display || !form) return;
        form.reset();
        form.hidden = true;
        display.hidden = false;
      }}

      function handleInlineEditorKey(event, containerId) {{
        if (event.key === "Enter" || event.key === " ") {{
          event.preventDefault();
          activateInlineEditor(containerId);
        }}
      }}

      source.addEventListener("task_header", e => {{
        if (!headerHasOpenEditor()) swap("task-header-wrap", JSON.parse(e.data));
      }});
      source.addEventListener("task_runtime",  e => swap("task-runtime-wrap",  JSON.parse(e.data)));
      source.addEventListener("task_comments", e => swap("task-comments-wrap", JSON.parse(e.data)));
      source.addEventListener("task_log",      e => swap("task-log-wrap",      JSON.parse(e.data)));

      source.onerror = () => console.log("SSE connection interrupted");
    </script>
    """
    nav_extra = f'<a href="/task/{task_id}">Task #{task_id}</a>'
    return HTMLResponse(
        _page_shell(f"Task #{task_id} — {task['title']}", body, nav_extra),
        status_code=status_code,
    )


# ── Routes ─────────────────────────────────────────────────────────────

@app.get("/favicon.ico")
def favicon():
    return FileResponse(_FAVICON, media_type="image/x-icon")


@app.get("/", response_class=HTMLResponse)
def index():
    conn = _open_conn()
    try:
        runtime = db.get_runtime(conn) if conn else None
        health_html = render_health_card(runtime)
        current_html = render_current_task_card(runtime, conn)
        active_supertasks_html = render_active_supertasks(conn)
        ready_html = render_ready_queue(conn, runtime)
        icebox_html = render_icebox(conn)
        blocked_html = render_blocked_tasks(conn)
        done_html = render_recently_done(conn)
        global_log_html = render_global_run_log_panel(conn)
    finally:
        if conn:
            conn.close()

    body = f"""
    <h1>Kanban Orchestra</h1>
    <p class="lede">Running against <code>{_esc(_running_directory_display())}</code></p>
    <div id="health-wrap">{health_html}</div>
    <div id="current-task-wrap">{current_html}</div>
    <div id="active-supertasks-wrap">{active_supertasks_html}</div>
    <div id="ready-queue-wrap">{ready_html}</div>
    <div id="icebox-wrap">{icebox_html}</div>
    <div id="blocked-wrap">{blocked_html}</div>
    <div id="done-wrap">{done_html}</div>
    <div id="global-log-wrap">{global_log_html}</div>
    <p class="muted tz-note">Times shown in server local time ({_server_tz_label()}).</p>
    <script>
      const source = new EventSource("/events");

      function swap(id, html) {{
        const el = document.getElementById(id);
        if (el) {{
          el.innerHTML = html;
          if (window.stickLogsToBottom) {{
            window.stickLogsToBottom(el);
          }}
          if (window.hydrateShowMore) {{
            const card = el.querySelector("[data-show-more-root]");
            if (card) {{
              const priorVisible = window.__recentlyDoneVisibleCount;
              if (Number.isFinite(priorVisible)) {{
                card.dataset.visibleCount = String(priorVisible);
              }}
              window.hydrateShowMore(card);
            }}
          }}
        }}
      }}

      source.addEventListener("health",       e => swap("health-wrap",       JSON.parse(e.data)));
      source.addEventListener("current_task", e => swap("current-task-wrap", JSON.parse(e.data)));
      source.addEventListener("active_supertasks", e => swap("active-supertasks-wrap", JSON.parse(e.data)));
      source.addEventListener("ready_queue",  e => swap("ready-queue-wrap",  JSON.parse(e.data)));
      source.addEventListener("icebox",       e => swap("icebox-wrap",       JSON.parse(e.data)));
      source.addEventListener("blocked",      e => swap("blocked-wrap",      JSON.parse(e.data)));
      source.addEventListener("done",         e => swap("done-wrap",         JSON.parse(e.data)));
      source.addEventListener("global_log",   e => swap("global-log-wrap",   JSON.parse(e.data)));

      source.onerror = () => console.log("SSE connection interrupted");
    </script>
    """
    return _page_shell("Kanban Orchestra Dashboard", body)


@app.get("/task/{task_id}", response_class=HTMLResponse)
def task_detail(task_id: int):
    conn = _open_conn()
    if conn is None:
        body = '<div class="card"><p class="muted">Database not available.</p></div>'
        return _page_shell("Task Not Found", body)

    try:
        response = _task_detail_response(task_id, conn)
    finally:
        conn.close()
    return response


@app.post("/task/{task_id}/edit")
async def task_edit(task_id: int, request: Request):
    conn = _open_conn()
    if conn is None:
        body = '<div class="card"><p class="muted">Database not available.</p></div>'
        return HTMLResponse(_page_shell("Task Not Found", body), status_code=503)

    try:
        task = db.get_task(conn, task_id)
        if task is None:
            body = f'<div class="card"><p class="muted">Task #{task_id} not found.</p></div>'
            return HTMLResponse(_page_shell("Task Not Found", body), status_code=404)

        form_data = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        raw_title = form_data.get("title", [""])[0]
        raw_description = form_data.get("description", [""])[0]
        title = raw_title.strip()
        if not title:
            form_task = dict(task)
            form_task["title"] = raw_title
            form_task["description"] = raw_description
            return _task_detail_response(
                task_id,
                conn,
                form_task=form_task,
                edit_error="Title is required.",
                status_code=400,
            )

        db.update_task(
            conn,
            task_id,
            title=title,
            description=_normalize_description_source(raw_description),
        )
    finally:
        conn.close()

    return RedirectResponse(url=f"/task/{task_id}", status_code=303)


@app.get("/events")
def events_overview():
    """SSE stream for the overview page. Pushes named events every 5 s."""
    def stream():
        while True:
            conn = _open_conn()
            try:
                runtime = db.get_runtime(conn) if conn else None
                health = render_health_card(runtime)
                current = render_current_task_card(runtime, conn)
                active_supertasks = render_active_supertasks(conn)
                ready = render_ready_queue(conn, runtime)
                icebox = render_icebox(conn)
                blocked = render_blocked_tasks(conn)
                done = render_recently_done(conn)
                global_log = render_global_run_log_panel(conn)
            finally:
                if conn:
                    conn.close()

            yield f"event: health\ndata: {json.dumps(health)}\n\n"
            yield f"event: current_task\ndata: {json.dumps(current)}\n\n"
            yield f"event: active_supertasks\ndata: {json.dumps(active_supertasks)}\n\n"
            yield f"event: ready_queue\ndata: {json.dumps(ready)}\n\n"
            yield f"event: icebox\ndata: {json.dumps(icebox)}\n\n"
            yield f"event: blocked\ndata: {json.dumps(blocked)}\n\n"
            yield f"event: done\ndata: {json.dumps(done)}\n\n"
            yield f"event: global_log\ndata: {json.dumps(global_log)}\n\n"
            time.sleep(5)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/events/{task_id}")
def events_task(task_id: int):
    """SSE stream for a task detail page. Pushes named events every 5 s."""
    def stream():
        while True:
            conn = _open_conn()
            try:
                task = db.get_task(conn, task_id) if conn else None
                if task and conn:
                    runtime = db.get_runtime(conn)
                    prev_id, next_id = _get_prev_next_task_ids(conn, task_id)
                    header_html = render_task_header(task, conn, prev_id=prev_id, next_id=next_id)
                    runtime_html = render_task_runtime_panel(task, runtime)
                    comments_html = render_comments_panel(task_id, conn)
                    log_html = render_run_log_panel(task_id, conn)
                else:
                    header_html = '<div class="card" id="task-header"><p class="muted">Task not found.</p></div>'
                    runtime_html = '<div class="card" id="task-runtime-panel"><p class="muted">Task not found.</p></div>'
                    comments_html = '<div class="card" id="comments-panel"><p class="muted">Task not found.</p></div>'
                    log_html = '<div class="card" id="run-log-panel"><p class="muted">Task not found.</p></div>'
            finally:
                if conn:
                    conn.close()

            yield f"event: task_header\ndata: {json.dumps(header_html)}\n\n"
            yield f"event: task_runtime\ndata: {json.dumps(runtime_html)}\n\n"
            yield f"event: task_comments\ndata: {json.dumps(comments_html)}\n\n"
            yield f"event: task_log\ndata: {json.dumps(log_html)}\n\n"
            time.sleep(5)

    return StreamingResponse(stream(), media_type="text/event-stream")


def _find_free_port(host: str, preferred_port: int) -> int:
    """Return the first free TCP port on *host* starting at *preferred_port*.

    Probes each candidate by binding and immediately releasing the socket so
    uvicorn can bind normally and emit its standard http://host:port banner.
    Non-EADDRINUSE OSErrors propagate immediately so callers see the real failure.
    """
    port = preferred_port
    while True:
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
                return port
            except OSError as exc:
                if exc.errno == errno.EADDRINUSE:
                    port += 1
                    continue
                raise


def _run_dashboard(host: str, preferred_port: int, *, _uvicorn=None) -> None:
    """Find a free port and start the uvicorn server.

    *_uvicorn* is the uvicorn module to use; if None the real uvicorn is
    imported at call time.  The parameter exists solely for test injection.
    """
    if _uvicorn is None:
        try:
            import uvicorn as _uvicorn  # type: ignore[no-redef]
        except ModuleNotFoundError:
            print(
                "uvicorn is not installed. Run: python3 -m pip install uvicorn",
                file=sys.stderr,
            )
            raise SystemExit(1)

    port = _find_free_port(host, preferred_port)
    if port != preferred_port:
        print(
            f"Port {preferred_port} is in use; dashboard starting on port {port}.",
            flush=True,
        )
    try:
        _uvicorn.run(
            "dashboard:app",
            host=host,
            port=port,
            reload=False,
            log_level="info",
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    _run_dashboard(
        host="127.0.0.1",
        preferred_port=int(os.environ.get("KO_DASH_PORT", "8427")),
    )
