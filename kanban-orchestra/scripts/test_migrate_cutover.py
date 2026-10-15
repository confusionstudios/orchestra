"""Behavior coverage for the single legacy-to-central cutover."""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import db
import migrate_cutover


LEGACY_SQL = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'none',
    next_step TEXT NOT NULL DEFAULT 'commit-make',
    branch TEXT,
    commit_hash TEXT,
    stash_ref TEXT,
    coder_agent TEXT,
    reviewer_agent TEXT,
    review_round INTEGER DEFAULT 0,
    max_review_rounds INTEGER NOT NULL DEFAULT 3,
    last_review_decision TEXT DEFAULT 'none',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    ready_at DATETIME,
    last_ready_at DATETIME,
    first_started_at DATETIME,
    done_at DATETIME,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    kind TEXT NOT NULL DEFAULT 'commit',
    parent_task_id INTEGER REFERENCES tasks(id),
    sequence_index INTEGER,
    commit_plan TEXT,
    follow_up_task_id INTEGER REFERENCES tasks(id),
    allow_when_blocked INTEGER NOT NULL DEFAULT 0,
    block_reason TEXT,
    resume_next_step TEXT
);
CREATE TABLE task_agent_snapshots (
    task_id INTEGER PRIMARY KEY REFERENCES tasks(id),
    snapshot TEXT NOT NULL
);
CREATE TABLE task_skips (
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    step TEXT NOT NULL,
    PRIMARY KEY (task_id, step)
);
CREATE TABLE run_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id),
    verb TEXT,
    author TEXT,
    message TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id),
    review_round INTEGER,
    verb TEXT,
    author TEXT,
    message TEXT,
    kind TEXT DEFAULT 'comment',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE orchestrator_runtime (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    status TEXT NOT NULL,
    pid INTEGER,
    started_at DATETIME,
    last_heartbeat_at DATETIME,
    current_task_id INTEGER REFERENCES tasks(id),
    current_step TEXT,
    current_branch TEXT,
    review_round INTEGER,
    active_agents INTEGER NOT NULL DEFAULT 0,
    status_message TEXT,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    _git(path, "config", "user.email", "migrate@example.com")
    _git(path, "config", "user.name", "Migrate Test")
    (path / "README").write_text("work\n", encoding="utf-8")
    (path / ".gitignore").write_text(
        "kanban-orchestra.db\n"
        "kanban-orchestra.db-wal\n"
        "kanban-orchestra.db-shm\n"
        "kanban-orchestra.db-journal\n"
        "kanban-orchestra.lock\n"
        ".kanban-orchestra/\n",
        encoding="utf-8",
    )
    _git(path, "add", "README", ".gitignore")
    _git(path, "commit", "-m", "init")


def _git_status(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "status", "--short"],
        check=True, capture_output=True, text=True,
    )
    return result.stdout


def _leave_uncheckpointed_wal(path: Path, title: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """INSERT INTO tasks (title, status, next_step, kind, max_review_rounds)
               VALUES (?, 'ready', 'commit-make', 'commit', 3)""",
            (title,),
        )
        conn.commit()
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
    finally:
        conn.close()
    wal = Path(str(path) + "-wal")
    assert wal.is_file() and wal.stat().st_size > 0


def _listed_titles(cwd: Path, destination: Path) -> list[str]:
    env = os.environ.copy()
    env["KANBAN_DB"] = str(destination)
    env["KANBAN_NONINTERACTIVE"] = "1"
    env.pop("ORCHESTRA_QUEUE_ID", None)
    env.pop("ORCHESTRA_REPO_ROOT", None)
    script = Path(__file__).resolve().parent / "task.py"
    result = subprocess.run(
        [sys.executable, str(script), "list"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return [row["title"] for row in json.loads(result.stdout)]


def _legacy_db(worktree: Path) -> Path:
    path = worktree / "kanban-orchestra.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_SQL)
        conn.commit()
    finally:
        conn.close()
    return path


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _task_row(conn, title: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE title = ?", (title,)).fetchone()
    assert row is not None
    return row


def test_cutover_preserves_collisions_relationships_artifacts_and_history(tmp_path):
    primary = tmp_path / "primary"
    linked = tmp_path / "linked-dev"
    extra = tmp_path / "extra-source"
    _init_repo(primary)
    _git(primary, "worktree", "add", "-b", "dev", str(linked), "HEAD")
    _init_repo(extra)
    state_db = Path(os.environ["ORCHESTRA_STATE_DIR"]) / "orchestra.db"
    state_before = state_db.read_bytes()
    primary_db = _legacy_db(primary)
    linked_db = _legacy_db(linked)
    extra_db = _legacy_db(extra)

    with _connect(primary_db) as conn:
        conn.execute(
            """INSERT INTO tasks (
                   title, status, next_step, branch, stash_ref, kind, max_review_rounds
               ) VALUES ('Running parent', 'running', 'commit-make', 'feature-a', 'stash@{1}', 'commit', 3)"""
        )
        parent_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """INSERT INTO tasks (
                   title, status, next_step, branch, parent_task_id, kind, max_review_rounds, ready_at
               ) VALUES ('Ready child', 'ready', 'commit-review', 'feature-a', ?, 'commit', 3, '2026-01-01')""",
            (parent_id,),
        )
        conn.execute(
            """INSERT INTO tasks (
                   title, status, next_step, branch, kind, max_review_rounds, commit_hash
               ) VALUES ('Done follow', 'done', 'none', 'feature-a', 'commit', 3, 'abc123')"""
        )
        follow_id = conn.execute("SELECT id FROM tasks WHERE title = 'Done follow'").fetchone()[0]
        conn.execute("UPDATE tasks SET follow_up_task_id = ? WHERE id = ?", (follow_id, parent_id))
        conn.execute(
            "INSERT INTO comments (task_id, message, kind) VALUES (?, ?, 'comment')",
            (parent_id, "see artifacts/task-1/note.txt"),
        )
        conn.execute(
            """INSERT INTO comments (task_id, author, message, kind)
               VALUES (NULL, 'operator', 'queue-level note', 'comment')"""
        )
        conn.execute(
            "INSERT INTO run_log (task_id, verb, message) VALUES (?, 'commit-make', 'ran')",
            (parent_id,),
        )
        conn.execute(
            "INSERT INTO task_skips (task_id, step) VALUES (?, 'commit-plan')",
            (parent_id,),
        )
        conn.execute(
            "INSERT INTO task_agent_snapshots (task_id, snapshot) VALUES (?, ?)",
            (parent_id, json.dumps({"log": "artifacts/task-1/note.txt"})),
        )
        conn.execute(
            """INSERT INTO orchestrator_runtime (
                   singleton, status, pid, current_task_id, current_step, active_agents, status_message
               ) VALUES (1, 'running', 999999, ?, 'commit-make', 1, 'stale status')""",
            (parent_id,),
        )
        conn.commit()
    (primary / "kanban-orchestra.lock").write_text(
        f"role=orchestrator\npid={os.getpid()}\nrepo_root={primary}\n",
        encoding="utf-8",
    )
    note = primary / ".kanban-orchestra" / "artifacts" / "task-1" / "note.txt"
    note.parent.mkdir(parents=True)
    note.write_text("from-primary\n", encoding="utf-8")

    with _connect(linked_db) as conn:
        conn.execute(
            """INSERT INTO tasks (title, status, next_step, kind, max_review_rounds)
               VALUES ('Other draft', 'none', 'commit-make', 'commit', 3)"""
        )
        draft_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """INSERT INTO tasks (
                   title, status, next_step, kind, max_review_rounds, block_reason
               ) VALUES ('Other blocked', 'blocked', 'commit-make', 'commit', 3, 'needs-human')"""
        )
        conn.execute(
            "INSERT INTO comments (task_id, message, kind) VALUES (?, 'see artifacts/task-1/other.txt', 'comment')",
            (draft_id,),
        )
        conn.execute(
            """INSERT INTO comments (task_id, author, message, kind)
               VALUES (NULL, 'operator', 'linked queue note', 'comment')"""
        )
        conn.commit()
    other = linked / ".kanban-orchestra" / "artifacts" / "task-1" / "other.txt"
    other.parent.mkdir(parents=True)
    other.write_text("from-linked\n", encoding="utf-8")
    _leave_uncheckpointed_wal(linked_db, "Wal only")
    assert Path(str(linked_db) + "-wal").stat().st_size > 0

    with _connect(extra_db) as conn:
        conn.execute(
            """INSERT INTO tasks (title, status, next_step, kind, max_review_rounds)
               VALUES ('Explicit done', 'done', 'none', 'commit', 3)"""
        )
        conn.commit()

    primary_bytes = primary_db.read_bytes()
    linked_bytes = linked_db.read_bytes()
    extra_bytes = extra_db.read_bytes()
    linked_wal_size = Path(str(linked_db) + "-wal").stat().st_size
    status_before = {repo: _git_status(repo) for repo in (primary, linked, extra)}
    assert status_before == {primary: "", linked: "", extra: ""}
    destination = tmp_path / "central" / "orchestra.db"
    fleet_config = tmp_path / "fleet.repos"
    fleet_config.write_text(str(primary) + "\n", encoding="utf-8")

    discovered = migrate_cutover.discover_sources([], fleet_config)
    assert {path.parent.name for path in discovered} == {"primary", "linked-dev"}
    preview = migrate_cutover.build_plan(destination, [extra], fleet_config)
    assert not destination.exists()
    assert not destination.parent.exists()
    assert primary_db.read_bytes() == primary_bytes
    assert linked_db.read_bytes() == linked_bytes
    assert extra_db.read_bytes() == extra_bytes
    assert Path(str(linked_db) + "-wal").stat().st_size == linked_wal_size
    assert {repo: _git_status(repo) for repo in (primary, linked, extra)} == status_before
    assert preview["destination"]["kind"] == "absent"
    assert preview["workers_live"] == []
    assert "development queue" in preview["development_queue_note"]
    assert len(preview["sources"]) == 3
    maps = {source["worktree"]: source["planned_id_map"] for source in preview["sources"]}
    assert maps[str(primary.resolve())]["1"] != maps[str(linked.resolve())]["1"]

    result = migrate_cutover.cutover(destination, [extra], fleet_config)
    assert result["activated"] is True
    for database in (primary_db, linked_db, extra_db):
        assert not database.exists()
        assert not Path(str(database) + "-wal").exists()
        assert not Path(str(database) + ".inactive").exists()
    assert {repo: _git_status(repo) for repo in (primary, linked, extra)} == status_before
    assert state_db.read_bytes() == state_before

    conn = sqlite3.connect(destination)
    conn.row_factory = sqlite3.Row
    try:
        running = _task_row(conn, "Running parent")
        child = _task_row(conn, "Ready child")
        follow = _task_row(conn, "Done follow")
        draft = _task_row(conn, "Other draft")
        blocked = _task_row(conn, "Other blocked")
        wal_only = _task_row(conn, "Wal only")
        explicit = _task_row(conn, "Explicit done")
        assert running["status"] == "blocked"
        assert running["block_reason"] == "migration_recovery"
        assert running["stash_ref"] == "stash@{1}"
        assert running["branch"] == "feature-a"
        assert running["resume_next_step"] == "commit-make"
        assert child["status"] == "ready"
        assert child["parent_task_id"] == running["id"]
        assert follow["status"] == "done"
        assert running["follow_up_task_id"] == follow["id"]
        assert draft["status"] == "none"
        assert blocked["status"] == "blocked"
        assert blocked["block_reason"] == "needs-human"
        assert wal_only["status"] == "ready"
        assert wal_only["queue_id"] == draft["queue_id"]
        assert explicit["status"] == "done"
        queue_count = conn.execute("SELECT COUNT(*) FROM queues").fetchone()[0]
        assert len({running["id"], draft["id"], explicit["id"]}) == 3
        assert running["queue_id"] != draft["queue_id"]
        assert running["repository_id"] == child["repository_id"]
        assert running["repository_id"] == follow["repository_id"]
        assert draft["repository_id"] == running["repository_id"]
        assert explicit["repository_id"] != running["repository_id"]
        comment = conn.execute(
            "SELECT message FROM comments WHERE task_id = ? AND COALESCE(verb, '') != 'migrate-cutover'",
            (draft["id"],),
        ).fetchone()
        assert f"artifacts/task-{draft['id']}/other.txt" in comment["message"]
        assert "artifacts/task-1/" not in comment["message"]
        queue_note = conn.execute(
            "SELECT task_id, author FROM comments WHERE message = 'queue-level note'"
        ).fetchone()
        assert queue_note["task_id"] is None
        assert queue_note["author"] == "operator"
        owned = conn.execute(
            """SELECT c.message, m.queue_id, m.source_id
               FROM comments AS c
               JOIN migration_comment_map AS m ON m.new_comment_id = c.id
               WHERE c.task_id IS NULL
               ORDER BY c.message"""
        ).fetchall()
        assert [(row["message"], row["queue_id"]) for row in owned] == [
            ("linked queue note", draft["queue_id"]),
            ("queue-level note", running["queue_id"]),
        ]
        assert owned[0]["source_id"] != owned[1]["source_id"]
        assert owned[0]["queue_id"] != owned[1]["queue_id"]
        snapshot = conn.execute(
            "SELECT snapshot FROM task_agent_snapshots WHERE task_id = ?",
            (running["id"],),
        ).fetchone()
        assert f"artifacts/task-{running['id']}/note.txt" in snapshot["snapshot"]
        recovery = conn.execute(
            "SELECT message FROM comments WHERE task_id = ? AND verb = 'migrate-cutover'",
            (running["id"],),
        ).fetchone()
        assert "stash@{1}" in recovery["message"]
        assert str(primary.resolve()) in recovery["message"]
        runtime = conn.execute(
            "SELECT status, pid, current_task_id FROM orchestrator_runtime WHERE queue_id = ?",
            (running["queue_id"],),
        ).fetchone()
        assert runtime["status"] == "stopped"
        assert runtime["pid"] is None
        assert runtime["current_task_id"] == running["id"]
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'running'").fetchone()[0] == 0
    finally:
        conn.close()

    primary_artifact = (
        destination.parent / "queues" / running["queue_id"] / "artifacts" / f"task-{running['id']}" / "note.txt"
    )
    linked_artifact = (
        destination.parent / "queues" / draft["queue_id"] / "artifacts" / f"task-{draft['id']}" / "other.txt"
    )
    assert primary_artifact.read_text(encoding="utf-8") == "from-primary\n"
    assert linked_artifact.read_text(encoding="utf-8") == "from-linked\n"
    assert not (destination.parent / "queues" / running["queue_id"] / "artifacts" / f"task-{draft['id']}").exists()
    primary_repo, primary_queue = db._identity_dirs(primary)
    linked_repo, linked_queue = db._identity_dirs(linked)
    assert (primary_repo / db.REPOSITORY_ID_FILENAME).read_text(encoding="utf-8").strip() == running["repository_id"]
    assert (linked_repo / db.REPOSITORY_ID_FILENAME).read_text(encoding="utf-8").strip() == draft["repository_id"]
    assert (primary_queue / db.QUEUE_ID_FILENAME).read_text(encoding="utf-8").strip() == running["queue_id"]
    assert (linked_queue / db.QUEUE_ID_FILENAME).read_text(encoding="utf-8").strip() == draft["queue_id"]
    assert set(_listed_titles(primary, destination)) == {"Running parent", "Ready child", "Done follow"}
    assert set(_listed_titles(linked, destination)) == {"Other draft", "Other blocked", "Wal only"}
    check = sqlite3.connect(destination)
    try:
        assert check.execute("SELECT COUNT(*) FROM queues").fetchone()[0] == queue_count
    finally:
        check.close()

    first_ids = {
        "Running parent": running["id"],
        "Other draft": draft["id"],
        "Explicit done": explicit["id"],
    }
    again = migrate_cutover.cutover(destination, [extra], fleet_config)
    assert again["idempotent"] is True
    conn = sqlite3.connect(destination)
    try:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 7
        for title, task_id in first_ids.items():
            assert conn.execute("SELECT id FROM tasks WHERE title = ?", (title,)).fetchone()[0] == task_id
    finally:
        conn.close()

    later = tmp_path / "later-source"
    _init_repo(later)
    later_db = _legacy_db(later)
    with _connect(later_db) as conn:
        conn.execute(
            """INSERT INTO tasks (title, status, next_step, kind, max_review_rounds)
               VALUES ('Later task', 'ready', 'commit-make', 'commit', 3)"""
        )
        conn.commit()
    migrate_cutover.cutover(destination, [extra, later], fleet_config)
    conn = sqlite3.connect(destination)
    try:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 8
        for title, task_id in first_ids.items():
            assert conn.execute("SELECT id FROM tasks WHERE title = ?", (title,)).fetchone()[0] == task_id
        later_row = conn.execute("SELECT id, status FROM tasks WHERE title = 'Later task'").fetchone()
        assert later_row[1] == "ready"
        assert later_row[0] not in first_ids.values()
    finally:
        conn.close()
    assert state_db.read_bytes() == state_before


def test_retry_after_failure_is_idempotent_and_rollback_restores(tmp_path):
    tracked = tmp_path / "tracked"
    _init_repo(tracked)
    tracked_db = _legacy_db(tracked)
    _git(tracked, "add", "-f", "kanban-orchestra.db")
    _git(tracked, "commit", "-m", "track the legacy database")
    tracked_destination = tmp_path / "tracked-central" / "orchestra.db"
    empty_fleet = tmp_path / "empty-fleet.repos"
    empty_fleet.write_text("", encoding="utf-8")
    with pytest.raises(migrate_cutover.CutoverError, match="Tracked legacy"):
        migrate_cutover.cutover(tracked_destination, [tracked], empty_fleet)
    assert tracked_db.is_file()
    assert not tracked_destination.exists()
    assert _git_status(tracked) == ""

    source = tmp_path / "source"
    _init_repo(source)
    database = _legacy_db(source)
    with _connect(database) as conn:
        conn.execute(
            """INSERT INTO tasks (title, status, next_step, kind, max_review_rounds, parent_task_id)
               VALUES ('Keep', 'done', 'none', 'commit', 3, NULL)"""
        )
        conn.commit()
    original = database.read_bytes()
    destination = tmp_path / "central" / "orchestra.db"
    fleet_config = empty_fleet

    with pytest.raises(migrate_cutover.CutoverError, match="before activation"):
        migrate_cutover.cutover(destination, [source], fleet_config, fail_after="before-activation")
    assert not destination.exists()
    assert not (destination.parent / "migration-rollback.json").exists()
    assert database.read_bytes() == original

    with pytest.raises(migrate_cutover.ActivationCrash):
        migrate_cutover.cutover(destination, [source], fleet_config, fail_after="after-replace")
    assert destination.is_file()
    with pytest.raises(RuntimeError, match="Unfinished migration activation"):
        db.connect(str(destination))
    raw = sqlite3.connect(destination)
    try:
        with pytest.raises(RuntimeError, match="Unfinished migration activation"):
            db.claim_ready_task(raw, 1)
    finally:
        raw.close()

    with pytest.raises(migrate_cutover.ActivationCrash):
        migrate_cutover.cutover(destination, [source], fleet_config, fail_after="after-legacy-unlink")
    assert not database.exists()
    crashed_manifest = json.loads((destination.parent / "migration-rollback.json").read_text(encoding="utf-8"))
    crashed_source = crashed_manifest["sources"][0]["path"]
    assert crashed_source not in crashed_manifest["removed_legacy_files"]
    assert not Path(crashed_source).exists()
    assert Path(crashed_source + "-wal").is_file()
    with pytest.raises(RuntimeError, match="Unfinished migration activation"):
        db.connect(str(destination))

    restored_partial = migrate_cutover.rollback(destination)
    assert restored_partial["rolled_back"] is True
    assert not destination.exists()
    with _connect(Path(crashed_source)) as restored_source:
        assert restored_source.execute("SELECT title FROM tasks").fetchone()[0] == "Keep"
    assert not Path(str(crashed_source) + "-wal").exists()

    recovered = migrate_cutover.cutover(destination, [source], fleet_config)
    assert recovered["activated"] is True
    db.connect(str(destination)).close()

    activated = migrate_cutover.cutover(destination, [source], fleet_config)
    assert activated["activated"] is True
    assert activated["idempotent"] is True
    conn = sqlite3.connect(destination)
    try:
        first_id = conn.execute("SELECT id FROM tasks WHERE title = 'Keep'").fetchone()[0]
        queue_id = conn.execute("SELECT queue_id FROM tasks WHERE title = 'Keep'").fetchone()[0]
    finally:
        conn.close()
    assert not database.exists()
    assert not (source / "kanban-orchestra.db.inactive").exists()
    success_manifest = json.loads((destination.parent / "migration-rollback.json").read_text(encoding="utf-8"))
    assert str(database.resolve()) in success_manifest["removed_legacy_files"]

    runtime = destination.parent / "queues" / queue_id
    runtime.mkdir(parents=True, exist_ok=True)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    dashboard = runtime / "dashboard.json"
    dashboard.write_text(
        json.dumps({
            "role": "dashboard",
            "pid": 999999,
            "host": "127.0.0.1",
            "port": port,
            "url": f"http://127.0.0.1:{port}",
        }),
        encoding="utf-8",
    )
    try:
        with pytest.raises(migrate_cutover.WorkersLive, match="central dashboard"):
            migrate_cutover.rollback(destination)
        assert destination.is_file()
        assert not database.exists()
    finally:
        sock.close()
        dashboard.unlink()

    rolled = migrate_cutover.rollback(destination)
    assert rolled["rolled_back"] is True
    assert not destination.exists()
    with _connect(database) as restored_source:
        assert restored_source.execute("SELECT title FROM tasks").fetchone()[0] == "Keep"
    with pytest.raises(migrate_cutover.CutoverError, match="already been rolled back"):
        migrate_cutover.rollback(destination)

    restored = migrate_cutover.cutover(destination, [source], fleet_config)
    assert restored["activated"] is True
    conn = sqlite3.connect(destination)
    try:
        assert conn.execute("SELECT id FROM tasks WHERE title = 'Keep'").fetchone()[0] == first_id
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    finally:
        conn.close()

    absent = migrate_cutover.cutover(destination, [source], fleet_config)
    assert absent["idempotent"] is True
    assert not database.exists()

    reappeared = _legacy_db(source)
    with _connect(reappeared) as conn:
        conn.execute(
            """INSERT INTO tasks (id, title, status, next_step, kind, max_review_rounds)
               VALUES (1, 'Keep changed', 'done', 'none', 'commit', 3)"""
        )
        conn.execute(
            "INSERT INTO comments (task_id, message, kind) VALUES (1, 'new history', 'comment')"
        )
        conn.commit()
    later = tmp_path / "later"
    _init_repo(later)
    later_db = _legacy_db(later)
    with _connect(later_db) as conn:
        conn.execute(
            """INSERT INTO tasks (title, status, next_step, kind, max_review_rounds)
               VALUES ('Later', 'ready', 'commit-make', 'commit', 3)"""
        )
        conn.commit()
    manifest_path = destination.parent / "migration-rollback.json"
    manifest_before = manifest_path.read_bytes()
    attempts = destination.parent / "migration-attempts"
    attempts_before = sorted(path.name for path in attempts.iterdir())
    with pytest.raises(migrate_cutover.CutoverError, match="differs from its recorded cutover snapshot"):
        migrate_cutover.cutover(destination, [source, later], fleet_config)
    assert reappeared.is_file()
    assert later_db.is_file()
    assert manifest_path.read_bytes() == manifest_before
    assert sorted(path.name for path in attempts.iterdir()) == attempts_before
    with _connect(reappeared) as conn:
        assert conn.execute("SELECT title FROM tasks").fetchone()[0] == "Keep changed"
        assert conn.execute("SELECT message FROM comments").fetchone()[0] == "new history"
    with _connect(later_db) as conn:
        assert conn.execute("SELECT title FROM tasks").fetchone()[0] == "Later"
    conn = sqlite3.connect(destination)
    try:
        assert conn.execute("SELECT id FROM tasks WHERE title = 'Keep'").fetchone()[0] == first_id
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM comments WHERE message = 'new history'"
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE title = 'Later'").fetchone()[0] == 0
    finally:
        conn.close()

    reappeared.unlink()
    clean = migrate_cutover.cutover(destination, [source], fleet_config)
    assert clean["idempotent"] is True
    assert not reappeared.exists()
    assert later_db.is_file()
    with _connect(later_db) as conn:
        assert conn.execute("SELECT title FROM tasks").fetchone()[0] == "Later"
    conn = sqlite3.connect(destination)
    try:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute("SELECT id FROM tasks WHERE title = 'Keep'").fetchone()[0] == first_id
        recorded_backup = Path(conn.execute(
            "SELECT backup_path FROM migration_sources"
        ).fetchone()[0])
    finally:
        conn.close()
    assert recorded_backup.is_file()
    database.write_bytes(recorded_backup.read_bytes())
    repo_dir, queue_dir = db._identity_dirs(source)
    source_identity_files = [
        repo_dir / db.REPOSITORY_ID_FILENAME,
        queue_dir / db.QUEUE_ID_FILENAME,
    ]
    source_identity_before = [
        path.read_text(encoding="utf-8") for path in source_identity_files
    ]

    mixed = migrate_cutover.cutover(destination, [source, later], fleet_config)
    assert mixed["activated"] is True
    assert mixed["idempotent"] is False
    assert [item["path"] for item in mixed["imported"]] == [str(later_db.resolve())]
    assert not database.exists()
    assert not later_db.exists()
    conn = sqlite3.connect(destination)
    try:
        assert conn.execute("SELECT id FROM tasks WHERE title = 'Keep'").fetchone()[0] == first_id
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE title = 'Keep'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE title = 'Later'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
        backups = [
            row[0] for row in conn.execute("SELECT backup_path FROM migration_sources")
        ]
        assert str(recorded_backup) in backups
        assert len(backups) == 2
    finally:
        conn.close()
    later_repo_dir, later_queue_dir = db._identity_dirs(later)
    later_identity_files = [
        later_repo_dir / db.REPOSITORY_ID_FILENAME,
        later_queue_dir / db.QUEUE_ID_FILENAME,
    ]
    assert all(path.is_file() for path in later_identity_files)
    manifest = json.loads((destination.parent / "migration-rollback.json").read_text(encoding="utf-8"))
    by_path = {item["path"]: item for item in manifest["sources"]}
    assert by_path[str(database.resolve())]["backup_path"] == str(recorded_backup)
    assert Path(by_path[str(later_db.resolve())]["backup_path"]).is_file()
    assert str(database.resolve()) in manifest["removed_legacy_files"]
    assert str(later_db.resolve()) in manifest["removed_legacy_files"]
    assert manifest["activated"] is True

    rolled_mixed = migrate_cutover.rollback(destination)
    assert rolled_mixed["rolled_back"] is True
    assert database.read_bytes() == recorded_backup.read_bytes()
    with _connect(database) as restored_source:
        assert restored_source.execute("SELECT title FROM tasks").fetchone()[0] == "Keep"
    with _connect(later_db) as restored_later:
        assert restored_later.execute("SELECT title FROM tasks").fetchone()[0] == "Later"
    conn = sqlite3.connect(destination)
    try:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute("SELECT id FROM tasks WHERE title = 'Keep'").fetchone()[0] == first_id
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE title = 'Later'").fetchone()[0] == 0
    finally:
        conn.close()
    assert [
        path.read_text(encoding="utf-8") for path in source_identity_files
    ] == source_identity_before
    assert all(not path.exists() for path in later_identity_files)


def test_cutover_refuses_live_worker_or_dashboard(tmp_path, monkeypatch):
    # GNU ps may truncate long command paths to this width unless -ww is used.
    monkeypatch.setenv("COLUMNS", "80")
    source = tmp_path / "source"
    _init_repo(source)
    _legacy_db(source)
    destination = tmp_path / "central" / "orchestra.db"
    fleet_config = tmp_path / "empty-fleet.repos"
    fleet_config.write_text("", encoding="utf-8")
    script = source / "orchestrator.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    process = subprocess.Popen([sys.executable, str(script)], cwd=source)
    try:
        time.sleep(0.3)
        with pytest.raises(migrate_cutover.WorkersLive):
            migrate_cutover.cutover(destination, [source], fleet_config)
        assert not destination.exists()
    finally:
        process.terminate()
        process.wait(timeout=5)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    dashboard = source / ".kanban-orchestra" / "dashboard.json"
    dashboard.parent.mkdir(parents=True, exist_ok=True)
    dashboard.write_text(
        json.dumps({
            "role": "dashboard",
            "pid": 999999,
            "host": "127.0.0.1",
            "port": port,
            "url": f"http://127.0.0.1:{port}",
        }),
        encoding="utf-8",
    )
    try:
        with pytest.raises(migrate_cutover.WorkersLive):
            migrate_cutover.cutover(destination, [source], fleet_config)
        assert not destination.exists()
    finally:
        sock.close()


@pytest.mark.parametrize("existing_destination", [False, True])
def test_rollback_refuses_work_added_after_activation(tmp_path, existing_destination):
    source = tmp_path / "source"
    _init_repo(source)
    legacy = _legacy_db(source)
    destination = tmp_path / "central" / "orchestra.db"
    if existing_destination:
        db.connect(str(destination)).close()
    fleet_config = tmp_path / "empty-fleet.repos"
    fleet_config.write_text("", encoding="utf-8")
    result = migrate_cutover.cutover(destination, [source], fleet_config)
    queue_id = result["imported"][0]["queue_id"]
    conn = db.connect(str(destination), queue_id=queue_id)
    try:
        task_id = db.add_task(conn, "After cutover")
    finally:
        conn.close()
    artifact = destination.parent / "queues" / queue_id / "new-work.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("keep", encoding="utf-8")
    before = migrate_cutover._durable_snapshot(destination)
    with pytest.raises(migrate_cutover.CutoverError, match="changed after cutover"):
        migrate_cutover.rollback(destination)
    assert migrate_cutover._durable_snapshot(destination) == before
    assert artifact.read_text(encoding="utf-8") == "keep"
    assert not legacy.exists()
    with _connect(destination) as conn:
        assert conn.execute("SELECT title FROM tasks WHERE id = ?", (task_id,)).fetchone()[0] == "After cutover"


def test_retire_queue_keeps_ids_and_does_not_import(tmp_path):
    database = tmp_path / "orchestra.db"
    conn = db.connect(str(database))
    try:
        ready_id = db.add_task(conn, "Still open", status="ready", branch="feature")
        done_id = db.add_task(conn, "Finished", status="done", branch="feature")
        queue_id = db.connection_queue_id(conn)
    finally:
        conn.close()
    script = Path(__file__).resolve().parent / "task.py"
    env = os.environ.copy()
    env["KANBAN_DB"] = str(database)
    env["KANBAN_NONINTERACTIVE"] = "1"

    refused = subprocess.run(
        [sys.executable, str(script), "retire-queue"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert refused.returncode != 0
    assert "abandon-unfinished" in refused.stderr
    assert "not imported" in refused.stderr

    # Even a missing checkout must not hide a live central dashboard.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    dashboard = database.parent / "queues" / queue_id / "dashboard.json"
    dashboard.parent.mkdir(parents=True, exist_ok=True)
    dashboard.write_text(json.dumps({
        "role": "dashboard", "pid": 999999,
        "host": "127.0.0.1", "port": sock.getsockname()[1],
    }), encoding="utf-8")
    try:
        live = subprocess.run(
            [sys.executable, str(script), "retire-queue", "--abandon-unfinished"],
            cwd=tmp_path, env=env, capture_output=True, text=True,
        )
        assert live.returncode != 0
        assert "worker or dashboard is live" in live.stderr
    finally:
        sock.close()
        dashboard.unlink()

    retired = subprocess.run(
        [sys.executable, str(script), "retire-queue", "--abandon-unfinished"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert retired.returncode == 0, retired.stderr
    payload = json.loads(retired.stdout)
    assert payload["queue_id"] == queue_id
    assert payload["state"] == "retired"
    assert payload["task_ids"] == [ready_id, done_id]
    assert payload["abandoned_unfinished"] == [ready_id]

    check = sqlite3.connect(database)
    check.row_factory = sqlite3.Row
    try:
        rows = check.execute("SELECT id, status, queue_id FROM tasks ORDER BY id").fetchall()
        assert [(row["id"], row["status"], row["queue_id"]) for row in rows] == [
            (ready_id, "ready", queue_id),
            (done_id, "done", queue_id),
        ]
        assert check.execute("SELECT state FROM queues WHERE id = ?", (queue_id,)).fetchone()["state"] == "retired"
        assert check.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
    finally:
        check.close()


def test_preexisting_queue_directory_survives_activation_conflict(tmp_path):
    source = tmp_path / "source"
    _init_repo(source)
    database = _legacy_db(source)
    with _connect(database) as conn:
        conn.execute(
            """INSERT INTO tasks (title, status, next_step, kind, max_review_rounds)
               VALUES ('Keep', 'ready', 'commit-make', 'commit', 3)"""
        )
        conn.commit()
    destination = tmp_path / "central" / "orchestra.db"
    queue_id = migrate_cutover.queue_identity(database)
    sentinel = destination.parent / "queues" / queue_id / "sentinel.txt"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("keep\n", encoding="utf-8")
    fleet_config = tmp_path / "empty-fleet.repos"
    fleet_config.write_text("", encoding="utf-8")

    with pytest.raises(migrate_cutover.CutoverError, match="already exists"):
        migrate_cutover.cutover(destination, [source], fleet_config)

    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert sentinel.parent.is_dir()
    assert not destination.exists()
    assert database.is_file()
    with _connect(database) as conn:
        assert conn.execute("SELECT title FROM tasks").fetchone()[0] == "Keep"
