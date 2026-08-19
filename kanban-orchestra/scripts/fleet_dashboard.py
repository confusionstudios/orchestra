#!/usr/bin/env python3
"""Collect Fleet Dashboard card state from configured fleet repositories."""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

import dashboard
import db
import fleet


@dataclass(frozen=True)
class FleetCurrentTask:
    id: int
    title: str


@dataclass(frozen=True)
class FleetCard:
    name: str
    path: str
    branch: str | None
    status: str
    invalid: bool
    current_task: FleetCurrentTask | None
    ready_count: int
    recently_done_count: int
    icebox_count: int
    dashboard_url: str | None
    tailscale_url: str | None
    last_start_failure: str | None


def collect_cards() -> list[FleetCard]:
    """Return one card-data record per configured fleet repo."""
    return [collect_card(repo) for repo in fleet.load_repos()]


def collect_card(repo: fleet.FleetRepo) -> FleetCard:
    """Return card-data for one configured fleet repo from live state."""
    process_status, _, _, _ = fleet.repo_process_state(repo)
    runtime = fleet.repo_runtime_status(repo)
    conn = _open_repo_db(repo)
    try:
        status = _product_status(process_status, conn)
        current_task = None
        if not repo.error and status != "stopped":
            current_task = _current_task(runtime, conn)
        ready_count, recently_done_count, icebox_count = _queue_counts(conn)
    finally:
        if conn is not None:
            conn.close()

    dashboard_url = _local_dashboard_url(repo)
    tailscale_url = None
    if dashboard_url is not None:
        tailscale_url = fleet.tailscale_dashboard_url(dashboard_url)

    last_start_failure = None
    if status == "stopped" and fleet.dirty_lines(repo):
        last_start_failure = "Worktree dirty"

    return FleetCard(
        name=repo.label,
        path=fleet.display_path(repo.root or repo.path),
        branch=_repo_branch(repo),
        status=status,
        invalid=bool(repo.error),
        current_task=current_task,
        ready_count=ready_count,
        recently_done_count=recently_done_count,
        icebox_count=icebox_count,
        dashboard_url=dashboard_url,
        tailscale_url=tailscale_url,
        last_start_failure=last_start_failure,
    )


def _open_repo_db(repo: fleet.FleetRepo) -> sqlite3.Connection | None:
    """Open a repo Kanban DB read-only without creating or migrating it."""
    if repo.root is None:
        return None
    db_path = repo.root / "kanban-orchestra.db"
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _product_status(process_status: str, conn: sqlite3.Connection | None) -> str:
    if process_status == "invalid":
        return "error"
    if process_status == "stopped":
        return "stopped"

    activity = "busy"
    if process_status.startswith("running/"):
        activity = process_status.split("/", 1)[1]
    if activity == "idle":
        if _blocked_by_gate(conn):
            return "blocked"
        return "idle"
    if activity == "starting":
        return "starting"
    if activity in {"error", "hard-break"}:
        return "error"
    return "running"


def _blocked_by_gate(conn: sqlite3.Connection | None) -> bool:
    if conn is None:
        return False
    try:
        return bool(db.list_ready_tasks_blocked_by_blocked_gate(conn))
    except sqlite3.Error:
        return False


def _current_task(
    runtime: dict | None,
    conn: sqlite3.Connection | None,
) -> FleetCurrentTask | None:
    if not runtime or conn is None:
        return None
    task_id = runtime.get("current_task_id")
    if not task_id:
        return None
    try:
        row = conn.execute(
            "SELECT id, title FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return FleetCurrentTask(id=int(row["id"]), title=str(row["title"]))


def _queue_counts(conn: sqlite3.Connection | None) -> tuple[int, int, int]:
    if conn is None:
        return 0, 0, 0
    try:
        ready = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'ready'"
        ).fetchone()[0]
        icebox = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'none'"
        ).fetchone()[0]
        done_rows = conn.execute(
            "SELECT done_at FROM tasks WHERE status = 'done'"
        ).fetchall()
    except sqlite3.Error:
        return 0, 0, 0
    recently_done = sum(1 for row in done_rows if _is_recently_done(row["done_at"]))
    return int(ready), recently_done, int(icebox)


def _parse_utc_datetime(dt_str: str | None) -> datetime | None:
    """Parse a timestamp and normalize it to UTC, matching dashboard recency rules."""
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_recently_done(done_at: str | None) -> bool:
    dt = _parse_utc_datetime(done_at)
    if dt is None:
        return False
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 0:
        secs = 0
    return secs < int(dashboard.DONE_RECENCY_CUTOFF.total_seconds())


def _local_dashboard_url(repo: fleet.FleetRepo) -> str | None:
    if repo.error:
        return None
    url = fleet.dashboard_status_url(repo)
    if not url or url == "-":
        return None
    return url


def _repo_branch(repo: fleet.FleetRepo) -> str | None:
    if repo.root is None:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(repo.root), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch or None
