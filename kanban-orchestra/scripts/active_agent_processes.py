#!/usr/bin/env python3
"""Runtime metadata helpers for active agent child processes."""

import json
import fcntl
import os
import signal
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from contextlib import contextmanager

import db
from queue_control_errors import AgentRegistrationError

_PROCESS_POPEN = subprocess.Popen
_PS_BINARY = shutil.which("ps", path=os.defpath) or "/bin/ps"


def _ps_output(args):
    # Keep process inspection independent of agent-launch instrumentation.
    with _PROCESS_POPEN([_PS_BINARY, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True) as proc:
        output, _ = proc.communicate()
        if proc.returncode not in (0, 1):
            raise RuntimeError("Cannot inspect process identity")
        return output


ACTIVE_AGENTS_FILE = "active-agent-processes.json"


@contextmanager
def _registry_lock(db_path=None, *, queue_id=None):
    path = _path(db_path, queue_id=queue_id).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(db_path: str | None = None, *, queue_id: str | None = None) -> Path:
    return db.get_runtime_root(db_path, queue_id=queue_id) / ACTIVE_AGENTS_FILE


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


def _read_active_agents(db_path: str | None = None, *, queue_id: str | None = None) -> list[dict[str, Any]]:
    payload = _read_json(_path(db_path, queue_id=queue_id))
    if not payload:
        return []
    records = payload.get("agents", [])
    return records if isinstance(records, list) else []


def _write_active_agents(
    records: list[dict[str, Any]],
    db_path: str | None = None,
    *,
    queue_id: str | None = None,
) -> None:
    path = _path(db_path, queue_id=queue_id)
    live_records = [record for record in records if is_pid_alive(_coerce_int(record.get("pid")))
                    or group_members(_coerce_int(record.get("pgid")))]
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


def live_agents(
    db_path: str | None = None,
    *,
    queue_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return registered agent processes whose pids are still alive."""
    return [
        record for record in _read_active_agents(db_path, queue_id=queue_id)
        if is_pid_alive(_coerce_int(record.get("pid")))
    ]


def register_active_agent(**kwargs):
    try:
        with _registry_lock(kwargs.get("db_path"), queue_id=kwargs.get("queue_id")):
            return _register_active_agent(**kwargs)
    except Exception as exc:
        pid = _coerce_int(kwargs.get("pid"))
        detail = "Process exit needs manual verification"
        try:
            # waitpid establishes that this is our direct child, even when
            # process inspection itself failed. Never signal an arbitrary PID.
            os.waitpid(pid, os.WNOHANG)
            if pid > 1 and pid != os.getpgrp():
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(pid, sig)
                    except ProcessLookupError:
                        break
                    deadline = time.monotonic() + 2
                    while group_members(pid) and time.monotonic() < deadline:
                        time.sleep(0.05)
                if not group_members(pid):
                    detail = "Launched group exited"
        except (OSError, TypeError, RuntimeError):
            pass
        raise AgentRegistrationError(f"Agent registration failed: {exc}. {detail}.") from exc


def _register_active_agent(
    *,
    task_id: int,
    verb: str,
    agent_name: str,
    pid: int,
    db_path: str | None = None,
    queue_id: str | None = None,
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
        record for record in _read_active_agents(db_path, queue_id=queue_id)
        if record.get("id") != record_id
    ]
    records.append(
        {
            "id": record_id,
            "task_id": task_id,
            "verb": verb,
            "agent_name": agent_name,
            "pid": pid,
            "pgid": pgid,
            "process_identity": process_identity(pid),
            "queue_id": queue_id,
            "started_at": _now_iso(),
        }
    )
    _write_active_agents(records, db_path, queue_id=queue_id)
    return record_id


def clear_active_agent(
    record_id: str | None = None,
    *,
    pid: int | None = None,
    db_path: str | None = None,
    queue_id: str | None = None,
) -> None:
    with _registry_lock(db_path, queue_id=queue_id):
        _clear_active_agent(record_id, pid=pid, db_path=db_path, queue_id=queue_id)


def _clear_active_agent(record_id=None, *, pid=None, db_path=None, queue_id=None):
    records = _read_active_agents(db_path, queue_id=queue_id)
    if record_id is None and pid is None:
        # Stop/break must also retain evidence of surviving descendants.
        remaining = [record for record in records if not record.get("pgid")
                     or group_members(_coerce_int(record.get("pgid")))]
        _write_active_agents(remaining, db_path, queue_id=queue_id)
        return
    kept = []
    for record in records:
        # Retain evidence of surviving descendants even after the leader exits.
        if group_members(_coerce_int(record.get("pgid"))):
            kept.append(record)
            continue
        if record_id is not None and record.get("id") == record_id:
            continue
        if pid is not None and _coerce_int(record.get("pid")) == pid:
            continue
        kept.append(record)
    _write_active_agents(kept, db_path, queue_id=queue_id)


def process_identity(pid):
    return _ps_output(["-p", str(pid), "-o", "lstart="]).strip() or None


def group_members(pgid):
    """List live group members, excluding unreapable zombie descendants."""
    if not pgid:
        return []
    output = _ps_output(["-axo", "pid=,pgid=,stat="])
    return [int(parts[0]) for line in output.splitlines()
            if len(parts := line.split()) >= 3 and int(parts[1]) == pgid and not parts[2].startswith("Z")]


def stop_verified_agents(db_path=None, *, queue_id=None, timeout=2.0):
    """Fail closed on foreign/stale identities and verify whole groups exited."""
    with _registry_lock(db_path, queue_id=queue_id):
        return _stop_verified_agents(db_path, queue_id=queue_id, timeout=timeout)


def _stop_verified_agents(db_path=None, *, queue_id=None, timeout=2.0):
    path = _path(db_path, queue_id=queue_id)
    payload = _read_json(path)
    if path.exists() and (not isinstance(payload, dict) or not isinstance(payload.get("agents"), list)):
        raise RuntimeError("Unreadable active-agent metadata")
    records = payload["agents"] if payload else []
    for record in records:
        if not isinstance(record, dict):
            raise RuntimeError("Malformed active-agent record")
        pid, pgid = _coerce_int(record.get("pid")), _coerce_int(record.get("pgid"))
        if not pid or not pgid or pid <= 1 or pgid <= 1:
            raise RuntimeError("Missing process/group identity; preserve active-agent metadata")
        members = group_members(pgid)
        if not members:
            continue
        if (not pid or pgid != pid or pgid == os.getpgrp() or record.get("queue_id") != queue_id
                or not record.get("process_identity")):
            raise RuntimeError("Unverifiable process ownership; inspect active-agent metadata")
        # A surviving descendant keeps the registered group reserved after leader exit.
        identity = process_identity(pid)
        if identity is not None and identity != record["process_identity"]:
            raise RuntimeError("Agent PID was reused; refusing to signal it")
        if identity is not None and os.getpgid(pid) != pgid:
            raise RuntimeError("Agent process group changed")
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if not group_members(pgid):
                break
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + timeout
            while group_members(pgid) and time.monotonic() < deadline:
                time.sleep(0.05)
        if group_members(pgid):
            raise RuntimeError(f"Agent group {pgid} has surviving processes")
    _write_active_agents([], db_path, queue_id=queue_id)
    return records


def kill_active_agents(
    db_path: str | None = None,
    *,
    timeout: float = 2.0,
    queue_id: str | None = None,
) -> list[dict[str, Any]]:
    """Terminate recorded active agent process groups and clear the metadata file."""
    records = _read_active_agents(db_path, queue_id=queue_id)
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

    deadline = time.monotonic() + timeout
    while any(group_members(_coerce_int(record.get("pgid"))) for record in records) and time.monotonic() < deadline:
        time.sleep(0.05)
    clear_active_agent(db_path=db_path, queue_id=queue_id)
    return killed
