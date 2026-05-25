#!/usr/bin/env python3
"""
get_kanban_update.py — print a concise plain-text Kanban Orchestra status update.

Usage:
    python3 get_kanban_update.py [--db <path>]

Exit codes:
    0  — success (even if orchestrator is stopped/stale)
    1  — database not found or not in a git repo
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
import db


STALE_SECONDS = 60
RUNTIME_STATUSES_WITHOUT_HEARTBEAT_STALE = {"starting", "stopping", "stopped", "hard-break"}


def _task_reviewer(task) -> str:
    if not task:
        return ""
    return task.get("reviewer_agent") or config.DEFAULT_REVIEWER


def _age(dt_str: str | None) -> str:
    if not dt_str:
        return "unknown"
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
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
        return str(dt_str)


def _is_stale(last_heartbeat: str | None) -> bool:
    if not last_heartbeat:
        return True
    try:
        dt = datetime.fromisoformat(last_heartbeat)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() > STALE_SECONDS
    except Exception:
        return True


def _display_status(status: str, last_heartbeat: str | None) -> str:
    if _is_stale(last_heartbeat) and status not in RUNTIME_STATUSES_WITHOUT_HEARTBEAT_STALE:
        return "stale"
    return status


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_key_value_or_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        payload = {}
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                payload[key.strip()] = value.strip()
        return payload


def _metadata_pid(path: Path, role: str) -> int | None:
    payload = _read_key_value_or_json(path)
    if payload.get("role") != role:
        return None
    try:
        return int(payload["pid"])
    except (KeyError, TypeError, ValueError):
        return None


def _kanban_pids(identity: dict) -> list[tuple[int, str]]:
    """Return repo-local [(pid, role), ...] for orchestrator and dashboard."""
    results = []
    for path, role in (
        (Path(identity["lock_path"]), "orchestrator"),
        (Path(identity["runtime_root"]) / "dashboard.json", "dashboard"),
    ):
        pid = _metadata_pid(path, role)
        if _pid_alive(pid):
            results.append((pid, role))
    return results


def _format_skips(skips: list[str] | None) -> str:
    if not skips:
        return ""
    return ", ".join(skips)


def build_update(conn) -> str:
    lines = []
    identity = db.get_instance_identity(db.get_connection_db_path(conn))

    lines.append(f"INSTANCE: {identity['repo_label']}  ({identity['repo_root']})")
    lines.append(f"  lock: {identity['lock_path']}")
    lines.append("")

    # ── Orchestrator health ──────────────────────────────────────────────
    runtime = db.get_runtime(conn)

    if runtime is None:
        lines.append("ORCHESTRATOR: no runtime row — orchestrator has never started")
        lines.append("")
    else:
        status = runtime.get("status") or "unknown"
        hb = runtime.get("last_heartbeat_at")
        display_status = _display_status(status, hb)
        hb_age = _age(hb)

        lines.append(f"ORCHESTRATOR: {display_status}  (heartbeat {hb_age})")

        if runtime.get("status_message"):
            lines.append(f"  message: {runtime['status_message']}")

        # ── Process PIDs ─────────────────────────────────────────────────
        pids = _kanban_pids(identity)
        if pids:
            pid_parts = [f"{role} PID {pid}" for pid, role in pids]
            lines.append(f"  processes: {', '.join(pid_parts)}")
        else:
            lines.append("  processes: none found")

        # ── Active task ──────────────────────────────────────────────────
        tid = runtime.get("current_task_id")
        if tid:
            task = db.get_task(conn, tid)
            step = runtime.get("current_step") or (task.get("next_step") if task else "")
            branch = (task.get("branch") if task else None) or runtime.get("current_branch") or ""
            coder = (task.get("coder_agent") if task else None) or ""
            reviewer = _task_reviewer(task)
            rround = runtime.get("review_round") or 0
            title = task.get("title") if task else "(unknown)"

            lines.append("")
            lines.append(f"ACTIVE TASK: #{tid} — {title}")
            lines.append(f"  branch: {branch}  coder: {coder}  reviewer: {reviewer}  step: {step}  review-round: {rround}")
            if task and task.get("skips"):
                lines.append(f"  skips: {_format_skips(task.get('skips'))}")

            if step == "commit-review":
                lines.append("  review status: in progress")
        else:
            lines.append("")
            lines.append("ACTIVE TASK: none (orchestrator idle)")

    lines.append("")

    # ── Ready queue ──────────────────────────────────────────────────────
    ready_tasks = db.list_tasks(conn, status="ready", page_size=None)
    if ready_tasks:
        lines.append(f"READY ({len(ready_tasks)}):")
        for t in ready_tasks:
            skip_part = f"  skips: {_format_skips(t.get('skips'))}" if t.get("skips") else ""
            lines.append(
                f"  #{t['id']} [{t['next_step']}] {t['title']}  branch: {t.get('branch') or 'unset'}  coder: {t.get('coder_agent') or ''}  reviewer: {_task_reviewer(t)}{skip_part}"
            )
    else:
        lines.append("READY: none")

    lines.append("")

    # ── Blocked tasks ────────────────────────────────────────────────────
    blocked_tasks = db.list_tasks(conn, status="blocked", page_size=None)
    if blocked_tasks:
        lines.append(f"BLOCKED ({len(blocked_tasks)}):")
        for t in blocked_tasks:
            # Fetch most recent comment as a short blocker note
            row = conn.execute(
                "SELECT message FROM comments WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (t["id"],),
            ).fetchone()
            note = row["message"] if row else ""
            if len(note) > 80:
                note = note[:77] + "..."
            note_part = f"  — {note}" if note else ""
            skip_part = f"  skips: {_format_skips(t.get('skips'))}" if t.get("skips") else ""
            lines.append(f"  #{t['id']} {t['title']}  coder: {t.get('coder_agent') or ''}  reviewer: {_task_reviewer(t)}{note_part}{skip_part}")
    else:
        lines.append("BLOCKED: none")

    lines.append("")

    # ── Recently done ────────────────────────────────────────────────────
    done_rows = conn.execute(
        "SELECT id, title, branch, commit_hash, coder_agent, reviewer_agent FROM tasks "
        "WHERE status = 'done' ORDER BY updated_at DESC, id DESC LIMIT 3"
    ).fetchall()
    if done_rows:
        lines.append(f"RECENTLY DONE ({len(done_rows)} shown):")
        for r in done_rows:
            h = (r["commit_hash"] or "")[:8]
            commit_part = f"  commit: {h}" if h else ""
            coder_part = f"  coder: {r['coder_agent']}" if r["coder_agent"] else ""
            configured_reviewer_part = f"  configured reviewer: {r['reviewer_agent'] or config.DEFAULT_REVIEWER}"
            reviewer_rows = conn.execute(
                "SELECT DISTINCT author FROM comments "
                "WHERE task_id = ? AND kind = 'approval' AND author IS NOT NULL ORDER BY id",
                (r["id"],),
            ).fetchall()
            reviewers = ", ".join(row["author"] for row in reviewer_rows)
            reviewer_part = f"  approver: {reviewers}" if reviewers else ""
            lines.append(f"  #{r['id']} {r['title']}{commit_part}{coder_part}{configured_reviewer_part}{reviewer_part}")
    else:
        lines.append("RECENTLY DONE: none")

    lines.append("")

    # ── What likely needs attention ──────────────────────────────────────
    attention = _attention_summary(runtime, ready_tasks, blocked_tasks)
    lines.append(f"ATTENTION: {attention}")

    return "\n".join(lines)


def _attention_summary(runtime, ready_tasks, blocked_tasks) -> str:
    if runtime is None:
        return "start the orchestrator — no runtime row found"

    status = runtime.get("status") or "unknown"
    display_status = _display_status(status, runtime.get("last_heartbeat_at"))

    if display_status == "error":
        msg = runtime.get("status_message") or ""
        return f"orchestrator is in error state — investigate: {msg}"

    if display_status == "stale":
        return "orchestrator heartbeat is stale — it may have crashed; check the process"

    if display_status in ("stopped", "stopping"):
        if ready_tasks:
            return f"orchestrator is stopped but {len(ready_tasks)} task(s) are ready — restart the orchestrator"
        return "orchestrator is stopped — restart when ready to process work"

    if display_status == "starting":
        return "orchestrator is starting — wait for runtime to become idle or running"

    if display_status == "hard-break":
        return "hard BREAK completed — inspect blocked task/worktree before restarting"

    if blocked_tasks:
        return f"{len(blocked_tasks)} task(s) are blocked — review and unblock them"

    if not runtime.get("current_task_id") and not ready_tasks:
        return "queue is empty and orchestrator is idle — add tasks when ready"

    if display_status in ("idle", "running"):
        return "work is progressing normally"

    return f"orchestrator status is '{display_status}' — monitor closely"


def main():
    parser = argparse.ArgumentParser(
        description="Print a concise plain-text Kanban Orchestra status update."
    )
    parser.add_argument("--db", metavar="PATH", help="Path to kanban-orchestra.db")
    args = parser.parse_args()

    try:
        db_path = db.get_db_path(args.db)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not Path(db_path).exists():
        print(f"Error: database not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = db.connect(db_path)
    try:
        print(build_update(conn))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
