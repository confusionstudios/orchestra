#!/usr/bin/env python3
"""Runtime metadata helpers for active agent child processes."""

import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db


ACTIVE_AGENTS_FILE = "active-agent-processes.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(db_path: str | None = None) -> Path:
    return db.get_runtime_root(db_path) / ACTIVE_AGENTS_FILE


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read_active_agents(db_path: str | None = None) -> list[dict[str, Any]]:
    payload = _read_json(_path(db_path))
    if not payload:
        return []
    records = payload.get("agents", [])
    return records if isinstance(records, list) else []


def _write_active_agents(records: list[dict[str, Any]], db_path: str | None = None) -> None:
    path = _path(db_path)
    live_records = [record for record in records if is_pid_alive(_coerce_int(record.get("pid")))]
    if not live_records:
        _safe_unlink(path)
        return
    _atomic_write_json(
        path,
        {
            "updated_at": _now_iso(),
            "agents": live_records,
        },
    )


def register_active_agent(
    *,
    task_id: int,
    verb: str,
    agent_name: str,
    pid: int,
    db_path: str | None = None,
) -> str:
    """Record an active agent process so stop/break flows can terminate it."""
    pid_value = _coerce_int(pid)
    if pid_value is None:
        return ""
    pid = pid_value
    record_id = f"{task_id}:{verb}:{agent_name}:{pid}"
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = pid
    records = [
        record for record in _read_active_agents(db_path)
        if record.get("id") != record_id and is_pid_alive(_coerce_int(record.get("pid")))
    ]
    records.append(
        {
            "id": record_id,
            "task_id": task_id,
            "verb": verb,
            "agent_name": agent_name,
            "pid": pid,
            "pgid": pgid,
            "started_at": _now_iso(),
        }
    )
    _write_active_agents(records, db_path)
    return record_id


def clear_active_agent(record_id: str | None = None, *, pid: int | None = None, db_path: str | None = None) -> None:
    records = _read_active_agents(db_path)
    if record_id is None and pid is None:
        _write_active_agents([], db_path)
        return
    kept = []
    for record in records:
        if record_id is not None and record.get("id") == record_id:
            continue
        if pid is not None and _coerce_int(record.get("pid")) == pid:
            continue
        kept.append(record)
    _write_active_agents(kept, db_path)


def kill_active_agents(db_path: str | None = None, *, timeout: float = 2.0) -> list[dict[str, Any]]:
    """Terminate recorded active agent process groups and clear the metadata file."""
    records = _read_active_agents(db_path)
    killed = []
    current_pgid = os.getpgrp()
    for record in records:
        pid = _coerce_int(record.get("pid"))
        pgid = _coerce_int(record.get("pgid")) or pid
        if not pid or not is_pid_alive(pid):
            continue
        try:
            if pgid and pgid != current_pgid:
                os.killpg(pgid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
            killed.append(record)
        except (ProcessLookupError, PermissionError):
            continue

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(not is_pid_alive(_coerce_int(record.get("pid"))) for record in killed):
            break
        time.sleep(0.05)

    for record in killed:
        pid = _coerce_int(record.get("pid"))
        pgid = _coerce_int(record.get("pgid")) or pid
        if not pid or not is_pid_alive(pid):
            continue
        try:
            if pgid and pgid != current_pgid:
                os.killpg(pgid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    clear_active_agent(db_path=db_path)
    return killed
