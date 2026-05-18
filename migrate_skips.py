import sqlite3
import os
import sys
from pathlib import Path

# Allow importing db.py from the scripts directory
scripts_dir = Path(__file__).resolve().parent / "kanban-orchestra" / "scripts"
if scripts_dir.exists():
    sys.path.insert(0, str(scripts_dir))

try:
    import db
except ImportError:
    # Fallback to current directory if not found in expected location
    sys.path.insert(0, os.getcwd())
    try:
        import db
    except ImportError:
        db = None

def migrate():
    if db:
        try:
            db_path = db.get_db_path()
        except Exception:
            db_path = "kanban-orchestra.db"
    else:
        db_path = os.environ.get("KANBAN_DB", "kanban-orchestra.db")

    if not os.path.exists(db_path):
        print(f"No database found at {db_path}, skipping migration.")
        return

    print(f"Connecting to {db_path}...")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=OFF")

    # Check if we need to migrate
    cur = conn.execute("PRAGMA table_info(tasks)")
    cols = [row[1] for row in cur.fetchall()]
    if "skip_commit_plan" not in cols:
        print("skip_commit_plan not found in tasks table, already migrated or fresh DB.")
        conn.close()
        return

    print("Migrating skip_commit_plan to task_skips table...")

    # 1. Create task_skips table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_skips (
            task_id  INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            step     TEXT    NOT NULL
                             CHECK(step IN ('commit-plan','commit-plan-review',
                                            'commit-review','commit-review-supertask')),
            PRIMARY KEY (task_id, step)
        )
    """)

    # 2. Copy skip_commit_plan = 1 into task_skips
    conn.execute("""
        INSERT OR IGNORE INTO task_skips (task_id, step)
        SELECT id, 'commit-plan' FROM tasks WHERE skip_commit_plan = 1
    """)

    # 3. Recreate tasks table without skip_commit_plan
    new_tasks_sql = """
    CREATE TABLE tasks_new (
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
        commit_plan             TEXT
    )
    """
    conn.execute(new_tasks_sql)

    # Copy data
    conn.execute("""
        INSERT INTO tasks_new (
            id, title, description, status, next_step, branch, commit_hash,
            stash_ref, coder_agent, review_round, last_review_decision,
            created_at, ready_at, updated_at, kind, parent_task_id,
            sequence_index, commit_plan
        )
        SELECT
            id, title, description, status, next_step, branch, commit_hash,
            stash_ref, coder_agent, review_round, last_review_decision,
            created_at, ready_at, updated_at, kind, parent_task_id,
            sequence_index, commit_plan
        FROM tasks
    """)

    conn.execute("DROP TABLE tasks")
    conn.execute("ALTER TABLE tasks_new RENAME TO tasks")

    conn.commit()
    conn.close()
    print("Migration complete.")

if __name__ == "__main__":
    migrate()
