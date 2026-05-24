#!/usr/bin/env python3
"""Filesystem-backed control helpers for the orchestra-ui supervisor."""

import json
import os
import signal
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fcntl

import config
import db


REQUEST_FILE = "orchestrator-control-request.json"
RESPONSE_FILE = "orchestrator-control-response.json"
SUPERVISOR_HEARTBEAT_FILE = "orchestra-ui-supervisor.json"
ACTIVE_AGENTS_FILE = "active-agent-processes.json"
CONTROL_COMMANDS = {"status", "start", "stop", "break"}
SUPERVISOR_STALE_SECONDS = 20.0
PENDING_REQUEST_STALE_SECONDS = 30.0


class ControlError(RuntimeError):
    """Raised when an operator control request cannot be delivered."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paths(db_path: str | None = None) -> dict[str, Path]:
    root = db.get_runtime_root(db_path)
    return {
        "root": root,
        "request": root / REQUEST_FILE,
        "response": root / RESPONSE_FILE,
        "supervisor": root / SUPERVISOR_HEARTBEAT_FILE,
        "active_agents": root / ACTIVE_AGENTS_FILE,
        "stop_after_task": Path(db.get_db_path(db_path)).resolve().parent / config.STOP_AFTER_TASK_FILE,
    }


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


def read_singleton_lock_metadata(
    db_path: str | None = None,
    *,
    lock_path: str | Path | None = None,
) -> dict[str, str]:
    """Read best-effort identity metadata from the repo singleton lock file."""
    path = Path(lock_path).resolve() if lock_path is not None else db.get_lock_path(db_path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    except OSError:
        return {}

    metadata = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key:
            metadata[key] = value
    return metadata


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _is_stale(path: Path, stale_seconds: float) -> bool:
    try:
        return time.time() - path.stat().st_mtime > stale_seconds
    except FileNotFoundError:
        return False


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


def write_supervisor_heartbeat(payload: dict[str, Any] | None = None, db_path: str | None = None) -> None:
    """Write the live orchestra-ui supervisor heartbeat for control clients."""
    paths = _paths(db_path)
    identity = db.get_instance_identity(db_path)
    body = {
        **identity,
        "pid": os.getpid(),
        "updated_at": _now_iso(),
    }
    if payload:
        body.update(payload)
    _atomic_write_json(paths["supervisor"], body)


def supervisor_status(db_path: str | None = None, stale_seconds: float = SUPERVISOR_STALE_SECONDS) -> tuple[bool, str, dict[str, Any] | None]:
    """Return (is_live, reason, heartbeat_payload)."""
    path = _paths(db_path)["supervisor"]
    payload = _read_json(path)
    if not payload:
        return False, "No live orchestra-ui supervisor heartbeat found.", None
    if _is_stale(path, stale_seconds):
        return False, "The orchestra-ui supervisor heartbeat is stale.", payload
    if not is_pid_alive(_coerce_int(payload.get("pid"))):
        return False, "The orchestra-ui supervisor process is no longer running.", payload
    expected = db.get_instance_identity(db_path)
    heartbeat_repo = payload.get("repo_root")
    if heartbeat_repo and str(Path(heartbeat_repo).expanduser().resolve()) != expected["repo_root"]:
        return False, "The orchestra-ui supervisor heartbeat belongs to a different repo.", payload
    return True, "Supervisor is live.", payload


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def singleton_lock_available(db_path: str | None = None) -> bool:
    """Return whether the repo-scoped orchestrator singleton lock is free."""
    lock_path = db.get_lock_path(db_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def read_control_request(db_path: str | None = None) -> dict[str, Any] | None:
    return _read_json(_paths(db_path)["request"])


def write_control_response(response: dict[str, Any], db_path: str | None = None) -> None:
    _atomic_write_json(_paths(db_path)["response"], response)


def clear_control_request(db_path: str | None = None) -> None:
    _safe_unlink(_paths(db_path)["request"])


def clear_control_response(db_path: str | None = None) -> None:
    _safe_unlink(_paths(db_path)["response"])


def submit_control_command(
    command: str,
    *,
    timeout: float = 15.0,
    poll_interval: float = 0.2,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Submit a command to a live orchestra-ui supervisor and wait for its response."""
    command = command.strip().lower()
    if command not in CONTROL_COMMANDS:
        raise ControlError(f"Unknown orchestrator control command: {command}")

    live, reason, _payload = supervisor_status(db_path)
    if not live:
        raise ControlError(reason)

    paths = _paths(db_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    if paths["request"].exists():
        if _is_stale(paths["request"], PENDING_REQUEST_STALE_SECONDS):
            _safe_unlink(paths["request"])
        else:
            raise ControlError(f"A control request is already pending at {paths['request']}.")

    clear_control_response(db_path)
    request_id = uuid.uuid4().hex
    request = {
        **db.get_instance_identity(db_path),
        "id": request_id,
        "command": command,
        "pid": os.getpid(),
        "created_at": _now_iso(),
    }
    _atomic_write_json(paths["request"], request)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = _read_json(paths["response"])
        if response and response.get("id") == request_id:
            clear_control_response(db_path)
            return response
        live, reason, _payload = supervisor_status(db_path)
        if not live:
            raise ControlError(f"Lost orchestra-ui supervisor while waiting for response: {reason}")
        time.sleep(poll_interval)

    current = _read_json(paths["request"])
    if current and current.get("id") == request_id:
        clear_control_request(db_path)
    raise ControlError(f"Timed out waiting for orchestra-ui supervisor response to {command}.")


def _read_active_agents(db_path: str | None = None) -> list[dict[str, Any]]:
    payload = _read_json(_paths(db_path)["active_agents"])
    if not payload:
        return []
    records = payload.get("agents", [])
    return records if isinstance(records, list) else []


def _write_active_agents(records: list[dict[str, Any]], db_path: str | None = None) -> None:
    path = _paths(db_path)["active_agents"]
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
    """Record an active agent process so STOP/BREAK can terminate it by process group."""
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


def remove_stop_after_task_marker(db_path: str | None = None) -> bool:
    path = _paths(db_path)["stop_after_task"]
    existed = path.exists()
    _safe_unlink(path)
    return existed
