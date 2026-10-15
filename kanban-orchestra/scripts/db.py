"""
Database layer for Kanban Orchestra.

Handles SQLite schema creation, connection management (WAL mode),
and all queries used by task.py and orchestrator.py.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import config

SCHEMA_VERSION = 21
LOCK_FILE_NAME = "kanban-orchestra.lock"
LEGACY_DB_FILENAME = "kanban-orchestra.db"
CENTRAL_DB_FILENAME = "orchestra.db"
REPOSITORY_ID_FILENAME = "orchestra-repository-id"
QUEUE_ID_FILENAME = "orchestra-queue-id"
QUEUES_DIR_NAME = "queues"
COMMIT_TASK_KINDS = {"commit", "task"}
BLOCK_REASON_REVIEW_CAP = "review_cap"
BLOCK_REASON_REVIEWER_UNAVAILABLE = "reviewer_unavailable"
DEFAULT_MAX_REVIEW_ROUNDS = config.MAX_REVIEW_ROUNDS
RUN_LOG_RETENTION_DAYS = 7
COMPLETED_TASK_STATUS = "done"
TASK_ARTIFACT_DIR_PREFIX = "task-"
_QUEUE_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

SCHEMA_SQL = f"""\
CREATE TABLE IF NOT EXISTS repositories (
    id          TEXT PRIMARY KEY,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS queues (
    id              TEXT PRIMARY KEY,
    repository_id   TEXT NOT NULL REFERENCES repositories(id),
    path            TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'enabled'
                    CHECK(state IN ('enabled', 'retired')),
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    retired_at      DATETIME
);

CREATE UNIQUE INDEX IF NOT EXISTS queues_enabled_path
    ON queues(path) WHERE state = 'enabled' AND path != '';

CREATE TABLE IF NOT EXISTS tasks (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_id           TEXT NOT NULL REFERENCES repositories(id),
    queue_id                TEXT NOT NULL REFERENCES queues(id),
    title                   TEXT NOT NULL,
    description             TEXT,
    status                  TEXT NOT NULL DEFAULT 'none'
        CHECK(status IN ('none', 'ready', 'running', 'done', 'blocked', 'pending_subtasks')),
    next_step               TEXT NOT NULL DEFAULT 'commit-make'
        CHECK(next_step IN ('commit-make', 'commit-review',
                            'commit-make-supertask', 'commit-review-supertask',
                            'commit-plan', 'commit-plan-review',
                            'pull-request-make', 'pull-request-review',
                            'other-make', 'other-review',
                            'none')),
    branch                  TEXT,
    commit_hash             TEXT,
    stash_ref               TEXT,
    coder_agent             TEXT,
    reviewer_agent          TEXT,
    review_round            INTEGER DEFAULT 0,
    max_review_rounds       INTEGER NOT NULL DEFAULT {DEFAULT_MAX_REVIEW_ROUNDS},
    last_review_decision    TEXT DEFAULT 'none'
        CHECK(last_review_decision IN ('none', 'approve', 'reject')),
    created_at              DATETIME DEFAULT CURRENT_TIMESTAMP,
    ready_at                DATETIME DEFAULT NULL,
    last_ready_at           DATETIME DEFAULT NULL,
    first_started_at        DATETIME DEFAULT NULL,
    done_at                 DATETIME DEFAULT NULL,
    updated_at              DATETIME DEFAULT CURRENT_TIMESTAMP,
    kind                    TEXT NOT NULL DEFAULT 'commit'
        CHECK(kind IN ('commit', 'task', 'supertask', 'pull_request', 'other')),
    parent_task_id          INTEGER REFERENCES tasks(id),
    sequence_index          INTEGER,
    commit_plan             TEXT,
    follow_up_task_id       INTEGER REFERENCES tasks(id),
    allow_when_blocked      INTEGER NOT NULL DEFAULT 0,
    block_reason            TEXT,
    resume_next_step        TEXT
);

CREATE INDEX IF NOT EXISTS tasks_queue_status ON tasks(queue_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS tasks_one_running_per_queue
    ON tasks(queue_id) WHERE status = 'running';

CREATE TABLE IF NOT EXISTS task_agent_snapshots (
    task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    snapshot TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_worker_config (
    queue_id TEXT PRIMARY KEY REFERENCES queues(id),
    fingerprint TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_skips (
    task_id  INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step     TEXT    NOT NULL
                     CHECK(step IN ('commit-plan','commit-plan-review',
                                    'commit-review','commit-review-supertask',
                                    'pull-request-review','other-review')),
    PRIMARY KEY (task_id, step)
);

CREATE TABLE IF NOT EXISTS run_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER REFERENCES tasks(id),
    queue_id    TEXT NOT NULL REFERENCES queues(id),
    verb        TEXT,
    author      TEXT,
    message     TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS comments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id        INTEGER REFERENCES tasks(id),
    review_round   INTEGER,
    verb           TEXT,
    author         TEXT,
    message        TEXT,
    kind           TEXT DEFAULT 'comment'
        CHECK(kind IN ('comment', 'approval', 'rejection', 'commit-message', 'validation',
                       'plan-approval', 'plan-rejection', 'done-without-commit',
                       'deferred-build-changed')),
    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS orchestrator_runtime (
    queue_id             TEXT PRIMARY KEY REFERENCES queues(id),
    status               TEXT NOT NULL
        CHECK(status IN ('idle', 'waiting-dirty', 'running', 'starting', 'stopping', 'stopped', 'hard-break', 'error')),
    pid                  INTEGER,
    started_at           DATETIME,
    last_heartbeat_at    DATETIME,
    current_task_id      INTEGER REFERENCES tasks(id),
    current_step         TEXT
        CHECK(current_step IN (
            'commit-make', 'commit-review',
            'commit-make-supertask', 'commit-review-supertask',
            'commit-plan', 'commit-plan-review',
            'pull-request-make', 'pull-request-review',
            'other-make', 'other-review',
            'none'
        )),
    current_branch       TEXT,
    review_round         INTEGER,
    active_agents        INTEGER NOT NULL DEFAULT 0,
    status_message       TEXT,
    updated_at           DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TRIGGER IF NOT EXISTS tasks_queue_repository_insert
BEFORE INSERT ON tasks
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM queues
    WHERE id = NEW.queue_id AND repository_id = NEW.repository_id
)
BEGIN
    SELECT RAISE(ABORT, 'task queue does not belong to its repository');
END;

CREATE TRIGGER IF NOT EXISTS tasks_parent_same_queue_insert
BEFORE INSERT ON tasks
FOR EACH ROW
WHEN NEW.parent_task_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM tasks parent
    WHERE parent.id = NEW.parent_task_id
      AND parent.repository_id = NEW.repository_id
      AND parent.queue_id = NEW.queue_id
)
BEGIN
    SELECT RAISE(ABORT, 'parent task must belong to the same repository and queue');
END;

CREATE TRIGGER IF NOT EXISTS tasks_repository_immutable
BEFORE UPDATE OF repository_id ON tasks
FOR EACH ROW
WHEN NEW.repository_id != OLD.repository_id
BEGIN
    SELECT RAISE(ABORT, 'task repository cannot change');
END;

CREATE TRIGGER IF NOT EXISTS tasks_queue_same_repository_update
BEFORE UPDATE OF queue_id ON tasks
FOR EACH ROW
WHEN NEW.queue_id != OLD.queue_id AND NOT EXISTS (
    SELECT 1 FROM queues
    WHERE id = NEW.queue_id AND repository_id = NEW.repository_id AND state = 'enabled'
)
BEGIN
    SELECT RAISE(ABORT, 'task queue does not belong to its repository');
END;

CREATE TRIGGER IF NOT EXISTS tasks_parent_same_queue_update
BEFORE UPDATE OF parent_task_id ON tasks
FOR EACH ROW
WHEN NEW.parent_task_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM tasks parent
    WHERE parent.id = NEW.parent_task_id
      AND parent.repository_id = NEW.repository_id
      AND parent.queue_id = NEW.queue_id
)
BEGIN
    SELECT RAISE(ABORT, 'parent task must belong to the same repository and queue');
END;

CREATE TRIGGER IF NOT EXISTS tasks_follow_up_same_queue_update
BEFORE UPDATE OF follow_up_task_id ON tasks
FOR EACH ROW
WHEN NEW.follow_up_task_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM tasks follow_up
    WHERE follow_up.id = NEW.follow_up_task_id
      AND follow_up.repository_id = NEW.repository_id
      AND follow_up.queue_id = NEW.queue_id
)
BEGIN
    SELECT RAISE(ABORT, 'follow-up task must belong to the same repository and queue');
END;

CREATE TRIGGER IF NOT EXISTS runtime_task_same_queue_insert
BEFORE INSERT ON orchestrator_runtime
FOR EACH ROW
WHEN NEW.current_task_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM tasks
    WHERE id = NEW.current_task_id AND queue_id = NEW.queue_id
)
BEGIN
    SELECT RAISE(ABORT, 'runtime task belongs to a different queue');
END;

CREATE TRIGGER IF NOT EXISTS runtime_task_same_queue_update
BEFORE UPDATE OF current_task_id ON orchestrator_runtime
FOR EACH ROW
WHEN NEW.current_task_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM tasks
    WHERE id = NEW.current_task_id AND queue_id = NEW.queue_id
)
BEGIN
    SELECT RAISE(ABORT, 'runtime task belongs to a different queue');
END;

CREATE TABLE IF NOT EXISTS migration_sources (
    id              TEXT PRIMARY KEY,
    source_path     TEXT NOT NULL UNIQUE,
    queue_id        TEXT NOT NULL REFERENCES queues(id),
    repository_id   TEXT NOT NULL REFERENCES repositories(id),
    status          TEXT NOT NULL
                    CHECK(status IN ('imported', 'activated')),
    task_count      INTEGER NOT NULL,
    attempt_id      TEXT NOT NULL,
    backup_path     TEXT,
    imported_at     DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS migration_task_map (
    source_id       TEXT NOT NULL REFERENCES migration_sources(id),
    old_task_id     INTEGER NOT NULL,
    new_task_id     INTEGER NOT NULL REFERENCES tasks(id),
    PRIMARY KEY (source_id, old_task_id),
    UNIQUE (new_task_id)
);

CREATE TABLE IF NOT EXISTS migration_comment_map (
    source_id       TEXT NOT NULL REFERENCES migration_sources(id),
    old_comment_id  INTEGER NOT NULL,
    new_comment_id  INTEGER NOT NULL REFERENCES comments(id),
    queue_id        TEXT NOT NULL REFERENCES queues(id),
    PRIMARY KEY (source_id, old_comment_id),
    UNIQUE (new_comment_id)
);

CREATE TABLE IF NOT EXISTS migration_attempts (
    id              TEXT PRIMARY KEY,
    destination     TEXT NOT NULL,
    status          TEXT NOT NULL,
    manifest_path   TEXT NOT NULL,
    started_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    finished_at     DATETIME
);
"""


class MigrationRequired(RuntimeError):
    """Raised when a legacy per-worktree database must be migrated by hand.

    The legacy file is left untouched. This class carries no reader.
    """


class OrchestraConnection(sqlite3.Connection):
    """SQLite connection tagged with the queue it is operating on."""

    orchestra_queue_id: str | None = None
    orchestra_repository_id: str | None = None
    orchestra_repo_root: str | None = None
    orchestra_queue_created: bool = False


def migration_required_message(path: str | Path) -> str:
    """Return the operator diagnostic for an unmigrated legacy database."""
    return (
        f"Migration required: {path} is a legacy per-worktree Kanban Orchestra database. "
        "The central store will not read, modify, or delete it. "
        "Inventory it with ko-migrate preview and import it only with an approved "
        "ko-migrate cutover after its worker and dashboard are stopped. "
        "An empty queue was not created."
    )


def _is_id(value: str | None) -> bool:
    return bool(value) and _QUEUE_ID_RE.fullmatch(value) is not None


def get_state_root() -> Path:
    """Return the durable Orchestra state directory for this user and machine.

    ``ORCHESTRA_STATE_DIR`` isolates tests and Linux CI. macOS otherwise uses
    Application Support. Other platforms use ``$XDG_DATA_HOME/orchestra`` or
    ``~/.local/share/orchestra``.
    """
    override = os.environ.get("ORCHESTRA_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "Orchestra").resolve()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return (base / "orchestra").resolve()


def default_db_path() -> Path:
    """Return the central database path for this user and machine."""
    return get_state_root() / CENTRAL_DB_FILENAME


def get_db_path(db_path: str | None = None) -> str:
    """Return the database path. Explicit arg, then KANBAN_DB, then the central default."""
    if db_path:
        return str(Path(db_path).expanduser().resolve())
    env = os.environ.get("KANBAN_DB")
    if env:
        return str(Path(env).expanduser().resolve())
    return str(default_db_path())


def get_db_path_without_git(
    db_path: str | None = None,
    *,
    cwd: str | None = None,
) -> str:
    """Resolve the central database path without invoking Git.

    Order: explicit arg, ``KANBAN_DB``, then the platform central default.
    ``cwd`` is accepted for callers and is not used to walk a checkout for a
    legacy database file.
    """
    del cwd
    return get_db_path(db_path)


def get_repo_root(cwd: str | None = None) -> Path:
    """Resolve the current git repo root."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            cwd=cwd,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError("Not inside a git repository and KANBAN_DB is not set.") from exc
    return Path(result.stdout.strip()).resolve()


def get_orchestra_dir() -> Path:
    """Resolve the Orchestra checkout from ORCHESTRA_DIR."""
    raw = os.environ.get("ORCHESTRA_DIR")
    if not raw:
        raise RuntimeError(
            "ORCHESTRA_DIR is not set. Export ORCHESTRA_DIR to the Orchestra checkout root "
            "before using Kanban Orchestra."
        )

    orchestra_dir = Path(raw).expanduser().resolve()
    if not orchestra_dir.exists():
        raise RuntimeError(f"ORCHESTRA_DIR points to a missing path: {orchestra_dir}")

    expected = orchestra_dir / "kanban-orchestra"
    if not expected.is_dir():
        raise RuntimeError(
            "ORCHESTRA_DIR does not look like an Orchestra checkout: "
            f"missing {expected}"
        )

    return orchestra_dir


def get_workspace(cwd: str | None = None) -> dict[str, Path]:
    """Resolve the worktree and the central database paths."""
    repo_root = get_repo_root(cwd)
    db_path = Path(get_db_path())
    return {
        "repo_root": repo_root,
        "db_path": db_path,
        "sql_path": db_path.with_name("orchestra.sql"),
    }


def _readonly_uri(path: Path) -> str:
    return "file:" + quote(path.resolve().as_posix()) + "?mode=ro"


def _peek_database_kind(path: str | Path) -> str:
    """Classify a database file without writing it.

    Returns ``absent``, ``empty``, ``current``, ``legacy``, or ``incompatible``.
    """
    db_file = Path(path)
    if not db_file.exists() or db_file.stat().st_size == 0:
        return "absent" if not db_file.exists() else "empty"
    conn = sqlite3.connect(_readonly_uri(db_file), uri=True, timeout=30.0)
    try:
        conn.row_factory = sqlite3.Row
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        if not tables:
            return "empty"
        if _schema_is_current(conn, tables):
            return "current"
        if _schema_is_legacy(conn, tables):
            return "legacy"
        return "incompatible"
    except sqlite3.Error:
        return "incompatible"
    finally:
        conn.close()


def _schema_is_current(conn: sqlite3.Connection, tables: set[str]) -> bool:
    if not {"repositories", "queues", "tasks", "orchestrator_runtime"}.issubset(tables):
        return False
    task_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    runtime_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(orchestrator_runtime)").fetchall()
    }
    return "queue_id" in task_cols and "repository_id" in task_cols and "queue_id" in runtime_cols


def _schema_is_legacy(conn: sqlite3.Connection, tables: set[str]) -> bool:
    if "queues" in tables and "repositories" in tables:
        return False
    if "tasks" in tables:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "queue_id" not in cols:
            return True
    if "orchestrator_runtime" in tables and "queues" not in tables:
        return True
    return False


def legacy_database_file(repo_root: str | Path) -> Path | None:
    """Return a legacy worktree database path when one is present and unmigrated."""
    path = Path(repo_root) / LEGACY_DB_FILENAME
    if not path.is_file():
        return None
    if _peek_database_kind(path) == "legacy":
        return path
    return None


def assert_worktree_not_legacy(repo_root: str | Path) -> None:
    """Refuse to register a queue while a legacy database still sits in the worktree."""
    legacy = legacy_database_file(repo_root)
    if legacy is not None:
        raise MigrationRequired(migration_required_message(legacy))


def _git_dir(repo_root: Path, flag: str) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", flag],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


def _identity_dirs(repo_root: Path) -> tuple[Path, Path]:
    """Return directories that anchor repository identity and queue identity.

    Git checkouts keep the repository id in the common git dir and the queue
    id in the worktree git dir, so linked worktrees share a repository and
    keep distinct queues. Non-git directories keep both ids under
    ``.kanban-orchestra`` beside agent preference files.
    """
    common = _git_dir(repo_root, "--git-common-dir")
    git_dir = _git_dir(repo_root, "--git-dir")
    if common is not None and git_dir is not None:
        return common, git_dir
    local = repo_root / ".kanban-orchestra"
    return local, local


def _read_identity(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not _is_id(text):
        raise RuntimeError(f"Orchestra identity file is invalid: {path}")
    return text


def _write_identity(path: Path, value: str) -> None:
    if not _is_id(value):
        raise RuntimeError(f"Refusing to store an unsafe Orchestra id: {value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(value + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_or_create_identity(path: Path) -> str:
    existing = _read_identity(path)
    if existing:
        return existing
    value = str(uuid.uuid4())
    _write_identity(path, value)
    return value


def connection_queue_id(conn: sqlite3.Connection) -> str:
    queue_id = getattr(conn, "orchestra_queue_id", None)
    if not _is_id(queue_id):
        raise RuntimeError("Orchestra connection is missing queue context")
    return queue_id


def connection_repository_id(conn: sqlite3.Connection) -> str:
    repository_id = getattr(conn, "orchestra_repository_id", None)
    if not _is_id(repository_id):
        raise RuntimeError("Orchestra connection is missing repository context")
    return repository_id


def connection_worktree(conn: sqlite3.Connection) -> Path | None:
    """Return the bound worktree path, when this queue has one."""
    raw = getattr(conn, "orchestra_repo_root", None)
    if not isinstance(raw, (str, Path)) or not str(raw):
        return None
    return Path(raw)


def execution_root(conn: sqlite3.Connection, *, repo_root: str | Path | None = None) -> Path:
    """Return the worktree used for Git, agents, and stop files.

    A bound queue path wins. An isolated database uses its parent directory
    when that parent is not the central state directory. The central state
    directory is never treated as a checkout.
    """
    if repo_root:
        return Path(repo_root).expanduser().resolve()
    bound = connection_worktree(conn)
    if bound is not None and bound.is_dir():
        return bound
    db_file = get_connection_db_path(conn)
    if not db_file:
        raise RuntimeError("connection has no database path")
    parent = Path(db_file).resolve().parent
    if parent == get_state_root():
        raise RuntimeError(
            "This queue has no worktree. Refusing to use the central state directory as a checkout."
        )
    return parent


def get_runtime_root(db_path: str | None = None, *, queue_id: str | None = None) -> Path:
    """Return the durable runtime directory for one queue."""
    path = get_db_path(db_path)
    qid = queue_id or _sole_queue_id(path)
    if not _is_id(qid):
        raise RuntimeError("queue id is required for runtime paths")
    return Path(path).resolve().parent / QUEUES_DIR_NAME / qid


def get_lock_path(db_path: str | None = None, *, queue_id: str | None = None) -> Path:
    """Return the queue-scoped orchestrator lock path."""
    return get_runtime_root(db_path, queue_id=queue_id) / LOCK_FILE_NAME


def get_artifacts_root(db_path: str | None = None, *, queue_id: str | None = None) -> Path:
    """Return the directory used for one queue's run artifacts."""
    return get_runtime_root(db_path, queue_id=queue_id) / "artifacts"


def get_orchestrator_log_path(db_path: str | None = None, *, queue_id: str | None = None) -> Path:
    """Return the persisted orchestrator stdout log for one queue."""
    return get_runtime_root(db_path, queue_id=queue_id) / "orchestrator.log"


def _queue_count(db_path: str) -> int:
    """Return how many queues a current database has, or zero when it has none."""
    path = Path(db_path)
    if not path.is_file() or _peek_database_kind(path) != "current":
        return 0
    conn = sqlite3.connect(_readonly_uri(path), uri=True, timeout=30.0)
    try:
        row = conn.execute("SELECT COUNT(*) FROM queues").fetchone()
    except sqlite3.Error:
        return 0
    finally:
        conn.close()
    return int(row[0]) if row and row[0] is not None else 0


def _sole_queue_id(db_path: str) -> str:
    kind = _peek_database_kind(db_path)
    if kind != "current":
        raise RuntimeError(f"Orchestra database has no queue yet: {db_path}")
    conn = sqlite3.connect(_readonly_uri(Path(db_path)), uri=True, timeout=30.0)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id FROM queues").fetchall()
    finally:
        conn.close()
    if len(rows) != 1:
        raise RuntimeError(
            "This Orchestra database has more than one queue. "
            "Pass queue_id or open it from a worktree."
        )
    return rows[0]["id"]


def get_instance_identity(
    db_path: str | None = None,
    *,
    cwd: str | None = None,
    lock_path: str | Path | None = None,
    queue_id: str | None = None,
    repo_root: str | Path | None = None,
) -> dict[str, str]:
    """Return identifiers for one queue without treating the database directory as a checkout."""
    resolved_db = get_db_path(db_path)
    qid = queue_id
    if qid is None:
        try:
            qid = _sole_queue_id(resolved_db)
        except RuntimeError:
            qid = None
    runtime_root = get_runtime_root(resolved_db, queue_id=qid) if qid else None
    resolved_lock = (
        Path(lock_path).resolve()
        if lock_path is not None
        else (runtime_root / LOCK_FILE_NAME if runtime_root is not None else None)
    )
    resolved_repo = Path(repo_root).resolve() if repo_root else None
    if resolved_repo is None and qid:
        row_path = _queue_path(resolved_db, qid)
        if row_path:
            resolved_repo = Path(row_path)
    if resolved_repo is None and (cwd is not None or not os.environ.get("KANBAN_DB")):
        try:
            resolved_repo = get_repo_root(cwd)
        except RuntimeError:
            resolved_repo = None
    if resolved_repo is None:
        parent = Path(resolved_db).resolve().parent
        if parent != get_state_root().resolve():
            resolved_repo = parent
    identity = {
        "db_path": resolved_db,
    }
    if qid:
        identity["queue_id"] = qid
    if resolved_repo is not None:
        identity["repo_root"] = str(resolved_repo)
        identity["repo_label"] = resolved_repo.name or str(resolved_repo)
    if runtime_root is not None:
        identity["runtime_root"] = str(runtime_root)
    if resolved_lock is not None:
        identity["lock_path"] = str(resolved_lock)
    return identity


def _queue_path(db_path: str, queue_id: str) -> str:
    conn = sqlite3.connect(_readonly_uri(Path(db_path)), uri=True, timeout=30.0)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT path FROM queues WHERE id = ?", (queue_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return ""
    return row["path"] or ""


def new_agent_transcript_path(
    task_id: int,
    verb: str | None,
    agent_name: str | None,
    db_path: str | None = None,
    *,
    queue_id: str | None = None,
) -> Path:
    """Create and return a unique path for one agent transcript."""
    artifacts_root = get_artifacts_root(db_path, queue_id=queue_id)
    task_dir = artifacts_root / f"task-{task_id}"
    task_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    safe_verb = (verb or "run").replace("/", "-")
    safe_agent = (agent_name or "agent").replace("/", "-")
    return task_dir / f"{stamp}-{safe_verb}-{safe_agent}.log"


def _attach_queue(conn: OrchestraConnection, row: sqlite3.Row, *, created: bool = False) -> None:
    conn.orchestra_queue_id = row["id"]
    conn.orchestra_repository_id = row["repository_id"]
    conn.orchestra_repo_root = row["path"] or None
    conn.orchestra_queue_created = created
    runtime = get_runtime_root(get_connection_db_path(conn), queue_id=row["id"])
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "artifacts").mkdir(exist_ok=True)


def _insert_repository(conn: sqlite3.Connection, repository_id: str | None = None) -> str:
    repo_id = repository_id or str(uuid.uuid4())
    if not _is_id(repo_id):
        raise RuntimeError(f"Refusing unsafe repository id: {repo_id}")
    conn.execute("INSERT OR IGNORE INTO repositories (id) VALUES (?)", (repo_id,))
    return repo_id


def _insert_queue(
    conn: sqlite3.Connection,
    repository_id: str,
    path: str,
    *,
    queue_id: str | None = None,
    state: str = "enabled",
) -> str:
    qid = queue_id or str(uuid.uuid4())
    if not _is_id(qid):
        raise RuntimeError(f"Refusing unsafe queue id: {qid}")
    conn.execute(
        """INSERT INTO queues (id, repository_id, path, state)
           VALUES (?, ?, ?, ?)""",
        (qid, repository_id, path, state),
    )
    return qid


def _require_queue(conn: sqlite3.Connection, queue_id: str) -> sqlite3.Row:
    if not _is_id(queue_id):
        raise RuntimeError(f"Refusing unsafe queue id: {queue_id}")
    row = conn.execute("SELECT * FROM queues WHERE id = ?", (queue_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"Unknown queue: {queue_id}")
    return row


def _sole_or_default_queue(conn: sqlite3.Connection) -> tuple[sqlite3.Row, bool]:
    rows = conn.execute("SELECT * FROM queues ORDER BY created_at, id").fetchall()
    if len(rows) == 1:
        return rows[0], False
    if len(rows) == 0:
        repo_id = _insert_repository(conn)
        qid = _insert_queue(conn, repo_id, "")
        conn.commit()
        return conn.execute("SELECT * FROM queues WHERE id = ?", (qid,)).fetchone(), True
    raise RuntimeError(
        "This Orchestra database has more than one queue. "
        "Open it from a worktree or pass queue_id."
    )


def ensure_queue(conn: sqlite3.Connection, repo_root: str | Path) -> tuple[sqlite3.Row, bool]:
    """Register or refresh the enabled queue for a worktree.

    A legacy ``kanban-orchestra.db`` in the worktree aborts before any queue
    row is inserted. Retired queues stay retired. A missing directory is not
    given a replacement queue.
    """
    root = Path(repo_root).expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(
            f"Worktree path is missing: {root}. "
            "Unfinished tasks stay on their queue and will not run elsewhere."
        )
    assert_worktree_not_legacy(root)
    repo_dir, queue_dir = _identity_dirs(root)
    repository_id = _read_or_create_identity(repo_dir / REPOSITORY_ID_FILENAME)
    _insert_repository(conn, repository_id)
    path_text = str(root)
    queue_file = queue_dir / QUEUE_ID_FILENAME
    existing_queue_id = _read_identity(queue_file)
    created = False
    if existing_queue_id:
        row = conn.execute("SELECT * FROM queues WHERE id = ?", (existing_queue_id,)).fetchone()
        if row is None:
            _insert_queue(conn, repository_id, path_text, queue_id=existing_queue_id)
            created = True
        elif row["repository_id"] != repository_id:
            raise RuntimeError(
                f"Queue {existing_queue_id} belongs to a different repository than {root}"
            )
        elif row["state"] != "enabled":
            raise RuntimeError(
                f"Queue for {root} is retired. Its tasks stay on that queue "
                "and will not run on another worktree."
            )
        elif row["path"] != path_text:
            clash = conn.execute(
                """SELECT id FROM queues
                   WHERE path = ? AND state = 'enabled' AND id != ?""",
                (path_text, existing_queue_id),
            ).fetchone()
            if clash:
                raise RuntimeError(f"Another enabled queue already uses {root}")
            conn.execute(
                "UPDATE queues SET path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (path_text, existing_queue_id),
            )
        conn.commit()
        return conn.execute("SELECT * FROM queues WHERE id = ?", (existing_queue_id,)).fetchone(), created

    row = conn.execute(
        "SELECT * FROM queues WHERE path = ? AND state = 'enabled'",
        (path_text,),
    ).fetchone()
    if row is not None:
        if row["repository_id"] != repository_id:
            raise RuntimeError(f"Enabled queue at {root} belongs to another repository")
        _write_identity(queue_file, row["id"])
        conn.commit()
        return row, False

    qid = str(uuid.uuid4())
    try:
        _insert_queue(conn, repository_id, path_text, queue_id=qid)
        _write_identity(queue_file, qid)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return conn.execute("SELECT * FROM queues WHERE id = ?", (qid,)).fetchone(), True


def lookup_queue_by_path(repo_root: str | Path, *, db_path: str | None = None) -> dict | None:
    """Return an enabled queue row for a worktree path without creating the database."""
    db_file = Path(get_db_path(db_path))
    if not db_file.is_file() or _peek_database_kind(db_file) != "current":
        return None
    path_text = str(Path(repo_root).expanduser().resolve())
    conn = sqlite3.connect(_readonly_uri(db_file), uri=True, timeout=30.0)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM queues WHERE path = ? AND state = 'enabled'",
            (path_text,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return dict(row) if row else None


def queue_registration_error(repo_root: str | Path) -> str | None:
    """Return why a worktree's on-disk queue identity is unsafe to run.

    A missing identity file is not an error: the central row addressed by
    path is the registration. A present identity file must name that row.
    """
    root = Path(repo_root).expanduser().resolve()
    try:
        row = lookup_queue_by_path(root)
    except (RuntimeError, OSError):
        return None
    if row is None:
        return None
    try:
        _repo_dir, queue_dir = _identity_dirs(root)
        recorded = _read_identity(queue_dir / QUEUE_ID_FILENAME)
    except RuntimeError as exc:
        return str(exc)
    if recorded and recorded != row["id"]:
        return (
            f"Queue identity file at {queue_dir / QUEUE_ID_FILENAME} "
            f"does not match the central queue {row['id']} for {root}."
        )
    return None


def queue_dispatch_block_reason(conn: sqlite3.Connection) -> str | None:
    """Return why this queue must not claim tasks, or None when dispatch is allowed."""
    try:
        row = _require_queue(conn, connection_queue_id(conn))
    except (RuntimeError, sqlite3.Error, TypeError):
        return None
    if row["state"] != "enabled":
        return (
            f"Queue {row['id']} is retired. Its tasks stay on that queue "
            "and will not run on another worktree."
        )
    path = row["path"] or ""
    if not path:
        return None
    if not Path(path).is_dir():
        return (
            f"Worktree {path} is missing. Tasks on queue {row['id']} "
            "will not run on a different queue."
        )
    return None


def retire_queue(conn: sqlite3.Connection, queue_id: str | None = None) -> None:
    """Mark a queue retired and keep its tasks, runtime history, and files."""
    qid = queue_id or connection_queue_id(conn)
    _require_queue(conn, qid)
    conn.execute(
        """UPDATE queues
           SET state = 'retired', retired_at = CURRENT_TIMESTAMP,
               updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (qid,),
    )
    conn.commit()


def unfinished_activation_reason(db_path: str | Path) -> str | None:
    """Return why a central database must not be opened, or None when it is live.

    A cutover marks imported sources activated only after the replacement,
    artifact move, and identity write finish. A crash in that window leaves
    ``imported`` rows. That file is a partial store.
    """
    path = Path(db_path)
    if not path.is_file() or _peek_database_kind(path) != "current":
        return None
    conn = sqlite3.connect(_readonly_uri(path), uri=True, timeout=30.0)
    try:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        if "migration_sources" not in names:
            return None
        pending = conn.execute(
            "SELECT COUNT(*) FROM migration_sources WHERE status != 'activated'"
        ).fetchone()[0]
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if int(pending) == 0:
        return None
    return (
        f"Unfinished migration activation: {path} is not a live store. "
        "Run ko-migrate rollback before opening it or claiming tasks."
    )


def assert_activation_finished(conn: sqlite3.Connection) -> None:
    """Refuse claims on a database whose cutover stopped before activation."""
    try:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
    except sqlite3.Error:
        return
    if "migration_sources" not in names:
        return
    pending = conn.execute(
        "SELECT COUNT(*) FROM migration_sources WHERE status != 'activated'"
    ).fetchone()[0]
    if int(pending):
        raise RuntimeError(
            "Unfinished migration activation: this database is not a live store. "
            "Run ko-migrate rollback before opening it or claiming tasks."
        )


def connect(
    db_path: str | None = None,
    *,
    repo_root: str | Path | None = None,
    queue_id: str | None = None,
    cwd: str | None = None,
) -> OrchestraConnection:
    """Open the central database and bind one queue.

    An explicit ``queue_id`` or ``repo_root`` selects that queue. Otherwise the
    database's only queue is used, or a path-less default queue is created for
    an isolated database. A legacy database file is rejected without writes.
    """
    path = get_db_path(db_path)
    kind = _peek_database_kind(path)
    if kind == "legacy":
        raise MigrationRequired(migration_required_message(path))
    if kind == "incompatible":
        raise MigrationRequired(
            f"Migration required: {path} is not the central Orchestra schema. "
            "It was left unchanged."
        )
    unfinished = unfinished_activation_reason(path)
    if unfinished:
        raise RuntimeError(unfinished)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0, factory=OrchestraConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    if kind in ("absent", "empty"):
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    if queue_id is not None:
        _attach_queue(conn, _require_queue(conn, queue_id))
    elif repo_root is not None:
        row, created = ensure_queue(conn, repo_root)
        _attach_queue(conn, row, created=created)
    elif cwd is not None:
        row, created = ensure_queue(conn, get_repo_root(cwd))
        _attach_queue(conn, row, created=created)
    else:
        row, created = _sole_or_default_queue(conn)
        _attach_queue(conn, row, created=created)
    _recover_missing_start_times(conn)
    return conn


def _recover_missing_start_times(conn: sqlite3.Connection) -> None:
    """Fill this queue's missing start times from retained run history."""
    queue_id = getattr(conn, "orchestra_queue_id", None)
    if not isinstance(queue_id, str):
        return
    task_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM tasks WHERE queue_id = ? AND first_started_at IS NULL",
            (queue_id,),
        ).fetchall()
    ]
    if not task_ids:
        return
    _backfill_first_started_at(conn, task_ids=task_ids)
    conn.commit()


def launch_connection(
    db_path: str | None = None,
    *,
    repo_root: str | Path | None = None,
    queue_id: str | None = None,
    cwd: str | None = None,
) -> OrchestraConnection:
    """Open the database the way a CLI or worker process should.

    An explicit queue, repository, or working directory selects that queue.
    ``ORCHESTRA_QUEUE_ID`` does the same. An explicit database binds the
    invoking worktree when one can be inferred, even if another queue is
    already the only row. A path-less default queue is used only when no
    worktree can be inferred and the database has at most one queue.
    """
    selected_queue = queue_id or os.environ.get("ORCHESTRA_QUEUE_ID")
    if selected_queue or repo_root is not None or cwd is not None:
        return connect(db_path, repo_root=repo_root, queue_id=selected_queue, cwd=cwd)
    if db_path or os.environ.get("KANBAN_DB"):
        path = get_db_path(db_path)
        try:
            root = get_repo_root()
        except RuntimeError:
            root = None
        if root is not None:
            return connect(path, repo_root=root)
        if _queue_count(path) <= 1:
            return connect(path)
        raise RuntimeError(
            "This Orchestra database has more than one queue. "
            "Open it from a worktree or pass queue_id."
        )
    return connect(repo_root=get_repo_root(cwd))


def launch_worktree_connection(db_path: str | None = None) -> OrchestraConnection:
    """Open a status or dashboard connection for the invoking worktree."""
    return launch_connection(db_path)


def configure_for_connection(conn: sqlite3.Connection):
    """Load checkout-local agent preferences for the connection's worktree.

    Isolated databases use the database parent. That parent is a checkout
    when tests point ``KANBAN_DB`` at a file inside a temporary workspace.
    The platform state directory is used only as an empty preference root
    so agent config is not loaded from the central database directory's
    neighbors by accident; a bound worktree always wins.
    """
    bound = connection_worktree(conn)
    if bound is not None and bound.is_dir():
        return config.configure_agents(bound)
    db_file = get_connection_db_path(conn)
    if isinstance(db_file, (str, Path)):
        parent = Path(db_file).resolve().parent
        if parent != get_state_root():
            return config.configure_agents(parent)
    env_db = os.environ.get("KANBAN_DB")
    if env_db:
        return config.configure_agents(Path(env_db).expanduser().resolve().parent)
    return config.configure_agents(get_repo_root())

def get_connection_db_path(conn: sqlite3.Connection) -> str | None:
    """Return the on-disk main database path for an open SQLite connection."""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except (sqlite3.Error, AttributeError, TypeError):
        return None
    if not row:
        return None
    try:
        value = row["file"]
    except (KeyError, TypeError):
        try:
            value = row[2]
        except (IndexError, TypeError, KeyError):
            return None
    if not isinstance(value, str) or not value:
        return None
    return value


def _list_user_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name ASC"
    ).fetchall()
    return [row[0] for row in rows]


def _cleanup_db_artifacts(db_path: str) -> None:
    for suffix in ("", "-shm", "-wal"):
        try:
            Path(f"{db_path}{suffix}").unlink()
        except FileNotFoundError:
            pass


def restore_dump(
    db_path: str | None = None,
    sql_path: str | None = None,
) -> dict[str, str]:
    """Restore a SQL dump into a fresh central database file."""
    target_db_path = get_db_path(db_path)
    target_sql_path = sql_path or str(Path(target_db_path).resolve().with_name("orchestra.sql"))
    db_preexisted = os.path.exists(target_db_path)
    if db_preexisted and _peek_database_kind(target_db_path) in ("legacy", "incompatible"):
        raise MigrationRequired(migration_required_message(target_db_path))

    if not os.path.exists(target_sql_path):
        raise FileNotFoundError(f"SQL dump not found at {target_sql_path}")

    try:
        raw_conn = sqlite3.connect(target_db_path)
        try:
            existing_tables = _list_user_tables(raw_conn)
            if existing_tables:
                raise ValueError(
                    f"restore requires a fresh database; found existing tables in {target_db_path}: "
                    + ", ".join(existing_tables)
                )

            with open(target_sql_path, "r", encoding="utf-8") as f:
                raw_conn.executescript(f.read())
            raw_conn.commit()
        finally:
            raw_conn.close()

        conn = connect(target_db_path)
        conn.close()
    except Exception:
        if not db_preexisted:
            _cleanup_db_artifacts(target_db_path)
        raise

    return {
        "db_path": target_db_path,
        "sql_path": target_sql_path,
    }


def _backfill_first_started_at(conn, *, use_legacy_fallback=False, task_ids=None):
    """Fill missing first_started_at from run_log, optionally with a legacy fallback.

    Prefer the earliest retained task run_log row (first actual pickup). The
    last_ready_at/ready_at fallback is only for the one-time missing-column
    migration and legacy-source import. Current-schema reconnects must not
    treat queue timestamps as a start time, including blocked tasks that have
    not yet transitioned to running.
    """
    task_cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
    }
    if "first_started_at" not in task_cols:
        return
    id_clause = ""
    id_params = []
    if task_ids is not None:
        if not task_ids:
            return
        id_clause = f" AND tasks.id IN ({', '.join('?' for _ in task_ids)})"
        id_params = list(task_ids)
    tables = set(_list_user_tables(conn))
    if "run_log" in tables:
        conn.execute(
            f"""
            UPDATE tasks
            SET first_started_at = (
                SELECT MIN(created_at) FROM run_log WHERE run_log.task_id = tasks.id
            )
            WHERE first_started_at IS NULL
              AND EXISTS (SELECT 1 FROM run_log WHERE run_log.task_id = tasks.id)
              {id_clause}
            """,
            id_params,
        )
    if not use_legacy_fallback:
        return
    fallback_cols = [
        column
        for column in ("last_ready_at", "ready_at")
        if column in task_cols
    ]
    if fallback_cols:
        conn.execute(
            f"""
            UPDATE tasks
            SET first_started_at = COALESCE({", ".join(fallback_cols)})
            WHERE first_started_at IS NULL
              AND status IN ('running', 'blocked', 'done', 'pending_subtasks')
              {id_clause}
            """,
            id_params,
        )


def _row_to_task(row):
    return dict(row)


def task_type(task_or_kind) -> str:
    """Return the public task type name, mapping legacy kind='task' to commit."""
    kind = task_or_kind.get("kind") if isinstance(task_or_kind, dict) else task_or_kind
    return "commit" if kind in COMMIT_TASK_KINDS else kind


# ── Task CRUD ──────────────────────────────────────────────────────────

def _assert_same_queue_reference(conn, other_id, repository_id, queue_id, label):
    row = conn.execute(
        "SELECT repository_id, queue_id FROM tasks WHERE id = ?",
        (other_id,),
    ).fetchone()
    if row is None or row["repository_id"] != repository_id or row["queue_id"] != queue_id:
        raise ValueError(f"{label} task must belong to the same repository and queue")


def _task_family_ids(conn, task_id):
    """Return the task plus parent, children, and follow-up links."""
    seen = set()
    stack = [task_id]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        row = conn.execute(
            "SELECT id, parent_task_id, follow_up_task_id FROM tasks WHERE id = ?",
            (current,),
        ).fetchone()
        if row is None:
            continue
        seen.add(current)
        if row["parent_task_id"] is not None:
            stack.append(row["parent_task_id"])
        if row["follow_up_task_id"] is not None:
            stack.append(row["follow_up_task_id"])
        related = conn.execute(
            """SELECT id FROM tasks
               WHERE parent_task_id = ? OR follow_up_task_id = ?""",
            (current, current),
        ).fetchall()
        stack.extend(item["id"] for item in related)
    return seen


def _relocate_task_artifacts(db_path, task_ids, source_queue_id, dest_queue_id):
    """Move task transcript directories onto the destination queue.

    Returns ``(destination, source)`` pairs so the caller can undo a move
    when the database transaction rolls back.
    """
    source_root = get_artifacts_root(db_path, queue_id=source_queue_id)
    dest_root = get_artifacts_root(db_path, queue_id=dest_queue_id)
    dest_root.mkdir(parents=True, exist_ok=True)
    planned = []
    for task_id in task_ids:
        src = source_root / f"{TASK_ARTIFACT_DIR_PREFIX}{task_id}"
        if not src.exists() and not src.is_symlink():
            continue
        if src.is_symlink() or not src.is_dir():
            raise ValueError(f"refusing to move unsafe artifacts for task {task_id}")
        dst = dest_root / src.name
        if dst.exists() or dst.is_symlink():
            raise ValueError(
                f"destination queue already has artifacts for task {task_id}"
            )
        planned.append((src, dst))
    moved = []
    try:
        for src, dst in planned:
            src.rename(dst)
            moved.append((dst, src))
    except Exception:
        _restore_relocated_artifacts(moved)
        raise
    return moved


def _restore_relocated_artifacts(moved):
    for dst, src in reversed(moved):
        try:
            if dst.exists() and not src.exists():
                dst.rename(src)
        except OSError:
            pass


def reassign_task_queue(conn, task_id, queue_id):
    """Move a task family onto another enabled queue in the same repository.

    Parent, child, and follow-up links stay together. Their run history and
    queue-local transcripts move with them, and the source queue stops
    treating a moved task as its current task.
    """
    task = get_task(conn, task_id)
    if task is None:
        raise ValueError(f"Task {task_id} not found")
    target = _require_queue(conn, queue_id)
    if target["repository_id"] != task["repository_id"]:
        raise ValueError("cannot move a task onto a queue in another repository")
    if target["state"] != "enabled":
        raise ValueError("cannot move a task onto a retired queue")
    if task["queue_id"] == queue_id:
        return task
    family = _task_family_ids(conn, task_id)
    family_ids = tuple(family)
    placeholders = ", ".join("?" for _ in family_ids)
    db_path = get_connection_db_path(conn)
    moved = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        owners = conn.execute(
            f"SELECT id, repository_id, queue_id FROM tasks WHERE id IN ({placeholders})",
            family_ids,
        ).fetchall()
        if any(row["repository_id"] != task["repository_id"] for row in owners):
            raise ValueError("task family crosses repositories")
        source_ids = {row["queue_id"] for row in owners}
        if len(source_ids) != 1:
            raise ValueError("task family is not on one queue")
        source_queue_id = source_ids.pop()
        running_here = conn.execute(
            f"""SELECT 1 FROM tasks
                WHERE status = 'running' AND id IN ({placeholders})""",
            family_ids,
        ).fetchone()
        running_there = conn.execute(
            f"""SELECT 1 FROM tasks
                WHERE queue_id = ? AND status = 'running'
                  AND id NOT IN ({placeholders})""",
            (queue_id, *family_ids),
        ).fetchone()
        if running_here is not None and running_there is not None:
            raise ValueError("destination queue already has a running task")
        conn.execute(
            f"""UPDATE tasks
                SET queue_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id IN ({placeholders})""",
            (queue_id, *family_ids),
        )
        if db_path:
            source_root = str(get_artifacts_root(db_path, queue_id=source_queue_id))
            dest_root = str(get_artifacts_root(db_path, queue_id=queue_id))
            conn.execute(
                f"""UPDATE run_log
                    SET queue_id = ?,
                        message = REPLACE(message, ?, ?)
                    WHERE task_id IN ({placeholders})""",
                (queue_id, source_root, dest_root, *family_ids),
            )
        else:
            conn.execute(
                f"""UPDATE run_log SET queue_id = ?
                    WHERE task_id IN ({placeholders})""",
                (queue_id, *family_ids),
            )
        conn.execute(
            f"""UPDATE orchestrator_runtime
                SET current_task_id = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE queue_id = ? AND current_task_id IN ({placeholders})""",
            (source_queue_id, *family_ids),
        )
        if db_path:
            moved = _relocate_task_artifacts(
                db_path, family_ids, source_queue_id, queue_id
            )
        conn.commit()
    except Exception:
        conn.rollback()
        _restore_relocated_artifacts(moved)
        raise
    return get_task(conn, task_id)


def add_task(
    conn,
    title,
    description=None,
    branch=None,
    coder_agent=None,
    reviewer_agent=None,
    kind="commit",
    parent_task_id=None,
    sequence_index=None,
    status=None,
    skips=None,
    allow_when_blocked=False,
    agent_snapshot=None,
):
    if kind == "task":
        kind = "commit"

    if kind == "supertask":
        next_step = "commit-make-supertask"
    elif kind == "pull_request":
        next_step = "pull-request-make"
    elif kind == "other":
        next_step = "other-make"
    elif skips and "commit-plan" in skips:
        next_step = "commit-make"
    else:
        next_step = "commit-plan"

    if status is None:
        status = "none"
    repository_id = connection_repository_id(conn)
    queue_id = connection_queue_id(conn)
    if parent_task_id is not None:
        _assert_same_queue_reference(conn, parent_task_id, repository_id, queue_id, "parent")
    cur = conn.execute(
        """INSERT INTO tasks (
               repository_id, queue_id, title, description, branch, coder_agent,
               reviewer_agent, kind, parent_task_id, sequence_index, next_step,
               status, allow_when_blocked, max_review_rounds
           )
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            repository_id,
            queue_id,
            title,
            description,
            branch,
            coder_agent,
            reviewer_agent,
            kind,
            parent_task_id,
            sequence_index,
            next_step,
            status,
            int(bool(allow_when_blocked)),
            DEFAULT_MAX_REVIEW_ROUNDS,
        ),
    )
    task_id = cur.lastrowid
    if skips:
        for step in set(skips):
            conn.execute(
                "INSERT INTO task_skips (task_id, step) VALUES (?, ?)",
                (task_id, step),
            )
    if agent_snapshot is not None:
        save_agent_snapshot(conn, task_id, agent_snapshot, commit=False)
    conn.commit()
    return task_id


def require_task_queue(conn, task_id) -> None:
    """Reject a mutation unless the task is on this connection's queue.

    Missing tasks are left for the caller. Global reads stay available through
    ``get_task``. Binding the connection to a queue, including with an explicit
    queue id, is the queue selection.
    """
    row = conn.execute(
        "SELECT queue_id FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return
    if row["queue_id"] != connection_queue_id(conn):
        raise ValueError(
            f"Task {task_id} belongs to another queue. "
            "Inspect it globally, or select that queue explicitly before changing it."
        )


def save_agent_snapshot(conn, task_id, snapshot, *, commit=True):
    require_task_queue(conn, task_id)
    conn.execute(
        "INSERT INTO task_agent_snapshots(task_id, snapshot) VALUES (?, ?) "
        "ON CONFLICT(task_id) DO UPDATE SET snapshot=excluded.snapshot",
        (task_id, json.dumps(snapshot)),
    )
    if commit:
        conn.commit()


def get_agent_snapshot(conn, task_id):
    row = conn.execute(
        "SELECT snapshot FROM task_agent_snapshots WHERE task_id=?", (task_id,)
    ).fetchone()
    return json.loads(row[0]) if row else None


def should_skip_step(conn, task_id, step):
    if conn.execute(
        "SELECT 1 FROM task_skips WHERE task_id = ? AND step = ?",
        (task_id, step)
    ).fetchone() is not None:
        return True

    if step == "commit-plan-review":
        task = conn.execute(
            "SELECT commit_plan FROM tasks WHERE id = ?",
            (task_id,)
        ).fetchone()
        if task is None:
            return False
        return should_skip_step(conn, task_id, "commit-plan") or not task["commit_plan"]

    return False


def add_task_skip(conn, task_id, step):
    require_task_queue(conn, task_id)
    conn.execute(
        "INSERT OR IGNORE INTO task_skips (task_id, step) VALUES (?, ?)",
        (task_id, step)
    )
    conn.commit()


def remove_task_skip(conn, task_id, step):
    require_task_queue(conn, task_id)
    conn.execute(
        "DELETE FROM task_skips WHERE task_id = ? AND step = ?",
        (task_id, step)
    )
    conn.commit()


def get_task_skips(conn, task_id):
    rows = conn.execute(
        "SELECT step FROM task_skips WHERE task_id = ? ORDER BY step ASC",
        (task_id,)
    ).fetchall()
    return [row[0] for row in rows]


def get_task(conn, task_id):
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        return None
    task = _row_to_task(row)
    task["skips"] = get_task_skips(conn, task_id)
    return task


READY_TASK_ORDER_BY = (
    "CASE WHEN sequence_index IS NULL THEN 1 ELSE 0 END ASC, "
    "sequence_index ASC, id ASC"
)


def list_tasks(
    conn,
    status=None,
    next_step=None,
    branch=None,
    page=1,
    page_size=20,
    parent=None,
    *,
    all_queues=False,
    queue_id=None,
):
    query = (
        "SELECT id, title, status, next_step, branch, coder_agent, reviewer_agent, "
        "review_round, created_at, ready_at, last_ready_at, first_started_at, done_at, updated_at, "
        "kind, parent_task_id, sequence_index, allow_when_blocked, repository_id, queue_id "
        "FROM tasks WHERE 1=1"
    )
    params = []
    if not all_queues:
        query += " AND queue_id = ?"
        params.append(queue_id or connection_queue_id(conn))
    if status:
        query += " AND status = ?"
        params.append(status)
    if next_step:
        query += " AND next_step = ?"
        params.append(next_step)
    if branch:
        query += " AND branch = ?"
        params.append(branch)
    if parent is not None:
        query += " AND parent_task_id = ?"
        params.append(parent)
    if status == "ready" or parent is not None:
        query += f" ORDER BY {READY_TASK_ORDER_BY}"
    else:
        query += " ORDER BY id ASC"
    if page_size is not None:
        query += " LIMIT ? OFFSET ?"
        params.extend([page_size, (page - 1) * page_size])
    rows = conn.execute(query, params).fetchall()
    tasks = []
    for r in rows:
        t = _row_to_task(r)
        t["skips"] = get_task_skips(conn, t["id"])
        tasks.append(t)
    return tasks


def update_task(conn, task_id, *, commit=True, **fields):
    """Update task fields and maintain queue/completion timestamps."""
    if not fields:
        return
    require_task_queue(conn, task_id)
    allowed = {
        "title", "description", "status", "next_step", "branch",
        "commit_hash", "stash_ref", "coder_agent", "reviewer_agent",
        "review_round", "max_review_rounds", "last_review_decision", "ready_at",
        "last_ready_at", "first_started_at", "done_at",
        "sequence_index", "commit_plan", "follow_up_task_id",
        "allow_when_blocked", "block_reason", "resume_next_step",
    }
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"Cannot update fields: {bad}")
    if "follow_up_task_id" in fields and fields["follow_up_task_id"] is not None:
        current = conn.execute(
            "SELECT repository_id, queue_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if current is None:
            raise ValueError(f"Task {task_id} not found")
        _assert_same_queue_reference(
            conn,
            fields["follow_up_task_id"],
            current["repository_id"],
            current["queue_id"],
            "follow-up",
        )
    if "status" in fields:
        current = conn.execute(
            "SELECT status, ready_at, done_at, first_started_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if current:
            new_status = fields["status"]
            if new_status == "ready":
                if current["status"] != "ready" or current["ready_at"] is None:
                    fields["ready_at"] = "CURRENT_TIMESTAMP"
                    fields["last_ready_at"] = "CURRENT_TIMESTAMP"
                fields["done_at"] = None
            else:
                fields["ready_at"] = None
                if new_status == "done":
                    if current["status"] != "done" or current["done_at"] is None:
                        fields["done_at"] = "CURRENT_TIMESTAMP"
                else:
                    fields["done_at"] = None
            if (
                new_status == "running"
                and current["first_started_at"] is None
                and "first_started_at" not in fields
            ):
                fields["first_started_at"] = "CURRENT_TIMESTAMP"
    fields["updated_at"] = "CURRENT_TIMESTAMP"
    sets = []
    params = []
    for k, v in fields.items():
        if v == "CURRENT_TIMESTAMP":
            sets.append(f"{k} = CURRENT_TIMESTAMP")
        else:
            sets.append(f"{k} = ?")
            params.append(v)
    params.append(task_id)
    conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params)
    if commit:
        conn.commit()


def delete_task(conn, task_id):
    require_task_queue(conn, task_id)
    task = get_task(conn, task_id)
    if not task:
        raise ValueError(f"Task {task_id} not found")
    if task["status"] != "none":
        raise ValueError(f"Can only delete tasks with status 'none', got '{task['status']}'")
    conn.execute(
        """UPDATE tasks
           SET follow_up_task_id = NULL, updated_at = CURRENT_TIMESTAMP
           WHERE follow_up_task_id = ?""",
        (task_id,),
    )
    conn.execute("DELETE FROM run_log WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM comments WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()


# ── Run Log ────────────────────────────────────────────────────────────

def add_run_log(conn, task_id, message, verb=None, author=None):
    queue_id = connection_queue_id(conn)
    if task_id is not None:
        owner = conn.execute(
            "SELECT queue_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if owner is None:
            raise ValueError(f"Task {task_id} not found")
        require_task_queue(conn, task_id)
        queue_id = owner["queue_id"]
    conn.execute(
        "INSERT INTO run_log (task_id, queue_id, verb, author, message) VALUES (?, ?, ?, ?, ?)",
        (task_id, queue_id, verb, author, message),
    )
    conn.commit()


def get_run_log(conn, task_id):
    rows = conn.execute(
        "SELECT * FROM run_log WHERE task_id = ? ORDER BY id DESC", (task_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_global_run_log(conn, limit=50):
    rows = conn.execute(
        """SELECT * FROM run_log
           WHERE task_id IS NULL AND queue_id = ?
           ORDER BY id DESC LIMIT ?""",
        (connection_queue_id(conn), limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_orchestrator_run_log(conn, limit=200):
    rows = conn.execute(
        """
        SELECT
            run_log.*,
            tasks.title AS task_title
        FROM run_log
        LEFT JOIN tasks ON tasks.id = run_log.task_id
        WHERE run_log.author = 'orchestrator'
          AND run_log.queue_id = ?
        ORDER BY run_log.id DESC
        LIMIT ?
        """,
        (connection_queue_id(conn), limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _empty_purge_result(**overrides):
    result = {
        "deleted_rows": 0,
        "deleted_transcripts": 0,
        "reclaimed_db_bytes": 0,
        "reclaimed_artifact_bytes": 0,
        "compacted": False,
    }
    result.update(overrides)
    return result


def format_purge_summary(result):
    """Return one concise maintenance line for a purge result."""
    return (
        "Maintenance purge: "
        f"deleted {result.get('deleted_rows', 0)} run_log rows, "
        f"{result.get('deleted_transcripts', 0)} transcripts; "
        f"reclaimed {result.get('reclaimed_db_bytes', 0)} B database, "
        f"{result.get('reclaimed_artifact_bytes', 0)} B artifacts"
    )


def _sqlite_disk_usage(db_path):
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += Path(f"{db_path}{suffix}").stat().st_size
        except FileNotFoundError:
            pass
    return total


def _vacuum(conn):
    """Rewrite the database to reclaim free pages. Requires no open transaction.

    In WAL mode the compact copy lands in the WAL, so a truncate checkpoint is
    required before the main database file shrinks on disk.
    """
    conn.commit()
    isolation = conn.isolation_level
    try:
        conn.isolation_level = None
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.isolation_level = isolation


def _parse_before_date_posix(before_date):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(before_date, fmt).timestamp()
        except ValueError:
            continue
    return None


def _parse_task_artifact_id(name):
    if not name.startswith(TASK_ARTIFACT_DIR_PREFIX):
        return None
    suffix = name[len(TASK_ARTIFACT_DIR_PREFIX):]
    if not suffix.isdigit():
        return None
    return int(suffix)


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
_DIRECTORY_NOFOLLOW_FLAGS = _DIRECTORY_FLAGS | os.O_NOFOLLOW
_ARTIFACTS_DIR_NAME = "artifacts"


def _open_directory_nofollow(path, *, dir_fd=None):
    """Open a directory without following a symlink at the last component."""
    if dir_fd is None:
        return os.open(path, _DIRECTORY_NOFOLLOW_FLAGS)
    return os.open(path, _DIRECTORY_NOFOLLOW_FLAGS, dir_fd=dir_fd)


def _open_nested_directory_nofollow(parent_path, *components):
    """Open nested directories from a trusted parent without following links.

    The parent is opened by its already-resolved path. Each additional
    component is opened descriptor-relatively with O_DIRECTORY|O_NOFOLLOW so
    an intermediate symlink cannot redirect traversal.
    """
    fds = []
    try:
        fds.append(os.open(os.fspath(parent_path), _DIRECTORY_FLAGS))
        for component in components:
            if not _is_simple_filename(component):
                raise OSError("refusing unsafe path component")
            fds.append(_open_directory_nofollow(component, dir_fd=fds[-1]))
        return fds.pop()
    finally:
        for fd in fds:
            os.close(fd)


def _open_canonical_artifacts_fd(db_path, queue_id):
    """Open one queue's artifacts directory from the trusted state parent."""
    if not _is_id(queue_id):
        raise OSError("refusing unsafe queue id")
    return _open_nested_directory_nofollow(
        Path(db_path).resolve().parent,
        QUEUES_DIR_NAME,
        queue_id,
        _ARTIFACTS_DIR_NAME,
    )


def _is_simple_filename(name):
    return (
        name not in (".", "..")
        and os.sep not in name
        and (os.altsep is None or os.altsep not in name)
    )


def _completed_task_ids(conn):
    rows = conn.execute(
        "SELECT id FROM tasks WHERE status = ? AND queue_id = ?",
        (COMPLETED_TASK_STATUS, connection_queue_id(conn)),
    ).fetchall()
    return {row[0] for row in rows}


def _purge_completed_task_transcripts(db_path, queue_id, done_task_ids, cutoff_ts):
    """Delete old transcript files under one queue's artifacts root.

    Starts from the trusted directory that contains the database, then opens
    ``queues/<queue-id>/artifacts`` descriptor-relatively with
    O_DIRECTORY|O_NOFOLLOW. Task directories are opened the same way so a
    swapped symlink cannot redirect cleanup outside the canonical tree.
    """
    deleted = 0
    reclaimed = 0
    if cutoff_ts is None or not done_task_ids:
        return deleted, reclaimed

    try:
        root_fd = _open_canonical_artifacts_fd(db_path, queue_id)
    except OSError:
        return deleted, reclaimed

    try:
        with os.scandir(root_fd) as dir_entries:
            for entry in dir_entries:
                task_id = _parse_task_artifact_id(entry.name)
                if task_id is None or task_id not in done_task_ids:
                    continue
                if not _is_simple_filename(entry.name):
                    continue
                try:
                    if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                        continue
                    task_fd = _open_directory_nofollow(entry.name, dir_fd=root_fd)
                except OSError:
                    continue
                try:
                    deleted_here, reclaimed_here = _purge_task_transcript_dir(
                        task_fd, cutoff_ts
                    )
                finally:
                    os.close(task_fd)
                deleted += deleted_here
                reclaimed += reclaimed_here
                try:
                    os.rmdir(entry.name, dir_fd=root_fd)
                except OSError:
                    pass
    finally:
        os.close(root_fd)
    return deleted, reclaimed


def _purge_task_transcript_dir(task_fd, cutoff_ts):
    deleted = 0
    reclaimed = 0
    try:
        file_entries = os.scandir(task_fd)
    except OSError:
        return deleted, reclaimed

    with file_entries:
        for entry in file_entries:
            if not entry.name.endswith(".log") or not _is_simple_filename(entry.name):
                continue
            try:
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if st.st_mtime >= cutoff_ts:
                continue
            try:
                os.unlink(entry.name, dir_fd=task_fd)
            except OSError:
                continue
            deleted += 1
            reclaimed += max(0, st.st_size)
    return deleted, reclaimed


def purge_run_log(conn, before_date=None, days=None, compact=False):
    """Purge ephemeral runtime history for completed work.

    Default retention is seven days. Eligible rows are this queue's run_log
    entries for done tasks and this queue's global orchestrator entries
    (`task_id IS NULL`). Unfinished task history, task records, and comments
    are preserved. Old transcript files for this queue's completed tasks are
    removed from that queue's artifacts root. Cleanup opens
    ``queues/<queue-id>/artifacts`` from the database parent through
    no-follow directory descriptors. When `compact` is true and rows were
    deleted, SQLite pages are reclaimed with VACUUM.
    """
    if days is not None and days < 0:
        raise ValueError("days must be >= 0")

    queue_id = connection_queue_id(conn)
    params = [queue_id, COMPLETED_TASK_STATUS, queue_id]
    if before_date:
        cutoff_sql = "created_at < ?"
        params.append(before_date)
        file_cutoff = _parse_before_date_posix(before_date)
    elif days == 0:
        cutoff_sql = "1"
        file_cutoff = time.time()
    else:
        retention = RUN_LOG_RETENTION_DAYS if days is None else days
        cutoff_sql = "created_at < datetime('now', ?)"
        params.append(f"-{retention} days")
        file_cutoff = time.time() - retention * 86400

    db_path = get_connection_db_path(conn)
    before_disk = _sqlite_disk_usage(db_path) if compact and db_path else 0

    deleted_rows = conn.execute(
        "DELETE FROM run_log WHERE queue_id = ? AND "
        "(task_id IS NULL OR task_id IN "
        "(SELECT id FROM tasks WHERE status = ? AND queue_id = ?)) "
        f"AND ({cutoff_sql})",
        params,
    ).rowcount
    conn.commit()
    if deleted_rows is None or deleted_rows < 0:
        deleted_rows = 0

    deleted_transcripts = 0
    reclaimed_artifact_bytes = 0
    if db_path and file_cutoff is not None:
        deleted_transcripts, reclaimed_artifact_bytes = (
            _purge_completed_task_transcripts(
                db_path,
                queue_id,
                _completed_task_ids(conn),
                file_cutoff,
            )
        )

    compacted = False
    reclaimed_db_bytes = 0
    if compact and deleted_rows and db_path:
        try:
            _vacuum(conn)
            compacted = True
        except sqlite3.Error:
            compacted = False
        if compacted:
            reclaimed_db_bytes = max(0, before_disk - _sqlite_disk_usage(db_path))

    return _empty_purge_result(
        deleted_rows=deleted_rows,
        deleted_transcripts=deleted_transcripts,
        reclaimed_db_bytes=reclaimed_db_bytes,
        reclaimed_artifact_bytes=reclaimed_artifact_bytes,
        compacted=compacted,
    )


# ── Comments ───────────────────────────────────────────────────────────

def add_comment(
    conn,
    task_id,
    message,
    kind="comment",
    verb=None,
    author=None,
    review_round=None,
    expected_review_round=None,
):
    require_task_queue(conn, task_id)
    task = None
    if review_round is None or expected_review_round is not None:
        task = get_task(conn, task_id)

    if expected_review_round is not None:
        if not task:
            raise ValueError(f"Task {task_id} not found")
        if expected_review_round != task["review_round"]:
            raise ValueError(
                f"Review round mismatch for task {task_id}: expected {task['review_round']}, got {expected_review_round}"
            )

    if review_round is None:
        review_round = task["review_round"] if task else 0

    conn.execute(
        """INSERT INTO comments (task_id, review_round, verb, author, message, kind)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (task_id, review_round, verb, author, message, kind),
    )
    conn.commit()


def get_comments(conn, task_id):
    rows = conn.execute(
        "SELECT * FROM comments WHERE task_id = ? ORDER BY id ASC", (task_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ── Queries for orchestrator ───────────────────────────────────────────

def find_ready_task(conn, exclude_ids=None):
    """Find the first queued ready task, ordered by sequence_index then id.

    When any supertask is in ``pending_subtasks``, only its runnable children
    are eligible. This gives active supertask execution exclusive ownership of
    the queue until its child sequence finishes or blocks.

    Child tasks (parent_task_id IS NOT NULL) are only eligible when:
      1. Their parent supertask has status = 'pending_subtasks', AND
      2. No earlier sibling (lower sequence_index) is still non-done.

    ``exclude_ids`` skips those task ids (used to keep a task under
    smart-unblock consultation undispatchable even if status was mutated).
    A retired queue or a missing worktree has nothing dispatchable.
    """
    if queue_dispatch_block_reason(conn):
        return None
    queue_id = connection_queue_id(conn)
    excluded = [int(i) for i in (exclude_ids or ()) if i is not None]
    exclude_sql = ""
    params: list = [queue_id, queue_id, queue_id]
    if excluded:
        placeholders = ",".join("?" for _ in excluded)
        exclude_sql = f" AND id NOT IN ({placeholders})"
        params.extend(excluded)
    row = conn.execute(
        f"""WITH has_active_supertask(active) AS (
               SELECT EXISTS (
                   SELECT 1 FROM tasks active_parent
                   WHERE active_parent.status = 'pending_subtasks'
                     AND active_parent.queue_id = ?
               )
           ),
           has_blocked_task(blocked) AS (
               SELECT EXISTS (
                   SELECT 1 FROM tasks blocked_task
                   WHERE blocked_task.status = 'blocked'
                     AND blocked_task.queue_id = ?
               )
           )
           SELECT * FROM tasks
           WHERE queue_id = ?
             AND status = 'ready'
           {exclude_sql}
           AND (
               (SELECT blocked FROM has_blocked_task) = 0
               OR allow_when_blocked = 1
           )
           AND (
               (
                   (SELECT active FROM has_active_supertask) = 1
                   AND parent_task_id IS NOT NULL
                   AND (
                       SELECT status FROM tasks p WHERE p.id = tasks.parent_task_id
                   ) = 'pending_subtasks'
                   AND NOT EXISTS (
                       SELECT 1 FROM tasks sib
                       WHERE sib.parent_task_id = tasks.parent_task_id
                         AND sib.sequence_index IS NOT NULL
                         AND tasks.sequence_index IS NOT NULL
                         AND sib.sequence_index < tasks.sequence_index
                         AND sib.status != 'done'
                   )
               )
               OR (
                   (SELECT active FROM has_active_supertask) = 0
                   AND (
                       parent_task_id IS NULL
                       OR (
                           (SELECT status FROM tasks p WHERE p.id = tasks.parent_task_id)
                               = 'pending_subtasks'
                           AND NOT EXISTS (
                               SELECT 1 FROM tasks sib
                               WHERE sib.parent_task_id = tasks.parent_task_id
                                 AND sib.sequence_index IS NOT NULL
                                 AND tasks.sequence_index IS NOT NULL
                                 AND sib.sequence_index < tasks.sequence_index
                                 AND sib.status != 'done'
                           )
                       )
                   )
               )
           )
           ORDER BY CASE WHEN sequence_index IS NULL THEN 1 ELSE 0 END ASC,
                    sequence_index ASC, id ASC
           LIMIT 1""",
        params,
    ).fetchone()
    return _row_to_task(row) if row else None


def claim_ready_task(conn, task_id):
    """Atomically claim one ready task for this queue.

    Returns the claimed task, or None when it is no longer ready on this queue,
    the queue already has a running task, or the queue can no longer dispatch
    because it is retired or its worktree is gone. The unique running-task
    index is the backstop. An unfinished cutover activation is refused.
    """
    assert_activation_finished(conn)
    queue_id = connection_queue_id(conn)
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if queue_dispatch_block_reason(conn):
            conn.rollback()
            return None
        row = conn.execute(
            "SELECT status, queue_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None or row["queue_id"] != queue_id or row["status"] != "ready":
            conn.rollback()
            return None
        running = conn.execute(
            """SELECT id FROM tasks
               WHERE queue_id = ? AND status = 'running' AND id != ?""",
            (queue_id, task_id),
        ).fetchone()
        if running is not None:
            conn.rollback()
            return None
        update_task(conn, task_id, commit=False, status="running")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_task(conn, task_id)


def list_ready_tasks_blocked_by_blocked_gate(conn):
    """Return ready tasks skipped specifically by the blocked-task gate."""
    queue_id = connection_queue_id(conn)
    rows = conn.execute(
        """WITH has_active_supertask(active) AS (
               SELECT EXISTS (
                   SELECT 1 FROM tasks active_parent
                   WHERE active_parent.status = 'pending_subtasks'
                     AND active_parent.queue_id = ?
               )
           ),
           has_blocked_task(blocked) AS (
               SELECT EXISTS (
                   SELECT 1 FROM tasks blocked_task
                   WHERE blocked_task.status = 'blocked'
                     AND blocked_task.queue_id = ?
               )
           )
           SELECT * FROM tasks
           WHERE queue_id = ?
             AND status = 'ready'
           AND allow_when_blocked = 0
           AND (SELECT blocked FROM has_blocked_task) = 1
           AND (
               (
                   (SELECT active FROM has_active_supertask) = 1
                   AND parent_task_id IS NOT NULL
                   AND (
                       SELECT status FROM tasks p WHERE p.id = tasks.parent_task_id
                   ) = 'pending_subtasks'
                   AND NOT EXISTS (
                       SELECT 1 FROM tasks sib
                       WHERE sib.parent_task_id = tasks.parent_task_id
                         AND sib.sequence_index IS NOT NULL
                         AND tasks.sequence_index IS NOT NULL
                         AND sib.sequence_index < tasks.sequence_index
                         AND sib.status != 'done'
                   )
               )
               OR (
                   (SELECT active FROM has_active_supertask) = 0
                   AND (
                       parent_task_id IS NULL
                       OR (
                           (SELECT status FROM tasks p WHERE p.id = tasks.parent_task_id)
                               = 'pending_subtasks'
                           AND NOT EXISTS (
                               SELECT 1 FROM tasks sib
                               WHERE sib.parent_task_id = tasks.parent_task_id
                                 AND sib.sequence_index IS NOT NULL
                                 AND tasks.sequence_index IS NOT NULL
                                 AND sib.sequence_index < tasks.sequence_index
                                 AND sib.status != 'done'
                           )
                       )
                   )
               )
           )
           ORDER BY CASE WHEN sequence_index IS NULL THEN 1 ELSE 0 END ASC,
                    sequence_index ASC, id ASC""",
        (queue_id, queue_id, queue_id),
    ).fetchall()
    return [_row_to_task(row) for row in rows]


def get_child_tasks(conn, parent_task_id):
    """Return all child tasks of a supertask, ordered by sequence_index then id."""
    rows = conn.execute(
        "SELECT * FROM tasks WHERE parent_task_id = ? "
        "ORDER BY CASE WHEN sequence_index IS NULL THEN 1 ELSE 0 END ASC, "
        "sequence_index ASC, id ASC",
        (parent_task_id,),
    ).fetchall()
    return [_row_to_task(r) for r in rows]


def renumber_siblings(conn, parent_task_id):
    """Renumber all children of parent at 100-step intervals, preserving relative order."""
    require_task_queue(conn, parent_task_id)
    children = conn.execute(
        "SELECT id FROM tasks WHERE parent_task_id = ? "
        "ORDER BY CASE WHEN sequence_index IS NULL THEN 1 ELSE 0 END ASC, "
        "sequence_index ASC, id ASC",
        (parent_task_id,),
    ).fetchall()
    for i, child in enumerate(children):
        new_index = (i + 1) * 100
        conn.execute(
            "UPDATE tasks SET sequence_index = ? WHERE id = ?",
            (new_index, child["id"]),
        )
    conn.commit()


def attach_follow_up_to_supertask(conn, source_task_id, follow_up_task_id):
    """Attach a detached follow-up after its source within the same supertask."""
    source = get_task(conn, source_task_id)
    follow_up = get_task(conn, follow_up_task_id)
    if not source or not follow_up:
        raise ValueError("source task and follow-up task must both exist")

    parent_id = source.get("parent_task_id")
    if parent_id is None:
        return False
    if follow_up.get("parent_task_id") == parent_id:
        return True
    if follow_up.get("parent_task_id") is not None:
        raise ValueError(
            f"Follow-up task {follow_up_task_id} already belongs to "
            f"supertask {follow_up['parent_task_id']}"
        )

    parent = get_task(conn, parent_id)
    if not parent or parent.get("kind") != "supertask":
        raise ValueError(f"Parent task {parent_id} is not a supertask")
    if (
        follow_up.get("repository_id") != source.get("repository_id")
        or follow_up.get("queue_id") != source.get("queue_id")
        or parent.get("repository_id") != source.get("repository_id")
        or parent.get("queue_id") != source.get("queue_id")
    ):
        raise ValueError("follow-up task must belong to the same repository and queue")

    renumber_siblings(conn, parent_id)
    source = get_task(conn, source_task_id)
    sequence_index = (source.get("sequence_index") or 0) + 1
    conn.execute(
        """UPDATE tasks
           SET parent_task_id = ?, sequence_index = ?, branch = ?,
               updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (parent_id, sequence_index, parent["branch"], follow_up_task_id),
    )
    renumber_siblings(conn, parent_id)
    conn.commit()
    return True


def reposition_task(conn, task_id, *, before_id=None, after_id=None):
    """Reposition a top-level ready task before or after another top-level ready task.

    Exactly one of before_id or after_id must be supplied.
    Both task_id and the anchor must be top-level (parent_task_id IS NULL)
    and have status = 'ready'.

    Renumbers all top-level ready tasks at 100-step sequence_index intervals
    to preserve the new order.
    """
    if (before_id is None) == (after_id is None):
        raise ValueError("Exactly one of before_id or after_id must be supplied")

    anchor_id = before_id if before_id is not None else after_id

    if anchor_id == task_id:
        raise ValueError(f"Cannot requeue task {task_id} relative to itself")

    anchor = conn.execute("SELECT * FROM tasks WHERE id = ?", (anchor_id,)).fetchone()
    if not anchor:
        raise ValueError(f"Anchor task {anchor_id} not found")
    if anchor["parent_task_id"] is not None:
        raise ValueError(f"Anchor task {anchor_id} is a child task; only top-level tasks can be requeued")
    if anchor["status"] != "ready":
        raise ValueError(f"Anchor task {anchor_id} is not ready (status={anchor['status']})")

    target = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not target:
        raise ValueError(f"Task {task_id} not found")
    if target["parent_task_id"] is not None:
        raise ValueError(f"Task {task_id} is a child task; only top-level tasks can be requeued")
    if target["status"] != "ready":
        raise ValueError(f"Task {task_id} is not ready (status={target['status']})")

    queue_id = connection_queue_id(conn)
    if target["queue_id"] != queue_id or anchor["queue_id"] != queue_id:
        raise ValueError("requeue stays inside the current queue")
    if target["repository_id"] != anchor["repository_id"]:
        raise ValueError("requeue stays inside one repository")
    # Fetch all top-level ready tasks in current queue order
    rows = conn.execute(
        "SELECT id FROM tasks WHERE status = 'ready' AND parent_task_id IS NULL "
        "AND queue_id = ? "
        f"ORDER BY {READY_TASK_ORDER_BY}",
        (queue_id,),
    ).fetchall()
    ordered = [r["id"] for r in rows]

    # Remove target from its current position
    ordered = [tid for tid in ordered if tid != task_id]

    # Find anchor's index and insert target
    anchor_index = ordered.index(anchor_id)
    if before_id is not None:
        ordered.insert(anchor_index, task_id)
    else:
        ordered.insert(anchor_index + 1, task_id)

    # Renumber all tasks with explicit sequence_index values.
    for i, tid in enumerate(ordered):
        conn.execute(
            "UPDATE tasks SET sequence_index = ? WHERE id = ?",
            ((i + 1) * 100, tid),
        )
    conn.commit()

    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    task = _row_to_task(row)
    task["skips"] = get_task_skips(conn, task_id)
    return task


def get_tasks_on_branch(conn, branch, landed_only=True):
    """Get tasks on a given branch within the connection's repository."""
    repository_id = connection_repository_id(conn)
    if landed_only:
        rows = conn.execute(
            """SELECT * FROM tasks
               WHERE branch = ? AND repository_id = ? AND commit_hash IS NOT NULL
               ORDER BY id ASC""",
            (branch, repository_id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE branch = ? AND repository_id = ? ORDER BY id ASC",
            (branch, repository_id),
        ).fetchall()
    return [_row_to_task(r) for r in rows]


# ── Orchestrator Runtime ──────────────────────────────────────────────

def upsert_runtime(conn, **fields):
    """Insert or replace the runtime row for the connection's queue."""
    fields.pop("singleton", None)
    fields["queue_id"] = connection_queue_id(conn)
    cols = list(fields.keys())
    placeholders = []
    params = []
    for column in cols:
        if fields[column] == "CURRENT_TIMESTAMP":
            placeholders.append("CURRENT_TIMESTAMP")
        else:
            placeholders.append("?")
            params.append(fields[column])
    updates = ", ".join(
        f"{column} = excluded.{column}" for column in cols if column != "queue_id"
    )
    sql = (
        f"INSERT INTO orchestrator_runtime ({', '.join(cols)}) "
        f"VALUES ({', '.join(placeholders)}) "
        f"ON CONFLICT(queue_id) DO UPDATE SET {updates}"
    )
    conn.execute(sql, params)
    conn.commit()


def update_runtime(conn, **fields):
    """Update specific fields on this queue's runtime row."""
    if not fields:
        return
    fields.pop("singleton", None)
    fields["updated_at"] = "CURRENT_TIMESTAMP"
    sets = []
    params = []
    for key, value in fields.items():
        if value == "CURRENT_TIMESTAMP":
            sets.append(f"{key} = CURRENT_TIMESTAMP")
        else:
            sets.append(f"{key} = ?")
            params.append(value)
    params.append(connection_queue_id(conn))
    conn.execute(
        f"UPDATE orchestrator_runtime SET {', '.join(sets)} WHERE queue_id = ?",
        params,
    )
    conn.commit()


def get_runtime(conn):
    """Read this queue's orchestrator_runtime row. Returns dict or None."""
    row = conn.execute(
        "SELECT * FROM orchestrator_runtime WHERE queue_id = ?",
        (connection_queue_id(conn),),
    ).fetchone()
    return dict(row) if row else None

