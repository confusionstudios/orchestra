#!/usr/bin/env python3
"""Single cutover from legacy per-worktree databases into the central store.

``preview`` only reads. ``cutover`` imports into a staging database, verifies
it, and activates that file only after a rollback manifest is on disk.
Legacy database bytes are not modified. After a successful activation their
filenames are moved aside so normal runtime has one layout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import db
import fleet


SOURCE_NAMESPACE = uuid.UUID("6f1b4c3a-9e2d-4a7b-8c1e-0d5f6a7b8c9d")
TASK_STATUSES = {"none", "ready", "running", "done", "blocked", "pending_subtasks"}
NEXT_STEPS = {
    "commit-make", "commit-review",
    "commit-make-supertask", "commit-review-supertask",
    "commit-plan", "commit-plan-review",
    "pull-request-make", "pull-request-review",
    "other-make", "other-review",
    "none",
}
COMMENT_KINDS = {
    "comment", "approval", "rejection", "commit-message", "validation",
    "plan-approval", "plan-rejection", "done-without-commit",
    "deferred-build-changed",
}
TASK_COLUMNS = (
    "title", "description", "status", "next_step", "branch", "commit_hash",
    "stash_ref", "coder_agent", "reviewer_agent", "review_round",
    "max_review_rounds", "last_review_decision", "created_at", "ready_at",
    "last_ready_at", "first_started_at", "done_at", "updated_at", "kind",
    "sequence_index", "commit_plan", "allow_when_blocked", "block_reason",
    "resume_next_step",
)
RECOVERY_REASON = "migration_recovery"
ARTIFACT_REF = re.compile(r"(artifacts/task-)(\d+)\b")
DASHBOARD_COMMAND = re.compile(r"(^|\s)\S*(?:fleet_dashboard|dashboard)\.py(\s|$)")
DEVELOPMENT_NOTE = (
    "Stable primary tooling keeps running each legacy kanban-orchestra.db, "
    "including this development queue, until an operator approves "
    "ko-migrate cutover. That cutover must include the development database. "
    "Preview and this procedure do not stop Fleet or migrate a live database."
)


class CutoverError(RuntimeError):
    """A cutover stopped before the live destination changed."""


class WorkersLive(CutoverError):
    """A relevant worker or dashboard is still running."""


class ActivationCrash(BaseException):
    """Test hook for a crash after the staged database replaces the destination."""


def _uuid5(label: str) -> str:
    return str(uuid.uuid5(SOURCE_NAMESPACE, label))


def canonical_db_path(path: Path) -> Path:
    """Return the absolute legacy database path used for source identity."""
    return path.expanduser().resolve()


def source_identity(db_path: Path) -> str:
    return _uuid5("source:" + canonical_db_path(db_path).as_posix())


def queue_identity(db_path: Path) -> str:
    return _uuid5("queue:" + canonical_db_path(db_path).as_posix())


def _repository_key(worktree: Path) -> str:
    common = db._git_dir(worktree, "--git-common-dir")
    if common is not None:
        return common.as_posix()
    return worktree.resolve().as_posix()


def repository_identity(worktree: Path) -> str:
    return _uuid5("repository:" + _repository_key(worktree))


def _open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db._readonly_uri(path), uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return set(db._list_user_tables(conn))


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _cell(row: sqlite3.Row, name: str, default=None):
    return row[name] if name in row.keys() else default


def resolve_source_file(path: Path) -> Path | None:
    """Resolve a worktree or legacy database path.

    A worktree whose legacy file has already been removed returns None so a
    later cutover can recognize that source from migration provenance.
    """
    candidate = path.expanduser().resolve()
    if candidate.is_file():
        return candidate
    if not candidate.is_dir():
        raise CutoverError(f"Migration source not found: {candidate}")
    live = candidate / db.LEGACY_DB_FILENAME
    if live.is_file():
        return live
    return None


def _sqlite_backup(source: Path, dest: Path) -> None:
    """Copy a consistent snapshot, including committed WAL pages, into one file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = _open_ro(source)
    dst = sqlite3.connect(dest)
    try:
        dst.execute("PRAGMA journal_mode=DELETE")
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()


def _integrity_ok(path: Path) -> bool:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    return bool(row) and row[0] == "ok"


def _rewrite_artifact_text(value, id_map: dict[int, int]):
    if not isinstance(value, str) or "artifacts/task-" not in value:
        return value

    def repl(match: re.Match[str]) -> str:
        old = int(match.group(2))
        new = id_map.get(old)
        if new is None:
            return match.group(0)
        return f"{match.group(1)}{new}"

    return ARTIFACT_REF.sub(repl, value)


def _pid_alive(pid: int | None) -> bool:
    return fleet.pid_alive(pid)


def _command_for(pid: int, processes: list[tuple[int, str]]) -> str:
    for found, command in processes:
        if found == pid:
            return command
    return ""


def _worktree_for_pid(pid: int, cache: dict[int, Path | None] | None = None) -> Path | None:
    if cache is not None and pid in cache:
        return cache[pid]
    cwd = fleet.process_cwd(pid)
    if cwd is None:
        resolved = None
    else:
        root = fleet.repo_root_for_process_cwd(cwd)
        resolved = (root or cwd).resolve()
    if cache is not None:
        cache[pid] = resolved
    return resolved


def _metadata_pid(path: Path, role: str) -> int | None:
    payload = fleet.read_key_value_or_json(path)
    if payload.get("role") != role:
        return None
    try:
        return int(payload["pid"])
    except (KeyError, TypeError, ValueError):
        return None


def _port_open(payload: dict) -> bool:
    host = payload.get("host")
    port = payload.get("port")
    if not host or port is None:
        return False
    try:
        port_num = int(port)
    except (TypeError, ValueError):
        return False
    try:
        with socket.create_connection((str(host), port_num), timeout=0.2):
            return True
    except OSError:
        return False


def runtime_evidence(worktree: Path, processes: list[tuple[int, str]] | None = None,
                     cwd_cache: dict[int, Path | None] | None = None) -> dict:
    """Prove whether this worktree's worker and dashboard are alive.

    SQLite runtime status is ignored. A lock or dashboard file counts only
    when its PID is alive and the process command matches that role, or when
    the recorded dashboard port is accepting connections.
    """
    processes = fleet.process_table() if processes is None else processes
    root = worktree.expanduser().resolve()
    worker: list[str] = []
    dashboard: list[str] = []
    for pid, command in processes:
        if not fleet.is_orchestrator_command(command) and not DASHBOARD_COMMAND.search(command):
            continue
        owner = _worktree_for_pid(pid, cwd_cache)
        if owner != root:
            continue
        if fleet.is_orchestrator_command(command):
            worker.append(f"orchestrator pid {pid} cwd {owner}")
        if DASHBOARD_COMMAND.search(command):
            dashboard.append(f"dashboard pid {pid} cwd {owner}")

    lock_path = root / db.LOCK_FILE_NAME
    lock_pid = _metadata_pid(lock_path, "orchestrator")
    if lock_pid is not None and _pid_alive(lock_pid):
        command = _command_for(lock_pid, processes)
        if fleet.is_orchestrator_command(command) and _worktree_for_pid(lock_pid, cwd_cache) == root:
            evidence = f"lock {lock_path} pid {lock_pid}"
            if evidence not in worker:
                worker.append(evidence)

    dashboard_path = root / ".kanban-orchestra" / "dashboard.json"
    payload = fleet.read_key_value_or_json(dashboard_path)
    dash_pid = _metadata_pid(dashboard_path, "dashboard")
    if dash_pid is not None and _pid_alive(dash_pid):
        command = _command_for(dash_pid, processes)
        if DASHBOARD_COMMAND.search(command):
            evidence = f"dashboard metadata {dashboard_path} pid {dash_pid}"
            if evidence not in dashboard:
                dashboard.append(evidence)
    if payload and _port_open(payload):
        dashboard.append(
            f"dashboard port {payload.get('host')}:{payload.get('port')} is accepting connections"
        )
    return {
        "worktree": str(root),
        "worker_live": bool(worker),
        "dashboard_live": bool(dashboard),
        "worker_evidence": worker,
        "dashboard_evidence": dashboard,
        "lock_path": str(lock_path),
        "dashboard_path": str(dashboard_path),
    }


def _central_runtime_live(state_root: Path, queue_id: str, worktree: Path | None,
                          processes: list[tuple[int, str]]) -> dict:
    evidence = runtime_evidence(worktree, processes) if worktree and worktree.is_dir() else {
        "worker_live": False,
        "dashboard_live": False,
        "worker_evidence": [],
        "dashboard_evidence": [],
    }
    runtime = state_root / db.QUEUES_DIR_NAME / queue_id
    lock_pid = _metadata_pid(runtime / db.LOCK_FILE_NAME, "orchestrator")
    if lock_pid is not None and _pid_alive(lock_pid):
        command = _command_for(lock_pid, processes)
        if fleet.is_orchestrator_command(command):
            evidence["worker_live"] = True
            evidence["worker_evidence"].append(f"central lock pid {lock_pid}")
    dash_path = runtime / "dashboard.json"
    payload = fleet.read_key_value_or_json(dash_path)
    dash_pid = _metadata_pid(dash_path, "dashboard")
    if dash_pid is not None and _pid_alive(dash_pid) and DASHBOARD_COMMAND.search(_command_for(dash_pid, processes)):
        evidence["dashboard_live"] = True
        evidence["dashboard_evidence"].append(f"central dashboard pid {dash_pid}")
    if payload and _port_open(payload):
        evidence["dashboard_live"] = True
        evidence["dashboard_evidence"].append(
            f"central dashboard port {payload.get('host')}:{payload.get('port')} is accepting connections"
        )
    return evidence


def _fleet_roots(fleet_config: Path | None) -> list[Path]:
    path = fleet_config if fleet_config is not None else fleet.config_path()
    if not path.is_file():
        return []
    roots = []
    for raw in fleet.parse_config_lines(path.read_text(encoding="utf-8")):
        roots.append(fleet.expand_repo_path(raw))
    return roots


def _linked_worktrees(root: Path) -> list[Path]:
    found = []
    for entry in fleet.list_worktree_entries(root):
        if entry.missing:
            continue
        try:
            path = fleet.canonical_path(entry.path)
        except OSError:
            continue
        if path == root.resolve():
            continue
        if fleet.is_linked_worktree(path):
            found.append(path)
    return found


def discover_sources(
    extra_sources: list[Path] | None = None,
    fleet_config: Path | None = None,
) -> list[Path]:
    """Return legacy database files for the fleet and any explicit sources.

    Fleet discovery is read-only. Explicit sources may name a worktree or a
    database file. The same canonical file is returned once.
    """
    ordered: list[Path] = []
    seen: set[Path] = set()

    def add(candidate: Path | None) -> None:
        if candidate is None:
            return
        try:
            resolved = candidate.resolve()
        except OSError:
            return
        canonical = canonical_db_path(resolved)
        if canonical in seen or resolved in seen:
            return
        seen.add(canonical)
        ordered.append(resolved)

    for root in _fleet_roots(fleet_config):
        if not root.exists():
            continue
        try:
            root = fleet.canonical_path(root)
        except OSError:
            continue
        candidates = [root, *_linked_worktrees(root)]
        for checkout in candidates:
            live = checkout / db.LEGACY_DB_FILENAME
            if live.is_file() and db._peek_database_kind(live) == "legacy":
                add(live)
    for extra in extra_sources or []:
        resolved = resolve_source_file(Path(extra))
        if resolved is not None:
            add(resolved)
    return ordered


def _task_artifact_count(worktree: Path, task_id: int) -> int:
    root = worktree / ".kanban-orchestra" / "artifacts" / f"task-{task_id}"
    if not root.exists():
        return 0
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if not (Path(dirpath) / name).is_symlink()]
        for name in filenames:
            file_path = Path(dirpath) / name
            if file_path.is_symlink() or not file_path.is_file():
                continue
            count += 1
    return count


def _ensure_migration_tables(conn: sqlite3.Connection) -> None:
    parts = [statement.strip() for statement in db.SCHEMA_SQL.split(";") if "migration_" in statement]
    if parts:
        conn.executescript(";\n".join(parts) + ";")


def inspect_source(db_file: Path, processes: list[tuple[int, str]] | None = None,
                   cwd_cache: dict[int, Path | None] | None = None) -> dict:
    db_file = db_file.resolve()
    canonical = canonical_db_path(db_file)
    kind = db._peek_database_kind(db_file)
    if kind != "legacy":
        raise CutoverError(f"{db_file} is {kind}, not a legacy Kanban database")
    conn = _open_ro(db_file)
    try:
        tables = _tables(conn)
        if not db._schema_is_legacy(conn, tables):
            raise CutoverError(f"{db_file} is not a legacy per-worktree database")
        if "tasks" not in tables:
            raise CutoverError(f"{db_file} has no tasks table")
        task_cols = _columns(conn, "tasks")
        if "queue_id" in task_cols or "repository_id" in task_cols:
            raise CutoverError(f"{db_file} already has central queue columns")
        tasks = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        counts = {
            "tasks": len(tasks),
            "comments": _count_optional(conn, tables, "comments"),
            "run_log": _count_optional(conn, tables, "run_log"),
            "task_skips": _count_optional(conn, tables, "task_skips"),
            "task_agent_snapshots": _count_optional(conn, tables, "task_agent_snapshots"),
        }
        task_ids = [int(row["id"]) for row in tasks]
    finally:
        conn.close()
    worktree = canonical.parent
    evidence = runtime_evidence(worktree, processes, cwd_cache)
    artifact_count = sum(_task_artifact_count(worktree, task_id) for task_id in task_ids)
    return {
        "path": str(canonical),
        "reading": str(db_file),
        "inactive_backup": db_file != canonical,
        "identity": source_identity(canonical),
        "queue_id": queue_identity(canonical),
        "repository_id": repository_identity(worktree),
        "worktree": str(worktree),
        "task_ids": task_ids,
        "counts": counts,
        "artifact_count": artifact_count,
        **evidence,
    }


def _count_optional(conn: sqlite3.Connection, tables: set[str], name: str) -> int:
    if name not in tables:
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])


def _existing_provenance(destination: Path) -> tuple[dict[str, dict[int, int]], int]:
    if not destination.is_file() or db._peek_database_kind(destination) != "current":
        return {}, 0
    conn = _open_ro(destination)
    try:
        tables = _tables(conn)
        max_id = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM tasks").fetchone()[0])
        maps: dict[str, dict[int, int]] = {}
        if "migration_task_map" not in tables or "migration_sources" not in tables:
            return maps, max_id
        for row in conn.execute(
            "SELECT source_id, old_task_id, new_task_id FROM migration_task_map ORDER BY source_id, old_task_id"
        ):
            maps.setdefault(row["source_id"], {})[int(row["old_task_id"])] = int(row["new_task_id"])
        return maps, max_id
    finally:
        conn.close()


def _plan_maps(sources: list[dict], destination: Path) -> dict[str, dict[int, int]]:
    existing, next_id = _existing_provenance(destination)
    next_id += 1
    planned: dict[str, dict[int, int]] = {}
    for source in sorted(sources, key=lambda item: item["path"]):
        recorded = existing.get(source["identity"])
        if recorded is not None:
            if set(recorded) != set(source["task_ids"]):
                raise CutoverError(
                    f"Recorded id map for {source['path']} does not match its tasks. "
                    "Refusing to import it again."
                )
            planned[source["identity"]] = dict(recorded)
            continue
        mapping: dict[int, int] = {}
        for old_id in source["task_ids"]:
            mapping[old_id] = next_id
            next_id += 1
        planned[source["identity"]] = mapping
    return planned


def _destination_report(destination: Path) -> dict:
    if destination.exists() and not destination.is_file():
        raise CutoverError(f"Destination is not a file: {destination}")
    if not destination.exists():
        kind = "absent"
    else:
        kind = db._peek_database_kind(destination)
    return {"path": str(destination), "exists": destination.exists(), "kind": kind}


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _jsonable(value):
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    return value


def _durable_snapshot(path: Path):
    """Return the durable rows of a legacy database or its cutover backup.

    The snapshot is logical table content. Page layout and WAL sidecars are
    not part of the comparison.
    """
    conn = _open_ro(path)
    try:
        payload = []
        for name in sorted(_tables(conn)):
            columns = [
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({_quote_ident(name)})")
            ]
            quoted = ", ".join(_quote_ident(column) for column in columns)
            rows = conn.execute(
                f"SELECT {quoted} FROM {_quote_ident(name)} ORDER BY {quoted}"
            ).fetchall()
            payload.append({
                "table": name,
                "columns": columns,
                "rows": [[_jsonable(row[column]) for column in columns] for row in rows],
            })
        return payload
    finally:
        conn.close()


def _integrity_ok_ro(path: Path) -> bool:
    try:
        conn = _open_ro(path)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return bool(row) and row[0] == "ok"


def _import_records(destination: Path) -> dict[str, sqlite3.Row]:
    if not destination.is_file() or db._peek_database_kind(destination) != "current":
        return {}
    conn = _open_ro(destination)
    try:
        if "migration_sources" not in _tables(conn):
            return {}
        return {
            row["id"]: row
            for row in conn.execute(
                "SELECT id, status, backup_path FROM migration_sources"
            )
        }
    finally:
        conn.close()


def _changed_import_problems(destination: Path, sources: list[dict]) -> list[str]:
    """Refuse a present source whose rows differ from its cutover backup.

    An absent previously imported source is unchanged. A present source whose
    durable rows match the verified backup is marked safe to retire. Any
    other present previously imported source becomes a problem so cutover
    stops before importing a different source or deleting this file.
    """
    records = _import_records(destination)
    problems = []
    for source in sources:
        record = records.get(source["identity"])
        if record is None or record["status"] not in {"imported", "activated"}:
            continue
        live = Path(source["path"])
        if not live.is_file() or live.is_symlink():
            continue
        backup_text = record["backup_path"]
        backup = Path(backup_text) if backup_text else None
        if backup is None or not backup.is_file():
            problems.append(
                f"Previously imported source {live} is present but its recorded "
                "cutover snapshot is missing"
            )
            continue
        if not _integrity_ok_ro(backup):
            problems.append(
                f"Previously imported source {live} is present but its recorded "
                "cutover snapshot failed integrity check"
            )
            continue
        if _durable_snapshot(live) != _durable_snapshot(backup):
            problems.append(
                f"Previously imported source {live} has durable content that differs "
                "from its recorded cutover snapshot"
            )
            continue
        source["backup_path"] = str(backup)
        source["snapshot_matches"] = True
    return problems


def _problems(sources: list[dict], destination: dict) -> list[str]:
    problems = []
    if destination["kind"] in {"legacy", "incompatible"}:
        problems.append(
            f"Destination {destination['path']} is {destination['kind']} and cannot be activated"
        )
    identities = [item["identity"] for item in sources]
    if len(identities) != len(set(identities)):
        problems.append("Duplicate source identities")
    paths = [item["path"] for item in sources]
    if len(paths) != len(set(paths)):
        problems.append("Duplicate source paths")
    for source in sources:
        if source["path"] == destination["path"]:
            problems.append(f"Source and destination are the same file: {source['path']}")
        conflict = _identity_conflict(source)
        if conflict:
            problems.append(conflict)
        tracked = _tracked_legacy_paths(Path(source["worktree"]))
        if tracked:
            problems.append(
                f"Tracked legacy database files in {source['worktree']}: {', '.join(tracked)}. "
                "Untrack them before cutover so activation can remove the untracked copies."
            )
    return problems


def _rollback_steps(destination: Path) -> list[str]:
    return [
        "Stop every worker and dashboard for the source worktrees and the destination queues. Rollback refuses while any of them are still live.",
        "Confirm no orchestrator.py or dashboard.py process is alive for those paths. A stale status row is not proof.",
        f"Run ko-migrate rollback --destination {destination} before starting any new worker.",
        "Rollback restores the previous central database if there was one, copies each source backup back into its worktree, removes queue identity files created by that attempt, and removes queue directories created by that attempt.",
        "Do not start workers until rollback finishes.",
    ]


def build_plan(
    destination: Path,
    extra_sources: list[Path] | None = None,
    fleet_config: Path | None = None,
    *,
    processes: list[tuple[int, str]] | None = None,
) -> dict:
    """Inventory sources and the destination without creating or changing files."""
    processes = fleet.process_table() if processes is None else processes
    cwd_cache: dict[int, Path | None] = {}
    files = discover_sources(extra_sources, fleet_config)
    sources = [inspect_source(path, processes, cwd_cache) for path in files]
    destination_info = _destination_report(destination)
    problems = _problems(sources, destination_info)
    problems.extend(_changed_import_problems(destination, sources))
    maps = {} if problems else _plan_maps(sources, destination)
    reported = []
    for source in sources:
        mapping = maps.get(source["identity"], {})
        reported.append({
            **source,
            "planned_id_map": {str(old): new for old, new in mapping.items()},
        })
    live = [
        source["path"]
        for source in sources
        if source["worker_live"] or source["dashboard_live"]
    ]
    return {
        "mode": "preview",
        "destination": destination_info,
        "sources": reported,
        "problems": problems,
        "workers_live": live,
        "rollback_steps": _rollback_steps(destination),
        "development_queue_note": DEVELOPMENT_NOTE,
        "summary": _summary(reported, destination_info, problems, live),
    }


def _summary(sources: list[dict], destination: dict, problems: list[str], live: list[str]) -> str:
    lines = [
        f"Destination: {destination['path']} ({destination['kind']})",
        f"Sources: {len(sources)}",
    ]
    for source in sources:
        state = "worker/dashboard live" if source["path"] in live or source.get("worker_live") or source.get("dashboard_live") else "stopped"
        lines.append(
            f"- {source['path']} identity {source['identity']} "
            f"tasks {source['counts']['tasks']} {state}"
        )
    if problems:
        lines.append("Problems: " + "; ".join(problems))
    lines.append(DEVELOPMENT_NOTE)
    return "\n".join(lines)


def _validate_ready(plan: dict) -> None:
    if plan["problems"]:
        raise CutoverError("; ".join(plan["problems"]))
    if plan["workers_live"]:
        details = []
        for source in plan["sources"]:
            if source["path"] not in plan["workers_live"]:
                continue
            details.extend(source["worker_evidence"])
            details.extend(source["dashboard_evidence"])
        raise WorkersLive(
            "Refusing cutover while a worker or dashboard is live: " + "; ".join(details)
        )


def _load_source_rows(db_file: Path) -> dict:
    conn = _open_ro(db_file)
    try:
        tables = _tables(conn)
        return {
            "tasks": conn.execute("SELECT * FROM tasks ORDER BY id").fetchall(),
            "comments": conn.execute("SELECT * FROM comments ORDER BY id").fetchall() if "comments" in tables else [],
            "run_log": conn.execute("SELECT * FROM run_log ORDER BY id").fetchall() if "run_log" in tables else [],
            "skips": conn.execute("SELECT * FROM task_skips ORDER BY task_id, step").fetchall() if "task_skips" in tables else [],
            "snapshots": conn.execute("SELECT * FROM task_agent_snapshots").fetchall() if "task_agent_snapshots" in tables else [],
            "runtime": conn.execute("SELECT * FROM orchestrator_runtime").fetchall() if "orchestrator_runtime" in tables else [],
            "worker_config": conn.execute("SELECT * FROM agent_worker_config").fetchall() if "agent_worker_config" in tables else [],
        }
    finally:
        conn.close()


def _check_source_shape(rows: dict, source: dict) -> None:
    for task in rows["tasks"]:
        status = _cell(task, "status")
        if status not in TASK_STATUSES:
            raise CutoverError(f"Unsupported task status {status!r} in {source['path']}")
        step = _cell(task, "next_step") or "commit-make"
        if step not in NEXT_STEPS:
            raise CutoverError(f"Unsupported next_step {step!r} in {source['path']}")
    for comment in rows["comments"]:
        kind = _cell(comment, "kind") or "comment"
        if kind not in COMMENT_KINDS:
            raise CutoverError(f"Unsupported comment kind {kind!r} in {source['path']}")
    known = set(source["task_ids"])
    for task in rows["tasks"]:
        for label in ("parent_task_id", "follow_up_task_id"):
            ref = _cell(task, label)
            if ref is not None and int(ref) not in known:
                raise CutoverError(
                    f"Task {task['id']} in {source['path']} references missing {label} {ref}"
                )


def _copy_artifacts(worktree: Path, queue_root: Path, id_map: dict[int, int]) -> int:
    copied = 0
    source_root = worktree / ".kanban-orchestra" / "artifacts"
    for old_id, new_id in id_map.items():
        src = source_root / f"task-{old_id}"
        if not src.exists():
            continue
        dest = queue_root / "artifacts" / f"task-{new_id}"
        dest.mkdir(parents=True, exist_ok=True)
        for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
            dirnames[:] = [name for name in dirnames if not (Path(dirpath) / name).is_symlink()]
            relative = Path(dirpath).relative_to(src)
            target = dest / relative
            target.mkdir(parents=True, exist_ok=True)
            for name in filenames:
                file_path = Path(dirpath) / name
                if file_path.is_symlink() or not file_path.is_file():
                    continue
                shutil.copy2(file_path, target / name)
                copied += 1
    return copied


def _bump_sequence(conn: sqlite3.Connection, table: str, value: int) -> None:
    if value <= 0:
        return
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, value))
    elif int(row[0]) < value:
        conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (value, table))


def _import_source(conn: sqlite3.Connection, source: dict, rows: dict, attempt_id: str,
                   backup_path: Path, queue_root: Path) -> dict:
    id_map = {int(old): int(new) for old, new in source["planned_id_map"].items()}
    repository_id = source["repository_id"]
    queue_id = source["queue_id"]
    conn.execute("INSERT OR IGNORE INTO repositories (id) VALUES (?)", (repository_id,))
    existing_queue = conn.execute("SELECT * FROM queues WHERE id = ?", (queue_id,)).fetchone()
    if existing_queue is None:
        path_owner = conn.execute(
            "SELECT id FROM queues WHERE path = ? AND state = 'enabled'",
            (source["worktree"],),
        ).fetchone()
        if path_owner is not None:
            raise CutoverError(
                f"Another enabled queue already uses {source['worktree']}"
            )
        db._insert_queue(
            conn, repository_id, source["worktree"], queue_id=queue_id, state="enabled",
        )
    elif existing_queue["repository_id"] != repository_id or existing_queue["path"] != source["worktree"]:
        raise CutoverError(f"Queue {queue_id} does not match {source['path']}")

    interrupted: list[int] = []
    for task in rows["tasks"]:
        old_id = int(task["id"])
        new_id = id_map[old_id]
        fields = {name: _cell(task, name) for name in TASK_COLUMNS}
        if fields.get("kind") is None:
            fields["kind"] = "commit"
        if fields.get("status") == "running":
            fields["status"] = "blocked"
            fields["block_reason"] = RECOVERY_REASON
            if not fields.get("resume_next_step"):
                fields["resume_next_step"] = fields.get("next_step")
            interrupted.append(new_id)
        columns = ["id", "repository_id", "queue_id", *fields.keys()]
        values = [new_id, repository_id, queue_id, *fields.values()]
        conn.execute(
            f"INSERT INTO tasks ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            values,
        )
    for task in rows["tasks"]:
        old_id = int(task["id"])
        parent = _cell(task, "parent_task_id")
        follow = _cell(task, "follow_up_task_id")
        conn.execute(
            "UPDATE tasks SET parent_task_id = ?, follow_up_task_id = ? WHERE id = ?",
            (
                id_map[int(parent)] if parent is not None else None,
                id_map[int(follow)] if follow is not None else None,
                id_map[old_id],
            ),
        )
    for snapshot in rows["snapshots"]:
        old_id = int(snapshot["task_id"])
        if old_id not in id_map:
            raise CutoverError(f"Snapshot references missing task {old_id}")
        conn.execute(
            "INSERT INTO task_agent_snapshots (task_id, snapshot) VALUES (?, ?)",
            (id_map[old_id], _rewrite_artifact_text(snapshot["snapshot"], id_map)),
        )
    for skip in rows["skips"]:
        old_id = int(skip["task_id"])
        if old_id not in id_map:
            raise CutoverError(f"Skip references missing task {old_id}")
        conn.execute(
            "INSERT INTO task_skips (task_id, step) VALUES (?, ?)",
            (id_map[old_id], skip["step"]),
        )
    unattached_comments: list[tuple[int, int]] = []
    for comment in rows["comments"]:
        old_task_id = _cell(comment, "task_id")
        new_task = None if old_task_id is None else id_map.get(int(old_task_id))
        if old_task_id is not None and new_task is None:
            raise CutoverError(f"Comment references missing task {old_task_id}")
        conn.execute(
            """INSERT INTO comments (
                   task_id, review_round, verb, author, message, kind, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                new_task,
                _cell(comment, "review_round"),
                _cell(comment, "verb"),
                _cell(comment, "author"),
                _rewrite_artifact_text(_cell(comment, "message"), id_map),
                _cell(comment, "kind") or "comment",
                _cell(comment, "created_at"),
            ),
        )
        if old_task_id is None:
            old_comment_id = _cell(comment, "id")
            if old_comment_id is None:
                raise CutoverError(f"Unattached comment in {source['path']} has no id")
            new_comment_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            unattached_comments.append((int(old_comment_id), new_comment_id))
    for entry in rows["run_log"]:
        old_id = _cell(entry, "task_id")
        new_task = None if old_id is None else id_map.get(int(old_id))
        if old_id is not None and new_task is None:
            raise CutoverError(f"Run log references missing task {old_id}")
        conn.execute(
            """INSERT INTO run_log (task_id, queue_id, verb, author, message, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                new_task,
                queue_id,
                _cell(entry, "verb"),
                _cell(entry, "author"),
                _rewrite_artifact_text(_cell(entry, "message"), id_map),
                _cell(entry, "created_at"),
            ),
        )
    for new_id in interrupted:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (new_id,)).fetchone()
        conn.execute(
            """INSERT INTO comments (task_id, review_round, verb, author, message, kind)
               VALUES (?, 0, 'migrate-cutover', 'kanban', ?, 'comment')""",
            (
                new_id,
                (
                    "Interrupted at cutover and left blocked for explicit recovery. "
                    f"Worktree: {source['worktree']}. "
                    f"Branch: {task['branch'] or '(none)'}. "
                    f"Stash: {task['stash_ref'] or '(none)'}. "
                    f"Resume step: {task['resume_next_step'] or '(none)'}. "
                    "The migration did not requeue, reassign, or clean this task."
                ),
            ),
        )
    if rows["runtime"]:
        runtime = rows["runtime"][0]
        current = _cell(runtime, "current_task_id")
        current_new = id_map.get(int(current)) if current is not None else None
        message = _cell(runtime, "status_message") or ""
        if interrupted:
            message = (
                "Interrupted at cutover. Recover the blocked task explicitly. "
                "It was not requeued. " + message
            ).strip()
        else:
            message = ("Migrated from a legacy database. Start the central worker explicitly. " + message).strip()
        conn.execute(
            """INSERT INTO orchestrator_runtime (
                   queue_id, status, pid, started_at, last_heartbeat_at, current_task_id,
                   current_step, current_branch, review_round, active_agents, status_message
               ) VALUES (?, 'stopped', NULL, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (
                queue_id,
                _cell(runtime, "started_at"),
                _cell(runtime, "last_heartbeat_at"),
                current_new,
                _cell(runtime, "current_step") if _cell(runtime, "current_step") in NEXT_STEPS else None,
                _cell(runtime, "current_branch"),
                _cell(runtime, "review_round"),
                message,
            ),
        )
    if rows["worker_config"]:
        fingerprint = _cell(rows["worker_config"][0], "fingerprint")
        if fingerprint:
            conn.execute(
                "INSERT OR REPLACE INTO agent_worker_config (queue_id, fingerprint) VALUES (?, ?)",
                (queue_id, fingerprint),
            )
    conn.execute(
        """INSERT INTO migration_sources (
               id, source_path, queue_id, repository_id, status, task_count, attempt_id, backup_path
           ) VALUES (?, ?, ?, ?, 'imported', ?, ?, ?)""",
        (
            source["identity"], source["path"], queue_id, repository_id,
            source["counts"]["tasks"], attempt_id, str(backup_path),
        ),
    )
    for old_id, new_id in id_map.items():
        conn.execute(
            "INSERT INTO migration_task_map (source_id, old_task_id, new_task_id) VALUES (?, ?, ?)",
            (source["identity"], old_id, new_id),
        )
    for old_comment_id, new_comment_id in unattached_comments:
        conn.execute(
            """INSERT INTO migration_comment_map (
                   source_id, old_comment_id, new_comment_id, queue_id
               ) VALUES (?, ?, ?, ?)""",
            (source["identity"], old_comment_id, new_comment_id, queue_id),
        )
    if id_map:
        _bump_sequence(conn, "tasks", max(id_map.values()))
    copied = _copy_artifacts(Path(source["worktree"]), queue_root, id_map)
    return {
        "id_map": id_map,
        "copied_artifacts": copied,
        "interrupted": interrupted,
        "unattached_comments": len(unattached_comments),
    }


def _verify_staging(conn: sqlite3.Connection, imported: list[tuple[dict, dict, dict]],
                    artifact_roots: dict[str, Path]) -> None:
    fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk_errors:
        raise CutoverError(f"Staging database failed foreign key check: {fk_errors[:5]}")
    running = conn.execute("SELECT id FROM tasks WHERE status = 'running'").fetchall()
    if running:
        raise CutoverError("Staging database still has running tasks")
    for source, rows, result in imported:
        id_map = result["id_map"]
        queue_id = source["queue_id"]
        repository_id = source["repository_id"]
        for old_id, new_id in id_map.items():
            task = conn.execute("SELECT * FROM tasks WHERE id = ?", (new_id,)).fetchone()
            if task is None:
                raise CutoverError(f"Missing imported task {new_id}")
            if task["queue_id"] != queue_id or task["repository_id"] != repository_id:
                raise CutoverError(f"Task {new_id} has the wrong owner")
            source_row = next(row for row in rows["tasks"] if int(row["id"]) == old_id)
            if _cell(source_row, "status") == "running":
                if task["status"] != "blocked" or task["block_reason"] != RECOVERY_REASON:
                    raise CutoverError(f"Running task {old_id} was not parked for recovery")
                if task["stash_ref"] != _cell(source_row, "stash_ref"):
                    raise CutoverError(f"Recovery stash was dropped for task {old_id}")
            elif task["status"] != _cell(source_row, "status"):
                raise CutoverError(f"Status changed for task {old_id}")
            parent = _cell(source_row, "parent_task_id")
            follow = _cell(source_row, "follow_up_task_id")
            expect_parent = id_map[int(parent)] if parent is not None else None
            expect_follow = id_map[int(follow)] if follow is not None else None
            if task["parent_task_id"] != expect_parent or task["follow_up_task_id"] != expect_follow:
                raise CutoverError(f"Relationship rewrite failed for task {old_id}")
        attached_source = sum(
            1 for comment in rows["comments"] if _cell(comment, "task_id") is not None
        )
        unattached_source = len(rows["comments"]) - attached_source
        attached_count = conn.execute(
            """SELECT COUNT(*) FROM comments
               WHERE task_id IN (SELECT new_task_id FROM migration_task_map WHERE source_id = ?)
                 AND COALESCE(verb, '') != 'migrate-cutover'""",
            (source["identity"],),
        ).fetchone()[0]
        if int(attached_count) != attached_source:
            raise CutoverError(f"Attached comment count mismatch for {source['path']}")
        unattached_count = conn.execute(
            """SELECT COUNT(*) FROM comments AS c
               JOIN migration_comment_map AS m ON m.new_comment_id = c.id
               WHERE m.source_id = ? AND m.queue_id = ? AND c.task_id IS NULL""",
            (source["identity"], queue_id),
        ).fetchone()[0]
        if int(unattached_count) != unattached_source:
            raise CutoverError(f"Unattached comment count mismatch for {source['path']}")
        if int(unattached_count) != int(result["unattached_comments"]):
            raise CutoverError(f"Unattached comment provenance mismatch for {source['path']}")
        skip_count = conn.execute(
            """SELECT COUNT(*) FROM task_skips
               WHERE task_id IN (SELECT new_task_id FROM migration_task_map WHERE source_id = ?)""",
            (source["identity"],),
        ).fetchone()[0]
        if int(skip_count) != len(rows["skips"]):
            raise CutoverError(f"Skip count mismatch for {source['path']}")
        snapshot_count = conn.execute(
            """SELECT COUNT(*) FROM task_agent_snapshots
               WHERE task_id IN (SELECT new_task_id FROM migration_task_map WHERE source_id = ?)""",
            (source["identity"],),
        ).fetchone()[0]
        if int(snapshot_count) != len(rows["snapshots"]):
            raise CutoverError(f"Snapshot count mismatch for {source['path']}")
        run_count = conn.execute(
            "SELECT COUNT(*) FROM run_log WHERE queue_id = ?",
            (queue_id,),
        ).fetchone()[0]
        if int(run_count) != len(rows["run_log"]):
            raise CutoverError(f"Run log count mismatch for {source['path']}")
        if result["copied_artifacts"] != source["artifact_count"]:
            raise CutoverError(f"Artifact count mismatch for {source['path']}")
        root = artifact_roots[source["identity"]]
        for old_id, new_id in id_map.items():
            src = Path(source["worktree"]) / ".kanban-orchestra" / "artifacts" / f"task-{old_id}"
            dest_dir = root / "artifacts" / f"task-{new_id}"
            if src.is_dir() and not dest_dir.is_dir():
                raise CutoverError(f"Missing artifact directory for task {new_id}")


def _source_bytes(path: Path) -> bytes:
    return path.read_bytes() if path.is_file() else b""


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _fsync(tmp)
    os.replace(tmp, path)


def _legacy_file_paths(db_file: Path) -> list[Path]:
    return [Path(str(db_file) + suffix) for suffix in ("", "-wal", "-shm", "-journal")]


def _tracked_legacy_paths(worktree: Path) -> list[str]:
    """Return legacy database filenames that Git currently tracks."""
    names = [
        path.name
        for path in _legacy_file_paths(worktree / db.LEGACY_DB_FILENAME)
        if path.is_file()
    ]
    if not names:
        return []
    result = subprocess.run(
        ["git", "-C", str(worktree), "ls-files", "--", *names],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _identity_conflict(source: dict) -> str | None:
    worktree = Path(source["worktree"])
    repo_dir, queue_dir = db._identity_dirs(worktree)
    try:
        recorded_repo = db._read_identity(repo_dir / db.REPOSITORY_ID_FILENAME)
        recorded_queue = db._read_identity(queue_dir / db.QUEUE_ID_FILENAME)
    except RuntimeError as exc:
        return str(exc)
    if recorded_repo and recorded_repo != source["repository_id"]:
        return f"Repository identity at {worktree} does not match the migration id"
    if recorded_queue and recorded_queue != source["queue_id"]:
        return f"Queue identity at {worktree} does not match the migration id"
    return None


def _ensure_identity(source: dict) -> list[dict]:
    """Write the migrated repository and queue ids where a normal launch reads them."""
    worktree = Path(source["worktree"])
    repo_dir, queue_dir = db._identity_dirs(worktree)
    created = []
    for path, value in (
        (repo_dir / db.REPOSITORY_ID_FILENAME, source["repository_id"]),
        (queue_dir / db.QUEUE_ID_FILENAME, source["queue_id"]),
    ):
        existing = path.read_text(encoding="utf-8").strip() if path.is_file() else None
        if existing == value:
            continue
        if existing:
            raise CutoverError(f"Refusing to replace identity file {path}")
        db._write_identity(path, value)
        created.append({"path": str(path), "value": value})
    return created


def _present_legacy_files(db_file: Path) -> list[Path]:
    return [
        path for path in _legacy_file_paths(db_file)
        if path.is_file() and not path.is_symlink()
    ]


def _journal_legacy_removal(manifest: dict, pointer: Path, paths: list[Path]) -> None:
    """Record legacy paths before they are unlinked.

    A crash after this write and before the unlink leaves the original files
    in place. A crash after the unlink still has the paths on disk.
    """
    known = set(manifest.get("removed_legacy_files", []))
    changed = False
    for path in paths:
        text = str(path)
        if text in known:
            continue
        manifest.setdefault("removed_legacy_files", []).append(text)
        known.add(text)
        changed = True
    if changed:
        _write_json(pointer, manifest)


def _unlink_legacy_files(paths: list[Path]) -> list[str]:
    removed = []
    for path in paths:
        if path.is_file() and not path.is_symlink():
            path.unlink()
            removed.append(str(path))
    return removed


def _remove_untracked_legacy(source: dict, manifest: dict | None = None,
                             pointer: Path | None = None) -> list[str]:
    """Remove an untracked legacy database after its consistent backup is stored."""
    paths = _present_legacy_files(Path(source["path"]))
    if manifest is not None and pointer is not None:
        _journal_legacy_removal(manifest, pointer, paths)
    return _unlink_legacy_files(paths)


def _path_is_imported(destination: Path, canonical: Path) -> bool:
    if not destination.is_file() or db._peek_database_kind(destination) != "current":
        return False
    conn = _open_ro(destination)
    try:
        if "migration_sources" not in _tables(conn):
            return False
        row = conn.execute(
            "SELECT status FROM migration_sources WHERE source_path = ?",
            (str(canonical),),
        ).fetchone()
    finally:
        conn.close()
    return row is not None and row["status"] in {"imported", "activated"}


def _assert_absent_sources_imported(destination: Path, extra_sources: list[Path] | None) -> None:
    for extra in extra_sources or []:
        resolved = resolve_source_file(Path(extra))
        if resolved is not None:
            continue
        candidate = Path(extra).expanduser().resolve()
        if not candidate.is_dir():
            raise CutoverError(f"Migration source not found: {candidate}")
        canonical = candidate / db.LEGACY_DB_FILENAME
        if not _path_is_imported(destination, canonical):
            raise CutoverError(
                f"No legacy database at {candidate} and that worktree has not been imported"
            )


def _remove_sidecars(db_file: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(db_file) + suffix)
        if sidecar.exists():
            sidecar.unlink()


def _restore_missing_source(source: dict) -> None:
    """Copy a verified backup back when the live legacy file is gone.

    Removal is journaled first, but a crash between unlink and that write
    leaves the file absent while ``removed_legacy_files`` does not list it.
    The source entry's path and backup are the source of truth: a missing
    live file is restored, and leftover sidecars are dropped so SQLite does
    not replay a stale WAL onto the consistent backup.
    """
    live = Path(source["path"])
    if live.exists():
        return
    backup = source.get("backup_path")
    backup_path = Path(backup) if backup else None
    if backup_path is None or not backup_path.is_file():
        raise CutoverError(f"Cannot restore missing source {live}; backup is absent")
    if not _integrity_ok(backup_path):
        raise CutoverError(f"Refusing to restore {live} from a backup that failed integrity check")
    live.parent.mkdir(parents=True, exist_ok=True)
    temporary = live.with_name(live.name + ".restore-tmp")
    shutil.copy2(backup_path, temporary)
    _fsync(temporary)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(live) + suffix)
        if sidecar.is_file() and not sidecar.is_symlink():
            sidecar.unlink()
    os.replace(temporary, live)


def _apply_rollback(manifest: dict) -> dict:
    destination = Path(manifest["destination"])
    for source in manifest.get("sources", []):
        _restore_missing_source(source)
    for item in manifest.get("created_identity_files", []):
        path = Path(item["path"])
        if path.is_file() and path.read_text(encoding="utf-8").strip() == item["value"]:
            path.unlink()
    before = manifest.get("destination_before")
    if manifest.get("destination_existed") and before:
        backup = Path(before)
        if not backup.is_file():
            raise CutoverError(f"Rollback backup is missing: {backup}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        restored = destination.with_name(destination.name + ".rollback-tmp")
        shutil.copy2(backup, restored)
        os.replace(restored, destination)
        _remove_sidecars(destination)
    elif destination.exists():
        destination.unlink()
        _remove_sidecars(destination)
    for directory in manifest.get("created_queue_dirs", []):
        shutil.rmtree(directory, ignore_errors=True)
    manifest["activated"] = False
    manifest["rolled_back"] = True
    pointer = Path(manifest["manifest_path"])
    _write_json(pointer, manifest)
    return {"rolled_back": True, "destination": str(destination), "manifest": str(pointer)}


def _rollback_live_details(manifest: dict, processes: list[tuple[int, str]]) -> list[str]:
    details = []
    for source in manifest.get("sources", []):
        evidence = runtime_evidence(Path(source["worktree"]), processes, {})
        if evidence["worker_live"] or evidence["dashboard_live"]:
            details.extend(evidence["worker_evidence"])
            details.extend(evidence["dashboard_evidence"])
    destination = Path(manifest["destination"])
    if destination.is_file():
        for row in _destination_queue_evidence(destination, processes):
            if row["worker_live"] or row["dashboard_live"]:
                details.extend(row["worker_evidence"])
                details.extend(row["dashboard_evidence"])
    return details


def rollback(destination: Path, manifest_path: Path | None = None,
             fleet_config: Path | None = None) -> dict:
    """Restore the pre-activation database and source backups.

    Refuses while a source worktree or a destination queue still has a live
    worker or dashboard. Destination evidence uses the same process, lock,
    and port checks as cutover.
    """
    pointer = manifest_path or destination.expanduser().resolve().parent / "migration-rollback.json"
    if not pointer.is_file():
        raise CutoverError(f"Rollback manifest not found: {pointer}")
    manifest = json.loads(pointer.read_text(encoding="utf-8"))
    processes = fleet.process_table()
    details = _rollback_live_details(manifest, processes)
    if details:
        raise WorkersLive(
            "Refusing rollback while a source or destination worker or dashboard is live: "
            + "; ".join(details)
        )
    return _apply_rollback(manifest)


def _complete_sources(destination: Path, sources: list[dict]) -> set[str]:
    if not destination.is_file() or db._peek_database_kind(destination) != "current":
        return set()
    conn = _open_ro(destination)
    try:
        if "migration_sources" not in _tables(conn):
            return set()
        done = set()
        for source in sources:
            row = conn.execute(
                "SELECT status, task_count FROM migration_sources WHERE id = ?",
                (source["identity"],),
            ).fetchone()
            if row is None:
                continue
            if int(row["task_count"]) != int(source["counts"]["tasks"]):
                raise CutoverError(
                    f"Stored migration count for {source['path']} does not match the source"
                )
            mapped = conn.execute(
                "SELECT COUNT(*) FROM migration_task_map WHERE source_id = ?",
                (source["identity"],),
            ).fetchone()[0]
            if int(mapped) != int(source["counts"]["tasks"]):
                raise CutoverError(f"Stored id map for {source['path']} is incomplete")
            if row["status"] in {"imported", "activated"}:
                done.add(source["identity"])
        return done
    finally:
        conn.close()


def _legacy_file_present(source: dict) -> bool:
    live = Path(source["path"])
    return live.is_file() and not live.is_symlink()


def _assert_recorded_snapshot(source: dict) -> None:
    """Refuse removal when the live legacy file is not its cutover snapshot."""
    live = Path(source["path"])
    backup_text = source.get("backup_path")
    backup = Path(backup_text) if backup_text else None
    if not source.get("snapshot_matches") or backup is None or not backup.is_file():
        raise CutoverError(
            f"Refusing to remove {live} without a matching recorded cutover snapshot"
        )
    if not _integrity_ok_ro(backup):
        raise CutoverError(
            f"Refusing to remove {live}: recorded cutover snapshot failed integrity check"
        )
    if _durable_snapshot(live) != _durable_snapshot(backup):
        raise CutoverError(
            f"Previously imported source {live} has durable content that differs "
            "from its recorded cutover snapshot"
        )


def _manifest_source(source: dict) -> dict:
    return {
        "path": source["path"],
        "worktree": source["worktree"],
        "identity": source["identity"],
        "queue_id": source["queue_id"],
        "backup_path": source["backup_path"],
    }


def _retire_imported_sources(destination: Path, sources: list[dict]) -> tuple[list[dict], list[str]]:
    """Remove a present legacy file only when it matches its cutover snapshot.

    The rollback manifest records the verified backup before the unlink. A
    source whose durable rows differ is a plan problem and never reaches
    this removal. An absent source is left absent.
    """
    created: list[dict] = []
    pending_removal: list[tuple[dict, list[Path]]] = []
    for source in sources:
        if _legacy_file_present(source):
            _assert_recorded_snapshot(source)
            pending_removal.append((source, _present_legacy_files(Path(source["path"]))))
        created.extend(_ensure_identity(source))
    if not pending_removal:
        return created, []
    pointer = destination.parent / "migration-rollback.json"
    if not pointer.is_file():
        raise CutoverError(
            "Refusing to remove a previously imported legacy database without a rollback manifest"
        )
    manifest = json.loads(pointer.read_text(encoding="utf-8"))
    known = {item.get("path"): item for item in manifest.get("sources", [])}
    for source, _paths in pending_removal:
        _assert_recorded_snapshot(source)
        entry = known.get(source["path"])
        if entry is None:
            entry = _manifest_source(source)
            manifest.setdefault("sources", []).append(entry)
            known[source["path"]] = entry
        else:
            entry["backup_path"] = source["backup_path"]
    _write_json(pointer, manifest)
    removed: list[str] = []
    for source, paths in pending_removal:
        _assert_recorded_snapshot(source)
        _journal_legacy_removal(manifest, pointer, paths)
        removed.extend(_unlink_legacy_files(paths))
    return created, removed


def cutover(
    destination: Path | None = None,
    extra_sources: list[Path] | None = None,
    fleet_config: Path | None = None,
    *,
    fail_after: str | None = None,
) -> dict:
    """Import legacy databases and activate one central database.

    A failure before activation leaves ``destination`` unchanged. Source
    database bytes are never modified. Successful activation writes queue
    identity files, then removes untracked legacy files after journaling
    those paths. Their consistent backups stay under the destination's
    migration-attempts directory. A later cutover refuses a present
    previously imported source whose durable rows differ from that backup,
    and it does not import other sources or delete that file. A present
    copy whose rows still match is removed in that same run, including
    when the run also imports a new source. The attempt manifest records
    the verified backup before the unlink, and rollback restores that
    file with the previous central database. Rollback restores a missing
    source from its verified backup even when that journal did not record
    the unlink.
    """
    destination = (destination or db.default_db_path()).expanduser().resolve()
    if destination.is_file() and db.unfinished_activation_reason(destination):
        pointer = destination.parent / "migration-rollback.json"
        if not pointer.is_file():
            raise CutoverError(db.unfinished_activation_reason(destination))
        rollback(destination, pointer)
    processes = fleet.process_table()
    plan = build_plan(
        destination, extra_sources, fleet_config, processes=processes,
    )
    plan["mode"] = "cutover"
    _validate_ready(plan)
    _assert_absent_sources_imported(destination, extra_sources)
    if destination.exists() and destination.is_file():
        for row in _destination_queue_evidence(destination, processes):
            if row["worker_live"] or row["dashboard_live"]:
                raise WorkersLive(
                    "Refusing cutover while a destination worker or dashboard is live: "
                    + "; ".join(row["worker_evidence"] + row["dashboard_evidence"])
                )
    complete = _complete_sources(destination, plan["sources"])
    pending = [source for source in plan["sources"] if source["identity"] not in complete]
    retained = [
        source for source in plan["sources"]
        if source["identity"] in complete and _legacy_file_present(source)
    ]
    if not pending:
        created, removed = _retire_imported_sources(destination, plan["sources"])
        plan["mode"] = "cutover"
        plan["activated"] = bool(destination.is_file() or not plan["sources"])
        plan["idempotent"] = True
        plan["created_identity_files"] = created
        plan["removed_legacy_files"] = removed
        plan["summary"] = plan["summary"] + "\nAlready imported. No tasks were copied again."
        return plan
    for source in retained:
        _assert_recorded_snapshot(source)

    attempt_id = str(uuid.uuid4())
    attempt_dir = destination.parent / "migration-attempts" / attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=True)
    before_bytes = {
        source["reading"]: _source_bytes(Path(source["reading"]))
        for source in pending
    }
    for source in pending:
        backup = attempt_dir / "sources" / f"{source['identity']}.db"
        _sqlite_backup(Path(source["reading"]), backup)
        if not _integrity_ok(backup):
            raise CutoverError(f"Backup integrity check failed for {source['path']}")
        if _source_bytes(Path(source["reading"])) != before_bytes[source["reading"]]:
            raise CutoverError(f"Source changed while it was backed up: {source['path']}")
        source["backup_path"] = str(backup)

    staging = attempt_dir / "staging.db"
    if destination.is_file():
        _sqlite_backup(destination, staging)
    staging_conn = sqlite3.connect(staging)
    staging_conn.row_factory = sqlite3.Row
    try:
        staging_conn.execute("PRAGMA foreign_keys=ON")
        if not destination.is_file():
            staging_conn.executescript(db.SCHEMA_SQL)
        else:
            _ensure_migration_tables(staging_conn)
        imported: list[tuple[dict, dict, dict]] = []
        artifact_roots: dict[str, Path] = {}
        for source in sorted(pending, key=lambda item: item["path"]):
            rows = _load_source_rows(Path(source["reading"]))
            _check_source_shape(rows, source)
            queue_root = attempt_dir / "staged-queues" / source["queue_id"]
            result = _import_source(
                staging_conn, source, rows, attempt_id, Path(source["backup_path"]), queue_root,
            )
            artifact_roots[source["identity"]] = queue_root
            imported.append((source, rows, result))
        staging_conn.commit()
        _verify_staging(staging_conn, imported, artifact_roots)
        for backup in attempt_dir.glob("sources/*.db"):
            if not _integrity_ok(backup):
                raise CutoverError(f"Backup integrity check failed for {backup}")
    except Exception:
        staging_conn.rollback()
        raise
    finally:
        staging_conn.close()

    if fail_after == "before-activation":
        raise CutoverError("Injected failure before activation")

    destination_before = None
    if destination.is_file():
        destination_before = attempt_dir / "destination-before.db"
        _sqlite_backup(destination, destination_before)
        if not _integrity_ok(destination_before):
            raise CutoverError("Could not back up the current destination")
    created_dirs = [
        str(destination.parent / db.QUEUES_DIR_NAME / source["queue_id"])
        for source in pending
    ]
    manifest = {
        "attempt_id": attempt_id,
        "destination": str(destination),
        "destination_existed": destination.is_file(),
        "destination_before": str(destination_before) if destination_before else None,
        "activated": False,
        "rolled_back": False,
        "manifest_path": str(destination.parent / "migration-rollback.json"),
        "created_queue_dirs": created_dirs,
        "sources": [
            _manifest_source(source) for source in [*pending, *retained]
        ],
        "created_identity_files": [],
        "removed_legacy_files": [],
        "rollback_steps": _rollback_steps(destination),
    }
    pointer = Path(manifest["manifest_path"])
    _write_json(pointer, manifest)
    _write_json(attempt_dir / "rollback.json", manifest)
    try:
        _fsync(staging)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, destination)
        _remove_sidecars(destination)
        if fail_after == "after-replace":
            raise ActivationCrash("Simulated crash after the staged database was installed")
        for source in pending:
            staged_root = attempt_dir / "staged-queues" / source["queue_id"]
            live_root = destination.parent / db.QUEUES_DIR_NAME / source["queue_id"]
            if live_root.exists():
                raise CutoverError(f"Queue runtime already exists: {live_root}")
            if staged_root.exists():
                live_root.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged_root, live_root)
        for source in pending:
            manifest["created_identity_files"].extend(_ensure_identity(source))
            _write_json(pointer, manifest)
        for source in retained:
            _assert_recorded_snapshot(source)
        for source in pending:
            if fail_after == "after-legacy-unlink":
                live = Path(source["path"])
                if live.is_file() and not live.is_symlink():
                    live.unlink()
                # A crash after the main file is unlinked can leave its WAL
                # behind. Rollback must restore the consistent backup and
                # drop that sidecar instead of replaying it.
                sidecar = Path(str(live) + "-wal")
                if not sidecar.exists():
                    sidecar.write_bytes(b"stale")
                raise ActivationCrash(
                    "Simulated crash after a legacy database was removed and before it was journaled"
                )
            _remove_untracked_legacy(source, manifest, pointer)
        for source in retained:
            _assert_recorded_snapshot(source)
            manifest["created_identity_files"].extend(_ensure_identity(source))
            _write_json(pointer, manifest)
            _remove_untracked_legacy(source, manifest, pointer)
    except Exception as exc:
        _apply_rollback(manifest)
        raise CutoverError(f"Activation failed and was rolled back: {exc}") from exc

    activated = sqlite3.connect(destination)
    try:
        activated.execute(
            "UPDATE migration_sources SET status = 'activated' WHERE attempt_id = ?",
            (attempt_id,),
        )
        activated.execute(
            """INSERT INTO migration_attempts (id, destination, status, manifest_path, finished_at)
               VALUES (?, ?, 'activated', ?, CURRENT_TIMESTAMP)""",
            (attempt_id, str(destination), str(pointer)),
        )
        activated.commit()
    finally:
        activated.close()
    manifest["activated"] = True
    _write_json(pointer, manifest)
    for source in pending:
        if _source_bytes(Path(source["reading"])) not in (b"", before_bytes[source["reading"]]):
            raise CutoverError(f"Source bytes changed during activation: {source['path']}")
    plan["mode"] = "cutover"
    plan["activated"] = True
    plan["idempotent"] = False
    plan["attempt_id"] = attempt_id
    plan["manifest"] = str(pointer)
    plan["rollback_steps"] = manifest["rollback_steps"]
    plan["imported"] = [
        {
            "path": source["path"],
            "identity": source["identity"],
            "queue_id": source["queue_id"],
            "id_map": source["planned_id_map"],
            "task_count": source["counts"]["tasks"],
        }
        for source in pending
    ]
    plan["summary"] = plan["summary"] + f"\nActivated {destination}. Rollback: ko-migrate rollback --destination {destination}"
    return plan


def _destination_queue_evidence(destination: Path, processes: list[tuple[int, str]]) -> list[dict]:
    if db._peek_database_kind(destination) != "current":
        return []
    conn = _open_ro(destination)
    try:
        if "queues" not in _tables(conn):
            return []
        rows = conn.execute(
            "SELECT id, path FROM queues WHERE state = 'enabled'"
        ).fetchall()
    finally:
        conn.close()
    evidence = []
    for row in rows:
        worktree = Path(row["path"]) if row["path"] else None
        evidence.append(
            _central_runtime_live(destination.parent, row["id"], worktree, processes)
        )
    return evidence


def _print_plan(plan: dict) -> None:
    json.dump(plan, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ko-migrate",
        description="Inventory or cut over legacy Kanban databases into the central store.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("preview", "Read-only inventory and planned id map"),
        ("cutover", "Back up, stage, verify, and activate one central database"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--destination", type=Path, default=None)
        command.add_argument("--fleet-config", type=Path, default=None)
        command.add_argument("--source", action="append", default=[], type=Path)
    rollback_command = sub.add_parser("rollback", help="Restore the pre-activation database")
    rollback_command.add_argument("--destination", type=Path, required=True)
    rollback_command.add_argument("--manifest", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        if args.command == "preview":
            destination = (args.destination or db.default_db_path()).expanduser().resolve()
            plan = build_plan(destination, args.source, args.fleet_config)
            _print_plan(plan)
            return 0
        if args.command == "cutover":
            plan = cutover(args.destination, args.source, args.fleet_config)
            _print_plan(plan)
            return 0
        result = rollback(args.destination, args.manifest)
        _print_plan(result)
        return 0
    except WorkersLive as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 3
    except (CutoverError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
