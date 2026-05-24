"""
Database layer for Kanban Orchestra.

Handles SQLite schema creation, connection management (WAL mode),
and all queries used by task.py and orchestrator.py.
"""

import sqlite3
import os
import subprocess
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 15
LOCK_FILE_NAME = "kanban-orchestra.lock"

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS tasks (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    title                   TEXT NOT NULL,
    description             TEXT,
    status                  TEXT NOT NULL DEFAULT 'none'
        CHECK(status IN ('none', 'ready', 'running', 'done', 'blocked', 'pending_subtasks')),
    next_step               TEXT NOT NULL DEFAULT 'commit-make'
        CHECK(next_step IN ('commit-make', 'commit-review',
                            'commit-make-supertask', 'commit-review-supertask',
                            'commit-plan', 'commit-plan-review',
                            'none')),
    branch                  TEXT,
    commit_hash             TEXT,
    stash_ref               TEXT,
    coder_agent             TEXT,
    reviewer_agent          TEXT,
    review_round            INTEGER DEFAULT 0,
    last_review_decision    TEXT DEFAULT 'none'
        CHECK(last_review_decision IN ('none', 'approve', 'reject')),
    created_at              DATETIME DEFAULT CURRENT_TIMESTAMP,
    ready_at                DATETIME DEFAULT NULL,
    updated_at              DATETIME DEFAULT CURRENT_TIMESTAMP,
    kind                    TEXT NOT NULL DEFAULT 'task'
        CHECK(kind IN ('task', 'supertask')),
    parent_task_id          INTEGER REFERENCES tasks(id),
    sequence_index          INTEGER,
    commit_plan             TEXT,
    follow_up_task_id       INTEGER REFERENCES tasks(id),
    allow_when_blocked      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS task_skips (
    task_id  INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step     TEXT    NOT NULL
                     CHECK(step IN ('commit-plan','commit-plan-review',
                                    'commit-review','commit-review-supertask')),
    PRIMARY KEY (task_id, step)
);

CREATE TABLE IF NOT EXISTS run_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER REFERENCES tasks(id),
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
    singleton            INTEGER PRIMARY KEY CHECK(singleton = 1),
    status               TEXT NOT NULL
        CHECK(status IN ('idle', 'running', 'starting', 'stopping', 'stopped', 'hard-break', 'error')),
    pid                  INTEGER,
    started_at           DATETIME,
    last_heartbeat_at    DATETIME,
    current_task_id      INTEGER REFERENCES tasks(id),
    current_step         TEXT
        CHECK(current_step IN (
            'commit-make', 'commit-review',
            'commit-make-supertask', 'commit-review-supertask',
            'commit-plan', 'commit-plan-review',
            'none'
        )),
    current_branch       TEXT,
    review_round         INTEGER,
    active_agents        INTEGER NOT NULL DEFAULT 0,
    status_message       TEXT,
    updated_at           DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


def get_db_path(db_path: str | None = None) -> str:
    """Return the database path. Checks explicit arg, KANBAN_DB, then repo root."""
    if db_path:
        return db_path
    if os.environ.get("KANBAN_DB"):
        return os.environ["KANBAN_DB"]
    return str(get_workspace()["db_path"])


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
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise RuntimeError("Not inside a git repository and KANBAN_DB is not set.") from e
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
    """Resolve the kanban workspace paths for the current repo."""
    repo_root = get_repo_root(cwd)
    return {
        "repo_root": repo_root,
        "db_path": repo_root / "kanban-orchestra.db",
        "sql_path": repo_root / "kanban-orchestra.sql",
    }


def get_lock_path(db_path: str | None = None) -> Path:
    """Return the repo-scoped singleton lock path for the current workspace."""
    return Path(get_db_path(db_path)).resolve().with_name(LOCK_FILE_NAME)


def get_instance_identity(
    db_path: str | None = None,
    *,
    cwd: str | None = None,
    lock_path: str | Path | None = None,
) -> dict[str, str]:
    """Return stable identifiers for the current Orchestra work-repo instance.

    The git repo root is the human-facing instance identity. The database,
    runtime directory, and lock path remain explicit because advanced setups may
    override KANBAN_DB while still launching from a work repository.
    """
    resolved_db_path: Path | None = None
    if not (lock_path is not None and db_path is None and not os.environ.get("KANBAN_DB")):
        try:
            resolved_db_path = Path(get_db_path(db_path)).resolve()
        except RuntimeError:
            if lock_path is None:
                raise

    has_explicit_state_path = db_path is not None or lock_path is not None or os.environ.get("KANBAN_DB")
    repo_root = None
    if cwd is not None or not has_explicit_state_path:
        try:
            repo_root = get_repo_root(cwd)
        except RuntimeError:
            repo_root = None

    if repo_root is None:
        if resolved_db_path is not None:
            repo_root = resolved_db_path.parent
        elif lock_path is not None:
            repo_root = Path(lock_path).resolve().parent
        else:
            repo_root = get_repo_root(cwd)

    resolved_lock_path = Path(lock_path).resolve() if lock_path is not None else get_lock_path(db_path)
    runtime_root = get_runtime_root(db_path) if resolved_db_path is not None else repo_root / ".kanban-orchestra"
    resolved_db_path = resolved_db_path or repo_root / "kanban-orchestra.db"

    return {
        "repo_root": str(repo_root),
        "repo_label": repo_root.name or str(repo_root),
        "db_path": str(resolved_db_path),
        "runtime_root": str(runtime_root),
        "lock_path": str(resolved_lock_path),
    }


def get_runtime_root(db_path: str | None = None) -> Path:
    """Return the directory used for repo-scoped runtime artifacts."""
    return Path(get_db_path(db_path)).resolve().parent / ".kanban-orchestra"


def get_artifacts_root(db_path: str | None = None) -> Path:
    """Return the directory used for filesystem-backed run artifacts."""
    return get_runtime_root(db_path) / "artifacts"


def get_orchestrator_log_path(db_path: str | None = None) -> Path:
    """Return the path of the persisted orchestrator stdout log."""
    return get_runtime_root(db_path) / "orchestrator.log"


def new_agent_transcript_path(
    task_id: int,
    verb: str | None,
    agent_name: str | None,
    db_path: str | None = None,
) -> Path:
    """Create and return a unique path for one agent transcript."""
    artifacts_root = get_artifacts_root(db_path)
    task_dir = artifacts_root / f"task-{task_id}"
    task_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    safe_verb = (verb or "run").replace("/", "-")
    safe_agent = (agent_name or "agent").replace("/", "-")
    return task_dir / f"{stamp}-{safe_verb}-{safe_agent}.log"


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """Open (and possibly create) the database with WAL mode."""
    path = get_db_path(db_path)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    _check_schema_compatible(conn)
    conn.executescript(SCHEMA_SQL)
    return conn


def get_connection_db_path(conn: sqlite3.Connection) -> str | None:
    """Return the on-disk main database path for an open SQLite connection."""
    row = conn.execute("PRAGMA database_list").fetchone()
    if not row:
        return None
    try:
        return row["file"]
    except (KeyError, TypeError):
        return row[2]


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


def _migrate_orchestrator_runtime(conn: sqlite3.Connection) -> None:
    """Recreate the transient runtime table with current CHECK constraints."""
    cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(orchestrator_runtime)").fetchall()
    }
    required = {
        "singleton", "status", "pid", "started_at", "last_heartbeat_at",
        "current_task_id", "current_step", "current_branch", "review_round",
        "active_agents", "status_message", "updated_at",
    }
    if not required.issubset(cols):
        conn.executescript("DROP TABLE IF EXISTS orchestrator_runtime;")
        return

    conn.executescript("""
        BEGIN;
        CREATE TABLE orchestrator_runtime_migrated (
            singleton            INTEGER PRIMARY KEY CHECK(singleton = 1),
            status               TEXT NOT NULL
                CHECK(status IN ('idle', 'running', 'starting', 'stopping', 'stopped', 'hard-break', 'error')),
            pid                  INTEGER,
            started_at           DATETIME,
            last_heartbeat_at    DATETIME,
            current_task_id      INTEGER REFERENCES tasks(id),
            current_step         TEXT
                CHECK(current_step IN (
                    'commit-make', 'commit-review',
                    'commit-make-supertask', 'commit-review-supertask',
                    'commit-plan', 'commit-plan-review',
                    'none'
                )),
            current_branch       TEXT,
            review_round         INTEGER,
            active_agents        INTEGER NOT NULL DEFAULT 0,
            status_message       TEXT,
            updated_at           DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO orchestrator_runtime_migrated (
            singleton, status, pid, started_at, last_heartbeat_at, current_task_id,
            current_step, current_branch, review_round, active_agents, status_message,
            updated_at
        )
        SELECT
            singleton,
            CASE
                WHEN status IN ('idle', 'running', 'starting', 'stopping', 'stopped', 'hard-break', 'error')
                THEN status ELSE 'error'
            END,
            pid,
            started_at,
            last_heartbeat_at,
            CASE
                WHEN current_task_id IN (SELECT id FROM tasks) THEN current_task_id
                ELSE NULL
            END,
            CASE
                WHEN current_step IN (
                    'commit-make', 'commit-review',
                    'commit-make-supertask', 'commit-review-supertask',
                    'commit-plan', 'commit-plan-review',
                    'none'
                )
                THEN current_step ELSE 'none'
            END,
            current_branch,
            review_round,
            active_agents,
            status_message,
            updated_at
        FROM orchestrator_runtime;
        DROP TABLE orchestrator_runtime;
        ALTER TABLE orchestrator_runtime_migrated RENAME TO orchestrator_runtime;
        COMMIT;
    """)


def _migrate_skip_commit_plan_tasks(conn: sqlite3.Connection, task_cols: set[str]) -> None:
    """Migrate the old tasks.skip_commit_plan flag into task_skips rows."""
    value_expr = {
        "id": "id",
        "title": "title",
        "description": "description" if "description" in task_cols else "NULL",
        "status": "status" if "status" in task_cols else "'none'",
        "next_step": "next_step" if "next_step" in task_cols else "'commit-make'",
        "branch": "branch" if "branch" in task_cols else "NULL",
        "commit_hash": "commit_hash" if "commit_hash" in task_cols else "NULL",
        "stash_ref": "stash_ref" if "stash_ref" in task_cols else "NULL",
        "coder_agent": "coder_agent" if "coder_agent" in task_cols else "NULL",
        "reviewer_agent": "reviewer_agent" if "reviewer_agent" in task_cols else "NULL",
        "review_round": "review_round" if "review_round" in task_cols else "0",
        "last_review_decision": "last_review_decision" if "last_review_decision" in task_cols else "'none'",
        "created_at": "created_at" if "created_at" in task_cols else "CURRENT_TIMESTAMP",
        "ready_at": "ready_at" if "ready_at" in task_cols else "NULL",
        "updated_at": "updated_at" if "updated_at" in task_cols else "CURRENT_TIMESTAMP",
        "kind": "kind" if "kind" in task_cols else "'task'",
        "parent_task_id": "parent_task_id" if "parent_task_id" in task_cols else "NULL",
        "sequence_index": "sequence_index" if "sequence_index" in task_cols else "NULL",
        "commit_plan": "commit_plan" if "commit_plan" in task_cols else "NULL",
        "follow_up_task_id": "follow_up_task_id" if "follow_up_task_id" in task_cols else "NULL",
        "allow_when_blocked": "allow_when_blocked" if "allow_when_blocked" in task_cols else "0",
    }
    columns = list(value_expr)

    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript(f"""
            BEGIN;
            CREATE TEMP TABLE skip_commit_plan_tasks AS
                SELECT id FROM tasks WHERE skip_commit_plan = 1;
            CREATE TABLE tasks_migrated (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                title                   TEXT NOT NULL,
                description             TEXT,
                status                  TEXT NOT NULL DEFAULT 'none'
                    CHECK(status IN ('none', 'ready', 'running', 'done', 'blocked', 'pending_subtasks')),
                next_step               TEXT NOT NULL DEFAULT 'commit-make'
                    CHECK(next_step IN ('commit-make', 'commit-review',
                                        'commit-make-supertask', 'commit-review-supertask',
                                        'commit-plan', 'commit-plan-review',
                                        'none')),
                branch                  TEXT,
                commit_hash             TEXT,
                stash_ref               TEXT,
                coder_agent             TEXT,
                reviewer_agent          TEXT,
                review_round            INTEGER DEFAULT 0,
                last_review_decision    TEXT DEFAULT 'none'
                    CHECK(last_review_decision IN ('none', 'approve', 'reject')),
                created_at              DATETIME DEFAULT CURRENT_TIMESTAMP,
                ready_at                DATETIME DEFAULT NULL,
                updated_at              DATETIME DEFAULT CURRENT_TIMESTAMP,
                kind                    TEXT NOT NULL DEFAULT 'task'
                    CHECK(kind IN ('task', 'supertask')),
                parent_task_id          INTEGER REFERENCES tasks(id),
                sequence_index          INTEGER,
                commit_plan             TEXT,
                follow_up_task_id       INTEGER REFERENCES tasks(id),
                allow_when_blocked      INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO tasks_migrated ({", ".join(columns)})
                SELECT {", ".join(value_expr[column] for column in columns)}
                FROM tasks;
            DROP TABLE tasks;
            ALTER TABLE tasks_migrated RENAME TO tasks;
            CREATE TABLE IF NOT EXISTS task_skips (
                task_id  INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                step     TEXT    NOT NULL
                                 CHECK(step IN ('commit-plan','commit-plan-review',
                                                'commit-review','commit-review-supertask')),
                PRIMARY KEY (task_id, step)
            );
            INSERT OR IGNORE INTO task_skips (task_id, step)
                SELECT id, 'commit-plan' FROM skip_commit_plan_tasks;
            DROP TABLE skip_commit_plan_tasks;
            COMMIT;
        """)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()


def restore_dump(
    db_path: str | None = None,
    sql_path: str | None = None,
) -> dict[str, str]:
    """Restore a SQL dump into a fresh database file."""
    target_db_path = get_db_path(db_path)
    target_sql_path = sql_path or str(Path(target_db_path).resolve().parent / "kanban-orchestra.sql")
    db_preexisted = os.path.exists(target_db_path)

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


def _check_schema_compatible(conn: sqlite3.Connection) -> None:
    """Raise RuntimeError if the existing DB schema is incompatible.

    kanban-orchestra.db is local and expendable state. When a schema change
    requires a table recreation, the correct response is to delete
    kanban-orchestra.db and let it be recreated on next connect, or to restore
    from a current kanban-orchestra.sql dump.
    """
    task_cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
    }
    if not task_cols:
        return  # Fresh DB — no tables yet

    if "skip_commit_plan" in task_cols:
        _migrate_skip_commit_plan_tasks(conn, task_cols)
        task_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }

    # Columns that were removed in past schema versions and cannot be migrated.
    removed_cols = {
        "koid", "commit_hashes", "skip_same_branch_koid_check", "failed_coders",
    }
    present_legacy = removed_cols & task_cols
    if present_legacy:
        raise RuntimeError(
            f"kanban-orchestra.db has a legacy schema "
            f"(removed columns still present: {', '.join(sorted(present_legacy))}). "
            "Delete kanban-orchestra.db to recreate it, "
            "or run 'task restore' from a current kanban-orchestra.sql dump."
        )

    # Columns required in the current schema
    required_cols = {"commit_plan"}
    missing_cols = required_cols - task_cols
    if missing_cols:
        raise RuntimeError(
            f"kanban-orchestra.db is missing required columns: "
            f"{', '.join(sorted(missing_cols))}. "
            "Delete kanban-orchestra.db to recreate it, "
            "or run 'task restore' from a current kanban-orchestra.sql dump."
        )

    # Columns that can be added via ALTER TABLE migration
    if "follow_up_task_id" not in task_cols:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN follow_up_task_id INTEGER REFERENCES tasks(id)"
        )
        conn.commit()
    if "allow_when_blocked" not in task_cols:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN allow_when_blocked INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()
    if "reviewer_agent" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN reviewer_agent TEXT")
        conn.commit()

    # The task_skips table must exist and its step CHECK constraint must be present.
    # We check for the table name and for the presence of the 4 valid step names in the DDL.
    skips_schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_skips'"
    ).fetchone()
    if not skips_schema:
        raise RuntimeError(
            "kanban-orchestra.db is missing the 'task_skips' table. "
            "Delete kanban-orchestra.db to recreate it, "
            "or run 'task restore' from a current kanban-orchestra.sql dump."
        )
    valid_steps = ("'commit-plan'", "'commit-plan-review'", "'commit-review'", "'commit-review-supertask'")
    missing_steps = [s for s in valid_steps if s not in skips_schema[0]]
    if missing_steps:
        raise RuntimeError(
            f"kanban-orchestra.db has an outdated task_skips.step constraint "
            f"(missing: {', '.join(missing_steps)}). "
            "Delete kanban-orchestra.db to recreate it, "
            "or run 'task restore' from a current kanban-orchestra.sql dump."
        )

    # The next_step CHECK constraint must include 'commit-plan'
    tasks_schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
    ).fetchone()
    if tasks_schema and "'commit-plan'" not in tasks_schema[0]:
        raise RuntimeError(
            "kanban-orchestra.db has an outdated tasks.next_step constraint "
            "(missing 'commit-plan'). "
            "Delete kanban-orchestra.db to recreate it, "
            "or run 'task restore' from a current kanban-orchestra.sql dump."
        )

    # orchestrator_runtime must accept all current status and step names.
    # This table holds transient state, but preserve the singleton row when
    # possible so live deployments do not lose useful operator context.
    runtime_schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='orchestrator_runtime'"
    ).fetchone()
    if runtime_schema:
        required_runtime_tokens = ("'commit-make-supertask'", "'starting'", "'hard-break'")
        if any(token not in runtime_schema[0] for token in required_runtime_tokens):
            _migrate_orchestrator_runtime(conn)

    # run_log.task_id must be nullable so the orchestrator can write global entries
    # with task_id=NULL.  Older schema versions may have created it with NOT NULL.
    run_log_cols = {
        row["name"]: row
        for row in conn.execute("PRAGMA table_info(run_log)").fetchall()
    }
    if run_log_cols and run_log_cols.get("task_id", {})["notnull"]:
        conn.executescript("""
            BEGIN;
            CREATE TABLE run_log_migrated (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id     INTEGER REFERENCES tasks(id),
                verb        TEXT,
                author      TEXT,
                message     TEXT,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO run_log_migrated
                SELECT id, task_id, verb, author, message, created_at
                FROM run_log;
            DROP TABLE run_log;
            ALTER TABLE run_log_migrated RENAME TO run_log;
            COMMIT;
        """)

    # The comments.kind CHECK must include the full current kind set.
    # When it's outdated, recreate the table in-place preserving all rows.
    comments_schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='comments'"
    ).fetchone()
    if comments_schema:
        required_kinds = ("'validation'", "'plan-approval'", "'plan-rejection'",
                          "'done-without-commit'", "'deferred-build-changed'")
        missing_kinds = [k for k in required_kinds if k not in comments_schema[0]]
        if missing_kinds:
            conn.executescript("""
                BEGIN;
                CREATE TABLE comments_migrated (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id        INTEGER REFERENCES tasks(id),
                    review_round   INTEGER,
                    verb           TEXT,
                    author         TEXT,
                    message        TEXT,
                    kind           TEXT DEFAULT 'comment'
                        CHECK(kind IN ('comment', 'approval', 'rejection', 'commit-message',
                                       'validation', 'plan-approval', 'plan-rejection',
                                       'done-without-commit', 'deferred-build-changed')),
                    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO comments_migrated
                    SELECT id, task_id, review_round, verb, author, message, kind, created_at
                    FROM comments;
                DROP TABLE comments;
                ALTER TABLE comments_migrated RENAME TO comments;
                COMMIT;
            """)


def _row_to_task(row):
    return dict(row)


# ── Task CRUD ──────────────────────────────────────────────────────────

def add_task(
    conn,
    title,
    description=None,
    branch=None,
    coder_agent=None,
    reviewer_agent=None,
    kind="task",
    parent_task_id=None,
    sequence_index=None,
    status=None,
    skips=None,
    allow_when_blocked=False,
):
    if kind == "supertask":
        next_step = "commit-make-supertask"
    elif skips and "commit-plan" in skips:
        next_step = "commit-make"
    else:
        next_step = "commit-plan"

    if status is None:
        status = "none"
    cur = conn.execute(
        """INSERT INTO tasks (
               title, description, branch, coder_agent, reviewer_agent,
               kind, parent_task_id, sequence_index, next_step, status,
               allow_when_blocked
           )
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
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
        ),
    )
    task_id = cur.lastrowid
    if skips:
        for step in set(skips):
            conn.execute(
                "INSERT INTO task_skips (task_id, step) VALUES (?, ?)",
                (task_id, step),
            )
    conn.commit()
    return task_id


def should_skip_step(conn, task_id, step):
    return conn.execute(
        "SELECT 1 FROM task_skips WHERE task_id = ? AND step = ?",
        (task_id, step)
    ).fetchone() is not None


def add_task_skip(conn, task_id, step):
    conn.execute(
        "INSERT OR IGNORE INTO task_skips (task_id, step) VALUES (?, ?)",
        (task_id, step)
    )
    conn.commit()


def remove_task_skip(conn, task_id, step):
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


def list_tasks(conn, status=None, next_step=None, branch=None, page=1, page_size=20):
    query = (
        "SELECT id, title, status, next_step, branch, coder_agent, reviewer_agent, "
        "review_round, created_at, ready_at, updated_at, "
        "kind, parent_task_id, sequence_index, allow_when_blocked "
        "FROM tasks WHERE 1=1"
    )
    params = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if next_step:
        query += " AND next_step = ?"
        params.append(next_step)
    if branch:
        query += " AND branch = ?"
        params.append(branch)
    if status == "ready":
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


def update_task(conn, task_id, **fields):
    """Update arbitrary fields on a task. Automatically sets updated_at and manages ready_at."""
    if not fields:
        return
    allowed = {
        "title", "description", "status", "next_step", "branch",
        "commit_hash", "stash_ref", "coder_agent", "reviewer_agent",
        "review_round", "last_review_decision", "ready_at",
        "sequence_index", "commit_plan", "follow_up_task_id",
        "allow_when_blocked",
    }
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"Cannot update fields: {bad}")
    if "status" in fields:
        current = conn.execute(
            "SELECT status, ready_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if current:
            new_status = fields["status"]
            if new_status == "ready":
                if current["status"] != "ready" or current["ready_at"] is None:
                    fields["ready_at"] = "CURRENT_TIMESTAMP"
            else:
                fields["ready_at"] = None
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
    conn.commit()


def delete_task(conn, task_id):
    task = get_task(conn, task_id)
    if not task:
        raise ValueError(f"Task {task_id} not found")
    if task["status"] != "none":
        raise ValueError(f"Can only delete tasks with status 'none', got '{task['status']}'")
    conn.execute("DELETE FROM run_log WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM comments WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()


# ── Run Log ────────────────────────────────────────────────────────────

def add_run_log(conn, task_id, message, verb=None, author=None):
    conn.execute(
        "INSERT INTO run_log (task_id, verb, author, message) VALUES (?, ?, ?, ?)",
        (task_id, verb, author, message),
    )
    conn.commit()


def get_run_log(conn, task_id):
    rows = conn.execute(
        "SELECT * FROM run_log WHERE task_id = ? ORDER BY id DESC", (task_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_global_run_log(conn, limit=50):
    rows = conn.execute(
        "SELECT * FROM run_log WHERE task_id IS NULL ORDER BY id DESC LIMIT ?", (limit,)
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
        ORDER BY run_log.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def purge_run_log(conn, before_date=None, days=None):
    """Purge ephemeral run_log entries. Defaults to done tasks older than 30 days."""
    if before_date:
        conn.execute(
            "DELETE FROM run_log WHERE created_at < ?", (before_date,)
        )
    elif days is not None:
        if days == 0:
            conn.execute("DELETE FROM run_log")
        else:
            conn.execute(
                "DELETE FROM run_log WHERE created_at < datetime('now', ?)",
                (f"-{days} days",),
            )
    else:
        # Default: purge logs for done tasks older than 30 days
        conn.execute(
            """DELETE FROM run_log WHERE task_id IN (
                SELECT id FROM tasks WHERE status = 'done'
            ) AND created_at < datetime('now', '-30 days')"""
        )
    conn.commit()


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

def find_ready_task(conn):
    """Find the first queued ready task, ordered by sequence_index then id.

    When any supertask is in ``pending_subtasks``, only its runnable children
    are eligible. This gives active supertask execution exclusive ownership of
    the queue until its child sequence finishes or blocks.

    Child tasks (parent_task_id IS NOT NULL) are only eligible when:
      1. Their parent supertask has status = 'pending_subtasks', AND
      2. No earlier sibling (lower sequence_index) is still non-done.
    """
    row = conn.execute(
        """WITH has_active_supertask(active) AS (
               SELECT EXISTS (
                   SELECT 1 FROM tasks active_parent WHERE active_parent.status = 'pending_subtasks'
               )
           ),
           has_blocked_task(blocked) AS (
               SELECT EXISTS (
                   SELECT 1 FROM tasks blocked_task WHERE blocked_task.status = 'blocked'
               )
           )
           SELECT * FROM tasks
           WHERE status = 'ready'
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
           LIMIT 1"""
    ).fetchone()
    return _row_to_task(row) if row else None


def list_ready_tasks_blocked_by_blocked_gate(conn):
    """Return ready tasks skipped specifically by the blocked-task gate."""
    rows = conn.execute(
        """WITH has_active_supertask(active) AS (
               SELECT EXISTS (
                   SELECT 1 FROM tasks active_parent WHERE active_parent.status = 'pending_subtasks'
               )
           ),
           has_blocked_task(blocked) AS (
               SELECT EXISTS (
                   SELECT 1 FROM tasks blocked_task WHERE blocked_task.status = 'blocked'
               )
           )
           SELECT * FROM tasks
           WHERE status = 'ready'
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
                    sequence_index ASC, id ASC"""
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

    # Fetch all top-level ready tasks in current queue order
    rows = conn.execute(
        "SELECT id FROM tasks WHERE status = 'ready' AND parent_task_id IS NULL "
        f"ORDER BY {READY_TASK_ORDER_BY}"
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
    """Get tasks on a given branch. If landed_only, only those with a landed commit hash."""
    if landed_only:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE branch = ? AND commit_hash IS NOT NULL ORDER BY id ASC",
            (branch,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE branch = ? ORDER BY id ASC",
            (branch,),
        ).fetchall()
    return [_row_to_task(r) for r in rows]


# ── Orchestrator Runtime ──────────────────────────────────────────────

def upsert_runtime(conn, **fields):
    """Insert or replace the singleton orchestrator_runtime row."""
    fields["singleton"] = 1
    cols = list(fields.keys())
    placeholders = []
    params = []
    for c in cols:
        if fields[c] == "CURRENT_TIMESTAMP":
            placeholders.append("CURRENT_TIMESTAMP")
        else:
            placeholders.append("?")
            params.append(fields[c])
    sql = (
        f"INSERT OR REPLACE INTO orchestrator_runtime ({', '.join(cols)}) "
        f"VALUES ({', '.join(placeholders)})"
    )
    conn.execute(sql, params)
    conn.commit()


def update_runtime(conn, **fields):
    """Update specific fields on the singleton runtime row."""
    if not fields:
        return
    fields["updated_at"] = "CURRENT_TIMESTAMP"
    sets = []
    params = []
    for k, v in fields.items():
        if v == "CURRENT_TIMESTAMP":
            sets.append(f"{k} = CURRENT_TIMESTAMP")
        else:
            sets.append(f"{k} = ?")
            params.append(v)
    conn.execute(
        f"UPDATE orchestrator_runtime SET {', '.join(sets)} WHERE singleton = 1",
        params,
    )
    conn.commit()


def get_runtime(conn):
    """Read the singleton orchestrator_runtime row. Returns dict or None."""
    row = conn.execute(
        "SELECT * FROM orchestrator_runtime WHERE singleton = 1"
    ).fetchone()
    return dict(row) if row else None
