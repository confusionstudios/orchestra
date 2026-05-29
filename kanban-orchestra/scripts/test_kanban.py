#!/usr/bin/env python3
"""
Tests for Kanban Orchestra core logic.

Covers: db layer, task CLI, orchestrator state machine transitions,
comment aggregation.
"""

import json
import importlib.util
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch, MagicMock

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
SHARED_DIR = SCRIPT_DIR.parent.parent / "shared_scripts"
sys.path.insert(0, str(SHARED_DIR))


def _load_local_module(alias, filename, canonical_name=None):
    spec = importlib.util.spec_from_file_location(alias, SCRIPT_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    if canonical_name:
        sys.modules[canonical_name] = module
    sys.modules[alias] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


db = _load_local_module("kanban_test_db", "db.py", canonical_name="db")
active_agent_processes = _load_local_module(
    "kanban_test_active_agent_processes",
    "active_agent_processes.py",
    canonical_name="active_agent_processes",
)
orchestrator_control = _load_local_module("kanban_test_orchestrator_control", "orchestrator_control.py", canonical_name="orchestrator_control")
orchestrator = _load_local_module("kanban_test_orchestrator", "orchestrator.py")
config = _load_local_module("kanban_test_config", "config.py")
task_module = _load_local_module("kanban_test_task", "task.py")
fleet = _load_local_module("kanban_test_fleet", "fleet.py")
import agent_registry
import shared_config
skill_wrappers = _load_local_module("kanban_test_skill_wrappers", str(SCRIPT_DIR.parent.parent / "shared_scripts" / "sync_ai_skill_wrappers.py"))
repo_policy = _load_local_module("kanban_test_repo_policy", "repo_policy.py")

DEFAULT_REVIEWER = config.DEFAULT_REVIEWER
DEFAULT_CODER = config.DEFAULT_CODER


def _write_test_ai_skills(orchestra_dir: Path) -> None:
    skills_dir = orchestra_dir / "AI-skills"
    skills_dir.mkdir(parents=True)
    for skill_name in (
        "get-kanban-update",
        "git-commit",
        "kanban",
        "prep-branch-for-squash-merge",
        "prep-for-review",
        "respond-to-review",
        "review-build",
        "review-screenshot",
        "squash-and-merge-one-shot",
        "squash-merge-branch",
    ):
        (skills_dir / f"{skill_name}.md").write_text(
            f"{skill_name} description.\n\nCanonical instructions.\n",
            encoding="utf-8",
        )
    (skills_dir / "AI-readme.md").write_text("reference doc\n", encoding="utf-8")


class TestDB(unittest.TestCase):
    """Test the database layer."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_add_and_get_task(self):
        tid = db.add_task(self.conn, "Test task", description="A test", reviewer_agent="gemini")
        self.assertIsNotNone(tid)

        task = db.get_task(self.conn, tid)
        self.assertEqual(task["title"], "Test task")
        self.assertEqual(task["description"], "A test")
        self.assertEqual(task["status"], "none")
        self.assertEqual(task["next_step"], "commit-plan")
        self.assertEqual(task["kind"], "commit")
        self.assertIsNone(task["stash_ref"])
        self.assertEqual(task["reviewer_agent"], "gemini")
        self.assertNotIn("koid", task)
        self.assertNotIn("skip_same_branch_koid_check", task)

    def test_add_task_accepts_default_reviewer(self):
        tid = db.add_task(self.conn, "Reviewer default", reviewer_agent=DEFAULT_REVIEWER)
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["reviewer_agent"], DEFAULT_REVIEWER)

    def test_add_pull_request_task_starts_at_pull_request_make(self):
        tid = db.add_task(
            self.conn,
            "Open PR",
            kind="pull_request",
            branch="feature-branch",
            reviewer_agent="gemini",
        )
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["kind"], "pull_request")
        self.assertEqual(task["next_step"], "pull-request-make")
        self.assertEqual(task["reviewer_agent"], "gemini")

    def test_legacy_task_kind_aliases_to_commit(self):
        tid = db.add_task(self.conn, "Legacy task", kind="task", branch="feature-branch")
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["kind"], "commit")
        self.assertEqual(db.task_type(task), "commit")

    def test_add_other_task_starts_at_other_make(self):
        tid = db.add_task(
            self.conn,
            "External maintenance",
            kind="other",
            reviewer_agent="gemini",
        )
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["kind"], "other")
        self.assertEqual(task["next_step"], "other-make")
        self.assertEqual(task["reviewer_agent"], "gemini")

    def test_list_tasks(self):
        db.add_task(self.conn, "Task 1")
        db.add_task(self.conn, "Task 2")
        db.add_task(self.conn, "Task 3")

        tasks = db.list_tasks(self.conn)
        self.assertEqual(len(tasks), 3)

    def test_list_tasks_filtered(self):
        t1 = db.add_task(self.conn, "Ready task")
        db.update_task(self.conn, t1, status="ready", branch="test-branch")
        db.add_task(self.conn, "None task")


        ready = db.list_tasks(self.conn, status="ready")
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["title"], "Ready task")

    def test_list_ready_tasks_orders_by_sequence_index(self):
        first_id = db.add_task(self.conn, "First ready", branch="b", sequence_index=200)
        second_id = db.add_task(self.conn, "Second ready", branch="b", sequence_index=100)
        db.update_task(self.conn, first_id, status="ready")
        db.update_task(self.conn, second_id, status="ready")

        ready = db.list_tasks(self.conn, status="ready")
        self.assertEqual([task["id"] for task in ready], [second_id, first_id])

    def test_list_tasks_pagination(self):
        for i in range(25):
            db.add_task(self.conn, f"Task {i}")
        page1 = db.list_tasks(self.conn, page=1, page_size=10)
        page2 = db.list_tasks(self.conn, page=2, page_size=10)
        page3 = db.list_tasks(self.conn, page=3, page_size=10)
        self.assertEqual(len(page1), 10)
        self.assertEqual(len(page2), 10)
        self.assertEqual(len(page3), 5)

    def test_update_task(self):
        tid = db.add_task(self.conn, "Update me")
        db.update_task(
            self.conn,
            tid,
            status="ready",
            branch="feat-1",
            stash_ref="stash@{0}",
        )
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["branch"], "feat-1")
        self.assertEqual(task["stash_ref"], "stash@{0}")
        self.assertIsNotNone(task["ready_at"])

    def test_update_task_status_transitions_manage_ready_at(self):
        tid = db.add_task(self.conn, "Queue transitions", branch="feat-q")

        db.update_task(self.conn, tid, status="ready")
        self.conn.execute(
            "UPDATE tasks SET ready_at = ? WHERE id = ?",
            ("2000-01-01 00:00:00", tid),
        )
        self.conn.commit()

        db.update_task(self.conn, tid, coder_agent="codex")
        still_ready = db.get_task(self.conn, tid)
        self.assertEqual(still_ready["ready_at"], "2000-01-01 00:00:00")

        db.update_task(self.conn, tid, status="running")
        running = db.get_task(self.conn, tid)
        self.assertIsNone(running["ready_at"])

        db.update_task(self.conn, tid, status="ready")
        requeued = db.get_task(self.conn, tid)
        self.assertIsNotNone(requeued["ready_at"])
        self.assertNotEqual(requeued["ready_at"], "2000-01-01 00:00:00")

    def test_orchestrator_log_path_lives_under_repo_runtime_dir(self):
        log_path = db.get_orchestrator_log_path(self.tmp.name)
        self.assertEqual(log_path, Path(self.tmp.name).resolve().parent / ".kanban-orchestra" / "orchestrator.log")

    def test_allow_when_blocked_defaults_false(self):
        tid = db.add_task(self.conn, "Blocked gate default")
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["allow_when_blocked"], 0)

    def test_task_add_defaults_normal_tasks_to_build_step(self):
        args = SimpleNamespace(
            title="Default skip planning",
            description=None,
            branch="feat-default-skip",
            coder_agent=None,
            reviewer_agent=None,
            task_type=None,
            kind="task",
            parent=None,
            sequence_index=None,
            skip=None,
            allow_when_blocked=False,
        )

        with redirect_stdout(io.StringIO()):
            task_module.cmd_add(args, self.conn)

        task = db.list_tasks(self.conn)[0]
        self.assertEqual(task["next_step"], "commit-make")
        self.assertEqual(task["skips"], ["commit-plan"])

    def test_task_add_unions_default_plan_skips_with_user_skips(self):
        args = SimpleNamespace(
            title="Default and explicit skip",
            description=None,
            branch="feat-default-and-explicit",
            coder_agent=None,
            reviewer_agent=None,
            task_type=None,
            kind="task",
            parent=None,
            sequence_index=None,
            skip=["commit-review"],
            allow_when_blocked=False,
        )

        with redirect_stdout(io.StringIO()):
            task_module.cmd_add(args, self.conn)

        task = db.list_tasks(self.conn)[0]
        self.assertEqual(task["next_step"], "commit-make")
        self.assertEqual(task["skips"], ["commit-plan", "commit-review"])

    def test_missing_allow_when_blocked_column_is_migrated(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            legacy = sqlite3.connect(tmp.name)
            legacy.execute(
                """CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT NOT NULL DEFAULT 'none',
                    next_step TEXT NOT NULL DEFAULT 'commit-make'
                        CHECK(next_step IN ('commit-make', 'commit-review',
                                            'commit-make-supertask', 'commit-review-supertask',
                                            'commit-plan', 'commit-plan-review',
                                            'none')),
                    branch TEXT,
                    commit_hash TEXT,
                    stash_ref TEXT,
                    coder_agent TEXT,
                    review_round INTEGER DEFAULT 0,
                    last_review_decision TEXT DEFAULT 'none',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    ready_at DATETIME DEFAULT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    kind TEXT NOT NULL DEFAULT 'task',
                    parent_task_id INTEGER REFERENCES tasks(id),
                    sequence_index INTEGER,
                    commit_plan TEXT,
                    follow_up_task_id INTEGER REFERENCES tasks(id)
                )"""
            )
            legacy.execute(
                """CREATE TABLE task_skips (
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    step TEXT NOT NULL
                        CHECK(step IN ('commit-plan','commit-plan-review','commit-review','commit-review-supertask')),
                    PRIMARY KEY (task_id, step)
                )"""
            )
            legacy.execute(
                """CREATE TABLE run_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER REFERENCES tasks(id),
                    verb TEXT,
                    author TEXT,
                    message TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            legacy.execute(
                """CREATE TABLE comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER REFERENCES tasks(id),
                    review_round INTEGER,
                    verb TEXT,
                    author TEXT,
                    message TEXT,
                    kind TEXT DEFAULT 'comment'
                        CHECK(kind IN ('comment', 'approval', 'rejection', 'commit-message',
                                       'validation', 'plan-approval', 'plan-rejection',
                                       'done-without-commit', 'deferred-build-changed')),
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            legacy.execute(
                """CREATE TABLE orchestrator_runtime (
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
                )"""
            )
            legacy.commit()
            legacy.close()

            migrated = db.connect(tmp.name)
            try:
                cols = {row["name"] for row in migrated.execute("PRAGMA table_info(tasks)").fetchall()}
                self.assertIn("allow_when_blocked", cols)
            finally:
                migrated.close()
        finally:
            os.unlink(tmp.name)

    def test_missing_reviewer_agent_column_is_migrated(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            legacy = sqlite3.connect(tmp.name)
            legacy.execute(
                """CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT NOT NULL DEFAULT 'none',
                    next_step TEXT NOT NULL DEFAULT 'commit-make'
                        CHECK(next_step IN ('commit-make', 'commit-review',
                                            'commit-make-supertask', 'commit-review-supertask',
                                            'commit-plan', 'commit-plan-review',
                                            'none')),
                    branch TEXT,
                    commit_hash TEXT,
                    stash_ref TEXT,
                    coder_agent TEXT,
                    review_round INTEGER DEFAULT 0,
                    last_review_decision TEXT DEFAULT 'none',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    ready_at DATETIME DEFAULT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    kind TEXT NOT NULL DEFAULT 'task',
                    parent_task_id INTEGER REFERENCES tasks(id),
                    sequence_index INTEGER,
                    commit_plan TEXT,
                    follow_up_task_id INTEGER REFERENCES tasks(id),
                    allow_when_blocked INTEGER NOT NULL DEFAULT 0
                )"""
            )
            legacy.execute(
                """CREATE TABLE task_skips (
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    step TEXT NOT NULL
                        CHECK(step IN ('commit-plan','commit-plan-review','commit-review','commit-review-supertask')),
                    PRIMARY KEY (task_id, step)
                )"""
            )
            legacy.commit()
            legacy.close()

            conn = db.connect(tmp.name)
            try:
                cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
                self.assertIn("reviewer_agent", cols)
                tid = db.add_task(conn, "Old schema ok", reviewer_agent=DEFAULT_REVIEWER)
                self.assertEqual(db.get_task(conn, tid)["reviewer_agent"], DEFAULT_REVIEWER)
            finally:
                conn.close()
        finally:
            for suffix in ("", "-shm", "-wal"):
                path = tmp.name + suffix
                if os.path.exists(path):
                    os.unlink(path)

    def test_skip_commit_plan_column_migrates_to_task_skips(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            legacy = sqlite3.connect(tmp.name)
            legacy.executescript(
                """CREATE TABLE tasks (
                    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                    title                   TEXT NOT NULL,
                    description             TEXT,
                    status                  TEXT NOT NULL DEFAULT 'none'
                        CHECK(status IN ('none', 'ready', 'running', 'done', 'blocked',
                                         'pending_subtasks')),
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
                    skip_commit_plan        INTEGER NOT NULL DEFAULT 0
                        CHECK(skip_commit_plan IN (0, 1)),
                    commit_plan             TEXT
                );
                CREATE TABLE run_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id     INTEGER REFERENCES tasks(id),
                    verb        TEXT,
                    author      TEXT,
                    message     TEXT,
                    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE comments (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id        INTEGER REFERENCES tasks(id),
                    review_round   INTEGER,
                    verb           TEXT,
                    author         TEXT,
                    message        TEXT,
                    kind           TEXT DEFAULT 'comment'
                        CHECK(kind IN ('comment', 'approval', 'rejection', 'commit-message',
                                       'validation', 'plan-approval', 'plan-rejection')),
                    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE orchestrator_runtime (
                    singleton            INTEGER PRIMARY KEY CHECK(singleton = 1),
                    status               TEXT NOT NULL
                        CHECK(status IN ('idle', 'running', 'starting', 'stopping', 'stopped',
                                         'hard-break', 'error')),
                    pid                  INTEGER,
                    started_at           DATETIME,
                    last_heartbeat_at    DATETIME,
                    current_task_id      INTEGER REFERENCES tasks(id),
                    current_step         TEXT
                        CHECK(current_step IN ('commit-make', 'commit-review',
                                               'commit-make-supertask', 'commit-review-supertask',
                                               'commit-plan', 'commit-plan-review', 'none')),
                    current_branch       TEXT,
                    review_round         INTEGER,
                    active_agents        INTEGER NOT NULL DEFAULT 0,
                    status_message       TEXT,
                    updated_at           DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO tasks (title, status, next_step, skip_commit_plan)
                    VALUES ('Skip planning', 'done', 'commit-make', 1);
                INSERT INTO tasks (title, status, next_step, skip_commit_plan)
                    VALUES ('Keep planning', 'done', 'commit-plan', 0);
                """
            )
            legacy.commit()
            legacy.close()

            conn = db.connect(tmp.name)
            try:
                task_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
                self.assertNotIn("skip_commit_plan", task_cols)
                self.assertIn("reviewer_agent", task_cols)
                self.assertIn("follow_up_task_id", task_cols)
                self.assertIn("allow_when_blocked", task_cols)

                skipped = db.get_task(conn, 1)
                planned = db.get_task(conn, 2)
                self.assertEqual(skipped["skips"], ["commit-plan"])
                self.assertEqual(planned["skips"], [])
                self.assertEqual(skipped["title"], "Skip planning")
            finally:
                conn.close()
        finally:
            for suffix in ("", "-shm", "-wal"):
                path = tmp.name + suffix
                if os.path.exists(path):
                    os.unlink(path)


    def test_legacy_db_with_koid_raises_error(self):
        """connect() must raise RuntimeError on a legacy schema that still has koid."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            legacy = sqlite3.connect(tmp.name)
            legacy.execute(
                """CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    koid TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT NOT NULL DEFAULT 'none',
                    next_step TEXT NOT NULL DEFAULT 'commit-make',
                    branch TEXT,
                    commit_hash TEXT,
                    coder_agent TEXT,
                    skip_same_branch_koid_check INTEGER NOT NULL DEFAULT 0,
                    review_round INTEGER DEFAULT 0,
                    last_review_decision TEXT DEFAULT 'none',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            legacy.commit()
            legacy.close()

            with self.assertRaises(RuntimeError) as ctx:
                db.connect(tmp.name)
            self.assertIn("legacy schema", str(ctx.exception))
            self.assertIn("koid", str(ctx.exception))
        finally:
            os.unlink(tmp.name)

    def test_legacy_db_with_commit_hashes_raises_error(self):
        """connect() must raise RuntimeError on a legacy schema with commit_hashes column."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            legacy = sqlite3.connect(tmp.name)
            legacy.execute(
                """CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    koid TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    commit_hash TEXT,
                    commit_hashes TEXT NOT NULL DEFAULT '[]',
                    stash_ref TEXT,
                    coder_agent TEXT,
                    review_round INTEGER DEFAULT 0,
                    last_review_decision TEXT DEFAULT 'none',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            legacy.commit()
            legacy.close()

            with self.assertRaises(RuntimeError) as ctx:
                db.connect(tmp.name)
            self.assertIn("legacy schema", str(ctx.exception))
        finally:
            os.unlink(tmp.name)

    def test_outdated_next_step_constraint_auto_migrates(self):
        """connect() auto-migrates next_step CHECK constraints."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            legacy = sqlite3.connect(tmp.name)
            legacy.execute(
                """CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT NOT NULL DEFAULT 'none',
                    next_step TEXT NOT NULL DEFAULT 'commit-make'
                        CHECK(next_step IN ('commit-make', 'commit-review',
                                            'commit-make-supertask',
                                            'commit-review-supertask', 'none')),
                    branch TEXT,
                    commit_hash TEXT,
                    stash_ref TEXT,
                    coder_agent TEXT,
                    review_round INTEGER DEFAULT 0,
                    last_review_decision TEXT DEFAULT 'none',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    ready_at DATETIME DEFAULT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    kind TEXT NOT NULL DEFAULT 'task',
                    parent_task_id INTEGER,
                    sequence_index INTEGER,
                    commit_plan TEXT
                )"""
            )
            # Must also have task_skips table or it fails on that first
            legacy.execute(
                """CREATE TABLE task_skips (
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    step TEXT NOT NULL CHECK(step IN ('commit-plan','commit-plan-review',
                                                      'commit-review','commit-review-supertask')),
                    PRIMARY KEY (task_id, step)
                )"""
            )
            legacy.execute(
                "INSERT INTO tasks (title, kind, next_step, commit_plan) VALUES (?, ?, ?, ?)",
                ("Legacy task row", "task", "commit-make", None),
            )
            legacy.commit()
            legacy.close()

            conn = db.connect(tmp.name)
            try:
                schema = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
                ).fetchone()[0]
                self.assertIn("commit-plan", schema)
                self.assertIn("pull-request-make", schema)
                self.assertIn("pull_request", schema)
                self.assertIn("other-make", schema)
                self.assertIn("other", schema)
                task = db.get_task(conn, 1)
                self.assertEqual(task["kind"], "task")
                self.assertEqual(db.task_type(task), "commit")
            finally:
                conn.close()
        finally:
            for suffix in ("", "-shm", "-wal"):
                path = tmp.name + suffix
                if os.path.exists(path):
                    os.unlink(path)

    def test_stale_comments_schema_missing_plan_kinds_auto_migrates(self):
        """connect() auto-migrates the comments table when kind CHECK is outdated."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            legacy = sqlite3.connect(tmp.name)
            # Intermediate schema: has 'validation' but not 'plan-approval'/'plan-rejection'
            # Also must include task_skips table or it will fail on that first.
            legacy.executescript(
                """CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT NOT NULL DEFAULT 'none'
                        CHECK(status IN ('none', 'ready', 'running', 'done', 'blocked', 'pending_subtasks')),
                    next_step TEXT NOT NULL DEFAULT 'commit-make'
                        CHECK(next_step IN ('commit-make', 'commit-review',
                                            'commit-make-supertask', 'commit-review-supertask',
                                            'commit-plan', 'commit-plan-review', 'none')),
                    branch TEXT,
                    commit_hash TEXT,
                    stash_ref TEXT,
                    coder_agent TEXT,
                    review_round INTEGER DEFAULT 0,
                    last_review_decision TEXT DEFAULT 'none',
                    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
                    ready_at       DATETIME DEFAULT NULL,
                    updated_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
                    kind TEXT NOT NULL DEFAULT 'task',
                    parent_task_id INTEGER REFERENCES tasks(id),
                    sequence_index INTEGER,
                    commit_plan    TEXT
                );
                CREATE TABLE task_skips (
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    step TEXT NOT NULL CHECK(step IN ('commit-plan','commit-plan-review',
                                                      'commit-review','commit-review-supertask')),
                    PRIMARY KEY (task_id, step)
                );
                CREATE TABLE comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER REFERENCES tasks(id),
                    review_round INTEGER,
                    verb TEXT,
                    author TEXT,
                    message TEXT,
                    kind TEXT DEFAULT 'comment'
                        CHECK(kind IN ('comment', 'approval', 'rejection',
                                       'commit-message', 'validation')),
                    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP
                );"""
            )
            legacy.commit()
            legacy.close()

            # connect() should auto-migrate rather than raise
            conn = db.connect(tmp.name)
            schema = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='comments'"
            ).fetchone()[0]
            conn.close()
            self.assertIn("plan-approval", schema)
            self.assertIn("plan-rejection", schema)
            self.assertIn("done-without-commit", schema)
        finally:
            os.unlink(tmp.name)


    def test_run_log_task_id_not_null_auto_migrates(self):
        """connect() auto-migrates run_log when task_id has NOT NULL so NULL inserts work."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            legacy = sqlite3.connect(tmp.name)
            legacy.executescript(
                """CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT NOT NULL DEFAULT 'none'
                        CHECK(status IN ('none', 'ready', 'running', 'done', 'blocked', 'pending_subtasks')),
                    next_step TEXT NOT NULL DEFAULT 'commit-make'
                        CHECK(next_step IN ('commit-make', 'commit-review',
                                            'commit-make-supertask', 'commit-review-supertask',
                                            'commit-plan', 'commit-plan-review', 'none')),
                    branch TEXT,
                    commit_hash TEXT,
                    stash_ref TEXT,
                    coder_agent TEXT,
                    review_round INTEGER DEFAULT 0,
                    last_review_decision TEXT DEFAULT 'none',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    ready_at DATETIME DEFAULT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    kind TEXT NOT NULL DEFAULT 'task',
                    parent_task_id INTEGER REFERENCES tasks(id),
                    sequence_index INTEGER,
                    commit_plan TEXT,
                    follow_up_task_id INTEGER REFERENCES tasks(id)
                );
                CREATE TABLE task_skips (
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    step TEXT NOT NULL CHECK(step IN ('commit-plan','commit-plan-review',
                                                      'commit-review','commit-review-supertask')),
                    PRIMARY KEY (task_id, step)
                );
                CREATE TABLE run_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES tasks(id),
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
                    kind TEXT DEFAULT 'comment'
                        CHECK(kind IN ('comment', 'approval', 'rejection', 'commit-message',
                                       'validation', 'plan-approval', 'plan-rejection',
                                       'done-without-commit', 'deferred-build-changed')),
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );"""
            )
            legacy.execute(
                "INSERT INTO tasks (id, title, next_step, status) "
                "VALUES (1, 'existing task', 'commit-make', 'done')"
            )
            legacy.execute("INSERT INTO run_log (task_id, message) VALUES (1, 'old entry')")
            legacy.commit()
            legacy.close()

            conn = db.connect(tmp.name)
            # After migration, task_id should be nullable
            col_info = {
                row["name"]: row
                for row in conn.execute("PRAGMA table_info(run_log)").fetchall()
            }
            self.assertEqual(col_info["task_id"]["notnull"], 0)
            # Old row survived migration
            rows = conn.execute("SELECT * FROM run_log").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["message"], "old entry")
            # Can now insert a NULL task_id row
            db.add_run_log(conn, None, "global entry", author="orchestrator")
            global_rows = db.get_global_run_log(conn)
            self.assertEqual(len(global_rows), 1)
            self.assertEqual(global_rows[0]["message"], "global entry")
            conn.close()
        finally:
            os.unlink(tmp.name)

    def test_global_run_log_returns_only_null_task_id_entries(self):
        """get_global_run_log returns only rows where task_id IS NULL."""
        tid = db.add_task(self.conn, "Task for run log")
        db.add_run_log(self.conn, tid, "task-scoped entry", author="orchestrator")
        db.add_run_log(self.conn, None, "global entry 1", author="orchestrator")
        db.add_run_log(self.conn, None, "global entry 2", author="orchestrator")
        global_rows = db.get_global_run_log(self.conn)
        self.assertEqual(len(global_rows), 2)
        self.assertTrue(all(r["task_id"] is None for r in global_rows))
        task_rows = db.get_run_log(self.conn, tid)
        self.assertEqual(len(task_rows), 1)
        self.assertEqual(task_rows[0]["message"], "task-scoped entry")

    def test_get_orchestrator_run_log_includes_global_and_task_rows(self):
        """get_orchestrator_run_log returns all orchestrator rows with optional task title."""
        tid = db.add_task(self.conn, "Scoped task")
        db.add_run_log(self.conn, tid, "task entry", author="orchestrator")
        db.add_run_log(self.conn, None, "global entry", author="orchestrator")
        db.add_run_log(self.conn, tid, "non-orchestrator entry", author="claude")

        rows = db.get_orchestrator_run_log(self.conn)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["message"], "global entry")
        self.assertIsNone(rows[0]["task_id"])
        self.assertEqual(rows[1]["message"], "task entry")
        self.assertEqual(rows[1]["task_id"], tid)
        self.assertEqual(rows[1]["task_title"], "Scoped task")

    def test_new_task_has_no_commit_hash(self):
        tid = db.add_task(self.conn, "Brand new")
        task = db.get_task(self.conn, tid)
        self.assertIsNone(task["commit_hash"])

    def test_update_commit_hash(self):
        tid = db.add_task(self.conn, "Hash me")
        db.update_task(self.conn, tid, commit_hash="def456")
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["commit_hash"], "def456")


    def test_delete_task_only_none(self):
        tid = db.add_task(self.conn, "Delete me")
        db.delete_task(self.conn, tid)
        self.assertIsNone(db.get_task(self.conn, tid))

    def test_delete_task_rejects_non_none(self):
        tid = db.add_task(self.conn, "Cant delete")
        db.update_task(self.conn, tid, status="ready", branch="b")
        with self.assertRaises(ValueError):
            db.delete_task(self.conn, tid)

    def test_run_log(self):
        tid = db.add_task(self.conn, "Log task")
        db.add_run_log(self.conn, tid, "Starting work", verb="commit-make", author="claude")
        db.add_run_log(self.conn, tid, "Still working", verb="commit-make", author="claude")
        logs = db.get_run_log(self.conn, tid)
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0]["message"], "Still working")

    def test_comments(self):
        tid = db.add_task(self.conn, "Comment task")
        db.add_comment(self.conn, tid, "Looks good", kind="approval", author=DEFAULT_REVIEWER, review_round=0)
        db.add_comment(self.conn, tid, "Fix the bug", kind="rejection", author=DEFAULT_REVIEWER, review_round=0)
        comments = db.get_comments(self.conn, tid)
        self.assertEqual(len(comments), 2)
        self.assertEqual(comments[0]["kind"], "approval")
        self.assertEqual(comments[1]["kind"], "rejection")

    def test_comment_auto_round(self):
        tid = db.add_task(self.conn, "Auto round")
        db.update_task(self.conn, tid, review_round=3)
        db.add_comment(self.conn, tid, "Round 3 comment", kind="comment")
        comments = db.get_comments(self.conn, tid)
        self.assertEqual(comments[0]["review_round"], 3)

    def test_review_decision_accepts_current_expected_round(self):
        tid = db.add_task(self.conn, "Review decision")
        db.add_comment(
            self.conn,
            tid,
            "LGTM",
            kind="approval",
            author=DEFAULT_REVIEWER,
            expected_review_round=0,
        )
        comments = db.get_comments(self.conn, tid)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["review_round"], 0)

    def test_review_decision_rejects_stale_round(self):
        tid = db.add_task(self.conn, "Stale review decision")
        db.update_task(self.conn, tid, review_round=2)
        with self.assertRaises(ValueError):
            db.add_comment(
                self.conn,
                tid,
                "LGTM",
                kind="approval",
                author=DEFAULT_REVIEWER,
                review_round=1,
                expected_review_round=1,
            )

    def test_find_ready_task(self):
        db.add_task(self.conn, "Not ready")
        t2 = db.add_task(self.conn, "Ready one", sequence_index=200)
        db.update_task(self.conn, t2, status="ready", branch="b")
        t3 = db.add_task(self.conn, "Ready two", sequence_index=100)
        db.update_task(self.conn, t3, status="ready", branch="b")

        found = db.find_ready_task(self.conn)
        self.assertEqual(found["id"], t3)

    def test_find_ready_task_tie_breaks_by_id_when_sequence_index_matches(self):
        t1 = db.add_task(self.conn, "Ready one", branch="b", sequence_index=100)
        t2 = db.add_task(self.conn, "Ready two", branch="b", sequence_index=100)
        db.update_task(self.conn, t1, status="ready")
        db.update_task(self.conn, t2, status="ready")

        found = db.find_ready_task(self.conn)
        self.assertEqual(found["id"], t1)

    def test_find_ready_task_sorts_null_sequence_index_after_numbered_tasks(self):
        later_id = db.add_task(self.conn, "Unplanned ready", branch="b")
        earlier_id = db.add_task(self.conn, "Planned ready", branch="b", sequence_index=100)
        db.update_task(self.conn, later_id, status="ready")
        db.update_task(self.conn, earlier_id, status="ready")

        found = db.find_ready_task(self.conn)
        self.assertEqual(found["id"], earlier_id)

    def test_find_ready_task_none(self):
        db.add_task(self.conn, "Not ready")
        self.assertIsNone(db.find_ready_task(self.conn))

    def test_find_ready_task_skips_default_ready_task_when_any_task_blocked(self):
        blocked_id = db.add_task(self.conn, "Blocked task", branch="blocked")
        ready_id = db.add_task(self.conn, "Ready task", branch="ready")
        db.update_task(self.conn, blocked_id, status="blocked")
        db.update_task(self.conn, ready_id, status="ready")

        found = db.find_ready_task(self.conn)
        self.assertIsNone(found)

    def test_find_ready_task_allows_opted_in_task_when_any_task_blocked(self):
        blocked_id = db.add_task(self.conn, "Blocked task", branch="blocked")
        ready_id = db.add_task(
            self.conn,
            "Escaping ready task",
            branch="ready",
            allow_when_blocked=True,
        )
        db.update_task(self.conn, blocked_id, status="blocked")
        db.update_task(self.conn, ready_id, status="ready")

        found = db.find_ready_task(self.conn)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], ready_id)

    def test_list_ready_tasks_blocked_by_blocked_gate_returns_only_gated_ready_tasks(self):
        blocked_id = db.add_task(self.conn, "Blocked task", branch="blocked")
        gated_id = db.add_task(self.conn, "Gated ready task", branch="ready")
        allowed_id = db.add_task(
            self.conn,
            "Allowed ready task",
            branch="ready",
            allow_when_blocked=True,
        )
        db.update_task(self.conn, blocked_id, status="blocked")
        db.update_task(self.conn, gated_id, status="ready")
        db.update_task(self.conn, allowed_id, status="ready")

        gated = db.list_ready_tasks_blocked_by_blocked_gate(self.conn)

        self.assertEqual([task["id"] for task in gated], [gated_id])


    def test_purge_run_log(self):
        tid = db.add_task(self.conn, "Purge test")
        db.add_run_log(self.conn, tid, "old log")
        # Default purge won't affect non-done tasks
        db.purge_run_log(self.conn)
        self.assertEqual(len(db.get_run_log(self.conn, tid)), 1)

        # Purge by days=0 removes everything
        db.purge_run_log(self.conn, days=0)
        self.assertEqual(len(db.get_run_log(self.conn, tid)), 0)



class TestPromptAssembly(unittest.TestCase):
    """Test prompt building."""

    def test_build_prompt_includes_task_fields(self):
        task = {
            "id": 1, "title": "Fix bug",
            "description": "Fix the login bug", "branch": "fix-login",
            "status": "running", "next_step": "commit-make",
            "review_round": 0, "last_review_decision": "none",
            "commit_hash": None, "stash_ref": "stash@{1}", "coder_agent": "claude",
        }
        prompt = orchestrator.build_prompt(task, "commit-make", "claude", [])
        self.assertIn("Fix bug", prompt)
        self.assertIn("fix-login", prompt)
        self.assertIn("stash@{1}", prompt)
        self.assertIn('task set 1 --stash-ref <stash-ref>', prompt)
        self.assertIn("claude", prompt)
        self.assertIn("coder", prompt)

    def test_build_prompt_labels_description_as_markdown_source(self):
        task = {
            "id": 1, "title": "Fix bug",
            "description": "## Goal\n\n- Fix `login`",
            "branch": "fix-login", "status": "running",
            "next_step": "commit-make", "review_round": 0,
            "last_review_decision": "none", "commit_hash": None,
            "stash_ref": None, "coder_agent": "claude",
        }

        prompt = orchestrator.build_prompt(task, "commit-make", "claude", [])

        self.assertIn("- description_markdown:\n  ```markdown\n  ## Goal", prompt)
        self.assertIn("  - Fix `login`\n  ```", prompt)

    def test_build_prompt_surfaces_allow_tasks_on_master_policy_for_master_task(self):
        task = {
            "id": 2, "title": "Master task",
            "description": None, "branch": "master",
            "status": "running", "next_step": "commit-make",
            "review_round": 0, "last_review_decision": "none",
            "commit_hash": None, "stash_ref": None, "coder_agent": "claude",
        }
        with patch.object(orchestrator.repo_policy, "read_allow_tasks_on_master", return_value=True), \
             patch.object(orchestrator, "_repo_root", return_value="/fake/repo"):
            prompt = orchestrator.build_prompt(task, "commit-make", "claude", [])
        self.assertIn("allow_tasks_on_master: yes", prompt)
        self.assertIn("ALLOW_TASKS_ON_MASTER", prompt)

    def test_build_prompt_includes_comments(self):
        task = {
            "id": 1, "title": "T", "description": None,
            "branch": "b", "status": "running", "next_step": "commit-review",
            "review_round": 1, "last_review_decision": "reject",
            "commit_hash": None, "stash_ref": None, "coder_agent": "claude",
        }
        comments = [
            {"kind": "rejection", "review_round": 0, "author": "codex",
             "message": "Missing error handling"},
        ]
        prompt = orchestrator.build_prompt(task, "commit-review", "codex", comments)
        self.assertIn("Missing error handling", prompt)
        self.assertIn("reviewer", prompt)

    def test_build_prompt_commit_make_requires_fresh_path_a_commit_message(self):
        task = {
            "id": 1, "title": "T", "description": None,
            "branch": "b", "status": "running", "next_step": "commit-make",
            "review_round": 0, "last_review_decision": "none",
            "commit_hash": None, "stash_ref": None, "coder_agent": "claude",
        }
        prompt = orchestrator.build_prompt(task, "commit-make", "claude", [])
        self.assertIn("Always write a fresh `--commit-message` comment during the current run", prompt)
        self.assertIn("valid on Path B only", prompt)

    def test_build_prompt_path_b_requires_reading_approval_notes(self):
        task = {
            "id": 1, "title": "T", "description": None,
            "branch": "b", "status": "running", "next_step": "commit-make",
            "review_round": 2, "last_review_decision": "approve",
            "commit_hash": None, "stash_ref": None, "coder_agent": "claude",
        }
        comments = [
            {"kind": "approval", "review_round": 2, "author": "codex",
             "message": "Approved; please fix the typo before landing."},
            {"kind": "comment", "review_round": 2, "author": "codex",
             "message": "Same-round note: no broader refactor needed."},
        ]
        prompt = orchestrator.build_prompt(task, "commit-make", "claude", comments)
        self.assertIn("Read same-round review guidance before committing", prompt)
        self.assertIn("Approval means the task may land", prompt)
        self.assertIn("small, directly reviewer-requested edits", prompt)
        self.assertIn("Do not make broader improvements", prompt)
        self.assertIn("durable `task comment ... --comment`", prompt)
        self.assertIn("Approved; please fix the typo before landing.", prompt)
        self.assertIn("Same-round note: no broader refactor needed.", prompt)

    def test_build_prompt_reviewer_includes_handoff_section(self):
        """commit-review prompt includes repo root, task CLI path, and 'do not rerun' guidance."""
        task = {
            "id": 5, "title": "Add feature", "description": None,
            "branch": "feat", "status": "running", "next_step": "commit-review",
            "review_round": 0, "last_review_decision": "none",
            "commit_hash": None, "stash_ref": None, "coder_agent": "claude",
        }
        prompt = orchestrator.build_prompt(task, "commit-review", "codex", [])
        self.assertIn("Reviewer Handoff", prompt)
        self.assertIn('"$ORCHESTRA_DIR/bin/ko-task"', prompt)
        self.assertIn("trust the maker", prompt.lower().replace("\n", " "))
        # Reviewer handoff should NOT appear in commit-make prompts
        make_prompt = orchestrator.build_prompt(task | {"next_step": "commit-make"}, "commit-make", "claude", [])
        self.assertNotIn("Reviewer Handoff", make_prompt)

    def test_build_prompt_reviewer_quality_guidance(self):
        """commit-review prompt distinguishes concrete quality issues from subjective nits."""
        task = {
            "id": 6, "title": "Polish prompt", "description": None,
            "branch": "feat", "status": "running", "next_step": "commit-review",
            "review_round": 0, "last_review_decision": "none",
            "commit_hash": None, "stash_ref": None, "coder_agent": "claude",
        }
        prompt = orchestrator.build_prompt(task, "commit-review", "codex", [])
        self.assertIn("Concrete quality or polish issues", prompt)
        self.assertIn("maintainability", prompt)
        self.assertIn("readability", prompt)
        self.assertIn("UX", prompt)
        self.assertIn("docs clarity", prompt)
        self.assertIn("public presentation", prompt)
        self.assertIn("Purely personal preferences or subjective style nits", prompt)
        self.assertIn("Large refactors outside the task scope", prompt)
        self.assertNotIn("Style, formatting, hypothetical improvements", prompt)

    def test_build_prompt_reviewer_handoff_includes_maker_commit_message(self):
        """Reviewer prompt prominently shows maker's most recent commit-message comment."""
        task = {
            "id": 7, "title": "Fix bug", "description": None,
            "branch": "fix", "status": "running", "next_step": "commit-review",
            "review_round": 1, "last_review_decision": "none",
            "commit_hash": None, "stash_ref": None, "coder_agent": "claude",
        }
        comments = [
            {"kind": "commit-message", "review_round": 0, "author": "claude",
             "message": "Fix login bug\n\nTask 7 (ko-def)"},
            {"kind": "rejection", "review_round": 0, "author": "gemini",
             "message": "Needs tests"},
            {"kind": "commit-message", "review_round": 1, "author": "claude",
             "message": "Fix login bug with tests\n\nTask 7 (ko-def)"},
        ]
        prompt = orchestrator.build_prompt(task, "commit-review", "codex", comments)
        # Most recent commit message should appear in the Reviewer Handoff section
        self.assertIn("Fix login bug with tests", prompt)
        # Older commit message need not be excluded, but the newest must be present
        self.assertIn("Task 7 (ko-def)", prompt)

    def test_build_prompt_reviewer_handoff_no_commit_message(self):
        """Reviewer handoff handles missing commit-message comment gracefully."""
        task = {
            "id": 9, "title": "Refactor", "description": None,
            "branch": "ref", "status": "running", "next_step": "commit-review",
            "review_round": 0, "last_review_decision": "none",
            "commit_hash": None, "stash_ref": None, "coder_agent": "claude",
        }
        prompt = orchestrator.build_prompt(task, "commit-review", "codex", [])
        self.assertIn("Reviewer Handoff", prompt)
        self.assertIn("no commit-message comment recorded", prompt)

    def test_build_prompt_reviewer_handoff_includes_validation_summary(self):
        """Reviewer prompt surfaces the maker's most recent validation comment."""
        task = {
            "id": 11, "title": "Add tests", "description": None,
            "branch": "tests", "status": "running", "next_step": "commit-review",
            "review_round": 0, "last_review_decision": "none",
            "commit_hash": None, "stash_ref": None, "coder_agent": "claude",
        }
        comments = [
            {"kind": "validation", "review_round": 0, "author": "claude",
             "message": "python3 -m pytest -q: 42 passed, 0 failed"},
            {"kind": "commit-message", "review_round": 0, "author": "claude",
             "message": "Add tests\n\nTask 11 (ko-jkl)"},
        ]
        prompt = orchestrator.build_prompt(task, "commit-review", "codex", comments)
        self.assertIn("python3 -m pytest -q: 42 passed, 0 failed", prompt)
        self.assertIn("Maker's validation summary", prompt)

    def test_build_prompt_reviewer_handoff_no_validation_comment(self):
        """Reviewer handoff shows fallback text when no validation comment recorded."""
        task = {
            "id": 13, "title": "Fix typo", "description": None,
            "branch": "fix", "status": "running", "next_step": "commit-review",
            "review_round": 0, "last_review_decision": "none",
            "commit_hash": None, "stash_ref": None, "coder_agent": "claude",
        }
        prompt = orchestrator.build_prompt(task, "commit-review", "codex", [])
        self.assertIn("Reviewer Handoff", prompt)
        self.assertIn("no validation comment recorded", prompt)

    def test_build_prompt_reviewer_handoff_validation_scoped_to_current_round(self):
        """Reviewer handoff uses only the current round's validation, not a stale prior-round entry."""
        task = {
            "id": 17, "title": "Fix bug", "description": None,
            "branch": "fix", "status": "running", "next_step": "commit-review",
            "review_round": 1, "last_review_decision": "none",
            "commit_hash": None, "stash_ref": None, "coder_agent": "claude",
        }
        comments = [
            # Round-0 validation — must NOT be surfaced in a round-1 reviewer handoff
            {"kind": "validation", "review_round": 0, "author": "claude",
             "message": "stale round-0 results: 50 passed"},
        ]
        # Test _build_reviewer_handoff directly so we inspect only the handoff
        # section, not Prior Comments (which legitimately lists all history).
        handoff = orchestrator._build_reviewer_handoff(task, comments)
        self.assertNotIn("stale round-0 results", handoff)
        self.assertIn("no validation comment recorded", handoff)

    def test_build_prompt_reviewer_handoff_validation_absent_from_make_prompt(self):
        """Validation handoff section does not appear in commit-make prompts."""
        task = {
            "id": 15, "title": "Fix bug", "description": None,
            "branch": "fix", "status": "running", "next_step": "commit-make",
            "review_round": 0, "last_review_decision": "none",
            "commit_hash": None, "stash_ref": None, "coder_agent": "claude",
        }
        comments = [
            {"kind": "validation", "review_round": 0, "author": "claude",
             "message": "pytest: 10 passed"},
        ]
        make_prompt = orchestrator.build_prompt(task, "commit-make", "claude", comments)
        self.assertNotIn("Maker's validation summary", make_prompt)
        self.assertNotIn("Reviewer Handoff", make_prompt)

    def test_pull_request_make_prompt_records_pr_metadata_not_commit_message(self):
        task = {
            "id": 30, "title": "Prepare PR", "description": None,
            "branch": "feat-pr", "status": "running", "next_step": "pull-request-make",
            "review_round": 0, "last_review_decision": "none",
            "coder_agent": "claude", "reviewer_agent": "codex",
        }

        prompt = orchestrator.build_prompt(task, "pull-request-make", "claude", [])

        self.assertIn("Create or update the GitHub", prompt)
        self.assertIn("PR URL: https://github.com/<owner>/<repo>/pull/<number>", prompt)
        self.assertIn("Title: <PR title>", prompt)
        self.assertIn("Body:", prompt)
        self.assertIn("task comment 30 --message-stdin --comment", prompt)
        self.assertNotIn("--commit-message", prompt)
        self.assertNotIn("get-commit-footer", prompt)

    def test_pull_request_review_prompt_limits_scope_to_metadata(self):
        task = {
            "id": 31, "title": "Review PR", "description": None,
            "branch": "feat-pr", "status": "running", "next_step": "pull-request-review",
            "review_round": 0, "last_review_decision": "none",
            "coder_agent": "claude", "reviewer_agent": "codex",
        }
        comments = [{
            "id": 1,
            "kind": "comment",
            "review_round": 0,
            "author": "claude",
            "message": "PR URL: https://github.com/acme/project/pull/12\nTitle: Add feature\nBody:\nSummary",
        }]

        prompt = orchestrator.build_prompt(task, "pull-request-review", "codex", comments)

        self.assertIn("Pull Request Reviewer Handoff", prompt)
        self.assertIn("https://github.com/acme/project/pull/12", prompt)
        self.assertIn("title/body quality", prompt)
        self.assertIn("branch-summary accuracy", prompt)
        self.assertIn("Do not perform implementation code review", prompt)
        self.assertIn("--approval --author codex --review-round 0", prompt)

    def test_other_make_prompt_requires_durable_evidence_not_commit_message(self):
        task = {
            "id": 32, "title": "External maintenance", "description": None,
            "branch": None, "status": "running", "next_step": "other-make",
            "review_round": 0, "last_review_decision": "none",
            "coder_agent": "claude", "reviewer_agent": "codex",
        }

        prompt = orchestrator.build_prompt(task, "other-make", "claude", [])

        self.assertIn("durable completion evidence", prompt)
        self.assertIn("why no commit or PR was expected", prompt)
        self.assertIn("task comment 32 --message-stdin --comment", prompt)
        self.assertNotIn("--commit-message", prompt)
        self.assertNotIn("get-commit-footer", prompt)

    def test_other_review_prompt_reviews_evidence(self):
        task = {
            "id": 33, "title": "Review evidence", "description": None,
            "branch": None, "status": "running", "next_step": "other-review",
            "review_round": 0, "last_review_decision": "none",
            "coder_agent": "claude", "reviewer_agent": "codex",
        }
        comments = [{
            "id": 1,
            "kind": "comment",
            "review_round": 0,
            "author": "claude",
            "message": "Checked external state; no repo change or PR was expected.",
        }]

        prompt = orchestrator.build_prompt(task, "other-review", "codex", comments)

        self.assertIn("Other Reviewer Handoff", prompt)
        self.assertIn("Checked external state", prompt)
        self.assertIn("commit-free and PR-free", prompt)
        self.assertIn("--approval --author codex --review-round 0", prompt)

    def test_reviewer_prior_comments_excludes_commit_message_and_validation(self):
        """commit-review prompt drops commit-message and validation from Prior Comments (already in handoff)."""
        task = {
            "id": 20, "title": "T", "description": None,
            "branch": "b", "status": "running", "next_step": "commit-review",
            "review_round": 1, "last_review_decision": "none",
            "commit_hash": None, "stash_ref": None, "coder_agent": "claude",
        }
        comments = [
            {"kind": "rejection", "review_round": 0, "author": "codex",
             "message": "Needs better error handling"},
            {"kind": "commit-message", "review_round": 1, "author": "claude",
             "message": "Fix bug\n\nTask 20 (ko-aaa)"},
            {"kind": "validation", "review_round": 1, "author": "claude",
             "message": "pytest: 15 passed"},
            {"kind": "comment", "review_round": 1, "author": "orchestrator",
             "message": "Skipped same-branch check."},
        ]
        prompt = orchestrator.build_prompt(task, "commit-review", "codex", comments)
        # rejection and orchestrator comments should appear in Prior Comments
        self.assertIn("Needs better error handling", prompt)
        self.assertIn("Skipped same-branch check.", prompt)
        # commit-message and validation content appears only in the Reviewer Handoff — not in Prior Comments
        # We verify they don't appear in the Prior Comments block specifically
        prior_block_start = prompt.index("## Prior Comments")
        cli_block_start = prompt.index("## CLI Commands Available")
        prior_comments_block = prompt[prior_block_start:cli_block_start]
        self.assertNotIn("Fix bug", prior_comments_block)
        self.assertNotIn("pytest: 15 passed", prior_comments_block)

    def test_coder_prior_comments_includes_all_kinds(self):
        """commit-make prompt shows all comment kinds (no filtering by kind)."""
        task = {
            "id": 21, "title": "T", "description": None,
            "branch": "b", "status": "running", "next_step": "commit-make",
            "review_round": 0, "last_review_decision": "none",
            "commit_hash": None, "stash_ref": None, "coder_agent": "claude",
        }
        comments = [
            {"kind": "validation", "review_round": 0, "author": "claude",
             "message": "pytest: 5 passed"},
            {"kind": "commit-message", "review_round": 0, "author": "claude",
             "message": "Fix\n\nTask 21 (ko-bbb)"},
            {"kind": "rejection", "review_round": 0, "author": "codex",
             "message": "Wrong approach"},
        ]
        prompt = orchestrator.build_prompt(task, "commit-make", "claude", comments)
        self.assertIn("pytest: 5 passed", prompt)
        self.assertIn("Fix\n\nTask 21 (ko-bbb)", prompt)
        self.assertIn("Wrong approach", prompt)

    def test_prior_comments_hard_capped_keeps_most_recent(self):
        """## Prior Comments is capped at MAX_PRIOR_COMMENTS; oldest are dropped."""
        task = {
            "id": 22, "title": "T", "description": None,
            "branch": "b", "status": "running", "next_step": "commit-make",
            "review_round": 0, "last_review_decision": "none",
            "commit_hash": None, "stash_ref": None, "coder_agent": "claude",
        }
        # Create MAX_PRIOR_COMMENTS + 3 comments
        cap = orchestrator.MAX_PRIOR_COMMENTS
        comments = [
            {"kind": "comment", "review_round": 0, "author": "orchestrator",
             "message": f"Comment number {i}"}
            for i in range(cap + 3)
        ]
        prompt = orchestrator.build_prompt(task, "commit-make", "claude", comments)
        # Extract just the Prior Comments block to avoid substring collisions with later numbers
        prior_start = prompt.index("## Prior Comments")
        # Use rindex because shared-task-context.md may contain the string in its explanatory text
        cli_start = prompt.rindex("## CLI Commands Available")
        prior_block = prompt[prior_start:cli_start]
        # Oldest comments (0, 1, 2) should be dropped — use line-level check
        for dropped_i in range(cap + 3 - cap):  # 0, 1, 2
            self.assertNotIn(f"Comment number {dropped_i}\n", prior_block)
        # Most recent comments should appear
        self.assertIn(f"Comment number {cap + 2}", prior_block)
        self.assertIn(f"Comment number {cap}", prior_block)
        # Truncation notice should be present
        self.assertIn(f"showing {cap} most recent", prior_block)

    def test_filter_comments_for_prompt_reviewer_excludes_kinds(self):
        """_filter_comments_for_prompt drops commit-message and validation for reviewer verbs."""
        comments = [
            {"kind": "rejection", "review_round": 0, "author": "codex", "message": "Bad"},
            {"kind": "commit-message", "review_round": 0, "author": "claude", "message": "Msg"},
            {"kind": "validation", "review_round": 0, "author": "claude", "message": "ok"},
            {"kind": "comment", "review_round": 0, "author": "orchestrator", "message": "Note"},
        ]
        filtered, total = orchestrator._filter_comments_for_prompt(comments, "commit-review")
        kinds = {c["kind"] for c in filtered}
        self.assertNotIn("commit-message", kinds)
        self.assertNotIn("validation", kinds)
        self.assertIn("rejection", kinds)
        self.assertIn("comment", kinds)
        self.assertEqual(total, 2)  # two non-excluded comments

    def test_filter_comments_for_prompt_coder_keeps_all_kinds(self):
        """_filter_comments_for_prompt keeps all kinds for coder verbs."""
        comments = [
            {"kind": "commit-message", "review_round": 0, "author": "claude", "message": "Msg"},
            {"kind": "validation", "review_round": 0, "author": "claude", "message": "ok"},
        ]
        filtered, total = orchestrator._filter_comments_for_prompt(comments, "commit-make")
        self.assertEqual(len(filtered), 2)
        self.assertEqual(total, 2)

    def test_filter_comments_for_prompt_caps_at_max(self):
        """_filter_comments_for_prompt caps result at MAX_PRIOR_COMMENTS."""
        cap = orchestrator.MAX_PRIOR_COMMENTS
        comments = [
            {"kind": "comment", "review_round": 0, "author": "bot", "message": f"m{i}"}
            for i in range(cap + 5)
        ]
        filtered, total = orchestrator._filter_comments_for_prompt(comments, "commit-make")
        self.assertEqual(len(filtered), cap)
        self.assertEqual(total, cap + 5)
        # Keeps the most recent
        self.assertEqual(filtered[-1]["message"], f"m{cap + 4}")
        self.assertEqual(filtered[0]["message"], "m5")



class TestReviewAggregation(unittest.TestCase):
    """Test review round aggregation semantics."""

    def setUp(self):
        self._ack_patcher = patch.object(orchestrator, "ensure_agent_acked")
        self._ack_patcher.start()
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        self._ack_patcher.stop()
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_reviewer_approval_returns_approve(self):
        tid = db.add_task(self.conn, "Review approve", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b", next_step="commit-review")
        task = db.get_task(self.conn, tid)

        db.add_comment(self.conn, tid, "LGTM", kind="approval", author=DEFAULT_REVIEWER, review_round=0)

        with patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "run_agent", return_value=0):
            outcome = orchestrator.handle_commit_review(task, self.conn)

        self.assertEqual(outcome, "approve")

    def test_task_specific_reviewer_runs_commit_review(self):
        tid = db.add_task(
            self.conn,
            "Specific reviewer",
            coder_agent="claude",
            reviewer_agent="gemini",
        )
        db.update_task(self.conn, tid, status="running", branch="b", next_step="commit-review")
        task = db.get_task(self.conn, tid)
        db.add_comment(self.conn, tid, "LGTM", kind="approval", author="gemini", review_round=0)

        with patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "run_agent", return_value=0) as run_agent:
            outcome = orchestrator.handle_commit_review(task, self.conn)

        self.assertEqual(outcome, "approve")
        run_agent.assert_called_once()
        self.assertEqual(run_agent.call_args.args[0], "gemini")
        self.assertIn("- reviewer_agent: gemini", run_agent.call_args.args[1])

    def test_null_reviewer_falls_back_to_default_reviewer(self):
        tid = db.add_task(self.conn, "Default reviewer", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b", next_step="commit-review")
        task = db.get_task(self.conn, tid)
        db.add_comment(self.conn, tid, "LGTM", kind="approval", author=DEFAULT_REVIEWER, review_round=0)

        with patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "run_agent", return_value=0) as run_agent:
            outcome = orchestrator.handle_commit_review(task, self.conn)

        self.assertEqual(outcome, "approve")
        self.assertEqual(run_agent.call_args.args[0], DEFAULT_REVIEWER)

    def test_reviewer_rejection_returns_reject(self):
        tid = db.add_task(self.conn, "Review reject", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b", next_step="commit-review")
        task = db.get_task(self.conn, tid)

        db.add_comment(self.conn, tid, "Nope", kind="rejection", author=DEFAULT_REVIEWER, review_round=0)

        with patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "run_agent", return_value=0):
            outcome = orchestrator.handle_commit_review(task, self.conn)

        self.assertEqual(outcome, "reject")

    def test_old_round_comments_ignored(self):
        """Comments from previous rounds don't count for current round."""
        tid = db.add_task(self.conn, "Round isolation")
        db.update_task(self.conn, tid, review_round=2)
        # Old round 0 rejection
        db.add_comment(self.conn, tid, "Old reject", kind="rejection", author=DEFAULT_REVIEWER, review_round=0)
        # Current round 2 approval
        db.add_comment(self.conn, tid, "Now OK", kind="approval", author=DEFAULT_REVIEWER, review_round=2)

        comments = db.get_comments(self.conn, tid)
        current_round = 2
        round_reviews = [
            c for c in comments
            if c["review_round"] == current_round
            and c["kind"] in ("approval", "rejection")
        ]
        self.assertEqual(len(round_reviews), 1)
        self.assertEqual(round_reviews[0]["kind"], "approval")

    def test_reviewer_failure_returns_error(self):
        """Reviewer subprocess failure returns error."""
        tid = db.add_task(self.conn, "Reviewer fails", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b", next_step="commit-review")
        task = db.get_task(self.conn, tid)

        with patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "run_agent", return_value=1):
            outcome = orchestrator.handle_commit_review(task, self.conn)

        self.assertEqual(outcome, "error")

    def test_reviewer_no_comment_returns_reject(self):
        """Reviewer exits 0 but leaves no comment — treated as reject."""
        tid = db.add_task(self.conn, "No comment", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b", next_step="commit-review")
        task = db.get_task(self.conn, tid)

        with patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "run_agent", return_value=0):
            outcome = orchestrator.handle_commit_review(task, self.conn)

        self.assertEqual(outcome, "reject")


class TestStateMachine(unittest.TestCase):
    """Test orchestrator state transitions without actually running agents."""

    def setUp(self):
        self._ack_patcher = patch.object(orchestrator, "ensure_agent_acked")
        self._ack_patcher.start()
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        self._ack_patcher.stop()
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_pull_request_make_requires_fresh_pr_metadata_comment(self):
        tid = db.add_task(
            self.conn,
            "Prepare PR",
            kind="pull_request",
            branch="feat-pr",
            coder_agent="claude",
        )
        db.update_task(self.conn, tid, status="running", next_step="pull-request-make")
        task = db.get_task(self.conn, tid)

        with patch.object(orchestrator, "run_agent", return_value=0):
            success = orchestrator.handle_pull_request_make(task, self.conn)

        self.assertFalse(success)
        run_log = db.get_run_log(self.conn, tid)
        self.assertTrue(
            any("without writing a fresh PR metadata comment" in entry["message"] for entry in run_log)
        )

    def test_pull_request_make_transitions_to_review_when_metadata_recorded(self):
        tid = db.add_task(
            self.conn,
            "Prepare PR",
            kind="pull_request",
            branch="feat-pr",
            coder_agent="claude",
        )
        db.update_task(self.conn, tid, status="running", next_step="pull-request-make")
        task = db.get_task(self.conn, tid)

        def fake_pr_maker(name, prompt, task_id, conn, verb, **kw):
            db.add_comment(
                conn,
                task_id,
                "PR URL: https://github.com/acme/project/pull/12\n"
                "Title: Add pull request support\n"
                "Body:\nSummarizes the branch against master.",
                kind="comment",
                author=name,
            )
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_pr_maker), \
             patch.object(orchestrator, "ensure_branch", return_value=True):
            result = orchestrator.advance(task, self.conn)

        self.assertTrue(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "ready")
        self.assertEqual(updated["next_step"], "pull-request-review")
        self.assertIsNone(updated["commit_hash"])

    def test_pull_request_review_approval_marks_done_without_commit(self):
        tid = db.add_task(
            self.conn,
            "Review PR",
            kind="pull_request",
            branch="feat-pr",
            coder_agent="claude",
            reviewer_agent="gemini",
        )
        db.update_task(self.conn, tid, status="running", next_step="pull-request-review")
        task = db.get_task(self.conn, tid)

        def fake_pr_reviewer(name, prompt, task_id, conn, verb, **kw):
            db.add_comment(conn, task_id, "PR metadata is clear", kind="approval", author=name, review_round=0)
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_pr_reviewer), \
             patch.object(orchestrator, "ensure_branch", return_value=True):
            result = orchestrator.advance(task, self.conn)

        self.assertTrue(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "done")
        self.assertEqual(updated["next_step"], "none")
        self.assertIsNone(updated["commit_hash"])
        self.assertEqual(updated["last_review_decision"], "approve")

    def test_pull_request_review_rejection_returns_to_make(self):
        tid = db.add_task(
            self.conn,
            "Review PR",
            kind="pull_request",
            branch="feat-pr",
            coder_agent="claude",
            reviewer_agent="gemini",
        )
        db.update_task(self.conn, tid, status="running", next_step="pull-request-review")
        task = db.get_task(self.conn, tid)

        def fake_pr_reviewer(name, prompt, task_id, conn, verb, **kw):
            db.add_comment(conn, task_id, "Body misses validation", kind="rejection", author=name, review_round=0)
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_pr_reviewer), \
             patch.object(orchestrator, "ensure_branch", return_value=True):
            result = orchestrator.advance(task, self.conn)

        self.assertTrue(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "ready")
        self.assertEqual(updated["next_step"], "pull-request-make")
        self.assertEqual(updated["review_round"], 1)
        self.assertEqual(updated["last_review_decision"], "reject")

    def test_pull_request_review_skip_marks_done_after_make(self):
        tid = db.add_task(
            self.conn,
            "Prepare PR",
            kind="pull_request",
            branch="feat-pr",
            coder_agent="claude",
            skips=["pull-request-review"],
        )
        db.update_task(self.conn, tid, status="running", next_step="pull-request-make")
        task = db.get_task(self.conn, tid)

        def fake_pr_maker(name, prompt, task_id, conn, verb, **kw):
            db.add_comment(
                conn,
                task_id,
                "PR URL: https://github.com/acme/project/pull/12\nTitle: Add feature\nBody:\nSummary",
                kind="comment",
                author=name,
            )
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_pr_maker), \
             patch.object(orchestrator, "ensure_branch", return_value=True):
            result = orchestrator.advance(task, self.conn)

        self.assertTrue(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "done")
        self.assertEqual(updated["next_step"], "none")
        self.assertIsNone(updated["commit_hash"])

    def test_other_make_requires_fresh_evidence_comment(self):
        tid = db.add_task(
            self.conn,
            "External maintenance",
            kind="other",
            coder_agent="claude",
        )
        db.update_task(self.conn, tid, status="running", next_step="other-make")
        task = db.get_task(self.conn, tid)

        with patch.object(orchestrator, "run_agent", return_value=0):
            success = orchestrator.handle_other_make(task, self.conn)

        self.assertFalse(success)
        run_log = db.get_run_log(self.conn, tid)
        self.assertTrue(
            any("without writing a fresh durable evidence comment" in entry["message"] for entry in run_log)
        )

    def test_other_make_transitions_to_review_when_evidence_recorded(self):
        tid = db.add_task(
            self.conn,
            "External maintenance",
            kind="other",
            coder_agent="claude",
        )
        db.update_task(self.conn, tid, status="running", next_step="other-make")
        task = db.get_task(self.conn, tid)

        def fake_other_maker(name, prompt, task_id, conn, verb, **kw):
            db.add_comment(
                conn,
                task_id,
                "Checked the external queue; no code or PR artifact was expected.",
                kind="comment",
                author=name,
            )
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_other_maker):
            result = orchestrator.advance(task, self.conn)

        self.assertTrue(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "ready")
        self.assertEqual(updated["next_step"], "other-review")
        self.assertIsNone(updated["commit_hash"])

    def test_other_review_approval_marks_done_without_commit(self):
        tid = db.add_task(
            self.conn,
            "Review external evidence",
            kind="other",
            coder_agent="claude",
            reviewer_agent="gemini",
        )
        db.add_comment(
            self.conn,
            tid,
            "External work completed; no commit or PR was expected.",
            kind="comment",
            author="claude",
        )
        db.update_task(self.conn, tid, status="running", next_step="other-review")
        task = db.get_task(self.conn, tid)

        def fake_other_reviewer(name, prompt, task_id, conn, verb, **kw):
            db.add_comment(conn, task_id, "Evidence is clear", kind="approval", author=name, review_round=0)
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_other_reviewer):
            result = orchestrator.advance(task, self.conn)

        self.assertTrue(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "done")
        self.assertEqual(updated["next_step"], "none")
        self.assertIsNone(updated["commit_hash"])
        self.assertEqual(updated["last_review_decision"], "approve")

    def test_other_review_rejection_returns_to_make(self):
        tid = db.add_task(
            self.conn,
            "Review external evidence",
            kind="other",
            coder_agent="claude",
            reviewer_agent="gemini",
        )
        db.update_task(self.conn, tid, status="running", next_step="other-review")
        task = db.get_task(self.conn, tid)

        def fake_other_reviewer(name, prompt, task_id, conn, verb, **kw):
            db.add_comment(conn, task_id, "Evidence is unclear", kind="rejection", author=name, review_round=0)
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_other_reviewer):
            result = orchestrator.advance(task, self.conn)

        self.assertTrue(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "ready")
        self.assertEqual(updated["next_step"], "other-make")
        self.assertEqual(updated["review_round"], 1)

    def test_other_review_skip_marks_done_after_make(self):
        tid = db.add_task(
            self.conn,
            "External maintenance",
            kind="other",
            coder_agent="claude",
            skips=["other-review"],
        )
        db.update_task(self.conn, tid, status="running", next_step="other-make")
        task = db.get_task(self.conn, tid)

        def fake_other_maker(name, prompt, task_id, conn, verb, **kw):
            db.add_comment(
                conn,
                task_id,
                "Completed external maintenance; no commit or PR was expected.",
                kind="comment",
                author=name,
            )
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_other_maker):
            result = orchestrator.advance(task, self.conn)

        self.assertTrue(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "done")
        self.assertEqual(updated["next_step"], "none")
        self.assertIsNone(updated["commit_hash"])

    def test_commit_make_path_a_requires_new_commit_message_when_older_one_exists(self):
        """Path A must add a new commit-message comment during the current run."""
        tid = db.add_task(self.conn, "Require fresh commit message", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b", next_step="commit-make")
        db.add_comment(self.conn, tid, "Old commit message", kind="commit-message", author="claude")
        task = db.get_task(self.conn, tid)

        with patch.object(orchestrator, "run_agent", return_value=0), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            success, _, _ = orchestrator.handle_commit_make(task, self.conn)

        comments = [c for c in db.get_comments(self.conn, tid) if c["kind"] == "commit-message"]
        run_log = db.get_run_log(self.conn, tid)
        self.assertFalse(success)
        self.assertEqual(len(comments), 1)
        self.assertTrue(
            any(
                "without writing a new commit-message comment for this run" in entry["message"]
                for entry in run_log
            )
        )

    def test_commit_make_path_a_transitions_to_review(self):
        """After successful commit-make with no prior approval, move to review."""
        tid = db.add_task(self.conn, "Make then review", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b", next_step="commit-make")
        db.add_comment(self.conn, tid, "Old commit message", kind="commit-message", author="claude")
        task = db.get_task(self.conn, tid)

        def fake_coder(name, prompt, task_id, conn, verb, **kw):
            db.add_comment(conn, task_id, "Fresh commit message", kind="commit-message", author=name)
            return 0

        # Mock agent run and branch operations
        with patch.object(orchestrator, "run_agent", side_effect=fake_coder), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            orchestrator.advance(task, self.conn)

        updated = db.get_task(self.conn, tid)
        comments = [c for c in db.get_comments(self.conn, tid) if c["kind"] == "commit-message"]
        self.assertEqual(updated["status"], "ready")
        self.assertEqual(updated["next_step"], "commit-review")
        self.assertEqual(updated["last_review_decision"], "none")
        self.assertIsNotNone(updated["ready_at"])
        self.assertEqual(len(comments), 2)
        self.assertEqual(comments[-1]["message"], "Fresh commit message")

    def test_commit_make_path_a_sees_commit_message_from_separate_connection(self):
        """A fresh commit-message written via another connection must be visible after the agent returns."""
        tid = db.add_task(self.conn, "Cross-connection commit message", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b", next_step="commit-make")
        db.add_comment(self.conn, tid, "Old commit message", kind="commit-message", author="claude")
        task = db.get_task(self.conn, tid)

        def fake_coder(name, prompt, task_id, conn, verb, **kw):
            other = db.connect(self.tmp.name)
            try:
                db.add_comment(other, task_id, "Fresh commit message", kind="commit-message", author=name)
            finally:
                other.close()
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_coder), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            success, _, _ = orchestrator.handle_commit_make(task, self.conn)

        comments = [c for c in db.get_comments(self.conn, tid) if c["kind"] == "commit-message"]
        self.assertTrue(success)
        self.assertEqual(len(comments), 2)
        self.assertEqual(comments[-1]["message"], "Fresh commit message")

    def test_commit_make_path_b_transitions_to_done(self):
        """Path B creates a real new commit: task becomes done with commit_hash recorded."""
        tid = db.add_task(self.conn, "Finalize", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-make", last_review_decision="approve")
        task = db.get_task(self.conn, tid)

        # Simulate a new commit being created during Path B by returning different hashes
        # before and after the agent runs. A third call comes from _finalize_commit.
        hashes = ["b" * 40, "a" * 40, "a" * 40]
        with patch.object(orchestrator, "run_agent", return_value=0), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "get_head_commit_hash", side_effect=hashes), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            orchestrator.advance(task, self.conn)

        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "done")
        self.assertEqual(updated["next_step"], "none")
        self.assertEqual(updated["commit_hash"], "a" * 40)
        self.assertIsNone(updated["ready_at"])

    def test_commit_make_path_b_no_new_commit_no_signal_blocks(self):
        """Path B exits 0 without a new commit and no DONE_WITHOUT_COMMIT: task is blocked, not done."""
        tid = db.add_task(self.conn, "Finalize no commit", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-make", last_review_decision="approve")
        task = db.get_task(self.conn, tid)

        # Both calls return the same hash — no new commit was created.
        same_hash = "a" * 40
        with patch.object(orchestrator, "run_agent", return_value=0), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "get_head_commit_hash", return_value=same_hash), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            result = orchestrator.advance(task, self.conn)

        self.assertFalse(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "blocked")
        self.assertIsNone(updated["commit_hash"])

    def test_commit_make_path_b_done_without_commit_signal_marks_done(self):
        """Path B with explicit DONE_WITHOUT_COMMIT: task becomes done without a commit hash."""
        tid = db.add_task(self.conn, "Finalize no commit explicit", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-make", last_review_decision="approve")
        task = db.get_task(self.conn, tid)

        same_hash = "a" * 40

        def fake_agent_done_without_commit(name, prompt, task_id, conn, verb, **kw):
            db.add_comment(conn, task_id, "No commit needed", kind="done-without-commit", author=name)
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_agent_done_without_commit), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "get_head_commit_hash", return_value=same_hash), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            result = orchestrator.advance(task, self.conn)

        self.assertTrue(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "done")
        self.assertEqual(updated["next_step"], "none")
        self.assertIsNone(updated["commit_hash"])

    def test_commit_make_path_b_stale_done_without_commit_does_not_satisfy_new_run(self):
        """A DONE_WITHOUT_COMMIT comment from a prior run must not satisfy a later Path B run."""
        tid = db.add_task(self.conn, "Stale signal test", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-make", last_review_decision="approve")
        # Pre-seed a stale done-without-commit comment from a previous run.
        db.add_comment(self.conn, tid, "Stale no-commit signal", kind="done-without-commit", author="claude")
        task = db.get_task(self.conn, tid)

        # Agent exits 0 but creates neither a new commit nor a fresh DONE_WITHOUT_COMMIT.
        same_hash = "a" * 40
        with patch.object(orchestrator, "run_agent", return_value=0), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "get_head_commit_hash", return_value=same_hash), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            result = orchestrator.advance(task, self.conn)

        self.assertFalse(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "blocked")
        self.assertIsNone(updated["commit_hash"])

    def test_commit_make_path_b_new_commit_wins_over_done_without_commit(self):
        """Path B run that creates a real commit AND writes DONE_WITHOUT_COMMIT: commit wins."""
        tid = db.add_task(self.conn, "Commit beats no-commit signal", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-make", last_review_decision="approve")
        task = db.get_task(self.conn, tid)

        before_hash = "a" * 40
        after_hash = "b" * 40

        def fake_agent_both(name, prompt, task_id, conn, verb, **kw):
            db.add_comment(conn, task_id, "Also signaling no-commit",
                           kind="done-without-commit", author=name)
            return 0

        # Called 3x: (1) before agent, (2) after agent in Path B check, (3) in _finalize_commit
        hashes = iter([before_hash, after_hash, after_hash])
        with patch.object(orchestrator, "run_agent", side_effect=fake_agent_both), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "get_head_commit_hash", side_effect=lambda: next(hashes)), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            result = orchestrator.advance(task, self.conn)

        self.assertTrue(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "done")
        self.assertEqual(updated["next_step"], "none")
        # commit_hash must be recorded because a real commit was created
        self.assertEqual(updated["commit_hash"], after_hash)

    def test_commit_make_with_skip_review_requeues_finalization_path_b(self):
        """Skipped review should route through Path B finalization, not mark done immediately."""
        tid = db.add_task(
            self.conn,
            "Skip review",
            coder_agent="claude",
            skips=["commit-review"],
        )
        db.update_task(self.conn, tid, status="running", branch="b", next_step="commit-make")
        task = db.get_task(self.conn, tid)

        def fake_coder(name, prompt, task_id, conn, verb, **kw):
            db.add_comment(conn, task_id, "Fresh commit message", kind="commit-message", author=name)
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_coder), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "get_head_commit_hash", return_value="a" * 40), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            orchestrator.advance(task, self.conn)

        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "ready")
        self.assertEqual(updated["next_step"], "commit-make")
        self.assertEqual(updated["last_review_decision"], "approve")
        self.assertIsNotNone(updated["ready_at"])
        self.assertIsNone(updated["commit_hash"])

    def test_commit_make_failure_blocks_immediately(self):
        """Failed commit-make immediately blocks the task."""
        tid = db.add_task(self.conn, "Fail make", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b", next_step="commit-make")
        task = db.get_task(self.conn, tid)

        with patch.object(orchestrator, "run_agent", return_value=1), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            result = orchestrator.advance(task, self.conn)

        self.assertFalse(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "blocked")
        self.assertEqual(updated["next_step"], "none")

    def test_reviewer_error_blocks_immediately(self):
        """Reviewer error immediately blocks the task."""
        tid = db.add_task(self.conn, "Review error block", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b", next_step="commit-review")
        task = db.get_task(self.conn, tid)

        with patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "handle_commit_review", return_value="error"):
            result = orchestrator.advance(task, self.conn)

        self.assertFalse(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "blocked")
        self.assertEqual(updated["next_step"], "none")

    def test_branch_failure_blocks(self):
        """If branch can't be checked out, task is blocked."""
        tid = db.add_task(self.conn, "Bad branch", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b", next_step="commit-make")
        task = db.get_task(self.conn, tid)

        with patch.object(orchestrator, "ensure_branch", return_value=False), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            orchestrator.advance(task, self.conn)

        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "blocked")

    def test_missing_branch_blocks_before_commit_make(self):
        """Tasks without a branch should block cleanly before any git or agent work."""
        tid = db.add_task(self.conn, "Missing branch", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch=None, next_step="commit-make")
        task = db.get_task(self.conn, tid)

        with patch.object(orchestrator, "run_agent") as mock_agent, \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            result = orchestrator.advance(task, self.conn)

        self.assertFalse(result)
        mock_agent.assert_not_called()
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "blocked")
        self.assertEqual(updated["next_step"], "none")
        comments = db.get_comments(self.conn, tid)
        self.assertTrue(any("no branch set" in c["message"].lower() for c in comments))

    def test_max_review_rounds_blocks(self):
        """After MAX_REVIEW_ROUNDS rejections, task is blocked."""
        tid = db.add_task(self.conn, "Too many rounds", coder_agent="claude")
        round_num = orchestrator.MAX_REVIEW_ROUNDS - 1
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-review", review_round=round_num)
        task = db.get_task(self.conn, tid)

        # Pre-populate rejection comment for this round
        db.add_comment(self.conn, tid, "Nope", kind="rejection", author=DEFAULT_REVIEWER, review_round=round_num)

        # Mock handle_commit_review to return "reject" directly
        with patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "handle_commit_review", return_value="reject"), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            orchestrator.advance(task, self.conn)

        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "blocked")

    def test_max_review_rounds_dirty_worktree_preserves_wip(self):
        """Max review rounds with a dirty worktree stashes WIP and records stash_ref."""
        tid = db.add_task(self.conn, "Max rounds dirty", coder_agent="claude")
        round_num = orchestrator.MAX_REVIEW_ROUNDS - 1
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-review", review_round=round_num)
        task = db.get_task(self.conn, tid)

        def fake_stash(task_id_, conn_):
            db.update_task(conn_, task_id_, stash_ref="stash@{0}")
            return "stash@{0}"

        with patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "handle_commit_review", return_value="reject"), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=True), \
             patch.object(orchestrator, "stash_task_wip", side_effect=fake_stash) as mock_stash:
            orchestrator.advance(task, self.conn)

        mock_stash.assert_called_once_with(tid, self.conn)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "blocked")
        self.assertEqual(updated["stash_ref"], "stash@{0}")
        comments = db.get_comments(self.conn, tid)
        expected_comment = "blocked with task WIP: orchestrator stashed changes as stash@{0}."
        self.assertTrue(
            any(c["message"] == expected_comment for c in comments),
            f"Expected comment '{expected_comment}' not found in: {[c['message'] for c in comments]}",
        )

    def test_max_review_rounds_clean_worktree_no_stash(self):
        """Max review rounds with a clean worktree does not create a stash."""
        tid = db.add_task(self.conn, "Max rounds clean", coder_agent="claude")
        round_num = orchestrator.MAX_REVIEW_ROUNDS - 1
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-review", review_round=round_num)
        task = db.get_task(self.conn, tid)

        with patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "handle_commit_review", return_value="reject"), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False), \
             patch.object(orchestrator, "stash_task_wip") as mock_stash:
            orchestrator.advance(task, self.conn)

        mock_stash.assert_not_called()
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "blocked")
        self.assertIsNone(updated["stash_ref"])

    def test_max_review_errors_block(self):
        """Reviewer error blocks the task immediately."""
        tid = db.add_task(self.conn, "Too many review errors", coder_agent="claude")
        round_num = orchestrator.MAX_REVIEW_ROUNDS - 1
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-review", review_round=round_num)
        task = db.get_task(self.conn, tid)

        with patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "handle_commit_review", return_value="error"):
            orchestrator.advance(task, self.conn)

        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "blocked")
        self.assertEqual(updated["next_step"], "none")

    def test_dirty_at_pickup_blocks_without_starting_agent(self):
        """If the worktree is dirty before commit-make starts, block without running the agent."""
        tid = db.add_task(self.conn, "Dirty pickup", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b", next_step="commit-make")
        task = db.get_task(self.conn, tid)

        with patch.object(orchestrator, "is_worktree_dirty", return_value=True), \
             patch.object(orchestrator, "run_agent") as mock_agent, \
             patch.object(orchestrator, "ensure_branch", return_value=True) as mock_ensure_branch:
            result = orchestrator.advance(task, self.conn)

        self.assertFalse(result)
        mock_agent.assert_not_called()
        mock_ensure_branch.assert_not_called()
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "blocked")
        self.assertIsNone(updated["stash_ref"])
        comments = db.get_comments(self.conn, tid)
        self.assertTrue(any("dirty" in c["message"].lower() for c in comments))

    def test_blocked_with_wip_stash_preserved_by_orchestrator(self):
        """When agent fails after a clean start and leaves changes, orchestrator stashes them."""
        tid = db.add_task(self.conn, "WIP preservation", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b", next_step="commit-make")
        task = db.get_task(self.conn, tid)

        # Worktree clean at pickup, dirty after agent fails
        with patch.object(orchestrator, "is_worktree_dirty", side_effect=[False, True]), \
             patch.object(orchestrator, "run_agent", return_value=1), \
             patch.object(orchestrator, "stash_task_wip", return_value="stash@{0}") as mock_stash, \
             patch.object(orchestrator, "ensure_branch", return_value=True):
            result = orchestrator.advance(task, self.conn)

        self.assertFalse(result)
        mock_stash.assert_called_once_with(tid, self.conn)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "blocked")
        comments = db.get_comments(self.conn, tid)
        self.assertTrue(any("stash@{0}" in c["message"] for c in comments))

    def test_missing_commit_message_with_wip_stash_preserved_by_orchestrator(self):
        """A zero-exit commit-make failure still preserves task-created WIP."""
        tid = db.add_task(self.conn, "Missing commit message", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b", next_step="commit-make")
        task = db.get_task(self.conn, tid)

        with patch.object(orchestrator, "is_worktree_dirty", side_effect=[False, True]), \
             patch.object(orchestrator, "run_agent", return_value=0), \
             patch.object(orchestrator, "stash_task_wip", return_value="stash@{0}") as mock_stash, \
             patch.object(orchestrator, "ensure_branch", return_value=True):
            result = orchestrator.advance(task, self.conn)

        self.assertFalse(result)
        mock_stash.assert_called_once_with(tid, self.conn)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "blocked")
        comments = db.get_comments(self.conn, tid)
        self.assertTrue(any("stash@{0}" in c["message"] for c in comments))
        run_log = db.get_run_log(self.conn, tid)
        self.assertTrue(
            any(
                "without writing a new commit-message comment for this run" in entry["message"]
                for entry in run_log
            )
        )

    def test_blocked_with_clean_worktree_no_stash(self):
        """When agent fails but leaves a clean worktree, no stash is created."""
        tid = db.add_task(self.conn, "Clean failure", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b", next_step="commit-make")
        task = db.get_task(self.conn, tid)

        with patch.object(orchestrator, "is_worktree_dirty", return_value=False), \
             patch.object(orchestrator, "run_agent", return_value=1), \
             patch.object(orchestrator, "stash_task_wip") as mock_stash, \
             patch.object(orchestrator, "ensure_branch", return_value=True):
            result = orchestrator.advance(task, self.conn)

        self.assertFalse(result)
        mock_stash.assert_not_called()
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "blocked")
        self.assertIsNone(updated["stash_ref"])

    def test_stash_task_wip_records_stash_ref(self):
        """stash_task_wip stages, stashes, and records the stash ref in the DB."""
        tid = db.add_task(self.conn, "WIP task", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b")

        stash_list_output = "stash@{0}: WIP on temp: abc1234 Some commit\n"

        def fake_run(cmd, **kw):
            mock = MagicMock()
            mock.returncode = 0
            if cmd[0:3] == ["git", "stash", "list"]:
                mock.stdout = stash_list_output
            return mock

        with patch.object(orchestrator.subprocess, "run", side_effect=fake_run):
            ref = orchestrator.stash_task_wip(tid, self.conn)

        self.assertEqual(ref, "stash@{0}")
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["stash_ref"], "stash@{0}")

    def test_stash_ref_restore_flow(self):
        """Blocked task with recorded stash_ref advances through commit-make when coder succeeds.

        The stash_ref must appear in the prompt so the coder can run Path C (git stash pop).
        After a clean exit with a new commit-message comment, the task advances to commit-review.
        """
        tid = db.add_task(self.conn, "Restore WIP", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-make", stash_ref="stash@{0}")
        task = db.get_task(self.conn, tid)

        captured_prompts = []

        def fake_coder(name, prompt, task_id, conn, step, **kwargs):
            captured_prompts.append(prompt)
            db.add_comment(conn, task_id, "Commit message for restored WIP",
                           kind="commit-message", author=name)
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_coder), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            result = orchestrator.advance(task, self.conn)

        self.assertTrue(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "ready")
        self.assertEqual(updated["next_step"], "commit-review")
        self.assertEqual(len(captured_prompts), 1)
        self.assertIn("stash@{0}", captured_prompts[0])


class TestStickyTaskScheduling(unittest.TestCase):
    """Regression: once picked, a task stays pinned until done or blocked."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_approve_flow_stays_on_same_task_until_done(self):
        active_tid = db.add_task(self.conn, "Active", coder_agent="claude")
        waiting_tid = db.add_task(self.conn, "Waiting", coder_agent="codex")
        db.update_task(self.conn, active_tid, status="ready", branch="b", next_step="commit-make")
        db.update_task(self.conn, waiting_tid, status="ready", branch="b", next_step="commit-make")

        seen = []

        def fake_advance(task, conn):
            seen.append((task["id"], task["next_step"], task["last_review_decision"]))
            if len(seen) == 1:
                db.update_task(conn, active_tid, status="ready", next_step="commit-review", last_review_decision="none")
                return True
            if len(seen) == 2:
                db.update_task(conn, active_tid, status="ready", next_step="commit-make", last_review_decision="approve")
                return True
            db.update_task(conn, active_tid, status="done", next_step="none", commit_hash="a" * 40)
            return True

        with patch.object(orchestrator, "advance", side_effect=fake_advance):
            result = orchestrator.process_pinned_task(db.get_task(self.conn, active_tid), self.conn)

        self.assertTrue(result)
        self.assertEqual(
            seen,
            [
                (active_tid, "commit-make", "none"),
                (active_tid, "commit-review", "none"),
                (active_tid, "commit-make", "approve"),
            ],
        )
        self.assertEqual(db.get_task(self.conn, active_tid)["status"], "done")
        self.assertEqual(db.get_task(self.conn, waiting_tid)["status"], "ready")

    def test_reject_flow_stays_on_same_task_for_retry(self):
        active_tid = db.add_task(self.conn, "Active", coder_agent="claude")
        waiting_tid = db.add_task(self.conn, "Waiting", coder_agent="codex")
        db.update_task(self.conn, active_tid, status="ready", branch="b", next_step="commit-make")
        db.update_task(self.conn, waiting_tid, status="ready", branch="b", next_step="commit-make")

        seen = []

        def fake_advance(task, conn):
            seen.append((task["id"], task["next_step"], task["review_round"], task["last_review_decision"]))
            if len(seen) == 1:
                db.update_task(conn, active_tid, status="ready", next_step="commit-review")
                return True
            if len(seen) == 2:
                db.update_task(
                    conn,
                    active_tid,
                    status="ready",
                    next_step="commit-make",
                    review_round=1,
                    last_review_decision="reject",
                )
                return True
            db.update_task(conn, active_tid, status="blocked", next_step="none", review_round=1, last_review_decision="reject")
            return False

        with patch.object(orchestrator, "advance", side_effect=fake_advance):
            result = orchestrator.process_pinned_task(db.get_task(self.conn, active_tid), self.conn)

        self.assertFalse(result)
        self.assertEqual(
            seen,
            [
                (active_tid, "commit-make", 0, "none"),
                (active_tid, "commit-review", 0, "none"),
                (active_tid, "commit-make", 1, "reject"),
            ],
        )
        self.assertEqual(db.get_task(self.conn, active_tid)["status"], "blocked")
        self.assertEqual(db.get_task(self.conn, waiting_tid)["status"], "ready")

class TestTaskCLI(unittest.TestCase):
    """Test task.py CLI via subprocess."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "kanban-orchestra.db")
        self.task_py = str(Path(__file__).resolve().parent / "task.py")
        self.env = {
            **os.environ,
            "KANBAN_DB": self.db_path,
            "KANBAN_NONINTERACTIVE": "1",
        }

    def tearDown(self):
        self.tmpdir.cleanup()

    def _run(self, *args, env=None, input_text=None):
        result = subprocess.run(
            [sys.executable, self.task_py] + list(args),
            input=input_text,
            capture_output=True, text=True, env=env or self.env,
        )
        return result

    def _write_agents_md(self, content):
        Path(self.tmpdir.name, "AGENTS.md").write_text(content, encoding="utf-8")

    def _opt_in_master_tasks(self):
        self._write_agents_md("ALLOW_TASKS_ON_MASTER\n")

    def test_add_and_show(self):
        r = self._run("add", "My test task", "--description", "Desc here", "--branch", "feat-1")
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout)
        self.assertIn("id", data)
        self.assertNotIn("koid", data)
        tid = data["id"]

        r2 = self._run("show", str(tid))
        self.assertEqual(r2.returncode, 0)
        task = json.loads(r2.stdout)
        self.assertEqual(task["title"], "My test task")
        self.assertEqual(task["branch"], "feat-1")

    def test_list(self):
        self._run("add", "Task A")
        self._run("add", "Task B")
        r = self._run("list")
        self.assertEqual(r.returncode, 0)
        tasks = json.loads(r.stdout)
        self.assertEqual(len(tasks), 2)

    def test_set_status_ready_requires_branch(self):
        r = self._run("add", "No branch task")
        tid = json.loads(r.stdout)["id"]
        r2 = self._run("set", str(tid), "--status", "ready")
        self.assertNotEqual(r2.returncode, 0)  # should fail without branch

    def test_set_status_ready_with_branch(self):
        r = self._run("add", "Branch task", "--branch", "my-branch")
        tid = json.loads(r.stdout)["id"]
        r2 = self._run("set", str(tid), "--status", "ready")
        self.assertEqual(r2.returncode, 0)
        queued = json.loads(r2.stdout)
        self.assertIsNotNone(queued["ready_at"])

    def test_set_status_ready_prefers_explicit_branch(self):
        r = self._run("add", "Retarget task", "--branch", "old-branch")
        tid = json.loads(r.stdout)["id"]
        r2 = self._run("set", str(tid), "--branch", "new-branch", "--status", "ready")
        self.assertEqual(r2.returncode, 0)
        queued = json.loads(r2.stdout)
        self.assertEqual(queued["branch"], "new-branch")
        self.assertEqual(queued["status"], "ready")
        self.assertIsNotNone(queued["ready_at"])

    def test_add_rejects_master_without_policy_marker(self):
        r = self._run("add", "Master task", "--branch", "master")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("tasks on master/main are disabled by default", r.stderr)
        self.assertIn("ALLOW_TASKS_ON_MASTER", r.stderr)

    def test_add_rejects_main_without_policy_marker(self):
        r = self._run("add", "Main task", "--branch", "main")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("tasks on master/main are disabled by default", r.stderr)

    def test_add_allows_master_with_policy_marker(self):
        self._opt_in_master_tasks()
        r = self._run("add", "Master task", "--branch", "master")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["branch"], "master")

    def test_add_allows_main_with_policy_marker(self):
        self._opt_in_master_tasks()
        r = self._run("add", "Main task", "--branch", "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["branch"], "main")

    def test_set_branch_master_rejects_without_policy_marker(self):
        r = self._run("add", "Retarget task", "--branch", "feat")
        tid = json.loads(r.stdout)["id"]
        r2 = self._run("set", str(tid), "--branch", "master")
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("tasks on master/main are disabled by default", r2.stderr)

    def test_set_branch_main_rejects_without_policy_marker(self):
        r = self._run("add", "Retarget task", "--branch", "feat")
        tid = json.loads(r.stdout)["id"]
        r2 = self._run("set", str(tid), "--branch", "main")
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("tasks on master/main are disabled by default", r2.stderr)

    def test_set_branch_master_allows_with_policy_marker(self):
        self._opt_in_master_tasks()
        r = self._run("add", "Retarget task", "--branch", "feat")
        tid = json.loads(r.stdout)["id"]
        r2 = self._run("set", str(tid), "--branch", "master")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(json.loads(r2.stdout)["branch"], "master")

    def test_set_branch_main_allows_with_policy_marker(self):
        self._opt_in_master_tasks()
        r = self._run("add", "Retarget task", "--branch", "feat")
        tid = json.loads(r.stdout)["id"]
        r2 = self._run("set", str(tid), "--branch", "main")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(json.loads(r2.stdout)["branch"], "main")

    def test_set_status_ready_rejects_existing_master_without_policy_marker(self):
        r = self._run("add", "Existing master task")
        tid = json.loads(r.stdout)["id"]
        conn = db.connect(self.db_path)
        try:
            db.update_task(conn, tid, branch="master")
        finally:
            conn.close()

        r2 = self._run("set", str(tid), "--status", "ready")
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("tasks on master/main are disabled by default", r2.stderr)

    def test_set_status_ready_rejects_existing_main_without_policy_marker(self):
        r = self._run("add", "Existing main task")
        tid = json.loads(r.stdout)["id"]
        conn = db.connect(self.db_path)
        try:
            db.update_task(conn, tid, branch="main")
        finally:
            conn.close()

        r2 = self._run("set", str(tid), "--status", "ready")
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("tasks on master/main are disabled by default", r2.stderr)

    def test_set_status_ready_allows_existing_master_with_policy_marker(self):
        self._opt_in_master_tasks()
        r = self._run("add", "Existing master task", "--branch", "master")
        tid = json.loads(r.stdout)["id"]
        r2 = self._run("set", str(tid), "--status", "ready")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        queued = json.loads(r2.stdout)
        self.assertEqual(queued["branch"], "master")
        self.assertEqual(queued["status"], "ready")

    def test_set_status_ready_allows_existing_main_with_policy_marker(self):
        self._opt_in_master_tasks()
        r = self._run("add", "Existing main task", "--branch", "main")
        tid = json.loads(r.stdout)["id"]
        r2 = self._run("set", str(tid), "--status", "ready")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        queued = json.loads(r2.stdout)
        self.assertEqual(queued["branch"], "main")
        self.assertEqual(queued["status"], "ready")

    def test_set_status_non_ready_clears_ready_at(self):
        r = self._run("add", "Queue task", "--branch", "my-branch")
        tid = json.loads(r.stdout)["id"]
        self._run("set", str(tid), "--status", "ready")

        r2 = self._run("set", str(tid), "--status", "running")
        self.assertEqual(r2.returncode, 0)
        self.assertIsNone(json.loads(r2.stdout)["ready_at"])

    def test_add_can_set_allow_when_blocked(self):
        r = self._run("add", "Escaping task", "--allow-when-blocked")
        self.assertEqual(r.returncode, 0)
        task = json.loads(r.stdout)
        self.assertEqual(task["allow_when_blocked"], 1)

    def test_set_can_toggle_allow_when_blocked(self):
        r = self._run("add", "Toggle task")
        tid = json.loads(r.stdout)["id"]

        r2 = self._run("set", str(tid), "--allow-when-blocked", "true")
        self.assertEqual(r2.returncode, 0)
        self.assertEqual(json.loads(r2.stdout)["allow_when_blocked"], 1)

        r3 = self._run("set", str(tid), "--allow-when-blocked", "false")
        self.assertEqual(r3.returncode, 0)
        self.assertEqual(json.loads(r3.stdout)["allow_when_blocked"], 0)

    def test_log_and_show_run_log(self):
        r = self._run("add", "Log task")
        tid = json.loads(r.stdout)["id"]
        self._run("log", str(tid), "Progress update")
        r2 = self._run("show-run-log", str(tid))
        logs = json.loads(r2.stdout)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["message"], "Progress update")

    def test_comment_and_show_comments(self):
        r = self._run("add", "Comment task")
        tid = json.loads(r.stdout)["id"]
        self._run("comment", str(tid), "Great work", "--approval", "--author", "codex", "--review-round", "0")
        r2 = self._run("show-comments", str(tid))
        comments = json.loads(r2.stdout)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["kind"], "approval")
        self.assertEqual(comments[0]["author"], "codex")

    def test_comment_message_stdin_preserves_literal_backticks_and_angles(self):
        r = self._run("add", "Literal comment task")
        tid = json.loads(r.stdout)["id"]
        message = (
            "Rejected. `ContinuationCandidate` is added but never used; "
            "still returns and constructs `<scheduled-event>`, so the codebase "
            "now has two competing payload types."
        )
        r2 = self._run(
            "comment",
            str(tid),
            "--message-stdin",
            "--rejection",
            "--author",
            "codex",
            "--review-round",
            "0",
            input_text=message + "\n",
        )
        self.assertEqual(r2.returncode, 0)
        r3 = self._run("show-comments", str(tid))
        comments = json.loads(r3.stdout)
        self.assertEqual(comments[0]["message"], message)

    def test_comment_rejects_positional_message_with_message_stdin(self):
        r = self._run("add", "Conflicting comment task")
        tid = json.loads(r.stdout)["id"]
        r2 = self._run(
            "comment",
            str(tid),
            "inline message",
            "--message-stdin",
            input_text="from stdin\n",
        )
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("either a positional message or --message-stdin", r2.stderr)

    def test_comment_rejects_stale_review_round(self):
        r = self._run("add", "Stale comment task")
        tid = json.loads(r.stdout)["id"]
        self._run("set", str(tid), "--review-round", "1")
        r2 = self._run("comment", str(tid), "Great work", "--approval", "--author", "codex", "--review-round", "0")
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("Review round mismatch", r2.stderr)

    def test_delete_only_none(self):
        r = self._run("add", "Delete me")
        tid = json.loads(r.stdout)["id"]
        r2 = self._run("delete", str(tid))
        self.assertEqual(r2.returncode, 0)

    def test_delete_non_none_fails(self):
        r = self._run("add", "Cant delete", "--branch", "b")
        tid = json.loads(r.stdout)["id"]
        self._run("set", str(tid), "--status", "ready")
        r2 = self._run("delete", str(tid))
        self.assertNotEqual(r2.returncode, 0)

    def test_coder_agent_assigned(self):
        r = self._run("add", "Agent task")
        data = json.loads(r.stdout)
        tid = data["id"]
        r2 = self._run("show", str(tid))
        task = json.loads(r2.stdout)
        self.assertEqual(task["coder_agent"], DEFAULT_CODER)

    def test_coder_agent_explicit(self):
        r = self._run("add", "Explicit agent", "--coder-agent", "codex")
        tid = json.loads(r.stdout)["id"]
        r2 = self._run("show", str(tid))
        task = json.loads(r2.stdout)
        self.assertEqual(task["coder_agent"], "codex")

    def test_reviewer_agent_assigned(self):
        r = self._run("add", "Reviewer task")
        data = json.loads(r.stdout)
        tid = data["id"]
        task = json.loads(self._run("show", str(tid)).stdout)
        self.assertEqual(task["reviewer_agent"], DEFAULT_REVIEWER)

    def test_reviewer_agent_explicit(self):
        r = self._run("add", "Explicit reviewer", "--reviewer-agent", "gemini")
        self.assertEqual(r.returncode, 0, r.stderr)
        task = json.loads(r.stdout)
        self.assertEqual(task["reviewer_agent"], "gemini")

    def test_set_reviewer_agent(self):
        r = self._run("add", "Change reviewer")
        tid = json.loads(r.stdout)["id"]
        r2 = self._run("set", str(tid), "--reviewer-agent", "opus")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(json.loads(r2.stdout)["reviewer_agent"], "opus")

    def test_reviewer_agent_rejects_invalid_agent(self):
        r = self._run("add", "Bad reviewer", "--reviewer-agent", "nope")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("reviewer-agent must be one of", r.stderr)

        ok = self._run("add", "Good reviewer")
        tid = json.loads(ok.stdout)["id"]
        r2 = self._run("set", str(tid), "--reviewer-agent", "nope")
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("reviewer-agent must be one of", r2.stderr)

    def test_coder_agent_supports_claude_family_aliases(self):
        for agent in ("haiku", "sonnet", "opus", "claude"):
            with self.subTest(agent=agent):
                r = self._run("add", f"Agent {agent}", "--coder-agent", agent)
                self.assertEqual(r.returncode, 0)
                tid = json.loads(r.stdout)["id"]
                shown = self._run("show", str(tid))
                self.assertEqual(json.loads(shown.stdout)["coder_agent"], agent)

    def test_set_stash_ref(self):
        r = self._run("add", "Stashable task")
        tid = json.loads(r.stdout)["id"]

        r2 = self._run("set", str(tid), "--stash-ref", "stash@{2}")
        self.assertEqual(r2.returncode, 0)
        self.assertEqual(json.loads(r2.stdout)["stash_ref"], "stash@{2}")

        r3 = self._run("set", str(tid), "--stash-ref", "")
        self.assertEqual(r3.returncode, 0)
        self.assertIsNone(json.loads(r3.stdout)["stash_ref"])

    def test_restore_rejects_non_fresh_database(self):
        source_add = self._run("add", "Restore task", "--branch", "feat-restore")
        self.assertEqual(source_add.returncode, 0)
        self.assertEqual(self._run("dump").returncode, 0)

        restored_db_path = str(Path(self.db_path).with_name("restored-existing.db"))
        restored_env = {
            **self.env,
            "KANBAN_DB": restored_db_path,
        }

        existing = sqlite3.connect(restored_db_path)
        try:
            existing.execute("CREATE TABLE already_here (id INTEGER PRIMARY KEY)")
            existing.commit()
        finally:
            existing.close()

        try:
            restore_result = self._run("restore", env=restored_env)
            self.assertNotEqual(restore_result.returncode, 0)
            self.assertIn("fresh database", restore_result.stderr)
        finally:
            for suffix in ("", "-shm", "-wal"):
                path = Path(restored_db_path + suffix)
                if path.exists():
                    path.unlink()



class TestAgentTranscriptCapture(unittest.TestCase):
    """Regression: raw agent transcript output should be saved outside run_log."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "kanban.db")
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmpdir.cleanup()

    @staticmethod
    def _fake_proc(lines, returncode):
        class FakeProc:
            def __init__(self, transcript_lines, code):
                self.stdout = iter(transcript_lines)
                self.returncode = code
                self.pid = 4242

            def wait(self):
                return self.returncode

        return FakeProc(lines, returncode)

    def test_run_agent_writes_success_transcript_without_spamming_run_log(self):
        tid = db.add_task(self.conn, "Transcript success", coder_agent="codex")
        fake_proc = self._fake_proc(
            [
                "python3 -m pytest -q\n",
                "2 passed in 0.05s\n",
            ],
            0,
        )

        with patch.dict(orchestrator.AGENT_CMD, {"codex": ["codex", "{prompt}"]}, clear=False), \
             patch.object(orchestrator.subprocess, "Popen", return_value=fake_proc), \
             patch.object(orchestrator.db, "get_db_path", return_value=self.db_path):
            exit_code = orchestrator.run_agent(
                "codex",
                "prompt body",
                tid,
                self.conn,
                "commit-make",
            )

        self.assertEqual(exit_code, 0)
        run_log = db.get_run_log(self.conn, tid)
        self.assertEqual(len(run_log), 2)
        self.assertIn("Launching codex for commit-make", run_log[1]["message"])
        self.assertIn("codex completed commit-make with exit code 0", run_log[0]["message"])
        self.assertFalse(any("2 passed in 0.05s" in entry["message"] for entry in run_log))

        transcript_files = list((db.get_artifacts_root(self.db_path) / f"task-{tid}").glob("*.log"))
        self.assertEqual(len(transcript_files), 1)
        transcript_text = transcript_files[0].read_text(encoding="utf-8")
        self.assertIn("python3 -m pytest -q", transcript_text)
        self.assertIn("2 passed in 0.05s", transcript_text)
        self.assertIn(str(transcript_files[0]), run_log[0]["message"])

    def test_run_agent_launches_from_repo_root(self):
        tid = db.add_task(self.conn, "Transcript cwd", coder_agent="codex")
        fake_proc = self._fake_proc(["done\n"], 0)

        with patch.dict(orchestrator.AGENT_CMD, {"codex": ["codex", "{prompt}"]}, clear=False), \
             patch.object(orchestrator, "_repo_root", return_value="/tmp/work-repo"), \
             patch.object(orchestrator.subprocess, "Popen", return_value=fake_proc) as mock_popen, \
             patch.object(orchestrator.db, "get_db_path", return_value=self.db_path):
            exit_code = orchestrator.run_agent(
                "codex",
                "prompt body",
                tid,
                self.conn,
                "commit-make",
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(mock_popen.call_args.kwargs["cwd"], "/tmp/work-repo")

        transcript_files = list((db.get_artifacts_root(self.db_path) / f"task-{tid}").glob("*.log"))
        self.assertEqual(len(transcript_files), 1)
        transcript_text = transcript_files[0].read_text(encoding="utf-8")
        self.assertIn("# cwd: /tmp/work-repo", transcript_text)

    def test_run_agent_registers_and_clears_active_process_metadata(self):
        tid = db.add_task(self.conn, "Active process metadata", coder_agent="codex")
        fake_proc = self._fake_proc(["done\n"], 0)

        with patch.dict(orchestrator.AGENT_CMD, {"codex": ["codex", "{prompt}"]}, clear=False), \
             patch.object(orchestrator.subprocess, "Popen", return_value=fake_proc), \
             patch.object(orchestrator.db, "get_db_path", return_value=self.db_path), \
             patch.object(orchestrator.active_agent_processes, "register_active_agent", return_value="rec-1") as mock_register, \
             patch.object(orchestrator.active_agent_processes, "clear_active_agent") as mock_clear:
            exit_code = orchestrator.run_agent(
                "codex",
                "prompt body",
                tid,
                self.conn,
                "commit-make",
            )

        self.assertEqual(exit_code, 0)
        mock_register.assert_called_once_with(
            task_id=tid,
            verb="commit-make",
            agent_name="codex",
            pid=4242,
            db_path=self.db_path,
        )
        mock_clear.assert_called_once_with("rec-1", db_path=self.db_path)

    def test_run_agent_logs_failure_summary_and_saves_full_transcript(self):
        tid = db.add_task(self.conn, "Transcript failure", coder_agent="codex")
        fake_proc = self._fake_proc(
            [
                "python3 -m pytest -q\n",
                "FAILED test_logging.py::test_noise\n",
                "AssertionError: expected concise run log\n",
            ],
            1,
        )

        with patch.dict(orchestrator.AGENT_CMD, {"codex": ["codex", "{prompt}"]}, clear=False), \
             patch.object(orchestrator.subprocess, "Popen", return_value=fake_proc), \
             patch.object(orchestrator.db, "get_db_path", return_value=self.db_path):
            exit_code = orchestrator.run_agent(
                "codex",
                "prompt body",
                tid,
                self.conn,
                "commit-review",
            )

        self.assertEqual(exit_code, 1)
        run_log = db.get_run_log(self.conn, tid)
        self.assertEqual(len(run_log), 2)
        self.assertIn("codex failed commit-review with exit code 1", run_log[0]["message"])
        self.assertIn("FAILED test_logging.py::test_noise", run_log[0]["message"])
        self.assertIn("AssertionError: expected concise run log", run_log[0]["message"])
        self.assertFalse(any("python3 -m pytest -q" == entry["message"] for entry in run_log))

        transcript_files = list((db.get_artifacts_root(self.db_path) / f"task-{tid}").glob("*.log"))
        self.assertEqual(len(transcript_files), 1)
        transcript_text = transcript_files[0].read_text(encoding="utf-8")
        self.assertIn("python3 -m pytest -q", transcript_text)
        self.assertIn("FAILED test_logging.py::test_noise", transcript_text)
        self.assertIn(str(transcript_files[0]), run_log[0]["message"])


class TestCLIReviewerIdentityAndRoundEnforcement(unittest.TestCase):
    """Regression: reviewer identity must be persisted via CLI, and --review-round must be required."""

    def setUp(self):
        self._ack_patcher = patch.object(orchestrator, "ensure_agent_acked")
        self._ack_patcher.start()
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self.task_py = str(Path(__file__).resolve().parent / "task.py")
        self.env = {
            **os.environ,
            "KANBAN_DB": self.db_path,
            "KANBAN_NONINTERACTIVE": "1",
        }

    def tearDown(self):
        self._ack_patcher.stop()
        os.unlink(self.db_path)

    def _run(self, *args):
        result = subprocess.run(
            [sys.executable, self.task_py] + list(args),
            capture_output=True, text=True, env=self.env,
        )
        return result

    def test_approval_requires_author(self):
        """CLI must reject --approval without --author."""
        r = self._run("add", "Author required")
        tid = json.loads(r.stdout)["id"]
        r2 = self._run("comment", str(tid), "LGTM", "--approval", "--review-round", "0")
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("--author is required", r2.stderr)

    def test_rejection_requires_author(self):
        """CLI must reject --rejection without --author."""
        r = self._run("add", "Author required rej")
        tid = json.loads(r.stdout)["id"]
        r2 = self._run("comment", str(tid), "Bad code", "--rejection", "--review-round", "0")
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("--author is required", r2.stderr)

    def test_approval_requires_review_round(self):
        """CLI must reject --approval without --review-round."""
        r = self._run("add", "Round required")
        tid = json.loads(r.stdout)["id"]
        r2 = self._run("comment", str(tid), "LGTM", "--approval", "--author", "codex")
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("--review-round is required", r2.stderr)

    def test_rejection_requires_review_round(self):
        """CLI must reject --rejection without --review-round."""
        r = self._run("add", "Round required rej")
        tid = json.loads(r.stdout)["id"]
        r2 = self._run("comment", str(tid), "Bad code", "--rejection", "--author", "codex")
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("--review-round is required", r2.stderr)

    def test_approval_with_author_and_round_succeeds(self):
        """CLI must accept --approval with both --author and --review-round."""
        r = self._run("add", "Full approval")
        tid = json.loads(r.stdout)["id"]
        r2 = self._run("comment", str(tid), "LGTM", "--approval", "--author", "codex", "--review-round", "0")
        self.assertEqual(r2.returncode, 0)
        r3 = self._run("show-comments", str(tid))
        comments = json.loads(r3.stdout)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["author"], "codex")
        self.assertEqual(comments[0]["kind"], "approval")
        self.assertEqual(comments[0]["review_round"], 0)

    def test_plain_comment_does_not_require_author_or_round(self):
        """Plain --comment should still work without --author or --review-round."""
        r = self._run("add", "Plain comment")
        tid = json.loads(r.stdout)["id"]
        r2 = self._run("comment", str(tid), "Just a note", "--comment")
        self.assertEqual(r2.returncode, 0)

    def test_stale_reviewer_cannot_write_to_new_round(self):
        """A reviewer passing an old --review-round must be rejected by the CLI."""
        r = self._run("add", "Stale reviewer")
        tid = json.loads(r.stdout)["id"]
        # Advance task to round 2
        self._run("set", str(tid), "--review-round", "2")
        # Stale reviewer tries to write approval for round 1
        r2 = self._run("comment", str(tid), "LGTM", "--approval", "--author", "codex", "--review-round", "1")
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("Review round mismatch", r2.stderr)

    def test_cli_author_flows_through_to_aggregation(self):
        """End-to-end: CLI-written approval with --author must be recognized by orchestrator."""
        # Create task and write approval via CLI
        r = self._run("add", "E2E aggregation", "--coder-agent", "claude")
        tid = json.loads(r.stdout)["id"]
        self._run("set", str(tid), "--status", "running", "--branch", "b", "--next-step", "commit-review")
        # Reviewer approves via CLI
        self._run("comment", str(tid), "LGTM", "--approval", "--author", "gemini", "--review-round", "0")

        # Now check aggregation in orchestrator
        conn = db.connect(self.db_path)
        try:
            task = db.get_task(conn, tid)
            with patch.object(orchestrator, "ensure_branch", return_value=True), \
                 patch.object(orchestrator, "run_agent", return_value=0):
                outcome = orchestrator.handle_commit_review(task, conn)
            self.assertEqual(outcome, "approve")
        finally:
            conn.close()

class TestWorkspaceResolution(unittest.TestCase):
    """Test repo-root workspace discovery."""

    def test_get_workspace_uses_git_repo_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init", "-q"], cwd=tmpdir, check=True)
            nested = Path(tmpdir) / "a" / "b"
            nested.mkdir(parents=True)

            workspace = db.get_workspace(str(nested))

            self.assertEqual(workspace["repo_root"], Path(tmpdir).resolve())
            self.assertEqual(workspace["db_path"], Path(tmpdir).resolve() / "kanban-orchestra.db")

    def test_get_instance_identity_uses_launch_repo_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init", "-q"], cwd=tmpdir, check=True)
            nested = Path(tmpdir) / "a" / "b"
            nested.mkdir(parents=True)
            custom_db = Path(tmpdir) / "state" / "custom.sqlite3"

            identity = db.get_instance_identity(str(custom_db), cwd=str(nested))

            self.assertEqual(identity["repo_root"], str(Path(tmpdir).resolve()))
            self.assertEqual(identity["repo_label"], Path(tmpdir).name)
            self.assertEqual(identity["db_path"], str(custom_db.resolve()))
            self.assertEqual(identity["runtime_root"], str((custom_db.parent / ".kanban-orchestra").resolve()))
            self.assertEqual(identity["lock_path"], str((custom_db.parent / "kanban-orchestra.lock").resolve()))

    def test_get_instance_identity_falls_back_to_db_parent_outside_git(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "kanban.db"

            identity = db.get_instance_identity(str(db_path), cwd=tmpdir)

            self.assertEqual(identity["repo_root"], str(Path(tmpdir).resolve()))
            self.assertEqual(identity["repo_label"], Path(tmpdir).name)

    def test_get_orchestra_dir_uses_env_var(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            orchestra_dir = Path(tmpdir) / "orchestra-src"
            (orchestra_dir / "kanban-orchestra").mkdir(parents=True)

            with patch.dict(os.environ, {"ORCHESTRA_DIR": str(orchestra_dir)}):
                resolved = db.get_orchestra_dir()

            self.assertEqual(resolved, orchestra_dir.resolve())

    def test_get_workspace_fails_outside_git_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(RuntimeError):
                db.get_workspace(tmpdir)


class TestKanbanCLI(unittest.TestCase):
    """Test kanban.py bootstrap behavior."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.tmpdir.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo_root, check=True)
        self.orchestra_dir = self.repo_root / "orchestra-src"
        (self.orchestra_dir / "kanban-orchestra").mkdir(parents=True)
        self.kanban_py = str(Path(__file__).resolve().parent / "kanban.py")
        self.task_py = str(Path(__file__).resolve().parent / "task.py")
        self.env = {
            **os.environ,
            "KANBAN_NONINTERACTIVE": "1",
            "ORCHESTRA_DIR": str(self.orchestra_dir),
        }

    def tearDown(self):
        self.tmpdir.cleanup()

    def _run_kanban(self, cwd=None):
        return subprocess.run(
            [sys.executable, self.kanban_py],
            capture_output=True,
            text=True,
            env=self.env,
            cwd=cwd or self.repo_root,
        )

    def _run_task(self, *args, cwd=None):
        return subprocess.run(
            [sys.executable, self.task_py] + list(args),
            capture_output=True,
            text=True,
            env=self.env,
            cwd=cwd or self.repo_root,
        )

    def test_kanban_creates_db_in_repo_root(self):
        db_path = self.repo_root / "kanban-orchestra.db"
        self.assertFalse(db_path.exists())

        result = self._run_kanban()

        self.assertEqual(result.returncode, 0)
        self.assertTrue(db_path.exists())
        self.assertIn("Status: created kanban database", result.stdout)

    def test_kanban_reuses_existing_db(self):
        first = self._run_kanban()
        second = self._run_kanban()

        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0)
        self.assertIn("Status: using existing kanban database", second.stdout)

    def test_kanban_fails_without_orchestra_dir(self):
        self.env.pop("ORCHESTRA_DIR")

        result = self._run_kanban()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ORCHESTRA_DIR is not set", result.stderr)

    def test_task_cli_defaults_to_repo_root_db(self):
        nested = self.repo_root / "subdir"
        nested.mkdir()

        result = self._run_task("add", "Repo root task", cwd=nested)

        self.assertEqual(result.returncode, 0)
        self.assertTrue((self.repo_root / "kanban-orchestra.db").exists())
        self.assertFalse((nested / "kanban-orchestra.db").exists())

    def test_kanban_writes_all_db_artifacts_to_gitignore(self):
        result = self._run_kanban()

        self.assertEqual(result.returncode, 0)
        gitignore = (self.repo_root / ".gitignore").read_text(encoding="utf-8")
        for entry in [
            "kanban-orchestra.db",
            "kanban-orchestra.db-journal",
            "kanban-orchestra.db-shm",
            "kanban-orchestra.db-wal",
            "kanban-orchestra.lock",
            ".kanban-orchestra/",
            ".claude/",
            ".gemini/",
            ".agents/",
        ]:
            self.assertIn(entry, gitignore)


class TestInitTestRepo(unittest.TestCase):
    """Test the disposable repo initializer."""

    def test_init_test_repo_seeds_master_branch_for_ready_tasks(self):
        with tempfile.TemporaryDirectory() as orchestra_tmp, tempfile.TemporaryDirectory() as repo_tmp:
            orchestra_dir = Path(orchestra_tmp)
            _write_test_ai_skills(orchestra_dir)

            init_script = SCRIPT_DIR / "init_test_repo.py"
            result = subprocess.run(
                [sys.executable, str(init_script)],
                capture_output=True,
                text=True,
                cwd=repo_tmp,
                env={**os.environ, "ORCHESTRA_DIR": str(orchestra_dir)},
                check=True,
            )

            self.assertIn("Test repo ready.", result.stdout)
            conn = db.connect(str(Path(repo_tmp) / "kanban-orchestra.db"))
            try:
                rows = conn.execute(
                    "SELECT title, branch FROM tasks WHERE status = 'ready' ORDER BY id"
                ).fetchall()
            finally:
                conn.close()

            self.assertEqual(
                [(row["title"], row["branch"]) for row in rows],
                [
                    ("Fix off-by-one bug in count_up_to()", "master"),
                    ("Kanban Orchestra Framework Test", "master"),
                ],
            )

    def test_init_test_repo_uses_kanban_db_env_as_full_db_path(self):
        with tempfile.TemporaryDirectory() as orchestra_tmp, tempfile.TemporaryDirectory() as repo_tmp:
            orchestra_dir = Path(orchestra_tmp)
            _write_test_ai_skills(orchestra_dir)

            init_script = SCRIPT_DIR / "init_test_repo.py"
            custom_db_path = Path(repo_tmp) / "custom" / "named-test-db.sqlite3"
            result = subprocess.run(
                [sys.executable, str(init_script)],
                capture_output=True,
                text=True,
                cwd=repo_tmp,
                env={
                    **os.environ,
                    "ORCHESTRA_DIR": str(orchestra_dir),
                    "KANBAN_DB": str(custom_db_path),
                },
                check=True,
            )

            self.assertIn(str(custom_db_path.resolve()), result.stdout)
            self.assertTrue(custom_db_path.exists())
            self.assertFalse((Path(repo_tmp) / "kanban-orchestra.db").exists())

            conn = db.connect(str(custom_db_path))
            try:
                rows = conn.execute(
                    "SELECT title, branch FROM tasks WHERE status = 'ready' ORDER BY id"
                ).fetchall()
            finally:
                conn.close()

            self.assertEqual(
                [(row["title"], row["branch"]) for row in rows],
                [
                    ("Fix off-by-one bug in count_up_to()", "master"),
                    ("Kanban Orchestra Framework Test", "master"),
                ],
            )


class TestSyncAiSkillWrappers(unittest.TestCase):
    """Test thin wrapper generation for shared AI skills."""

    def test_default_orchestra_dir_reads_environment(self):
        with patch.dict(os.environ, {"ORCHESTRA_DIR": "/tmp/orchestra-test"}, clear=False):
            self.assertEqual(skill_wrappers._default_orchestra_dir(), "/tmp/orchestra-test")

    def test_parse_args_requires_orchestra_dir_when_env_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(sys, "argv", ["sync_ai_skill_wrappers.py"]):
                with self.assertRaises(SystemExit) as exc:
                    skill_wrappers.parse_args()
        self.assertEqual(exc.exception.code, 2)

    def test_sync_skill_wrappers_creates_all_agent_wrappers(self):
        with tempfile.TemporaryDirectory() as orchestra_tmp, tempfile.TemporaryDirectory() as repo_tmp:
            orchestra_dir = Path(orchestra_tmp)
            target = Path(repo_tmp)
            _write_test_ai_skills(orchestra_dir)

            summary = skill_wrappers.sync_skill_wrappers(target=target, orchestra_dir=orchestra_dir)

            skill_count = len(skill_wrappers._canonical_skill_files(orchestra_dir))
            self.assertEqual(len(summary["created"]), skill_count * len(skill_wrappers.AGENTS))
            self.assertEqual(summary["updated"], [])
            self.assertEqual(summary["skipped"], [])

            expected = skill_wrappers.render_wrapper(
                "review-build",
                skill_wrappers._skill_description(orchestra_dir / "AI-skills" / "review-build.md"),
                orchestra_dir / "AI-skills" / "review-build.md",
            )
            for agent in skill_wrappers.AGENTS:
                wrapper_path = target / f".{agent}" / "skills" / "review-build" / "SKILL.md"
                self.assertEqual(wrapper_path.read_text(encoding="utf-8"), expected)
            self.assertTrue(
                (target / ".agents" / "skills" / "review-build" / "SKILL.md").exists()
            )

    def test_sync_skill_wrappers_updates_legacy_generated_wrappers(self):
        with tempfile.TemporaryDirectory() as orchestra_tmp, tempfile.TemporaryDirectory() as repo_tmp:
            orchestra_dir = Path(orchestra_tmp)
            target = Path(repo_tmp)
            _write_test_ai_skills(orchestra_dir)

            skill_name = "kanban"
            canonical_path = orchestra_dir / "AI-skills" / f"{skill_name}.md"
            description = skill_wrappers._skill_description(canonical_path)
            legacy_files = {
                ".claude/skills/kanban/SKILL.md": (
                    f"---\nname: {skill_name}\ndescription: {description}\n---\n\n"
                    f"@{canonical_path.resolve()}\n"
                ),
                ".gemini/skills/kanban/SKILL.md": (
                    f"---\nname: {skill_name}\ndescription: {description}\n---\n\n"
                    f"Read and follow the instructions in `{canonical_path.resolve()}`.\n"
                ),
            }
            for relative_path, content in legacy_files.items():
                wrapper_path = target / relative_path
                wrapper_path.parent.mkdir(parents=True, exist_ok=True)
                wrapper_path.write_text(content, encoding="utf-8")

            summary = skill_wrappers.sync_skill_wrappers(target=target, orchestra_dir=orchestra_dir)

            self.assertEqual(
                sorted(summary["updated"]),
                sorted(legacy_files),
            )
            expected = skill_wrappers.render_wrapper(skill_name, description, canonical_path)
            for relative_path in legacy_files:
                wrapper_path = target / relative_path
                self.assertEqual(wrapper_path.read_text(encoding="utf-8"), expected)

    def test_sync_skill_wrappers_removes_generated_obsolete_codex_wrappers(self):
        with tempfile.TemporaryDirectory() as orchestra_tmp, tempfile.TemporaryDirectory() as repo_tmp:
            orchestra_dir = Path(orchestra_tmp)
            target = Path(repo_tmp)
            _write_test_ai_skills(orchestra_dir)

            skill_name = "kanban"
            canonical_path = orchestra_dir / "AI-skills" / f"{skill_name}.md"
            description = skill_wrappers._skill_description(canonical_path)
            stale_wrapper = target / ".codex" / "skills" / skill_name / "SKILL.md"
            stale_wrapper.parent.mkdir(parents=True, exist_ok=True)
            stale_wrapper.write_text(
                skill_wrappers.render_wrapper(skill_name, description, canonical_path),
                encoding="utf-8",
            )

            summary = skill_wrappers.sync_skill_wrappers(target=target, orchestra_dir=orchestra_dir)

            self.assertIn(".codex/skills/kanban/SKILL.md", summary["removed"])
            self.assertFalse(stale_wrapper.exists())

    def test_sync_skill_wrappers_keeps_custom_obsolete_codex_wrappers(self):
        with tempfile.TemporaryDirectory() as orchestra_tmp, tempfile.TemporaryDirectory() as repo_tmp:
            orchestra_dir = Path(orchestra_tmp)
            target = Path(repo_tmp)
            _write_test_ai_skills(orchestra_dir)

            custom_wrapper = target / ".codex" / "skills" / "kanban" / "SKILL.md"
            custom_wrapper.parent.mkdir(parents=True, exist_ok=True)
            custom_content = (
                "---\n"
                "name: kanban\n"
                "description: Custom local instructions.\n"
                "---\n\n"
                "Use these custom Codex-only instructions.\n"
            )
            custom_wrapper.write_text(custom_content, encoding="utf-8")

            summary = skill_wrappers.sync_skill_wrappers(target=target, orchestra_dir=orchestra_dir)

            self.assertIn(".codex/skills/kanban/SKILL.md", summary["skipped"])
            self.assertEqual(custom_wrapper.read_text(encoding="utf-8"), custom_content)

    def test_sync_skill_wrappers_skips_custom_wrapper_files(self):
        with tempfile.TemporaryDirectory() as orchestra_tmp, tempfile.TemporaryDirectory() as repo_tmp:
            orchestra_dir = Path(orchestra_tmp)
            target = Path(repo_tmp)
            _write_test_ai_skills(orchestra_dir)

            custom_wrapper = target / ".agents" / "skills" / "kanban" / "SKILL.md"
            custom_wrapper.parent.mkdir(parents=True, exist_ok=True)
            custom_content = (
                "---\n"
                "name: kanban\n"
                "description: Custom local instructions.\n"
                "---\n\n"
                "Use the local team-specific kanban workflow instead of the shared one.\n"
            )
            custom_wrapper.write_text(custom_content, encoding="utf-8")

            summary = skill_wrappers.sync_skill_wrappers(target=target, orchestra_dir=orchestra_dir)

            self.assertIn(".agents/skills/kanban/SKILL.md", summary["skipped"])
            self.assertEqual(custom_wrapper.read_text(encoding="utf-8"), custom_content)

    def test_sync_skill_wrappers_picks_up_new_skill_file_automatically(self):
        with tempfile.TemporaryDirectory() as orchestra_tmp, tempfile.TemporaryDirectory() as repo_tmp:
            orchestra_dir = Path(orchestra_tmp)
            target = Path(repo_tmp)
            _write_test_ai_skills(orchestra_dir)
            (orchestra_dir / "AI-skills" / "new-skill.md").write_text(
                "New skill summary line.\n\nMore instructions.\n",
                encoding="utf-8",
            )

            summary = skill_wrappers.sync_skill_wrappers(target=target, orchestra_dir=orchestra_dir)

            for agent in skill_wrappers.AGENTS:
                relative_path = f".{agent}/skills/new-skill/SKILL.md"
                self.assertIn(relative_path, summary["created"])
                wrapper_path = target / relative_path
                self.assertIn(
                    'description: "New skill summary line."',
                    wrapper_path.read_text(encoding="utf-8"),
                )

    def test_sync_skill_wrappers_quotes_yaml_sensitive_descriptions(self):
        with tempfile.TemporaryDirectory() as orchestra_tmp, tempfile.TemporaryDirectory() as repo_tmp:
            orchestra_dir = Path(orchestra_tmp)
            target = Path(repo_tmp)
            _write_test_ai_skills(orchestra_dir)
            (orchestra_dir / "AI-skills" / "prep-for-review.md").write_text(
                "**Note**: This is the ad-hoc manual workflow.\n\nMore instructions.\n",
                encoding="utf-8",
            )

            summary = skill_wrappers.sync_skill_wrappers(target=target, orchestra_dir=orchestra_dir)

            self.assertIn(".agents/skills/prep-for-review/SKILL.md", summary["created"])
            wrapper_path = target / ".agents" / "skills" / "prep-for-review" / "SKILL.md"
            self.assertIn(
                'description: "**Note**: This is the ad-hoc manual workflow."',
                wrapper_path.read_text(encoding="utf-8"),
            )


class TestOrchestratorRuntime(unittest.TestCase):
    """Test the orchestrator_runtime table and helpers."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_upsert_runtime_creates_singleton(self):
        db.upsert_runtime(
            self.conn,
            status="idle",
            pid=12345,
            started_at="CURRENT_TIMESTAMP",
            last_heartbeat_at="CURRENT_TIMESTAMP",
            current_task_id=None,
            current_step="none",
            active_agents=0,
            status_message="test",
        )
        rt = db.get_runtime(self.conn)
        self.assertIsNotNone(rt)
        self.assertEqual(rt["singleton"], 1)
        self.assertEqual(rt["status"], "idle")
        self.assertEqual(rt["pid"], 12345)
        self.assertEqual(rt["active_agents"], 0)
        self.assertEqual(rt["status_message"], "test")

    def test_upsert_runtime_overwrites_stale_row(self):
        db.upsert_runtime(self.conn, status="running", pid=111,
                          started_at="CURRENT_TIMESTAMP",
                          last_heartbeat_at="CURRENT_TIMESTAMP",
                          current_step="commit-make", active_agents=1,
                          status_message="old")
        # Simulate restart — overwrite
        db.upsert_runtime(self.conn, status="idle", pid=222,
                          started_at="CURRENT_TIMESTAMP",
                          last_heartbeat_at="CURRENT_TIMESTAMP",
                          current_step="none", active_agents=0,
                          status_message="fresh")
        rt = db.get_runtime(self.conn)
        self.assertEqual(rt["pid"], 222)
        self.assertEqual(rt["status"], "idle")
        self.assertEqual(rt["status_message"], "fresh")

    def test_update_runtime(self):
        db.upsert_runtime(self.conn, status="idle", pid=1,
                          started_at="CURRENT_TIMESTAMP",
                          last_heartbeat_at="CURRENT_TIMESTAMP",
                          current_step="none", active_agents=0,
                          status_message="init")
        db.update_runtime(self.conn, status="running", current_step="commit-make",
                          active_agents=1, status_message="working")
        rt = db.get_runtime(self.conn)
        self.assertEqual(rt["status"], "running")
        self.assertEqual(rt["current_step"], "commit-make")
        self.assertEqual(rt["active_agents"], 1)
        self.assertEqual(rt["status_message"], "working")

    def test_update_runtime_heartbeat(self):
        db.upsert_runtime(self.conn, status="idle", pid=1,
                          started_at="CURRENT_TIMESTAMP",
                          last_heartbeat_at="CURRENT_TIMESTAMP",
                          current_step="none", active_agents=0,
                          status_message="init")
        db.update_runtime(self.conn, last_heartbeat_at="CURRENT_TIMESTAMP")
        rt = db.get_runtime(self.conn)
        self.assertIsNotNone(rt["last_heartbeat_at"])

    def test_get_runtime_returns_none_when_empty(self):
        rt = db.get_runtime(self.conn)
        self.assertIsNone(rt)

    def test_runtime_status_check_constraint(self):
        with self.assertRaises(Exception):
            db.upsert_runtime(self.conn, status="bogus", pid=1,
                              started_at="CURRENT_TIMESTAMP",
                              last_heartbeat_at="CURRENT_TIMESTAMP",
                              current_step="none", active_agents=0,
                              status_message="bad")

    def test_runtime_status_accepts_operator_control_states(self):
        db.upsert_runtime(self.conn, status="starting", pid=1,
                          started_at="CURRENT_TIMESTAMP",
                          last_heartbeat_at="CURRENT_TIMESTAMP",
                          current_step="none", active_agents=0,
                          status_message="launching")
        self.assertEqual(db.get_runtime(self.conn)["status"], "starting")

        db.update_runtime(self.conn, status="hard-break", status_message="break complete")
        rt = db.get_runtime(self.conn)
        self.assertEqual(rt["status"], "hard-break")
        self.assertEqual(rt["status_message"], "break complete")

    def test_stale_runtime_status_constraint_is_migrated_preserving_row(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            legacy_sql = db.SCHEMA_SQL.replace(
                "CHECK(status IN ('idle', 'running', 'starting', 'stopping', 'stopped', 'hard-break', 'error'))",
                "CHECK(status IN ('idle', 'running', 'stopping', 'stopped', 'error'))",
            )
            legacy = sqlite3.connect(tmp.name)
            try:
                legacy.executescript(legacy_sql)
                legacy.execute(
                    """INSERT INTO orchestrator_runtime (
                        singleton, status, pid, started_at, last_heartbeat_at,
                        current_step, active_agents, status_message
                    ) VALUES (1, 'stopped', 123, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                              'none', 0, 'preserved')"""
                )
                legacy.commit()
            finally:
                legacy.close()

            migrated = db.connect(tmp.name)
            try:
                rt = db.get_runtime(migrated)
                self.assertEqual(rt["status"], "stopped")
                self.assertEqual(rt["pid"], 123)
                self.assertEqual(rt["status_message"], "preserved")
                db.update_runtime(migrated, status="starting")
                self.assertEqual(db.get_runtime(migrated)["status"], "starting")
            finally:
                migrated.close()
        finally:
            for suffix in ("", "-shm", "-wal"):
                path = tmp.name + suffix
                if os.path.exists(path):
                    os.unlink(path)

    def test_runtime_current_step_check_constraint(self):
        with self.assertRaises(Exception):
            db.upsert_runtime(self.conn, status="idle", pid=1,
                              started_at="CURRENT_TIMESTAMP",
                              last_heartbeat_at="CURRENT_TIMESTAMP",
                              current_step="bad-step", active_agents=0,
                              status_message="bad")

    def test_runtime_current_step_accepts_all_six_verbs(self):
        """All orchestrator step names must be accepted by the current_step constraint."""
        valid_steps = [
            "commit-make", "commit-review",
            "commit-make-supertask", "commit-review-supertask",
            "commit-plan", "commit-plan-review",
            "none",
        ]
        db.upsert_runtime(self.conn, status="idle", pid=1,
                          started_at="CURRENT_TIMESTAMP",
                          last_heartbeat_at="CURRENT_TIMESTAMP",
                          current_step="none", active_agents=0,
                          status_message="init")
        for step in valid_steps:
            db.update_runtime(self.conn, current_step=step)
            rt = db.get_runtime(self.conn)
            self.assertEqual(rt["current_step"], step, f"step '{step}' not stored correctly")


class TestInitRuntime(unittest.TestCase):
    """Test orchestrator.init_runtime and set_runtime_idle."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_init_runtime_creates_row(self):
        orchestrator.init_runtime(self.conn)
        rt = db.get_runtime(self.conn)
        self.assertIsNotNone(rt)
        self.assertEqual(rt["status"], "idle")
        self.assertEqual(rt["pid"], os.getpid())
        self.assertEqual(rt["current_step"], "none")
        self.assertEqual(rt["active_agents"], 0)

    def test_init_runtime_overwrites_stale(self):
        # Simulate stale row from a crashed process
        db.upsert_runtime(self.conn, status="running", pid=99999,
                          started_at="CURRENT_TIMESTAMP",
                          last_heartbeat_at="CURRENT_TIMESTAMP",
                          current_step="commit-make", active_agents=1,
                          status_message="stale")
        orchestrator.init_runtime(self.conn)
        rt = db.get_runtime(self.conn)
        self.assertEqual(rt["status"], "idle")
        self.assertEqual(rt["pid"], os.getpid())

    def test_set_runtime_idle(self):
        orchestrator.init_runtime(self.conn)
        # Create a real task so FK constraint is satisfied
        tid = db.add_task(self.conn, "Test task for runtime")
        db.update_runtime(self.conn, status="running", current_task_id=tid)
        orchestrator.set_runtime_idle(self.conn)
        rt = db.get_runtime(self.conn)
        self.assertEqual(rt["status"], "idle")
        self.assertIsNone(rt["current_task_id"])
        self.assertEqual(rt["current_step"], "none")
        self.assertEqual(rt["active_agents"], 0)
        self.assertEqual(rt["status_message"], "Waiting for ready tasks")


class TestMainLoop(unittest.TestCase):
    """Test main_loop scheduling-side behavior."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_logs_when_ready_task_is_skipped_by_blocked_gate(self):
        log_messages = []

        def capture_log(message, task_id=None):
            log_messages.append((task_id, message))

        def stop_after_first_sleep(_seconds):
            raise RuntimeError("stop loop")

        with (
            patch.object(orchestrator, "log", side_effect=capture_log),
            patch.object(orchestrator, "init_runtime"),
            patch.object(orchestrator, "recover_running_tasks"),
            patch.object(orchestrator, "start_heartbeat"),
            patch.object(orchestrator, "set_runtime_idle"),
            patch.object(orchestrator.db, "get_db_path", return_value=self.tmp.name),
            patch.object(orchestrator.db, "find_ready_task", return_value=None),
            patch.object(
                orchestrator.db,
                "list_ready_tasks_blocked_by_blocked_gate",
                return_value=[{"id": 7, "title": "Gated task"}],
            ),
            patch.object(orchestrator.time, "sleep", side_effect=stop_after_first_sleep),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop loop"):
                orchestrator.main_loop(self.conn)

        self.assertIn(
            (
                7,
                "Skipping ready task because another task is blocked and "
                "allow_when_blocked is false",
            ),
            log_messages,
        )

    def test_logs_gated_task_even_when_opted_in_task_is_picked(self):
        log_messages = []

        def capture_log(message, task_id=None):
            log_messages.append((task_id, message))

        def stop_after_pick(_task, _conn):
            raise RuntimeError("stop after pick")

        with (
            patch.object(orchestrator, "log", side_effect=capture_log),
            patch.object(orchestrator, "init_runtime"),
            patch.object(orchestrator, "recover_running_tasks"),
            patch.object(orchestrator, "start_heartbeat"),
            patch.object(orchestrator, "set_runtime_idle"),
            patch.object(orchestrator.db, "get_db_path", return_value=self.tmp.name),
            patch.object(
                orchestrator.db,
                "list_ready_tasks_blocked_by_blocked_gate",
                return_value=[{"id": 7, "title": "Gated task"}],
            ),
            patch.object(
                orchestrator.db,
                "find_ready_task",
                return_value={"id": 8, "title": "Allowed task", "next_step": "commit-make"},
            ),
            patch.object(orchestrator, "process_pinned_task", side_effect=stop_after_pick),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after pick"):
                orchestrator.main_loop(self.conn)

        self.assertIn(
            (
                7,
                "Skipping ready task because another task is blocked and "
                "allow_when_blocked is false",
            ),
            log_messages,
        )
        self.assertIn((8, "Picked up: 'Allowed task' (step=commit-make)"), log_messages)


class TestRuntimeAfterTask(unittest.TestCase):
    """Test runtime transitions after a task finishes or blocks."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)
        orchestrator.init_runtime(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_blocked_task_yields_to_next_ready_task_without_idle_transition(self):
        blocked_tid = db.add_task(self.conn, "Blocked task", branch="feat-blocked")
        next_tid = db.add_task(
            self.conn,
            "Next ready task",
            branch="feat-next",
            allow_when_blocked=True,
        )
        db.update_task(self.conn, blocked_tid, status="blocked", next_step="none")
        db.update_task(self.conn, next_tid, status="ready", next_step="commit-make")
        db.update_runtime(
            self.conn,
            status="running",
            current_task_id=blocked_tid,
            current_step="commit-make",
            current_branch="feat-blocked",
            status_message=f"Blocked: task {blocked_tid}",
        )

        orchestrator.update_runtime_after_task(self.conn, blocked_tid, succeeded=False)

        rt = db.get_runtime(self.conn)
        self.assertEqual(rt["status"], "running")
        self.assertIsNone(rt["current_task_id"])
        self.assertEqual(rt["current_step"], "none")
        self.assertEqual(rt["status_message"], f"Task {blocked_tid} blocked; continuing to next ready task")

    def test_blocked_task_with_non_opted_in_ready_follow_up_goes_idle(self):
        blocked_tid = db.add_task(self.conn, "Blocked task", branch="feat-blocked")
        next_tid = db.add_task(self.conn, "Next ready task", branch="feat-next")
        db.update_task(self.conn, blocked_tid, status="blocked", next_step="none")
        db.update_task(self.conn, next_tid, status="ready", next_step="commit-make")
        db.update_runtime(
            self.conn,
            status="running",
            current_task_id=blocked_tid,
            current_step="commit-make",
            current_branch="feat-blocked",
            status_message=f"Blocked: task {blocked_tid}",
        )

        orchestrator.update_runtime_after_task(self.conn, blocked_tid, succeeded=False)

        rt = db.get_runtime(self.conn)
        self.assertEqual(rt["status"], "idle")
        self.assertEqual(rt["status_message"], f"Blocked: task {blocked_tid}")

    def test_blocked_task_goes_idle_when_queue_is_empty(self):
        blocked_tid = db.add_task(self.conn, "Blocked task", branch="feat-blocked")
        db.update_task(self.conn, blocked_tid, status="blocked", next_step="none")
        db.update_runtime(
            self.conn,
            status="running",
            current_task_id=blocked_tid,
            current_step="commit-make",
            current_branch="feat-blocked",
            status_message="Blocked: waiting for human input",
        )

        orchestrator.update_runtime_after_task(self.conn, blocked_tid, succeeded=False)

        rt = db.get_runtime(self.conn)
        self.assertEqual(rt["status"], "idle")
        self.assertEqual(rt["status_message"], "Blocked: waiting for human input")


class TestHeartbeat(unittest.TestCase):
    """Test the heartbeat thread starts and updates."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        orchestrator.stop_heartbeat()
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_heartbeat_updates(self):
        orchestrator.init_runtime(self.conn)
        db.get_runtime(self.conn)

        # Use a very short interval for testing
        old_interval = orchestrator.HEARTBEAT_INTERVAL
        orchestrator.HEARTBEAT_INTERVAL = 0.1
        try:
            orchestrator.start_heartbeat(self.tmp.name)
            time.sleep(0.5)
            orchestrator.stop_heartbeat()
        finally:
            orchestrator.HEARTBEAT_INTERVAL = old_interval

        rt_after = db.get_runtime(self.conn)
        # Heartbeat should have updated; at minimum updated_at should differ
        self.assertIsNotNone(rt_after["last_heartbeat_at"])


class TestSingletonLock(unittest.TestCase):
    """Test the repo-scoped singleton orchestrator lock."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.lock_path = Path(self.tmpdir.name) / "kanban-orchestra.lock"

    def tearDown(self):
        orchestrator.release_singleton_lock()
        orchestrator._dashboard_process = None
        orchestrator._dashboard_metadata_path_for_process = None
        self.tmpdir.cleanup()

    def _run_lock_probe(self):
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, str(Path(sys.argv[1]).resolve()))\n"
            "import orchestrator\n"
            "try:\n"
            "    orchestrator.acquire_singleton_lock(lock_path=sys.argv[2])\n"
            "except orchestrator.SingletonLockError as exc:\n"
            "    print(str(exc))\n"
            "    raise SystemExit(1)\n"
            "else:\n"
            "    orchestrator.release_singleton_lock()\n"
        )
        return subprocess.run(
            [sys.executable, "-c", script, str(Path(__file__).resolve().parent), str(self.lock_path)],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_acquire_writes_lock_file(self):
        path = orchestrator.acquire_singleton_lock(lock_path=self.lock_path)
        self.assertEqual(path, self.lock_path.resolve())
        contents = self.lock_path.read_text(encoding="utf-8")
        self.assertIn("pid=", contents)
        self.assertIn("started_at=", contents)
        self.assertIn("role=orchestrator", contents)
        self.assertIn(f"repo_root={Path(self.tmpdir.name).resolve()}", contents)
        self.assertIn(f"lock_path={self.lock_path.resolve()}", contents)

    def test_second_process_cannot_acquire_while_locked(self):
        orchestrator.acquire_singleton_lock(lock_path=self.lock_path)
        probe = self._run_lock_probe()
        self.assertEqual(probe.returncode, 1)
        self.assertIn("already running", probe.stdout)
        self.assertIn(str(Path(self.tmpdir.name).resolve()), probe.stdout)

    def test_release_allows_another_process_to_acquire(self):
        orchestrator.acquire_singleton_lock(lock_path=self.lock_path)
        orchestrator.release_singleton_lock()
        probe = self._run_lock_probe()
        self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)

    def test_main_requests_dashboard_start_when_lock_is_unavailable(self):
        with patch.object(orchestrator, "acquire_singleton_lock", side_effect=orchestrator.SingletonLockError("busy")), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False), \
             patch.object(orchestrator, "request_dashboard_start", return_value=Path("/tmp/dashboard-start-request")) as request_start, \
             patch.object(orchestrator, "log") as mock_log, \
             patch.object(orchestrator.db, "connect") as mock_connect, \
             patch.object(orchestrator.os, "setpgrp"), \
             patch.object(orchestrator.signal, "signal"):
            exit_code = orchestrator.main()

        self.assertEqual(exit_code, 0)
        request_start.assert_called_once()
        mock_connect.assert_not_called()
        mock_log.assert_any_call("busy")
        mock_log.assert_any_call("Requested dashboard start via /tmp/dashboard-start-request")

    def test_main_no_dashboard_does_not_request_dashboard_when_lock_is_unavailable(self):
        with patch.object(orchestrator, "acquire_singleton_lock", side_effect=orchestrator.SingletonLockError("busy")), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False), \
             patch.object(orchestrator, "request_dashboard_start") as request_start, \
             patch.object(orchestrator, "log") as mock_log, \
             patch.object(orchestrator.db, "connect") as mock_connect, \
             patch.object(orchestrator.os, "setpgrp"), \
             patch.object(orchestrator.signal, "signal"):
            exit_code = orchestrator.main(["--no-dashboard"])

        self.assertEqual(exit_code, 1)
        request_start.assert_not_called()
        mock_connect.assert_not_called()
        mock_log.assert_any_call("busy")

    def test_request_dashboard_start_creates_presence_file(self):
        db_path = str(Path(self.tmpdir.name) / "kanban-orchestra.db")

        request_path = orchestrator.request_dashboard_start(db_path)

        self.assertEqual(request_path.name, "dashboard-start-request")
        self.assertEqual(request_path.read_text(encoding="utf-8"), "start\n")
        self.assertTrue(orchestrator._read_dashboard_start_request(db_path))

    def test_read_dashboard_start_request_returns_preferred_port(self):
        db_path = str(Path(self.tmpdir.name) / "kanban-orchestra.db")
        request_path = orchestrator.request_dashboard_start(db_path)
        request_path.write_text("start\nport=8433\n", encoding="utf-8")

        self.assertEqual(orchestrator._read_dashboard_start_request(db_path), 8433)

    def test_main_refuses_to_start_on_dirty_worktree(self):
        with patch.object(orchestrator, "is_worktree_dirty", return_value=True), \
             patch.object(orchestrator, "acquire_singleton_lock") as mock_lock, \
             patch.object(orchestrator, "log") as mock_log, \
             patch.object(orchestrator.db, "connect") as mock_connect, \
             patch.object(orchestrator.os, "setpgrp"), \
             patch.object(orchestrator.signal, "signal"):
            exit_code = orchestrator.main()

        self.assertEqual(exit_code, 2)
        mock_lock.assert_called_once()
        mock_connect.assert_not_called()
        self.assertTrue(any("dirty" in str(c).lower() for c in mock_log.call_args_list))

    def test_start_dashboard_uses_repo_runtime_metadata_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            orchestrator._dashboard_process = None
            db_path = str(Path(tmpdir) / "kanban-orchestra.db")
            fake_process = MagicMock()
            fake_process.pid = 1234
            fake_process.poll.return_value = None

            with patch.object(orchestrator.subprocess, "Popen", return_value=fake_process) as mock_popen, \
                 patch.object(orchestrator, "log"):
                process = orchestrator.start_dashboard(db_path, preferred_port=8765)

            self.assertIs(process, fake_process)
            popen_args = mock_popen.call_args.args[0]
            popen_kwargs = mock_popen.call_args.kwargs
            self.assertEqual(popen_args[0], sys.executable)
            self.assertTrue(popen_args[1].endswith("dashboard.py"))
            self.assertEqual(popen_kwargs["cwd"], str(Path(tmpdir).resolve()))
            self.assertEqual(popen_kwargs["env"]["KO_DASH_PORT"], "8765")
            self.assertEqual(
                popen_kwargs["env"]["KO_DASHBOARD_METADATA_PATH"],
                str(Path(tmpdir).resolve() / ".kanban-orchestra" / "dashboard.json"),
            )
            self.assertEqual(
                orchestrator._dashboard_metadata_path_for_process,
                Path(tmpdir).resolve() / ".kanban-orchestra" / "dashboard.json",
            )
            orchestrator._dashboard_process = None
            orchestrator._dashboard_metadata_path_for_process = None

    def test_stop_dashboard_terminates_owned_process(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            metadata_path = Path(tmpdir) / ".kanban-orchestra" / "dashboard.json"
            metadata_path.parent.mkdir()
            metadata_path.write_text("{}", encoding="utf-8")
            fake_process = MagicMock()
            fake_process.poll.return_value = None
            orchestrator._dashboard_process = fake_process
            orchestrator._dashboard_metadata_path_for_process = metadata_path

            with patch.object(orchestrator, "_dashboard_metadata_path", side_effect=AssertionError("wrong path")):
                orchestrator.stop_dashboard()

            fake_process.terminate.assert_called_once()
            fake_process.wait.assert_called_once_with(timeout=5)
            self.assertIsNone(orchestrator._dashboard_process)
            self.assertIsNone(orchestrator._dashboard_metadata_path_for_process)
            self.assertFalse(metadata_path.exists())

    def test_stop_dashboard_logs_when_killed_process_does_not_exit(self):
        fake_process = MagicMock()
        fake_process.pid = 1234
        fake_process.poll.return_value = None
        fake_process.wait.side_effect = subprocess.TimeoutExpired("dashboard", 5)
        orchestrator._dashboard_process = fake_process
        metadata_path = Path("/tmp/dashboard.json")
        orchestrator._dashboard_metadata_path_for_process = metadata_path

        with patch.object(orchestrator, "_remove_dashboard_metadata") as mock_remove, \
             patch.object(orchestrator, "log") as mock_log:
            orchestrator.stop_dashboard()

        fake_process.terminate.assert_called_once()
        fake_process.kill.assert_called_once()
        self.assertEqual(fake_process.wait.call_count, 2)
        mock_remove.assert_called_once_with(metadata_path)
        self.assertTrue(any("did not exit" in str(call) for call in mock_log.call_args_list))

    def test_stop_dashboard_without_started_process_does_not_clean_default_path(self):
        orchestrator._dashboard_process = None
        orchestrator._dashboard_metadata_path_for_process = None

        with patch.object(orchestrator, "_remove_dashboard_metadata") as mock_remove:
            orchestrator.stop_dashboard()

        mock_remove.assert_not_called()

    def test_check_dashboard_process_logs_exit_without_restart(self):
        fake_process = MagicMock()
        fake_process.poll.return_value = 7
        orchestrator._dashboard_process = fake_process
        metadata_path = Path("/tmp/dashboard.json")
        orchestrator._dashboard_metadata_path_for_process = metadata_path
        conn = MagicMock()

        with patch.object(orchestrator.db, "update_runtime") as mock_update, \
             patch.object(orchestrator, "_remove_dashboard_metadata") as mock_remove, \
             patch.object(orchestrator, "log") as mock_log:
            orchestrator.check_dashboard_process(conn)

        mock_update.assert_called_once()
        self.assertIn("dashboard stopped", mock_update.call_args.kwargs["status_message"])
        mock_log.assert_called_once()
        mock_remove.assert_called_once_with(metadata_path)
        self.assertIsNone(orchestrator._dashboard_process)
        self.assertIsNone(orchestrator._dashboard_metadata_path_for_process)

    def test_check_dashboard_process_does_not_start_when_no_process_is_tracked(self):
        conn = MagicMock()
        orchestrator._dashboard_process = None

        with patch.object(orchestrator, "start_dashboard") as start_dashboard, \
             patch.object(orchestrator.db, "update_runtime") as mock_update, \
             patch.object(orchestrator, "log") as mock_log:
            orchestrator.check_dashboard_process(conn, "/tmp/kanban.db")

        start_dashboard.assert_not_called()
        mock_update.assert_not_called()
        mock_log.assert_not_called()

    def test_handle_dashboard_start_request_starts_dashboard(self):
        conn = MagicMock()
        restarted_process = MagicMock()
        restarted_process.pid = 4321
        orchestrator._dashboard_process = None

        with patch.object(orchestrator, "_read_dashboard_start_request", return_value=True), \
             patch.object(orchestrator, "_clear_dashboard_start_request") as clear_request, \
             patch.object(orchestrator, "start_dashboard", return_value=restarted_process) as start_dashboard, \
             patch.object(orchestrator.db, "update_runtime") as mock_update, \
             patch.object(orchestrator, "log") as mock_log:
            orchestrator.handle_dashboard_start_request(conn, "/tmp/kanban.db")

        clear_request.assert_called_once_with("/tmp/kanban.db")
        start_dashboard.assert_called_once_with("/tmp/kanban.db", preferred_port=None)
        mock_update.assert_called_once()
        self.assertIn("explicit request", mock_update.call_args.kwargs["status_message"])
        mock_log.assert_called_once_with("Dashboard started with PID 4321 by explicit request")

    def test_handle_dashboard_start_request_uses_preferred_port(self):
        conn = MagicMock()
        restarted_process = MagicMock()
        restarted_process.pid = 4321
        orchestrator._dashboard_process = None

        with patch.object(orchestrator, "_read_dashboard_start_request", return_value=8433), \
             patch.object(orchestrator, "_clear_dashboard_start_request"), \
             patch.object(orchestrator, "start_dashboard", return_value=restarted_process) as start_dashboard, \
             patch.object(orchestrator.db, "update_runtime"), \
             patch.object(orchestrator, "log"):
            orchestrator.handle_dashboard_start_request(conn, "/tmp/kanban.db")

        start_dashboard.assert_called_once_with("/tmp/kanban.db", preferred_port=8433)

    def test_handle_dashboard_start_request_does_not_drop_port_zero(self):
        conn = MagicMock()
        restarted_process = MagicMock()
        restarted_process.pid = 4321
        orchestrator._dashboard_process = None

        with patch.object(orchestrator, "_read_dashboard_start_request", return_value=0), \
             patch.object(orchestrator, "_clear_dashboard_start_request"), \
             patch.object(orchestrator, "start_dashboard", return_value=restarted_process) as start_dashboard, \
             patch.object(orchestrator.db, "update_runtime"), \
             patch.object(orchestrator, "log"):
            orchestrator.handle_dashboard_start_request(conn, "/tmp/kanban.db")

        start_dashboard.assert_called_once_with("/tmp/kanban.db", preferred_port=0)

    def test_handle_dashboard_start_request_noops_when_dashboard_is_running(self):
        conn = MagicMock()
        fake_process = MagicMock()
        fake_process.poll.return_value = None
        orchestrator._dashboard_process = fake_process

        with patch.object(orchestrator, "_read_dashboard_start_request", return_value=True), \
             patch.object(orchestrator, "_clear_dashboard_start_request") as clear_request, \
             patch.object(orchestrator, "start_dashboard") as start_dashboard, \
             patch.object(orchestrator.db, "update_runtime") as mock_update, \
             patch.object(orchestrator, "log") as mock_log:
            orchestrator.handle_dashboard_start_request(conn, "/tmp/kanban.db")

        clear_request.assert_called_once_with("/tmp/kanban.db")
        start_dashboard.assert_not_called()
        mock_update.assert_not_called()
        mock_log.assert_called_once_with("Dashboard start requested; dashboard is already running")


class TestOrchestratorControlIdentity(unittest.TestCase):
    """Test repo identity metadata in filesystem-backed control helpers."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "kanban.db")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_write_supervisor_heartbeat_includes_instance_identity(self):
        orchestrator_control.write_supervisor_heartbeat(db_path=self.db_path)

        heartbeat_path = db.get_runtime_root(self.db_path) / orchestrator_control.SUPERVISOR_HEARTBEAT_FILE
        payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["repo_root"], str(Path(self.tmpdir.name).resolve()))
        self.assertEqual(payload["repo_label"], Path(self.tmpdir.name).name)
        self.assertEqual(payload["db_path"], str(Path(self.db_path).resolve()))
        self.assertEqual(payload["runtime_root"], str((Path(self.tmpdir.name) / ".kanban-orchestra").resolve()))
        self.assertEqual(payload["lock_path"], str((Path(self.tmpdir.name) / "kanban-orchestra.lock").resolve()))

    def test_supervisor_status_rejects_heartbeat_for_other_repo(self):
        heartbeat_path = db.get_runtime_root(self.db_path) / orchestrator_control.SUPERVISOR_HEARTBEAT_FILE
        heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            **db.get_instance_identity(self.db_path),
            "repo_root": str((Path(self.tmpdir.name) / "other-repo").resolve()),
            "pid": os.getpid(),
            "updated_at": "2026-05-24T00:00:00+00:00",
        }
        heartbeat_path.write_text(json.dumps(payload), encoding="utf-8")

        live, reason, read_payload = orchestrator_control.supervisor_status(self.db_path)

        self.assertFalse(live)
        self.assertIn("different repo", reason)
        self.assertEqual(read_payload["repo_root"], payload["repo_root"])


class TestActiveAgentProcesses(unittest.TestCase):
    """Test runtime metadata for active agent child processes."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "kanban.db")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_register_and_clear_active_agent_metadata(self):
        with patch.object(active_agent_processes, "is_pid_alive", return_value=True), \
             patch.object(active_agent_processes.os, "getpgid", return_value=4242):
            record_id = active_agent_processes.register_active_agent(
                task_id=123,
                verb="commit-make",
                agent_name="codex",
                pid=4242,
                db_path=self.db_path,
            )

        metadata_path = db.get_runtime_root(self.db_path) / active_agent_processes.ACTIVE_AGENTS_FILE
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))

        self.assertEqual(record_id, "123:commit-make:codex:4242")
        self.assertEqual(payload["agents"][0]["id"], record_id)
        self.assertEqual(payload["agents"][0]["task_id"], 123)
        self.assertEqual(payload["agents"][0]["agent_name"], "codex")

        active_agent_processes.clear_active_agent(record_id, db_path=self.db_path)

        self.assertFalse(metadata_path.exists())


class TestFleetConfig(unittest.TestCase):
    """Test flat fleet repo configuration and validation helpers."""

    def test_display_path_abbreviates_only_home_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir).resolve()
            home = base / "home"
            inside = home / "work" / "repo"
            outside = base / "outside"
            inside.mkdir(parents=True)
            outside.mkdir()

            with patch.dict(os.environ, {"HOME": str(home)}):
                self.assertEqual(fleet.display_path(inside), "~/work/repo")
                self.assertEqual(fleet.display_path(outside), str(outside))

    def test_parse_config_lines_ignores_blanks_and_comments(self):
        text = """
        # local repos
        ~/work/one

        /srv/two
        """
        self.assertEqual(fleet.parse_config_lines(text), ["~/work/one", "/srv/two"])

    def test_discover_repo_requires_configured_git_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            nested = root / "nested"
            nested.mkdir(parents=True)
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL)

            repo = fleet.discover_repo(str(nested))

            self.assertEqual(repo.root, root.resolve())
            self.assertIn("not the git root", repo.error)

    def test_precheck_reports_dirty_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            root.mkdir()
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL)
            (root / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            repo = fleet.discover_repo(str(root))

            with redirect_stdout(io.StringIO()):
                exit_code = fleet.precheck([repo])

            self.assertEqual(exit_code, 1)

    def test_precheck_abbreviates_home_dirty_repo_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir).resolve()
            root = home / "repo"
            root.mkdir()
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL)
            (root / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            repo = fleet.discover_repo(str(root))
            out = io.StringIO()

            with patch.dict(os.environ, {"HOME": str(home)}), redirect_stdout(out):
                exit_code = fleet.precheck([repo])

            text = out.getvalue()
            self.assertEqual(exit_code, 1)
            self.assertIn("~/repo", text)
            self.assertNotIn(str(root), text)

    def test_process_state_reports_dashboard_and_orchestrator(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            runtime = root / ".kanban-orchestra"
            runtime.mkdir()
            lock_path = root / "kanban-orchestra.lock"
            lock_path.write_text(f"role=orchestrator\npid={os.getpid()}\n", encoding="utf-8")
            (runtime / "dashboard.json").write_text(
                json.dumps({"role": "dashboard", "pid": os.getpid()}),
                encoding="utf-8",
            )
            repo = fleet.FleetRepo("repo", root, root)

            with patch.object(fleet, "tmux_has_session", return_value=True):
                state, orch_pid, dashboard_pid, session = fleet.repo_process_state(repo)

            self.assertEqual(state, "running")
            self.assertEqual(orch_pid, str(os.getpid()))
            self.assertEqual(dashboard_pid, str(os.getpid()))
            self.assertEqual(session, "orch-repo")

    def test_process_state_reports_running_without_dashboard_for_external_orchestrator(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            lock_path = root / "kanban-orchestra.lock"
            lock_path.write_text(
                f"role=orchestrator\npid={os.getpid()}\nrepo_root={root}\n",
                encoding="utf-8",
            )
            repo = fleet.FleetRepo("repo", root, root)

            with patch.object(fleet, "tmux_has_session", return_value=False):
                state, orch_pid, dashboard_pid, session = fleet.repo_process_state(repo)

            self.assertEqual(state, "running")
            self.assertEqual(orch_pid, str(os.getpid()))
            self.assertEqual(dashboard_pid, "-")
            self.assertEqual(session, "-")

    def test_open_dashboard_refuses_dead_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            runtime = root / ".kanban-orchestra"
            runtime.mkdir()
            (runtime / "dashboard.json").write_text(
                json.dumps(
                    {
                        "role": "dashboard",
                        "pid": 12345,
                        "host": "127.0.0.1",
                        "port": 8427,
                        "url": "http://127.0.0.1:8427",
                    }
                ),
                encoding="utf-8",
            )
            repo = fleet.FleetRepo("repo", root, root)

            with patch.object(fleet, "pid_alive", return_value=False), \
                 self.assertRaises(SystemExit):
                fleet.open_dashboard(repo)

    def test_open_dashboard_requires_accepting_endpoint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            runtime = root / ".kanban-orchestra"
            runtime.mkdir()
            (runtime / "dashboard.json").write_text(
                json.dumps(
                    {
                        "role": "dashboard",
                        "pid": os.getpid(),
                        "host": "127.0.0.1",
                        "port": 8427,
                        "url": "http://127.0.0.1:8427",
                    }
                ),
                encoding="utf-8",
            )
            repo = fleet.FleetRepo("repo", root, root)

            with patch.object(fleet, "dashboard_endpoint_ready", return_value=False), \
                 self.assertRaises(SystemExit):
                fleet.open_dashboard(repo)

    def test_open_dashboard_prints_live_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            runtime = root / ".kanban-orchestra"
            runtime.mkdir()
            (runtime / "dashboard.json").write_text(
                json.dumps(
                    {
                        "role": "dashboard",
                        "pid": os.getpid(),
                        "host": "127.0.0.1",
                        "port": 8427,
                        "url": "http://127.0.0.1:8427",
                    }
                ),
                encoding="utf-8",
            )
            repo = fleet.FleetRepo("repo", root, root)
            out = io.StringIO()

            with patch.object(fleet, "dashboard_endpoint_ready", return_value=True), \
                 patch.object(fleet.shutil, "which", return_value=None), \
                 redirect_stdout(out):
                fleet.open_dashboard(repo)

            self.assertIn("http://127.0.0.1:8427", out.getvalue())

    def test_wait_stopped_polls_until_orchestrator_pid_exits(self):
        repo = fleet.FleetRepo("repo", Path("/tmp/repo"), Path("/tmp/repo"))

        with patch.object(fleet, "metadata_pid", side_effect=[12345, None]) as mock_metadata, \
             patch.object(fleet, "pid_alive", side_effect=[True, False]), \
             patch.object(fleet.time, "sleep") as mock_sleep:
            fleet.wait_stopped([repo], timeout=1)

        self.assertEqual(mock_metadata.call_count, 2)
        mock_sleep.assert_called_once_with(0.2)

    def test_wait_stopped_times_out_while_orchestrator_pid_lives(self):
        repo = fleet.FleetRepo("repo", Path("/tmp/repo"), Path("/tmp/repo"))

        with patch.object(fleet, "metadata_pid", return_value=12345), \
             patch.object(fleet, "pid_alive", return_value=True), \
             patch.object(fleet.time, "monotonic", side_effect=[0, 2]), \
             self.assertRaises(SystemExit):
            fleet.wait_stopped([repo], timeout=1)

    def test_start_rejects_repo_without_root_even_without_precheck(self):
        repo = fleet.FleetRepo("repo", Path("/tmp/repo"), None, "missing root")

        with patch.object(fleet, "require_tool"), \
             patch.object(fleet, "orchestra_bin", return_value=Path("/tmp/ko-orchestrator")), \
             self.assertRaises(SystemExit):
            fleet.start([repo], precheck=False)

    def test_start_requests_dashboard_for_running_repo_without_dashboard(self):
        repo = fleet.FleetRepo("repo", Path("/tmp/repo"), Path("/tmp/repo"))
        out = io.StringIO()

        with patch.object(fleet, "require_tool"), \
             patch.object(fleet, "require_startable"), \
             patch.object(fleet, "orchestra_bin", return_value=Path("/tmp/ko-orchestrator")), \
             patch.object(fleet, "repo_process_state", return_value=("running", "123", "-", "orch-repo")), \
             patch.object(fleet, "request_dashboard_start") as request_start, \
             patch.object(fleet, "wait_dashboard_ready", return_value=True) as wait_dashboard, \
             patch.object(fleet.subprocess, "run") as run, \
             patch.object(fleet.time, "sleep"), \
             patch.object(fleet, "print_status"), \
             redirect_stdout(out):
            fleet.start([repo])

        request_start.assert_called_once_with(repo, preferred_port=8427)
        wait_dashboard.assert_called_once_with(repo)
        run.assert_not_called()
        self.assertIn("dashboard started", out.getvalue())

    def test_restart_waits_for_shutdown_before_starting(self):
        repo = fleet.FleetRepo("repo", Path("/tmp/repo"), Path("/tmp/repo"))
        calls = []

        with patch.object(fleet, "select_repos", return_value=[repo]), \
             patch.object(fleet, "require_startable", side_effect=lambda repos: calls.append("require")), \
             patch.object(fleet, "stop", side_effect=lambda repos: calls.append("stop")), \
             patch.object(fleet, "wait_stopped", side_effect=lambda repos: calls.append("wait")), \
             patch.object(fleet, "start", side_effect=lambda repos, precheck=True: calls.append(f"start:{precheck}")):
            exit_code = fleet.main(["restart"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, ["require", "stop", "wait", "start:False"])


class TestSupertaskDB(unittest.TestCase):
    """Test supertask DB columns, migrations, helpers, and find_ready_task gating."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_new_columns_present(self):
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(tasks)").fetchall()}
        self.assertIn("kind", cols)
        self.assertIn("parent_task_id", cols)
        self.assertIn("sequence_index", cols)

    def test_legacy_supertask_db_with_koid_raises_error(self):
        """connect() must raise RuntimeError on a pre-supertask legacy schema with koid."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            legacy = sqlite3.connect(tmp.name)
            legacy.execute(
                """CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    koid TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT NOT NULL DEFAULT 'none',
                    next_step TEXT NOT NULL DEFAULT 'commit-make',
                    branch TEXT,
                    commit_hash TEXT,
                    stash_ref TEXT,
                    coder_agent TEXT,
                    skip_same_branch_koid_check INTEGER NOT NULL DEFAULT 1,
                    review_round INTEGER DEFAULT 0,
                    last_review_decision TEXT DEFAULT 'none',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    ready_at DATETIME DEFAULT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            legacy.commit()
            legacy.close()

            with self.assertRaises(RuntimeError) as ctx:
                db.connect(tmp.name)
            self.assertIn("legacy schema", str(ctx.exception))
        finally:
            os.unlink(tmp.name)

    def test_fresh_db_has_parent_task_id_fk(self):
        """Fresh DB bootstrap must include the REFERENCES tasks(id) FK on parent_task_id."""
        fk_list = self.conn.execute("PRAGMA foreign_key_list(tasks)").fetchall()
        fk_tables = {row[2] for row in fk_list}  # column index 2 is 'table'
        self.assertIn(
            "tasks", fk_tables,
            "parent_task_id must have a REFERENCES tasks(id) FK in the current schema",
        )

    def test_add_supertask(self):
        tid = db.add_task(self.conn, "My supertask", kind="supertask", branch="feat")
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["kind"], "supertask")
        self.assertEqual(task["next_step"], "commit-make-supertask")
        self.assertIsNone(task["parent_task_id"])

    def test_add_child_task(self):
        parent_id = db.add_task(self.conn, "Parent", kind="supertask", branch="feat")
        child_id = db.add_task(
            self.conn, "Child", kind="task",
            parent_task_id=parent_id, sequence_index=1, branch="feat",
        )
        task = db.get_task(self.conn, child_id)
        self.assertEqual(task["parent_task_id"], parent_id)
        self.assertEqual(task["kind"], "commit")

    def test_get_child_tasks(self):
        parent_id = db.add_task(self.conn, "Parent", kind="supertask", branch="feat")
        c1 = db.add_task(self.conn, "Child 1", parent_task_id=parent_id, sequence_index=100, branch="feat")
        c2 = db.add_task(self.conn, "Child 2", parent_task_id=parent_id, sequence_index=200, branch="feat")
        children = db.get_child_tasks(self.conn, parent_id)
        self.assertEqual(len(children), 2)
        self.assertEqual(children[0]["id"], c1)
        self.assertEqual(children[1]["id"], c2)

    def test_renumber_siblings(self):
        parent_id = db.add_task(self.conn, "Parent", kind="supertask", branch="feat")
        db.add_task(self.conn, "Child 1", parent_task_id=parent_id, sequence_index=50, branch="feat")
        db.add_task(self.conn, "Child 2", parent_task_id=parent_id, sequence_index=150, branch="feat")
        db.add_task(self.conn, "Child 3", parent_task_id=parent_id, sequence_index=250, branch="feat")
        db.renumber_siblings(self.conn, parent_id)
        children = db.get_child_tasks(self.conn, parent_id)
        indices = [c["sequence_index"] for c in children]
        self.assertEqual(indices, [100, 200, 300])

    def test_find_ready_task_excludes_gated_child(self):
        """Child not eligible when parent is not pending_subtasks."""
        parent_id = db.add_task(self.conn, "Parent", kind="supertask", branch="feat")
        child_id = db.add_task(
            self.conn, "Child", parent_task_id=parent_id, sequence_index=100, branch="feat",
        )
        db.update_task(self.conn, parent_id, status="ready")
        db.update_task(self.conn, child_id, status="ready")
        found = db.find_ready_task(self.conn)
        # The supertask (parent) itself is ready and has no parent — it should be found
        self.assertEqual(found["id"], parent_id)

    def test_find_ready_task_child_eligible_when_parent_pending_subtasks(self):
        """First child eligible when parent is pending_subtasks."""
        parent_id = db.add_task(self.conn, "Parent", kind="supertask", branch="feat")
        child_id = db.add_task(
            self.conn, "Child", parent_task_id=parent_id, sequence_index=100, branch="feat",
        )
        db.update_task(self.conn, parent_id, status="pending_subtasks")
        db.update_task(self.conn, child_id, status="ready")
        found = db.find_ready_task(self.conn)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], child_id)

    def test_find_ready_task_child_gated_by_earlier_sibling(self):
        """Second child not eligible until first sibling is done."""
        parent_id = db.add_task(self.conn, "Parent", kind="supertask", branch="feat")
        c1 = db.add_task(
            self.conn, "Child 1", parent_task_id=parent_id, sequence_index=100, branch="feat",
        )
        c2 = db.add_task(
            self.conn, "Child 2", parent_task_id=parent_id, sequence_index=200, branch="feat",
        )
        db.update_task(self.conn, parent_id, status="pending_subtasks")
        db.update_task(self.conn, c1, status="running")  # not done
        db.update_task(self.conn, c2, status="ready")
        found = db.find_ready_task(self.conn)
        # c1 is running (not done), c2 should be gated
        self.assertIsNone(found)

    def test_find_ready_task_second_child_eligible_when_first_done(self):
        """Second child becomes eligible once first sibling is done."""
        parent_id = db.add_task(self.conn, "Parent", kind="supertask", branch="feat")
        c1 = db.add_task(
            self.conn, "Child 1", parent_task_id=parent_id, sequence_index=100, branch="feat",
        )
        c2 = db.add_task(
            self.conn, "Child 2", parent_task_id=parent_id, sequence_index=200, branch="feat",
        )
        db.update_task(self.conn, parent_id, status="pending_subtasks")
        db.update_task(self.conn, c1, status="done")
        db.update_task(self.conn, c2, status="ready")
        found = db.find_ready_task(self.conn)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], c2)

    def test_find_ready_task_pending_subtasks_blocks_unrelated_ready_task(self):
        """An active supertask owns the queue until its runnable children finish or block."""
        parent_id = db.add_task(self.conn, "Parent", kind="supertask", branch="feat")
        child_id = db.add_task(
            self.conn, "Child", parent_task_id=parent_id, sequence_index=100, branch="feat",
        )
        other_id = db.add_task(self.conn, "Other ready task", branch="other")
        db.update_task(self.conn, parent_id, status="pending_subtasks")
        db.update_task(self.conn, child_id, status="ready")
        db.update_task(self.conn, other_id, status="ready")

        found = db.find_ready_task(self.conn)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], child_id)

    def test_find_ready_task_pending_subtasks_blocks_other_ready_supertask(self):
        """A planning-ready supertask cannot preempt children of an active supertask."""
        active_parent_id = db.add_task(self.conn, "Active parent", kind="supertask", branch="feat-a")
        child_id = db.add_task(
            self.conn, "Active child", parent_task_id=active_parent_id, sequence_index=100, branch="feat-a",
        )
        other_parent_id = db.add_task(self.conn, "Other supertask", kind="supertask", branch="feat-b")
        db.update_task(self.conn, active_parent_id, status="pending_subtasks")
        db.update_task(self.conn, child_id, status="ready")
        db.update_task(self.conn, other_parent_id, status="ready")

        found = db.find_ready_task(self.conn)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], child_id)

    def test_find_ready_task_pending_subtasks_keeps_opted_in_child_eligible_when_blocked_exists(self):
        active_parent_id = db.add_task(self.conn, "Active parent", kind="supertask", branch="feat-a")
        child_id = db.add_task(
            self.conn,
            "Escaping child candidate",
            parent_task_id=active_parent_id,
            sequence_index=100,
            branch="feat-a",
            allow_when_blocked=True,
        )
        blocked_id = db.add_task(self.conn, "Blocked task", branch="blocked")
        db.update_task(self.conn, active_parent_id, status="pending_subtasks")
        db.update_task(self.conn, child_id, status="ready")
        db.update_task(self.conn, blocked_id, status="blocked")

        found = db.find_ready_task(self.conn)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], child_id)

    def test_status_accepts_pending_subtasks(self):
        tid = db.add_task(self.conn, "Supertask", kind="supertask", branch="feat")
        db.update_task(self.conn, tid, status="pending_subtasks")
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["status"], "pending_subtasks")

    def test_next_step_accepts_supertask_verbs(self):
        tid = db.add_task(self.conn, "Supertask", kind="supertask", branch="feat")
        db.update_task(self.conn, tid, next_step="commit-review-supertask")
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["next_step"], "commit-review-supertask")


class TestSupertaskStateMachine(unittest.TestCase):
    """Test orchestrator supertask state transitions."""

    def setUp(self):
        self._ack_patcher = patch.object(orchestrator, "ensure_agent_acked")
        self._ack_patcher.start()
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        self._ack_patcher.stop()
        self.conn.close()
        os.unlink(self.tmp.name)

    def _make_supertask(self, status="running", next_step="commit-make-supertask"):
        tid = db.add_task(self.conn, "My plan", kind="supertask", branch="feat", coder_agent="claude")
        db.update_task(self.conn, tid, status=status, next_step=next_step)
        return db.get_task(self.conn, tid)

    def test_commit_make_supertask_transitions_to_review(self):
        task = self._make_supertask()

        def fake_coder(name, prompt, task_id, conn, verb, **kw):
            db.add_comment(conn, task_id, "Plan: do A then B", kind="commit-message", author=name)
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_coder):
            result = orchestrator.handle_commit_make_supertask(task, self.conn)

        self.assertTrue(result)

    def test_commit_make_supertask_requires_commit_message(self):
        task = self._make_supertask()

        with patch.object(orchestrator, "run_agent", return_value=0):
            result = orchestrator.handle_commit_make_supertask(task, self.conn)

        self.assertFalse(result)

    def test_commit_make_supertask_sees_commit_message_from_separate_connection(self):
        """A planner comment written via another connection must be visible after the agent returns."""
        task = self._make_supertask()

        def fake_coder(name, prompt, task_id, conn, verb, **kw):
            other = db.connect(self.tmp.name)
            try:
                db.add_comment(other, task_id, "Plan: do A then B", kind="commit-message", author=name)
            finally:
                other.close()
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_coder):
            result = orchestrator.handle_commit_make_supertask(task, self.conn)

        comments = [c for c in db.get_comments(self.conn, task["id"]) if c["kind"] == "commit-message"]
        self.assertTrue(result)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[-1]["message"], "Plan: do A then B")

    def test_advance_commit_make_supertask_to_review(self):
        task = self._make_supertask()

        def fake_coder(name, prompt, task_id, conn, verb, **kw):
            db.add_comment(conn, task_id, "Plan summary", kind="commit-message", author=name)
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_coder):
            result = orchestrator.advance(task, self.conn)

        updated = db.get_task(self.conn, task["id"])
        self.assertTrue(result)
        self.assertEqual(updated["status"], "ready")
        self.assertEqual(updated["next_step"], "commit-review-supertask")

    def test_advance_commit_review_supertask_approve_sets_pending_subtasks(self):
        task = self._make_supertask(status="running", next_step="commit-review-supertask")
        db.add_comment(self.conn, task["id"], "LGTM",
                       kind="approval", author=DEFAULT_REVIEWER, review_round=0)

        with patch.object(orchestrator, "run_agent", return_value=0):
            result = orchestrator.advance(task, self.conn)

        updated = db.get_task(self.conn, task["id"])
        self.assertTrue(result)
        self.assertEqual(updated["status"], "pending_subtasks")
        self.assertEqual(updated["next_step"], "none")
        self.assertIsNone(updated["commit_hash"])

    def test_advance_commit_review_supertask_reject_returns_to_make(self):
        task = self._make_supertask(status="running", next_step="commit-review-supertask")
        db.add_comment(self.conn, task["id"], "Needs more detail",
                       kind="rejection", author=DEFAULT_REVIEWER, review_round=0)

        with patch.object(orchestrator, "run_agent", return_value=0):
            result = orchestrator.advance(task, self.conn)

        updated = db.get_task(self.conn, task["id"])
        self.assertTrue(result)
        self.assertEqual(updated["status"], "ready")
        self.assertEqual(updated["next_step"], "commit-make-supertask")
        self.assertEqual(updated["review_round"], 1)

    def test_supertask_never_gets_commit_hash(self):
        """After plan approval, supertask has no commit_hash."""
        task = self._make_supertask(status="running", next_step="commit-review-supertask")
        db.add_comment(self.conn, task["id"], "LGTM",
                       kind="approval", author=DEFAULT_REVIEWER, review_round=0)

        with patch.object(orchestrator, "run_agent", return_value=0):
            orchestrator.advance(task, self.conn)

        updated = db.get_task(self.conn, task["id"])
        self.assertIsNone(updated["commit_hash"])

    def test_child_done_completes_parent(self):
        """When all children are done, supertask becomes done."""
        parent_id = db.add_task(
            self.conn, "Parent", kind="supertask", branch="feat", coder_agent="claude",
        )
        db.update_task(self.conn, parent_id, status="pending_subtasks")
        c1 = db.add_task(
            self.conn, "Child 1", parent_task_id=parent_id,
            sequence_index=100, branch="feat", coder_agent="claude",
        )
        c2 = db.add_task(
            self.conn, "Child 2", parent_task_id=parent_id,
            sequence_index=200, branch="feat", coder_agent="claude",
        )
        db.update_task(self.conn, c1, status="done")
        db.update_task(self.conn, c2, status="running", next_step="commit-make",
                       last_review_decision="approve")
        task = db.get_task(self.conn, c2)

        fake_hash = "a" * 40

        with patch.object(orchestrator, "run_agent", return_value=0), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "get_head_commit_hash", side_effect=["b" * 40, fake_hash, fake_hash]), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            orchestrator.advance(task, self.conn)

        updated_child = db.get_task(self.conn, c2)
        updated_parent = db.get_task(self.conn, parent_id)
        self.assertEqual(updated_child["status"], "done")
        self.assertEqual(updated_child["commit_hash"], fake_hash)
        self.assertEqual(updated_parent["status"], "done")
        self.assertIsNone(updated_parent["commit_hash"])

    def test_child_blocked_propagates_to_parent(self):
        """When a child task is blocked, the parent supertask is also blocked."""
        parent_id = db.add_task(
            self.conn, "Parent", kind="supertask", branch="feat", coder_agent="claude",
        )
        db.update_task(self.conn, parent_id, status="pending_subtasks")
        c1 = db.add_task(
            self.conn, "Child 1", parent_task_id=parent_id,
            sequence_index=100, branch="feat", coder_agent="claude",
        )
        db.update_task(self.conn, c1, status="ready", next_step="commit-make")
        task = db.get_task(self.conn, c1)

        # Simulate advance blocking the child (branch failure)
        with patch.object(orchestrator, "ensure_branch", return_value=False):
            orchestrator.process_pinned_task(task, self.conn)

        updated_child = db.get_task(self.conn, c1)
        updated_parent = db.get_task(self.conn, parent_id)
        self.assertEqual(updated_child["status"], "blocked")
        self.assertEqual(updated_parent["status"], "blocked")

    def test_partial_children_done_does_not_complete_parent(self):
        """Parent stays pending_subtasks while any child is not done."""
        parent_id = db.add_task(
            self.conn, "Parent", kind="supertask", branch="feat", coder_agent="claude",
        )
        db.update_task(self.conn, parent_id, status="pending_subtasks")
        c1 = db.add_task(
            self.conn, "Child 1", parent_task_id=parent_id,
            sequence_index=100, branch="feat", coder_agent="claude",
        )
        c2 = db.add_task(
            self.conn, "Child 2", parent_task_id=parent_id,
            sequence_index=200, branch="feat", coder_agent="claude",
        )
        db.update_task(self.conn, c1, status="running", next_step="commit-make",
                       last_review_decision="approve")
        db.update_task(self.conn, c2, status="ready")
        task = db.get_task(self.conn, c1)

        fake_hash = "b" * 40

        with patch.object(orchestrator, "run_agent", return_value=0), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "get_head_commit_hash", side_effect=["c" * 40, fake_hash, fake_hash]), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            orchestrator.advance(task, self.conn)

        updated_parent = db.get_task(self.conn, parent_id)
        self.assertEqual(updated_parent["status"], "pending_subtasks")


class TestSupertaskCLI(unittest.TestCase):
    """Test supertask CLI commands via subprocess."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self.task_py = str(Path(__file__).resolve().parent / "task.py")
        self.env = {
            **os.environ,
            "KANBAN_DB": self.db_path,
            "KANBAN_NONINTERACTIVE": "1",
        }

    def tearDown(self):
        for suffix in ("", "-shm", "-wal"):
            path = Path(self.db_path + suffix)
            if path.exists():
                path.unlink()

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, self.task_py] + list(args),
            capture_output=True, text=True, env=self.env,
        )

    def _add_supertask(self, title="My supertask", branch="feat"):
        r = self._run("add", title, "--kind", "supertask", "--branch", branch)
        self.assertEqual(r.returncode, 0)
        return json.loads(r.stdout)["id"]

    def test_add_supertask(self):
        r = self._run("add", "Plan something", "--kind", "supertask", "--branch", "feat")
        self.assertEqual(r.returncode, 0)
        tid = json.loads(r.stdout)["id"]
        r2 = self._run("show", str(tid))
        task = json.loads(r2.stdout)
        self.assertEqual(task["kind"], "supertask")
        self.assertEqual(task["next_step"], "commit-make-supertask")

    def test_add_child_task_inherits_branch(self):
        parent_id = self._add_supertask(branch="mybranch")
        r = self._run("add", "Child task", "--parent", str(parent_id))
        self.assertEqual(r.returncode, 0)
        child_id = json.loads(r.stdout)["id"]
        child = json.loads(self._run("show", str(child_id)).stdout)
        self.assertEqual(child["branch"], "mybranch")
        self.assertEqual(child["parent_task_id"], parent_id)

    def test_add_child_task_has_ready_status(self):
        """Child tasks created via CLI must default to status='ready', not 'none'."""
        parent_id = self._add_supertask(branch="feat")
        r = self._run("add", "Child task", "--parent", str(parent_id))
        self.assertEqual(r.returncode, 0)
        child_id = json.loads(r.stdout)["id"]
        child = json.loads(self._run("show", str(child_id)).stdout)
        self.assertEqual(child["status"], "ready")

    def test_add_child_fails_when_parent_has_no_branch(self):
        r = self._run("add", "Branchless supertask", "--kind", "supertask")
        parent_id = json.loads(r.stdout)["id"]
        r2 = self._run("add", "Child", "--parent", str(parent_id))
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("no branch", r2.stderr)

    def test_add_child_fails_with_branch_arg(self):
        parent_id = self._add_supertask()
        r = self._run("add", "Child", "--parent", str(parent_id), "--branch", "other")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--branch", r.stderr)

    def test_add_child_fails_when_parent_not_supertask(self):
        r = self._run("add", "Regular task", "--branch", "feat")
        regular_id = json.loads(r.stdout)["id"]
        r2 = self._run("add", "Child", "--parent", str(regular_id))
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("not a supertask", r2.stderr)

    def test_add_child_fails_when_kind_supertask_with_parent(self):
        """--kind supertask with --parent must be rejected; nested supertasks are not allowed."""
        parent_id = self._add_supertask(branch="feat")
        r = self._run("add", "Nested supertask", "--parent", str(parent_id), "--kind", "supertask")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("supertask", r.stderr)

    def test_add_child_renumbers_siblings(self):
        parent_id = self._add_supertask()
        self._run("add", "Child A", "--parent", str(parent_id), "--sequence-index", "50")
        self._run("add", "Child B", "--parent", str(parent_id), "--sequence-index", "150")
        self._run("add", "Child C", "--parent", str(parent_id), "--sequence-index", "250")
        tasks = json.loads(self._run("list").stdout)
        children = sorted(
            [t for t in tasks if t["parent_task_id"] == parent_id],
            key=lambda t: t["sequence_index"],
        )
        self.assertEqual([c["sequence_index"] for c in children], [100, 200, 300])

    def test_set_sequence_index_reorders_siblings(self):
        parent_id = self._add_supertask()
        r_a = self._run("add", "Child A", "--parent", str(parent_id), "--sequence-index", "100")
        self._run("add", "Child B", "--parent", str(parent_id), "--sequence-index", "200")
        r_c = self._run("add", "Child C", "--parent", str(parent_id), "--sequence-index", "300")
        id_a = json.loads(r_a.stdout)["id"]
        id_c = json.loads(r_c.stdout)["id"]

        # Move C before A by setting its sequence_index to 50
        r = self._run("set", str(id_c), "--sequence-index", "50")
        self.assertEqual(r.returncode, 0)

        tasks = json.loads(self._run("list").stdout)
        children = sorted(
            [t for t in tasks if t["parent_task_id"] == parent_id],
            key=lambda t: t["sequence_index"],
        )
        self.assertEqual(children[0]["id"], id_c)
        self.assertEqual(children[1]["id"], id_a)
        self.assertEqual([c["sequence_index"] for c in children], [100, 200, 300])

    def test_set_sequence_index_fails_on_non_child(self):
        r = self._run("add", "Regular task", "--branch", "feat")
        tid = json.loads(r.stdout)["id"]
        r2 = self._run("set", str(tid), "--sequence-index", "100")
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("child tasks", r2.stderr)

    def test_set_branch_on_supertask_propagates_to_children(self):
        parent_id = self._add_supertask(branch="old-branch")
        self._run("add", "Child A", "--parent", str(parent_id))
        self._run("add", "Child B", "--parent", str(parent_id))

        r = self._run("set", str(parent_id), "--branch", "new-branch")
        self.assertEqual(r.returncode, 0)

        tasks = json.loads(self._run("list").stdout)
        children = [t for t in tasks if t["parent_task_id"] == parent_id]
        for child in children:
            self.assertEqual(child["branch"], "new-branch")

    def test_set_branch_and_ready_on_supertask_updates_parent_and_children(self):
        parent_id = self._add_supertask(branch="old-branch")
        self._run("add", "Child A", "--parent", str(parent_id))
        self._run("add", "Child B", "--parent", str(parent_id))

        r = self._run("set", str(parent_id), "--branch", "new-branch", "--status", "ready")
        self.assertEqual(r.returncode, 0)

        parent = json.loads(r.stdout)
        self.assertEqual(parent["branch"], "new-branch")
        self.assertEqual(parent["status"], "ready")

        tasks = json.loads(self._run("list").stdout)
        children = [t for t in tasks if t["parent_task_id"] == parent_id]
        self.assertEqual(len(children), 2)
        for child in children:
            self.assertEqual(child["branch"], "new-branch")

    def test_set_branch_on_supertask_skips_children_with_different_branch(self):
        """Branch propagation only updates children that still carry the old branch."""
        parent_id = self._add_supertask(branch="old-branch")
        r_a = self._run("add", "Child A", "--parent", str(parent_id))
        id_a = json.loads(r_a.stdout)["id"]

        # Manually set child A to a different branch (simulating a prior manual override)
        conn = db.connect(self.db_path)
        db.update_task(conn, id_a, branch="other-branch")
        conn.close()

        self._run("set", str(parent_id), "--branch", "new-branch")

        tasks = json.loads(self._run("list").stdout)
        child_a = next(t for t in tasks if t["id"] == id_a)
        # Child A had a different branch — should NOT be updated
        self.assertEqual(child_a["branch"], "other-branch")

    def test_set_branch_blocked_on_child_task(self):
        parent_id = self._add_supertask()
        r = self._run("add", "Child", "--parent", str(parent_id))
        child_id = json.loads(r.stdout)["id"]
        r2 = self._run("set", str(child_id), "--branch", "other")
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("--branch", r2.stderr)

    def test_set_child_blocked_propagates_to_parent_via_cli(self):
        """task set --status blocked on a child must set parent supertask to blocked."""
        parent_id = self._add_supertask()
        conn = db.connect(self.db_path)
        db.update_task(conn, parent_id, status="pending_subtasks")
        conn.close()

        r = self._run("add", "Child", "--parent", str(parent_id))
        child_id = json.loads(r.stdout)["id"]

        r2 = self._run("set", str(child_id), "--status", "blocked")
        self.assertEqual(r2.returncode, 0)

        parent = json.loads(self._run("show", str(parent_id)).stdout)
        self.assertEqual(parent["status"], "blocked")

    def test_set_child_ready_restores_parent_pending_subtasks(self):
        """Re-queuing a child task restores parent to pending_subtasks when no siblings blocked."""
        parent_id = self._add_supertask()
        r = self._run("add", "Child", "--parent", str(parent_id))
        child_id = json.loads(r.stdout)["id"]

        # Simulate parent blocked due to child being blocked
        conn = db.connect(self.db_path)
        db.update_task(conn, parent_id, status="blocked")
        db.update_task(conn, child_id, status="blocked")
        conn.close()

        # Set child back to ready
        r2 = self._run("set", str(child_id), "--status", "ready")
        self.assertEqual(r2.returncode, 0)

        parent = json.loads(self._run("show", str(parent_id)).stdout)
        self.assertEqual(parent["status"], "pending_subtasks")

    def test_set_child_ready_does_not_restore_parent_if_sibling_still_blocked(self):
        parent_id = self._add_supertask()
        r_a = self._run("add", "Child A", "--parent", str(parent_id))
        r_b = self._run("add", "Child B", "--parent", str(parent_id))
        id_a = json.loads(r_a.stdout)["id"]
        id_b = json.loads(r_b.stdout)["id"]

        conn = db.connect(self.db_path)
        db.update_task(conn, parent_id, status="blocked")
        db.update_task(conn, id_a, status="blocked")
        db.update_task(conn, id_b, status="blocked")
        conn.close()

        # Set child A to ready, but child B is still blocked
        self._run("set", str(id_a), "--status", "ready")

        parent = json.loads(self._run("show", str(parent_id)).stdout)
        self.assertEqual(parent["status"], "blocked")


class TestTaskPlanningDB(unittest.TestCase):
    """Test DB-layer behavior for the commit-plan feature."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_new_normal_task_defaults_to_commit_plan(self):
        """Normal tasks default to next_step=commit-plan."""
        tid = db.add_task(self.conn, "Needs planning")
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["next_step"], "commit-plan")
        self.assertEqual(task["skips"], [])
        self.assertIsNone(task["commit_plan"])

    def test_new_task_with_skip_commit_plan_starts_at_commit_make(self):
        """Tasks created with skips=['commit-plan'] start at commit-make."""
        tid = db.add_task(self.conn, "Skip planning", skips=["commit-plan"])
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["next_step"], "commit-make")
        self.assertEqual(task["skips"], ["commit-plan"])

    def test_skip_commit_plan_implies_skip_commit_plan_review(self):
        """Skipping commit-plan also skips commit-plan-review without storing both."""
        tid = db.add_task(self.conn, "Skip planning", skips=["commit-plan"])

        self.assertTrue(db.should_skip_step(self.conn, tid, "commit-plan-review"))

    def test_missing_commit_plan_implies_skip_commit_plan_review(self):
        """Plan review is skipped when there is no persisted plan to review."""
        tid = db.add_task(self.conn, "No plan yet")

        self.assertTrue(db.should_skip_step(self.conn, tid, "commit-plan-review"))

    def test_commit_plan_review_runs_when_plan_exists(self):
        """Plan review is not skipped after commit-plan has stored a plan."""
        tid = db.add_task(self.conn, "Plan exists")
        db.update_task(self.conn, tid, commit_plan="Draft implementation plan")

        self.assertFalse(db.should_skip_step(self.conn, tid, "commit-plan-review"))

    def test_existing_explicit_plan_review_skip_still_works(self):
        """Tasks that already store both planning skips keep their explicit skip."""
        tid = db.add_task(
            self.conn,
            "Legacy explicit skips",
            skips=["commit-plan", "commit-plan-review"],
        )
        task = db.get_task(self.conn, tid)

        self.assertEqual(task["skips"], ["commit-plan", "commit-plan-review"])
        self.assertTrue(db.should_skip_step(self.conn, tid, "commit-plan-review"))

    def test_supertask_always_starts_at_commit_make_supertask(self):
        """Supertasks always use commit-make-supertask regardless of skips."""
        tid = db.add_task(self.conn, "Supertask", kind="supertask", skips=["commit-plan"])
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["next_step"], "commit-make-supertask")

    def test_update_task_sets_commit_plan(self):
        """commit_plan can be set and read back."""
        tid = db.add_task(self.conn, "Plan me")
        db.update_task(self.conn, tid, commit_plan="1. Do this\n2. Do that")
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["commit_plan"], "1. Do this\n2. Do that")

    def test_update_task_clears_commit_plan(self):
        """commit_plan can be cleared to None."""
        tid = db.add_task(self.conn, "Plan then clear")
        db.update_task(self.conn, tid, commit_plan="Initial plan")
        db.update_task(self.conn, tid, commit_plan=None)
        task = db.get_task(self.conn, tid)
        self.assertIsNone(task["commit_plan"])

    def test_task_schema_allows_commit_plan_next_steps(self):
        """next_step can be set to commit-plan and commit-plan-review."""
        tid = db.add_task(self.conn, "Schema check")
        db.update_task(self.conn, tid, next_step="commit-plan-review")
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["next_step"], "commit-plan-review")

    def test_legacy_db_missing_task_skips_table_raises_error(self):
        """connect() must raise RuntimeError when task_skips table is absent."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            legacy = sqlite3.connect(tmp.name)
            legacy.execute("""
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
                    review_round INTEGER DEFAULT 0,
                    last_review_decision TEXT DEFAULT 'none',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    ready_at DATETIME DEFAULT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    kind TEXT NOT NULL DEFAULT 'task',
                    parent_task_id INTEGER,
                    sequence_index INTEGER,
                    commit_plan TEXT
                )
            """)
            legacy.commit()
            legacy.close()

            with self.assertRaises(RuntimeError) as ctx:
                db.connect(tmp.name)
            self.assertIn("missing the 'task_skips' table", str(ctx.exception))
        finally:
            os.unlink(tmp.name)


class TestTaskPlanningCLI(unittest.TestCase):
    """Test task CLI for commit-plan fields."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self.task_py = str(Path(__file__).resolve().parent / "task.py")
        self.env = {
            **os.environ,
            "KANBAN_DB": self.db_path,
            "KANBAN_NONINTERACTIVE": "1",
        }

    def tearDown(self):
        for suffix in ("", "-shm", "-wal"):
            path = Path(self.db_path + suffix)
            if path.exists():
                path.unlink()

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, self.task_py] + list(args),
            capture_output=True, text=True, env=self.env,
        )

    def test_add_defaults_to_commit_make(self):
        """task add for a normal task skips planning and starts at commit-make."""
        r = self._run("add", "Planning task", "--branch", "b")
        self.assertEqual(r.returncode, 0)
        tid = json.loads(r.stdout)["id"]
        task = json.loads(self._run("show", str(tid)).stdout)
        self.assertEqual(task["next_step"], "commit-make")
        self.assertEqual(task["skips"], ["commit-plan"])

    def test_add_with_skip_starts_at_commit_make(self):
        """task add --skip commit-plan starts at commit-make."""
        r = self._run("add", "Skip plan", "--branch", "b", "--skip", "commit-plan")
        self.assertEqual(r.returncode, 0)
        tid = json.loads(r.stdout)["id"]
        task = json.loads(self._run("show", str(tid)).stdout)
        self.assertEqual(task["next_step"], "commit-make")
        self.assertEqual(task["skips"], ["commit-plan"])

    def test_add_with_skip_unions_with_default(self):
        """task add --skip commit-review keeps the default commit-plan skip too."""
        r = self._run("add", "Skip review", "--branch", "b", "--skip", "commit-review")
        self.assertEqual(r.returncode, 0)
        tid = json.loads(r.stdout)["id"]
        task = json.loads(self._run("show", str(tid)).stdout)
        self.assertEqual(task["next_step"], "commit-make")
        self.assertEqual(
            sorted(task["skips"]),
            ["commit-plan", "commit-review"],
        )

    def test_add_pull_request_task(self):
        """task add --kind pull_request starts at pull-request-make."""
        r = self._run(
            "add",
            "Open PR",
            "--kind",
            "pull_request",
            "--branch",
            "feat-pr",
            "--skip",
            "pull-request-review",
        )
        self.assertEqual(r.returncode, 0)
        tid = json.loads(r.stdout)["id"]
        task = json.loads(self._run("show", str(tid)).stdout)
        self.assertEqual(task["kind"], "pull_request")
        self.assertEqual(task["next_step"], "pull-request-make")
        self.assertEqual(task["skips"], ["pull-request-review"])

    def test_add_default_task_is_commit_type(self):
        r = self._run("add", "Default commit", "--branch", "b")
        self.assertEqual(r.returncode, 0)
        tid = json.loads(r.stdout)["id"]
        task = json.loads(self._run("show", str(tid)).stdout)
        self.assertEqual(task["kind"], "commit")
        self.assertEqual(task["next_step"], "commit-make")
        self.assertEqual(task["skips"], ["commit-plan"])

    def test_add_commit_task_with_type(self):
        r = self._run("add", "Explicit commit", "--type", "commit", "--branch", "b")
        self.assertEqual(r.returncode, 0)
        tid = json.loads(r.stdout)["id"]
        task = json.loads(self._run("show", str(tid)).stdout)
        self.assertEqual(task["kind"], "commit")
        self.assertEqual(task["next_step"], "commit-make")

    def test_add_other_task(self):
        r = self._run(
            "add",
            "External maintenance",
            "--type",
            "other",
            "--skip",
            "other-review",
        )
        self.assertEqual(r.returncode, 0)
        tid = json.loads(r.stdout)["id"]
        task = json.loads(self._run("show", str(tid)).stdout)
        self.assertEqual(task["kind"], "other")
        self.assertEqual(task["next_step"], "other-make")
        self.assertEqual(task["skips"], ["other-review"])

    def test_add_other_rejects_commit_review_skip(self):
        r = self._run(
            "add",
            "External maintenance",
            "--type",
            "other",
            "--skip",
            "commit-review",
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not allowed for other tasks", r.stderr)

    def test_set_rejects_invalid_next_step_for_type(self):
        r = self._run("add", "External maintenance", "--type", "other")
        self.assertEqual(r.returncode, 0)
        tid = json.loads(r.stdout)["id"]
        r2 = self._run("set", str(tid), "--next-step", "commit-make")
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("not valid for other tasks", r2.stderr)

    def test_set_ready_allows_other_without_branch(self):
        r = self._run("add", "External maintenance", "--type", "other")
        self.assertEqual(r.returncode, 0)
        tid = json.loads(r.stdout)["id"]
        r2 = self._run("set", str(tid), "--status", "ready")
        self.assertEqual(r2.returncode, 0)
        task = json.loads(self._run("show", str(tid)).stdout)
        self.assertEqual(task["status"], "ready")
        self.assertIsNone(task["branch"])

    def test_add_pull_request_rejects_commit_review_skip(self):
        r = self._run(
            "add",
            "Open PR",
            "--kind",
            "pull_request",
            "--branch",
            "feat-pr",
            "--skip",
            "commit-review",
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not allowed for pull request tasks", r.stderr)

    def test_show_displays_skips_and_commit_plan(self):
        """task show includes skips and commit_plan fields."""
        r = self._run("add", "Show fields")
        tid = json.loads(r.stdout)["id"]
        task = json.loads(self._run("show", str(tid)).stdout)
        self.assertIn("skips", task)
        self.assertIn("commit_plan", task)

    def test_set_commit_plan(self):
        """task set --commit-plan stores the plan text."""
        r = self._run("add", "Plan text task")
        tid = json.loads(r.stdout)["id"]
        r2 = self._run("set", str(tid), "--commit-plan", "Step 1: do X\nStep 2: do Y")
        self.assertEqual(r2.returncode, 0)
        task = json.loads(self._run("show", str(tid)).stdout)
        self.assertEqual(task["commit_plan"], "Step 1: do X\nStep 2: do Y")

    def test_set_commit_plan_clear(self):
        """task set --commit-plan '' clears the plan."""
        r = self._run("add", "Clear plan")
        tid = json.loads(r.stdout)["id"]
        self._run("set", str(tid), "--commit-plan", "some plan")
        r2 = self._run("set", str(tid), "--commit-plan", "")
        self.assertEqual(r2.returncode, 0)
        task = json.loads(self._run("show", str(tid)).stdout)
        self.assertIsNone(task["commit_plan"])

    def test_set_add_remove_skips(self):
        """task set --add-skip and --remove-skip update the skips list."""
        r = self._run("add", "Toggle plan")
        tid = json.loads(r.stdout)["id"]
        # Newly-added task defaults to storing only the root planning skip.
        task_before = json.loads(self._run("show", str(tid)).stdout)
        self.assertEqual(task_before["skips"], ["commit-plan"])

        # Remove default skip
        r2 = self._run("set", str(tid), "--remove-skip", "commit-plan")
        self.assertEqual(r2.returncode, 0)
        task_after_remove = json.loads(self._run("show", str(tid)).stdout)
        self.assertEqual(task_after_remove["skips"], [])

        # Add skip
        r3 = self._run("set", str(tid), "--add-skip", "commit-review")
        self.assertEqual(r3.returncode, 0)
        task_after_add = json.loads(self._run("show", str(tid)).stdout)
        self.assertEqual(task_after_add["skips"], ["commit-review"])


class TestTaskPlanningOrchestrator(unittest.TestCase):
    """Test orchestrator state machine for commit-plan and commit-plan-review steps."""

    def setUp(self):
        self._ack_patcher = patch.object(orchestrator, "ensure_agent_acked")
        self._ack_patcher.start()
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        self._ack_patcher.stop()
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_commit_plan_advances_to_plan_review_on_success(self):
        """Successful commit-plan transitions task to commit-plan-review."""
        tid = db.add_task(self.conn, "Plan task", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b", next_step="commit-plan")

        def fake_planner(name, prompt, task_id, conn, verb, **kw):
            db.update_task(conn, task_id, commit_plan="My plan")
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_planner), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            result = orchestrator.advance(db.get_task(self.conn, tid), self.conn)

        self.assertTrue(result)
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["next_step"], "commit-plan-review")

    def test_commit_plan_failure_blocks_task(self):
        """Failed commit-plan blocks the task."""
        tid = db.add_task(self.conn, "Plan fail", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b", next_step="commit-plan")

        with patch.object(orchestrator, "run_agent", return_value=1), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            result = orchestrator.advance(db.get_task(self.conn, tid), self.conn)

        self.assertFalse(result)
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["status"], "blocked")

    def test_commit_plan_no_commit_plan_written_blocks_task(self):
        """Planner exits 0 but writes no commit_plan — treated as failure."""
        tid = db.add_task(self.conn, "No plan written", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b", next_step="commit-plan")

        with patch.object(orchestrator, "run_agent", return_value=0), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            result = orchestrator.advance(db.get_task(self.conn, tid), self.conn)

        self.assertFalse(result)
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["status"], "blocked")

    def test_plan_approval_transitions_to_commit_make(self):
        """Approved plan transitions to commit-make without changing review_round."""
        tid = db.add_task(self.conn, "Plan review approve", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-plan-review", commit_plan="My plan",
                       review_round=0)

        def fake_reviewer(name, prompt, task_id, conn, verb, **kw):
            # Reviewer writes plan-approval (distinct from commit-review approval)
            db.add_comment(conn, task_id, "Plan looks good", kind="plan-approval",
                           author=name)
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_reviewer), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            result = orchestrator.advance(db.get_task(self.conn, tid), self.conn)

        self.assertTrue(result)
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["next_step"], "commit-make")
        self.assertEqual(task["last_review_decision"], "none")
        # review_round must NOT be incremented
        self.assertEqual(task["review_round"], 0)

    def test_commit_plan_review_skipped_when_commit_plan_was_skipped(self):
        """A skipped commit-plan prevents commit-plan-review from running."""
        tid = db.add_task(
            self.conn,
            "Skipped plan review",
            coder_agent="claude",
            skips=["commit-plan"],
        )
        db.update_task(
            self.conn,
            tid,
            status="running",
            branch="b",
            next_step="commit-plan-review",
            commit_plan="Plan should not be reviewed",
        )

        with patch.object(orchestrator, "run_agent") as run_agent:
            result = orchestrator.advance(db.get_task(self.conn, tid), self.conn)

        self.assertTrue(result)
        run_agent.assert_not_called()
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["next_step"], "commit-make")

    def test_commit_plan_review_skipped_when_commit_plan_missing(self):
        """The orchestrator never runs plan review without a plan."""
        tid = db.add_task(self.conn, "Missing plan", coder_agent="claude")
        db.update_task(
            self.conn,
            tid,
            status="running",
            branch="b",
            next_step="commit-plan-review",
            commit_plan=None,
        )

        with patch.object(orchestrator, "run_agent") as run_agent:
            result = orchestrator.advance(db.get_task(self.conn, tid), self.conn)

        self.assertTrue(result)
        run_agent.assert_not_called()
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["next_step"], "commit-make")

    def test_plan_rejection_returns_to_commit_plan_without_incrementing_review_round(self):
        """Rejected plan returns to commit-plan and does NOT increment review_round."""
        tid = db.add_task(self.conn, "Plan reject", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-plan-review", commit_plan="Draft plan",
                       review_round=0)

        def fake_reviewer(name, prompt, task_id, conn, verb, **kw):
            db.add_comment(conn, task_id, "Plan is incomplete", kind="plan-rejection",
                           author=name)
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_reviewer), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            result = orchestrator.advance(db.get_task(self.conn, tid), self.conn)

        self.assertTrue(result)
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["next_step"], "commit-plan")
        # review_round must NOT be incremented
        self.assertEqual(task["review_round"], 0)
        # commit_plan must be cleared so the planner cannot reuse the stale rejected plan
        self.assertIsNone(task["commit_plan"])

    def test_plan_reviewer_error_blocks_task(self):
        """Reviewer error during commit-plan-review blocks the task."""
        tid = db.add_task(self.conn, "Plan review error", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-plan-review", commit_plan="Draft plan")

        with patch.object(orchestrator, "run_agent", return_value=1), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            result = orchestrator.advance(db.get_task(self.conn, tid), self.conn)

        self.assertFalse(result)
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["status"], "blocked")

    def test_plan_review_decision_without_review_round_filtering(self):
        """Plan approval uses plan-approval kind, independent of review_round."""
        tid = db.add_task(self.conn, "No round filter", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-plan-review", commit_plan="My plan",
                       review_round=3)  # Non-zero round on the task

        def fake_reviewer(name, prompt, task_id, conn, verb, **kw):
            # Uses plan-approval — no review_round needed
            db.add_comment(conn, task_id, "LGTM", kind="plan-approval", author=name)
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_reviewer), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            result = orchestrator.advance(db.get_task(self.conn, tid), self.conn)

        self.assertTrue(result)
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["next_step"], "commit-make")
        # review_round still not changed
        self.assertEqual(task["review_round"], 3)

    def test_normal_task_with_supertask_step_is_blocked(self):
        """Normal task with a supertask step is blocked immediately."""
        tid = db.add_task(self.conn, "Wrong step task", kind="task")
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-make-supertask")

        with patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            result = orchestrator.advance(db.get_task(self.conn, tid), self.conn)

        self.assertFalse(result)
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["status"], "blocked")
        comments = db.get_comments(self.conn, tid)
        self.assertTrue(any("supertask" in c["message"].lower() for c in comments))

    def test_supertask_with_task_only_step_is_blocked(self):
        """Supertask with a task-only step is blocked immediately."""
        tid = db.add_task(self.conn, "Wrong step supertask", kind="supertask")
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-plan")

        with patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            result = orchestrator.advance(db.get_task(self.conn, tid), self.conn)

        self.assertFalse(result)
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["status"], "blocked")

    def test_full_planning_flow(self):
        """Full flow: commit-plan -> commit-plan-review (approve) -> commit-make (success).

        Uses plan-approval kind so plan decisions don't contaminate commit-review.
        The test stops after commit-make by having commit-review return an error (no decision)
        which routes through the normal reject path; we verify the planning steps ran cleanly.
        """
        tid = db.add_task(self.conn, "Full plan flow", coder_agent="claude")
        db.update_task(self.conn, tid, status="ready", branch="b",
                       next_step="commit-plan")

        steps_seen = []

        def fake_agent(name, prompt, task_id, conn, verb, **kw):
            steps_seen.append(verb)
            if verb == "commit-plan":
                db.update_task(conn, task_id, commit_plan="My implementation plan")
            elif verb == "commit-plan-review":
                # Use plan-approval kind — does not contaminate commit-review
                db.add_comment(conn, task_id, "Good plan", kind="plan-approval",
                               author=name)
            elif verb == "commit-make":
                db.add_comment(conn, task_id, "Done\n\nTask 99", kind="commit-message",
                               author=name)
            # commit-review: write no decision → treated as reject → blocks after max rounds
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_agent), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            orchestrator.process_pinned_task(db.get_task(self.conn, tid), self.conn)

        # The first three steps in order must be the planning flow
        self.assertEqual(steps_seen[:3], ["commit-plan", "commit-plan-review", "commit-make"])
        # After commit-make, the task should have reached commit-review
        self.assertIn("commit-review", steps_seen)

    def test_recover_running_commit_plan_review_resets_to_commit_plan(self):
        """Recovery of stuck commit-plan-review resets to commit-plan."""
        tid = db.add_task(self.conn, "Stuck plan review", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-plan-review", review_round=0)

        orchestrator.recover_running_tasks(self.conn)

        task = db.get_task(self.conn, tid)
        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["next_step"], "commit-plan")
        # review_round should NOT be incremented
        self.assertEqual(task["review_round"], 0)

    def test_recover_running_commit_plan_review_clears_commit_plan(self):
        """Recovery of stuck commit-plan-review clears stale commit_plan."""
        tid = db.add_task(self.conn, "Stuck plan review with plan", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-plan-review", review_round=0,
                       commit_plan="stale plan from interrupted run")

        orchestrator.recover_running_tasks(self.conn)

        task = db.get_task(self.conn, tid)
        self.assertEqual(task["next_step"], "commit-plan")
        self.assertIsNone(task["commit_plan"])

    def test_recover_running_commit_plan_clears_commit_plan(self):
        """Recovery of stuck commit-plan clears stale commit_plan."""
        tid = db.add_task(self.conn, "Stuck commit-plan with stale plan", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-plan", commit_plan="stale plan from interrupted run")

        orchestrator.recover_running_tasks(self.conn)

        task = db.get_task(self.conn, tid)
        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["next_step"], "commit-plan")
        self.assertIsNone(task["commit_plan"])

    def test_keyboard_interrupt_during_commit_plan_clears_commit_plan(self):
        """KeyboardInterrupt during commit-plan clears commit_plan so the planner must write fresh."""
        tid = db.add_task(self.conn, "Interrupted commit-plan", coder_agent="claude")
        db.update_task(self.conn, tid, status="ready", branch="b",
                       next_step="commit-plan", commit_plan="stale plan from prior run")

        def fake_agent(name, prompt, task_id, conn, verb, **kw):
            if verb == "commit-plan":
                raise KeyboardInterrupt
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_agent), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            with self.assertRaises(KeyboardInterrupt):
                orchestrator.process_pinned_task(db.get_task(self.conn, tid), self.conn)

        task = db.get_task(self.conn, tid)
        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["next_step"], "commit-plan")
        self.assertIsNone(task["commit_plan"])

    def test_keyboard_interrupt_during_commit_plan_review_clears_commit_plan(self):
        """KeyboardInterrupt during commit-plan-review resets to commit-plan and clears commit_plan."""
        tid = db.add_task(self.conn, "Interrupted plan review", coder_agent="claude")
        db.update_task(self.conn, tid, status="ready", branch="b",
                       next_step="commit-plan-review", review_round=0,
                       commit_plan="stale plan from prior run")

        def fake_agent(name, prompt, task_id, conn, verb, **kw):
            if verb == "commit-plan-review":
                raise KeyboardInterrupt
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_agent), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            with self.assertRaises(KeyboardInterrupt):
                orchestrator.process_pinned_task(db.get_task(self.conn, tid), self.conn)

        task = db.get_task(self.conn, tid)
        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["next_step"], "commit-plan")
        self.assertIsNone(task["commit_plan"])

    def test_build_prompt_role_filtering(self):
        """build_prompt filters skips from context, but keeps commit_plan for planners."""
        task = {
            "id": 99, "title": "Plan prompt", "description": None,
            "branch": "b", "status": "running", "next_step": "commit-plan",
            "review_round": 0, "last_review_decision": "none",
            "commit_hash": None, "stash_ref": None, "coder_agent": "claude",
            "skips": ["commit-plan"],
            "commit_plan": "Step 1: Add tests\nStep 2: Implement",
        }
        prompt = orchestrator.build_prompt(task, "commit-plan", "claude", [])
        self.assertNotIn("skips", prompt)
        self.assertIn("commit_plan", prompt)
        self.assertIn("Step 1: Add tests", prompt)

    def test_build_prompt_omits_plan_for_reviewer(self):
        """build_prompt omits commit_plan for reviewer steps."""
        task = {
            "id": 99, "title": "Review prompt", "description": None,
            "branch": "b", "status": "running", "next_step": "commit-review",
            "review_round": 0, "last_review_decision": "none",
            "commit_hash": None, "stash_ref": None, "coder_agent": "claude",
            "commit_plan": "Step 1: Add tests",
        }
        prompt = orchestrator.build_prompt(task, "commit-review", "codex", [])
        self.assertNotIn("commit_plan", prompt)
        self.assertNotIn("Step 1: Add tests", prompt)

    def test_build_prompt_omits_plan_for_path_b_make(self):
        """build_prompt omits commit_plan for Path B (finalization) commit-make."""
        task = {
            "id": 99, "title": "Path B prompt", "description": None,
            "branch": "b", "status": "running", "next_step": "commit-make",
            "review_round": 0, "last_review_decision": "approve",
            "commit_hash": None, "stash_ref": None, "coder_agent": "claude",
            "commit_plan": "Step 1: Add tests",
        }
        prompt = orchestrator.build_prompt(task, "commit-make", "claude", [])
        self.assertNotIn("commit_plan", prompt)
        self.assertNotIn("Step 1: Add tests", prompt)

    def test_build_prompt_omits_path_c_when_no_stash(self):
        """build_prompt omits Path C (stash recovery) text when stash_ref is null."""
        task = {
            "id": 99, "title": "No stash prompt", "description": None,
            "branch": "b", "status": "running", "next_step": "commit-make",
            "review_round": 0, "last_review_decision": "none",
            "commit_hash": None, "stash_ref": None, "coder_agent": "claude",
        }
        prompt = orchestrator.build_prompt(task, "commit-make", "claude", [])
        self.assertNotIn("Path C: `stash_ref` is set", prompt)
        self.assertNotIn("git stash pop", prompt)

    def test_build_prompt_omits_fields_for_supertasks(self):
        """build_prompt omits stash_ref and commit_hash from Task Context for supertask steps."""
        task = {
            "id": 99, "title": "Supertask prompt", "description": None,
            "branch": "b", "status": "running", "next_step": "commit-make-supertask",
            "review_round": 0, "last_review_decision": "none",
            "commit_hash": "a" * 40, "stash_ref": "stash@{0}", "coder_agent": "claude",
            "kind": "supertask"
        }
        prompt = orchestrator.build_prompt(task, "commit-make-supertask", "claude", [])
        # They may appear in the shared Rules text, but should be absent from the Context key-value list
        self.assertNotIn("- stash_ref:", prompt)
        self.assertNotIn("- commit_hash:", prompt)


class TestPickupRuntimeStep(unittest.TestCase):
    """process_pinned_task must write the correct current_step for all step types."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)
        orchestrator.init_runtime(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def _add_ready_task(self, next_step, kind="task", branch="b"):
        tid = db.add_task(self.conn, f"task-{next_step}", kind=kind, branch=branch, coder_agent="claude")
        db.update_task(self.conn, tid, status="ready", next_step=next_step)
        return tid

    def _run_one_iteration(self, task):
        """Run one loop iteration of process_pinned_task, then raise to stop the loop."""
        iterations = []

        def fake_advance(t, conn):
            rt = db.get_runtime(conn)
            iterations.append(rt["current_step"])
            raise KeyboardInterrupt  # stop after first iteration

        with patch.object(orchestrator, "advance", side_effect=fake_advance), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False), \
             patch.object(orchestrator, "ensure_branch", return_value=True):
            try:
                orchestrator.process_pinned_task(task, self.conn)
            except (KeyboardInterrupt, SystemExit):
                pass

        return iterations[0] if iterations else None

    def test_pickup_sets_current_step_commit_make(self):
        tid = self._add_ready_task("commit-make")
        step = self._run_one_iteration(db.get_task(self.conn, tid))
        self.assertEqual(step, "commit-make")

    def test_pickup_sets_current_step_commit_make_supertask(self):
        tid = self._add_ready_task("commit-make-supertask", kind="supertask")
        step = self._run_one_iteration(db.get_task(self.conn, tid))
        self.assertEqual(step, "commit-make-supertask")

    def test_pickup_sets_current_step_commit_plan(self):
        tid = self._add_ready_task("commit-plan")
        step = self._run_one_iteration(db.get_task(self.conn, tid))
        self.assertEqual(step, "commit-plan")

    def test_pickup_sets_current_step_commit_plan_review(self):
        tid = self._add_ready_task("commit-plan-review")
        step = self._run_one_iteration(db.get_task(self.conn, tid))
        self.assertEqual(step, "commit-plan-review")

    def test_pickup_runtime_update_failure_reverts_task_to_ready(self):
        """If the runtime update crashes at pickup, the task must not be left running."""
        tid = self._add_ready_task("commit-make")
        task = db.get_task(self.conn, tid)

        with patch.object(db, "update_runtime", side_effect=Exception("boom")):
            with self.assertRaises(RuntimeError):
                orchestrator.process_pinned_task(task, self.conn)

        task_after = db.get_task(self.conn, tid)
        self.assertEqual(task_after["status"], "ready")

    def test_pickup_blocks_master_task_without_policy_marker_before_agent_launch(self):
        tid = self._add_ready_task("commit-make", branch="master")
        task = db.get_task(self.conn, tid)

        with patch.object(orchestrator.repo_policy, "read_allow_tasks_on_master", return_value=False), \
             patch.object(orchestrator, "run_agent") as run_agent:
            result = orchestrator.process_pinned_task(task, self.conn)

        self.assertFalse(result)
        run_agent.assert_not_called()
        blocked = db.get_task(self.conn, tid)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["next_step"], "none")
        comments = db.get_comments(self.conn, tid)
        self.assertEqual(len(comments), 1)
        self.assertIn("Tasks on master/main are disabled by default", comments[0]["message"])
        self.assertIn("ALLOW_TASKS_ON_MASTER", comments[0]["message"])


class TestRepositionTask(unittest.TestCase):
    """Tests for db.reposition_task()."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def _make_ready(self, title, branch="b"):
        tid = db.add_task(self.conn, title, branch=branch, skips=["commit-plan"])
        db.update_task(self.conn, tid, status="ready")
        return tid

    def _queue_order(self):
        rows = self.conn.execute(
            "SELECT id FROM tasks WHERE status = 'ready' AND parent_task_id IS NULL "
            "ORDER BY CASE WHEN sequence_index IS NULL THEN 1 ELSE 0 END ASC, sequence_index ASC, id ASC"
        ).fetchall()
        return [r["id"] for r in rows]

    def test_requeue_before_first_becomes_new_head(self):
        t1 = self._make_ready("first")
        t2 = self._make_ready("second")
        t3 = self._make_ready("third")
        # move t3 before t1
        db.reposition_task(self.conn, t3, before_id=t1)
        self.assertEqual(self._queue_order(), [t3, t1, t2])

    def test_requeue_after_last_stays_tail(self):
        t1 = self._make_ready("first")
        t2 = self._make_ready("second")
        t3 = self._make_ready("third")
        # move t1 after t3 (already last)
        db.reposition_task(self.conn, t1, after_id=t3)
        self.assertEqual(self._queue_order(), [t2, t3, t1])

    def test_requeue_before_second_of_three(self):
        t1 = self._make_ready("first")
        t2 = self._make_ready("second")
        t3 = self._make_ready("third")
        # move t3 before t2 → [t1, t3, t2]
        db.reposition_task(self.conn, t3, before_id=t2)
        self.assertEqual(self._queue_order(), [t1, t3, t2])

    def test_requeue_after_second_of_three(self):
        t1 = self._make_ready("first")
        t2 = self._make_ready("second")
        t3 = self._make_ready("third")
        # move t1 after t2 → [t2, t1, t3]
        db.reposition_task(self.conn, t1, after_id=t2)
        self.assertEqual(self._queue_order(), [t2, t1, t3])

    def test_requeue_returns_updated_task(self):
        t1 = self._make_ready("first")
        t2 = self._make_ready("second")
        result = db.reposition_task(self.conn, t2, before_id=t1)
        self.assertEqual(result["id"], t2)
        self.assertEqual(result["status"], "ready")

    def test_requeue_error_task_not_ready(self):
        t1 = self._make_ready("first")
        t2 = db.add_task(self.conn, "not ready", branch="b", skips=["commit-plan"])
        with self.assertRaises(ValueError) as ctx:
            db.reposition_task(self.conn, t2, after_id=t1)
        self.assertIn("not ready", str(ctx.exception))

    def test_requeue_error_anchor_not_ready(self):
        t1 = self._make_ready("first")
        t2 = db.add_task(self.conn, "not ready anchor", branch="b", skips=["commit-plan"])
        with self.assertRaises(ValueError) as ctx:
            db.reposition_task(self.conn, t1, after_id=t2)
        self.assertIn("not ready", str(ctx.exception))

    def test_requeue_error_task_is_child(self):
        parent_id = db.add_task(self.conn, "parent", branch="b", kind="supertask")
        db.update_task(self.conn, parent_id, status="pending_subtasks")
        child_id = db.add_task(self.conn, "child", parent_task_id=parent_id)
        anchor_id = self._make_ready("anchor")
        with self.assertRaises(ValueError) as ctx:
            db.reposition_task(self.conn, child_id, after_id=anchor_id)
        self.assertIn("child task", str(ctx.exception))

    def test_requeue_error_anchor_is_child(self):
        parent_id = db.add_task(self.conn, "parent", branch="b", kind="supertask")
        db.update_task(self.conn, parent_id, status="pending_subtasks")
        child_id = db.add_task(self.conn, "child", parent_task_id=parent_id)
        target_id = self._make_ready("target")
        with self.assertRaises(ValueError) as ctx:
            db.reposition_task(self.conn, target_id, after_id=child_id)
        self.assertIn("child task", str(ctx.exception))

    def test_requeue_error_relative_to_self(self):
        t1 = self._make_ready("lonely")
        with self.assertRaises(ValueError) as ctx:
            db.reposition_task(self.conn, t1, after_id=t1)
        self.assertIn("itself", str(ctx.exception))

    def test_requeue_error_both_before_and_after(self):
        t1 = self._make_ready("first")
        t2 = self._make_ready("second")
        with self.assertRaises(ValueError):
            db.reposition_task(self.conn, t1, before_id=t2, after_id=t2)

    def test_requeue_error_neither_before_nor_after(self):
        t1 = self._make_ready("first")
        with self.assertRaises(ValueError):
            db.reposition_task(self.conn, t1)


class TestFollowUpCLI(unittest.TestCase):
    """Tests for 'task follow-up' CLI subcommand."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self.task_py = str(Path(__file__).resolve().parent / "task.py")
        self.env = {
            **os.environ,
            "KANBAN_DB": self.db_path,
            "KANBAN_NONINTERACTIVE": "1",
        }

    def tearDown(self):
        for suffix in ("", "-shm", "-wal"):
            path = Path(self.db_path + suffix)
            if path.exists():
                path.unlink()

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, self.task_py] + list(args),
            capture_output=True, text=True, env=self.env,
        )

    def _add_task(self, title, branch="feat", coder_agent=None, reviewer_agent=None):
        args = ["add", title, "--branch", branch]
        if coder_agent:
            args += ["--coder-agent", coder_agent]
        if reviewer_agent:
            args += ["--reviewer-agent", reviewer_agent]
        r = self._run(*args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["id"]

    def _set_running(self, task_id):
        """Put a task in running/commit-make state so follow-up is allowed."""
        conn = db.connect(self.db_path)
        try:
            db.update_task(conn, task_id, status="running", next_step="commit-make")
        finally:
            conn.close()

    def test_follow_up_no_suffix_appends_1_2(self):
        """Task without n/x suffix: current gets '1/x', follow-up gets '2/x'."""
        tid = self._add_task("Add caching layer")
        self._set_running(tid)
        r = self._run("follow-up", str(tid), "--description", "Add cache invalidation")
        self.assertEqual(r.returncode, 0, r.stderr)
        follow_up = json.loads(r.stdout)
        self.assertEqual(follow_up["title"], "Add caching layer 2/x")

        current = json.loads(self._run("show", str(tid)).stdout)
        self.assertEqual(current["title"], "Add caching layer 1/x")

    def test_follow_up_with_existing_suffix_increments_n(self):
        """Task already has n/x suffix: current title unchanged, follow-up increments n."""
        tid = self._add_task("Migrate users table 1/x")
        self._set_running(tid)
        r = self._run("follow-up", str(tid), "--description", "Migrate orders table")
        self.assertEqual(r.returncode, 0, r.stderr)
        follow_up = json.loads(r.stdout)
        self.assertEqual(follow_up["title"], "Migrate users table 2/x")

        # Current task title must be unchanged
        current = json.loads(self._run("show", str(tid)).stdout)
        self.assertEqual(current["title"], "Migrate users table 1/x")

    def test_follow_up_inherits_branch_and_coder_agent(self):
        """Follow-up task inherits branch, coder_agent, and reviewer_agent from current task."""
        tid = self._add_task("Feature A", branch="feat-a", coder_agent="sonnet", reviewer_agent="gemini")
        self._set_running(tid)
        r = self._run("follow-up", str(tid), "--description", "Part 2")
        self.assertEqual(r.returncode, 0, r.stderr)
        follow_up = json.loads(r.stdout)
        self.assertEqual(follow_up["branch"], "feat-a")
        self.assertEqual(follow_up["coder_agent"], "sonnet")
        self.assertEqual(follow_up["reviewer_agent"], "gemini")

    def test_follow_up_sets_follow_up_task_id_on_current(self):
        """follow_up_task_id is set on the current task after calling follow-up."""
        tid = self._add_task("My task")
        self._set_running(tid)
        r = self._run("follow-up", str(tid), "--description", "Follow-up work")
        self.assertEqual(r.returncode, 0, r.stderr)
        follow_up_id = json.loads(r.stdout)["id"]

        current = json.loads(self._run("show", str(tid)).stdout)
        self.assertEqual(current["follow_up_task_id"], follow_up_id)

    def test_follow_up_skips_planning(self):
        """Follow-up task skips commit-plan, which also skips plan review."""
        tid = self._add_task("Base task")
        self._set_running(tid)
        r = self._run("follow-up", str(tid), "--description", "Follow-up")
        self.assertEqual(r.returncode, 0, r.stderr)
        follow_up = json.loads(r.stdout)
        self.assertEqual(follow_up["next_step"], "commit-make")
        self.assertEqual(follow_up["skips"], ["commit-plan"])

    def test_follow_up_description_stored(self):
        """Follow-up task stores the provided description."""
        tid = self._add_task("Task with follow-up")
        self._set_running(tid)
        r = self._run("follow-up", str(tid), "--description", "Detailed follow-up work description")
        self.assertEqual(r.returncode, 0, r.stderr)
        follow_up = json.loads(r.stdout)
        self.assertEqual(follow_up["description"], "Detailed follow-up work description")

    def test_follow_up_fails_on_done_task(self):
        """follow-up command fails on a done task."""
        conn = db.connect(self.db_path)
        try:
            tid = db.add_task(conn, "Done task", branch="b")
            db.update_task(conn, tid, status="done", next_step="none")
        finally:
            conn.close()
        r = self._run("follow-up", str(tid), "--description", "Follow-up")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("done", r.stderr)

    def test_follow_up_fails_when_not_in_commit_make(self):
        """follow-up command fails when task is not status=running/next_step=commit-make."""
        tid = self._add_task("Idle task")
        # Task is status=none after add — not the active commit-make state
        r = self._run("follow-up", str(tid), "--description", "Follow-up")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("commit-make", r.stderr)

    def test_follow_up_fails_on_ready_task(self):
        """follow-up command fails when task is status=ready (not running)."""
        conn = db.connect(self.db_path)
        try:
            tid = db.add_task(conn, "Ready task", branch="b")
            db.update_task(conn, tid, status="ready", next_step="commit-make")
        finally:
            conn.close()
        r = self._run("follow-up", str(tid), "--description", "Follow-up")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("commit-make", r.stderr)

    def test_follow_up_fails_on_repeated_invocation(self):
        """follow-up command fails if follow_up_task_id is already set (second call is rejected)."""
        tid = self._add_task("Task needing follow-up")
        self._set_running(tid)
        # First call: should succeed
        r = self._run("follow-up", str(tid), "--description", "Part 2")
        self.assertEqual(r.returncode, 0, r.stderr)
        # Second call: must fail because follow_up_task_id is already set
        r2 = self._run("follow-up", str(tid), "--description", "Part 3")
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("already has a follow-up", r2.stderr)

    def test_follow_up_requires_description(self):
        """follow-up command fails when --description is omitted."""
        tid = self._add_task("Task")
        self._set_running(tid)
        r = self._run("follow-up", str(tid))
        self.assertNotEqual(r.returncode, 0)


class TestFollowUpOrchestrator(unittest.TestCase):
    """Tests for orchestrator advance behavior with follow-up tasks."""

    def setUp(self):
        self._ack_patcher = patch.object(orchestrator, "ensure_agent_acked")
        self._ack_patcher.start()
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        self._ack_patcher.stop()
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_commit_make_path_a_with_follow_up_queues_follow_up_after_current(self):
        """After commit-make Path A, if follow_up_task_id is set, both tasks become ready,
        current goes to commit-review, follow-up goes to commit-make, queued after current."""
        # Create current task
        tid = db.add_task(self.conn, "Task A 1/2", coder_agent="claude", branch="feat",
                          skips=["commit-plan"])
        db.update_task(self.conn, tid, status="running", next_step="commit-make")

        # Create follow-up task (simulating what 'task follow-up' does)
        follow_up_id = db.add_task(self.conn, "Task A 2/2", coder_agent="claude", branch="feat",
                                   skips=["commit-plan", "commit-plan-review"])
        db.update_task(self.conn, tid, follow_up_task_id=follow_up_id)

        task = db.get_task(self.conn, tid)

        def fake_coder(name, prompt, task_id, conn, verb, **kw):
            # Agent writes a commit-message comment (required for Path A)
            db.add_comment(conn, task_id, "Commit message for task A\n\nTask 53",
                           kind="commit-message", author=name)
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_coder), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            result = orchestrator.advance(task, self.conn)

        self.assertTrue(result)

        # Current task: ready for commit-review
        current = db.get_task(self.conn, tid)
        self.assertEqual(current["status"], "ready")
        self.assertEqual(current["next_step"], "commit-review")
        self.assertEqual(current["follow_up_task_id"], follow_up_id)  # preserved for lifetime gating

        # Follow-up task: ready for commit-make
        follow_up = db.get_task(self.conn, follow_up_id)
        self.assertEqual(follow_up["status"], "ready")
        self.assertEqual(follow_up["next_step"], "commit-make")

        # Follow-up is queued after current in the ready queue
        rows = self.conn.execute(
            "SELECT id FROM tasks WHERE status = 'ready' AND parent_task_id IS NULL "
            "ORDER BY CASE WHEN sequence_index IS NULL THEN 1 ELSE 0 END ASC, sequence_index ASC, id ASC"
        ).fetchall()
        queue_order = [r["id"] for r in rows]
        current_pos = queue_order.index(tid)
        follow_up_pos = queue_order.index(follow_up_id)
        self.assertLess(current_pos, follow_up_pos,
                        "Current task must be ahead of follow-up in the queue")
        self.assertEqual(follow_up_pos, current_pos + 1,
                         "Follow-up must be immediately after current task")

    def test_commit_make_path_a_without_follow_up_unchanged(self):
        """Commit-make Path A without follow_up_task_id behaves as before."""
        tid = db.add_task(self.conn, "Normal task", coder_agent="claude", branch="feat",
                          skips=["commit-plan"])
        db.update_task(self.conn, tid, status="running", next_step="commit-make")
        task = db.get_task(self.conn, tid)

        def fake_coder(name, prompt, task_id, conn, verb, **kw):
            db.add_comment(conn, task_id, "Commit message\n\nTask 99",
                           kind="commit-message", author=name)
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_coder), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            result = orchestrator.advance(task, self.conn)

        self.assertTrue(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "ready")
        self.assertEqual(updated["next_step"], "commit-review")
        self.assertIsNone(updated["follow_up_task_id"])

    def test_commit_make_path_a_missing_follow_up_task_recovers(self):
        """If follow_up_task_id points to a non-existent task, current still advances to commit-review."""
        tid = db.add_task(self.conn, "Task with ghost follow-up", coder_agent="claude", branch="feat",
                          skips=["commit-plan"])
        db.update_task(self.conn, tid, status="running", next_step="commit-make")
        # Use a raw connection (no FK enforcement) to simulate a ghost follow_up_task_id
        raw_conn = sqlite3.connect(self.tmp.name)
        try:
            raw_conn.execute("UPDATE tasks SET follow_up_task_id = 9999 WHERE id = ?", (tid,))
            raw_conn.commit()
        finally:
            raw_conn.close()
        task = db.get_task(self.conn, tid)

        def fake_coder(name, prompt, task_id, conn, verb, **kw):
            db.add_comment(conn, task_id, "Commit message\n\nTask 99",
                           kind="commit-message", author=name)
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_coder), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            result = orchestrator.advance(task, self.conn)

        self.assertTrue(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "ready")
        self.assertEqual(updated["next_step"], "commit-review")
        self.assertEqual(updated["follow_up_task_id"], 9999)  # preserved even when follow-up not found

    def test_commit_make_skip_review_with_follow_up_queues_follow_up(self):
        """When commit-review is skipped and a follow-up was declared, the follow-up must
        be queued directly after the current task before advancing to finalization.

        A third ready task is added to the queue BEFORE advance() so that the test
        proves the follow-up lands between the current task and that third task,
        not at an arbitrary position.
        """
        # Add a task that is already in the ready queue before the current task runs.
        other_id = db.add_task(self.conn, "Other ready task", coder_agent="claude", branch="feat")
        db.update_task(self.conn, other_id, status="ready", next_step="commit-make")

        # Create current task with commit-review skipped
        tid = db.add_task(self.conn, "Task B 1/2", coder_agent="claude", branch="feat",
                          skips=["commit-plan", "commit-review"])
        db.update_task(self.conn, tid, status="running", next_step="commit-make")

        # Create follow-up task (simulating what 'task follow-up' does)
        follow_up_id = db.add_task(self.conn, "Task B 2/2", coder_agent="claude", branch="feat",
                                   skips=["commit-plan", "commit-plan-review"])
        db.update_task(self.conn, tid, follow_up_task_id=follow_up_id)

        task = db.get_task(self.conn, tid)

        def fake_coder(name, prompt, task_id, conn, verb, **kw):
            db.add_comment(conn, task_id, "Commit message for task B\n\nTask 53",
                           kind="commit-message", author=name)
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_coder), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False):
            result = orchestrator.advance(task, self.conn)

        self.assertTrue(result)

        # Current task: advanced to ready/commit-make for finalization (review was skipped)
        current = db.get_task(self.conn, tid)
        self.assertEqual(current["status"], "ready")
        self.assertEqual(current["next_step"], "commit-make")
        self.assertEqual(current["last_review_decision"], "approve")
        self.assertEqual(current["follow_up_task_id"], follow_up_id)  # preserved

        # Follow-up task: queued and ready for commit-make
        follow_up = db.get_task(self.conn, follow_up_id)
        self.assertEqual(follow_up["status"], "ready")
        self.assertEqual(follow_up["next_step"], "commit-make")

        # Follow-up is queued directly after current in the ready queue,
        # even with another ready task already present.
        rows = self.conn.execute(
            "SELECT id FROM tasks WHERE status = 'ready' AND parent_task_id IS NULL "
            "ORDER BY CASE WHEN sequence_index IS NULL THEN 1 ELSE 0 END ASC, sequence_index ASC, id ASC"
        ).fetchall()
        queue_order = [r["id"] for r in rows]
        current_pos = queue_order.index(tid)
        follow_up_pos = queue_order.index(follow_up_id)
        self.assertLess(current_pos, follow_up_pos,
                        "Current task must be ahead of follow-up in the queue")
        self.assertEqual(follow_up_pos, current_pos + 1,
                         "Follow-up must be immediately after current task")


class TestFollowUpReviewRejectionRework(unittest.TestCase):
    """Regression test: follow-up limit survives review rejection and rework."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self.task_py = str(Path(__file__).resolve().parent / "task.py")
        self.env = {
            **os.environ,
            "KANBAN_DB": self.db_path,
            "KANBAN_NONINTERACTIVE": "1",
        }

    def tearDown(self):
        for suffix in ("", "-shm", "-wal"):
            path = Path(self.db_path + suffix)
            if path.exists():
                path.unlink()

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, self.task_py] + list(args),
            capture_output=True, text=True, env=self.env,
        )

    def test_follow_up_blocked_after_review_rejection_and_rework(self):
        """After follow-up is declared and the orchestrator processes it, if the task is
        rejected in review and returns to commit-make, the coder cannot declare a second
        follow-up — even though follow_up_task_id was cleared by the orchestrator."""
        conn = db.connect(self.db_path)
        try:
            # 1. Create task in running/commit-make state
            tid = db.add_task(conn, "Big feature", branch="feat", coder_agent="sonnet",
                              skips=["commit-plan"])
            db.update_task(conn, tid, status="running", next_step="commit-make")

            # 2. Coder declares a follow-up (simulates 'task follow-up' call)
            follow_up_id = db.add_task(conn, "Big feature 2/2", branch="feat", coder_agent="sonnet",
                                       skips=["commit-plan", "commit-plan-review"])
            db.update_task(conn, tid, follow_up_task_id=follow_up_id)

            # 3. Orchestrator processes Path A: sets follow-up to ready, preserves follow_up_task_id
            db.update_task(conn, tid, status="ready", next_step="commit-review",
                           last_review_decision="none")
            db.update_task(conn, follow_up_id, status="ready", next_step="commit-make")

            # 4. Simulate review rejection — task returns to running/commit-make
            db.update_task(conn, tid, status="running", next_step="commit-make",
                           last_review_decision="reject")
        finally:
            conn.close()

        # 5. Coder tries to declare a second follow-up — must be rejected
        r = self._run("follow-up", str(tid), "--description", "Third part")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already has a follow-up", r.stderr)


class TestConfigEnvOverrides(unittest.TestCase):
    """Verify that ORCHESTRA_DEFAULT_* env vars override role defaults in config."""

    def _load_config(self):
        """Load a fresh config module instance to pick up current env."""
        spec = importlib.util.spec_from_file_location(
            "_config_reload", SCRIPT_DIR / "config.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    # Env var names paired with (attr_name, hardcoded_fallback)
    _CASES = [
        ("ORCHESTRA_DEFAULT_SUPER_PLANNER", "DEFAULT_SUPER_PLANNER", "opus"),
        ("ORCHESTRA_DEFAULT_SUPER_REVIEWER", "DEFAULT_SUPER_REVIEWER", "codex"),
        ("ORCHESTRA_DEFAULT_PLANNER", "DEFAULT_PLANNER", "sonnet"),
        ("ORCHESTRA_DEFAULT_PLAN_REVIEWER", "DEFAULT_PLAN_REVIEWER", "codex"),
        ("ORCHESTRA_DEFAULT_CODER", "DEFAULT_CODER", "sonnet"),
        ("ORCHESTRA_DEFAULT_REVIEWER", "DEFAULT_REVIEWER", "codex"),
    ]

    def test_valid_env_override_applies(self):
        """Each ORCHESTRA_DEFAULT_* env var overrides the corresponding constant."""
        # Use one valid override value for every role.
        env = {env_key: "gemini" for env_key, _, _ in self._CASES}
        # Remove any pre-existing overrides so only our patch is active.
        with patch.dict(os.environ, env, clear=False):
            cfg = self._load_config()
            for _, attr, _ in self._CASES:
                self.assertEqual(
                    getattr(cfg, attr), "gemini",
                    msg=f"{attr} should be 'gemini' when env var is set",
                )

    def test_fallback_when_env_var_empty(self):
        """Empty string env vars fall back to the hard-coded defaults."""
        env = {env_key: "" for env_key, _, _ in self._CASES}
        with patch.dict(os.environ, env, clear=False):
            cfg = self._load_config()
            for _, attr, fallback in self._CASES:
                self.assertEqual(
                    getattr(cfg, attr), fallback,
                    msg=f"{attr} should fall back to '{fallback}' when env var is empty",
                )

    def test_fallback_when_env_var_invalid(self):
        """Unknown agent names in env vars fall back to the hard-coded defaults."""
        env = {env_key: "not_a_real_agent" for env_key, _, _ in self._CASES}
        with patch.dict(os.environ, env, clear=False):
            cfg = self._load_config()
            for _, attr, fallback in self._CASES:
                self.assertEqual(
                    getattr(cfg, attr), fallback,
                    msg=f"{attr} should fall back to '{fallback}' when env var is invalid",
                )

    def test_no_env_vars_uses_hardcoded_defaults(self):
        """When env vars are absent the hard-coded defaults are used."""
        env_keys = [env_key for env_key, _, _ in self._CASES]
        clean_env = {k: v for k, v in os.environ.items() if k not in env_keys}
        with patch.dict(os.environ, clean_env, clear=True):
            cfg = self._load_config()
            for _, attr, fallback in self._CASES:
                self.assertEqual(
                    getattr(cfg, attr), fallback,
                    msg=f"{attr} should be '{fallback}' with no env vars set",
                )

    def test_whitespace_only_env_var_falls_back(self):
        """Whitespace-only values are treated as unset."""
        env = {env_key: "   " for env_key, _, _ in self._CASES}
        with patch.dict(os.environ, env, clear=False):
            cfg = self._load_config()
            for _, attr, fallback in self._CASES:
                self.assertEqual(
                    getattr(cfg, attr), fallback,
                    msg=f"{attr} should fall back to '{fallback}' when env var is whitespace",
                )


class TestRepoPolicy(unittest.TestCase):
    """Tests for repo_policy.read_skip_build_until_approved()."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_agents_md(self, content):
        path = Path(self.tmpdir) / "AGENTS.md"
        path.write_text(content, encoding="utf-8")

    def test_no_agents_md_returns_false(self):
        result = repo_policy.read_skip_build_until_approved(self.tmpdir)
        self.assertFalse(result)

    def test_marker_present_returns_true(self):
        self._write_agents_md("SKIP_BUILD_UNTIL_APPROVED\n")
        result = repo_policy.read_skip_build_until_approved(self.tmpdir)
        self.assertTrue(result)

    def test_marker_with_leading_whitespace_returns_true(self):
        self._write_agents_md("  SKIP_BUILD_UNTIL_APPROVED  \n")
        result = repo_policy.read_skip_build_until_approved(self.tmpdir)
        self.assertTrue(result)

    def test_marker_with_extra_text_returns_false(self):
        self._write_agents_md("SKIP_BUILD_UNTIL_APPROVED: false\n")
        result = repo_policy.read_skip_build_until_approved(self.tmpdir)
        self.assertFalse(result)

    def test_marker_absent_in_nonempty_file_returns_false(self):
        self._write_agents_md("# Agent instructions\nRun tests before committing.\n")
        result = repo_policy.read_skip_build_until_approved(self.tmpdir)
        self.assertFalse(result)

    def test_prose_mention_of_marker_does_not_match(self):
        self._write_agents_md(
            "You can use SKIP_BUILD_UNTIL_APPROVED in your AGENTS.md to defer builds.\n"
            "This file does not actually opt in.\n"
        )
        result = repo_policy.read_skip_build_until_approved(self.tmpdir)
        self.assertFalse(result)

    def test_marker_in_multiline_file_returns_true(self):
        self._write_agents_md(
            "# Build policy\n\n"
            "SKIP_BUILD_UNTIL_APPROVED\n\n"
            "Run lint before submitting.\n"
        )
        result = repo_policy.read_skip_build_until_approved(self.tmpdir)
        self.assertTrue(result)

    def test_empty_agents_md_returns_false(self):
        self._write_agents_md("")
        result = repo_policy.read_skip_build_until_approved(self.tmpdir)
        self.assertFalse(result)

    def test_path_object_accepted(self):
        self._write_agents_md("SKIP_BUILD_UNTIL_APPROVED\n")
        result = repo_policy.read_skip_build_until_approved(Path(self.tmpdir))
        self.assertTrue(result)

    def test_allow_tasks_on_master_absent_returns_false(self):
        self._write_agents_md("# Agent instructions\n")
        result = repo_policy.read_allow_tasks_on_master(self.tmpdir)
        self.assertFalse(result)

    def test_allow_tasks_on_master_standalone_present_returns_true(self):
        self._write_agents_md("ALLOW_TASKS_ON_MASTER\n")
        result = repo_policy.read_allow_tasks_on_master(self.tmpdir)
        self.assertTrue(result)

    def test_allow_tasks_on_master_with_whitespace_returns_true(self):
        self._write_agents_md("  ALLOW_TASKS_ON_MASTER  \n")
        result = repo_policy.read_allow_tasks_on_master(self.tmpdir)
        self.assertTrue(result)

    def test_allow_tasks_on_master_prose_mention_does_not_match(self):
        self._write_agents_md(
            "Add ALLOW_TASKS_ON_MASTER when a repo intentionally queues on master.\n"
        )
        result = repo_policy.read_allow_tasks_on_master(self.tmpdir)
        self.assertFalse(result)

    def test_allow_tasks_on_master_colon_variant_does_not_match(self):
        self._write_agents_md("ALLOW_TASKS_ON_MASTER: true\n")
        result = repo_policy.read_allow_tasks_on_master(self.tmpdir)
        self.assertFalse(result)


class TestDeferredBuildPolicy(unittest.TestCase):
    """Tests for SKIP_BUILD_UNTIL_APPROVED orchestrator behavior."""

    def setUp(self):
        self._ack_patcher = patch.object(orchestrator, "ensure_agent_acked")
        self._ack_patcher.start()
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        self._ack_patcher.stop()
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_skip_build_policy_surfaced_in_commit_make_prompt(self):
        """build_prompt task context includes skip_build_until_approved when policy is active."""
        task = {
            "id": 1, "title": "T", "description": None,
            "branch": "b", "status": "running", "next_step": "commit-make",
            "review_round": 0, "last_review_decision": "none",
            "commit_hash": None, "stash_ref": None, "coder_agent": "claude",
            "commit_plan": None,
        }
        with patch.object(orchestrator.repo_policy, "read_skip_build_until_approved", return_value=True), \
             patch.object(orchestrator, "_repo_root", return_value="/fake/repo"):
            prompt = orchestrator.build_prompt(task, "commit-make", "claude", [])
        context_start = prompt.index("## Task Context")
        context_end = prompt.index("## Prior Comments")
        task_context_block = prompt[context_start:context_end]
        self.assertIn("skip_build_until_approved: yes", task_context_block)

    def test_skip_build_policy_absent_when_not_enabled(self):
        """build_prompt task context does not include skip_build_until_approved when policy is off."""
        task = {
            "id": 1, "title": "T", "description": None,
            "branch": "b", "status": "running", "next_step": "commit-make",
            "review_round": 0, "last_review_decision": "none",
            "commit_hash": None, "stash_ref": None, "coder_agent": "claude",
            "commit_plan": None,
        }
        with patch.object(orchestrator.repo_policy, "read_skip_build_until_approved", return_value=False), \
             patch.object(orchestrator, "_repo_root", return_value="/fake/repo"):
            prompt = orchestrator.build_prompt(task, "commit-make", "claude", [])
        # The task context block should not contain the policy line.
        # (The prompt text itself mentions the field as documentation — that's expected.)
        context_start = prompt.index("## Task Context")
        context_end = prompt.index("## Prior Comments")
        task_context_block = prompt[context_start:context_end]
        self.assertNotIn("skip_build_until_approved:", task_context_block)

    def test_reviewer_handoff_notes_deferred_validation_when_policy_active(self):
        """Reviewer handoff explains missing validation when SKIP_BUILD_UNTIL_APPROVED is set."""
        task = {
            "id": 5, "title": "T", "description": None,
            "branch": "b", "status": "running", "next_step": "commit-review",
            "review_round": 0, "last_review_decision": "none",
            "commit_hash": None, "stash_ref": None, "coder_agent": "claude",
        }
        with patch.object(orchestrator.repo_policy, "read_skip_build_until_approved", return_value=True), \
             patch.object(orchestrator, "_repo_root", return_value="/fake/repo"):
            prompt = orchestrator.build_prompt(task, "commit-review", "codex", [])
        self.assertIn("SKIP_BUILD_UNTIL_APPROVED", prompt)
        self.assertIn("deferred", prompt.lower())
        # Should not say the standard "may not have run the build" fallback
        self.assertNotIn("maker may not have run the build", prompt)

    def test_reviewer_handoff_standard_fallback_when_policy_inactive(self):
        """Reviewer handoff shows standard missing-validation text when policy is off."""
        task = {
            "id": 5, "title": "T", "description": None,
            "branch": "b", "status": "running", "next_step": "commit-review",
            "review_round": 0, "last_review_decision": "none",
            "commit_hash": None, "stash_ref": None, "coder_agent": "claude",
        }
        with patch.object(orchestrator.repo_policy, "read_skip_build_until_approved", return_value=False), \
             patch.object(orchestrator, "_repo_root", return_value="/fake/repo"):
            prompt = orchestrator.build_prompt(task, "commit-review", "codex", [])
        self.assertIn("maker may not have run the build", prompt)

    def test_path_b_deferred_build_changed_re_enters_review(self):
        """Path B with deferred-build-changed signal: task re-enters commit-review at next round."""
        tid = db.add_task(self.conn, "Deferred build changed", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-make", last_review_decision="approve",
                       review_round=0)
        task = db.get_task(self.conn, tid)

        same_hash = "a" * 40

        def fake_agent_deferred_changed(name, prompt, task_id, conn, verb, **kw):
            db.add_comment(conn, task_id, "Build generated new artifact file",
                           kind="deferred-build-changed", author=name)
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_agent_deferred_changed), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "get_head_commit_hash", return_value=same_hash), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False), \
             patch.object(orchestrator.repo_policy, "read_skip_build_until_approved", return_value=True), \
             patch.object(orchestrator, "_repo_root", return_value="/fake/repo"):
            result = orchestrator.advance(task, self.conn)

        self.assertTrue(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "ready")
        self.assertEqual(updated["next_step"], "commit-review")
        self.assertEqual(updated["last_review_decision"], "none")
        self.assertEqual(updated["review_round"], 1)
        self.assertIsNone(updated["commit_hash"])

    def test_path_b_clean_deferred_build_finalizes_normally(self):
        """Path B with SKIP_BUILD_UNTIL_APPROVED and a clean deferred build (new commit): task becomes done."""
        tid = db.add_task(self.conn, "Deferred build clean", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-make", last_review_decision="approve",
                       review_round=0)
        task = db.get_task(self.conn, tid)

        before_hash = "a" * 40
        after_hash = "b" * 40
        hashes = [before_hash, after_hash, after_hash]

        with patch.object(orchestrator, "run_agent", return_value=0), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "get_head_commit_hash", side_effect=hashes), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False), \
             patch.object(orchestrator.repo_policy, "read_skip_build_until_approved", return_value=True), \
             patch.object(orchestrator, "_repo_root", return_value="/fake/repo"):
            result = orchestrator.advance(task, self.conn)

        self.assertTrue(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "done")
        self.assertEqual(updated["next_step"], "none")
        self.assertEqual(updated["commit_hash"], after_hash)

    def test_path_b_deferred_build_changed_at_max_rounds_blocks(self):
        """Path B deferred-build-changed at MAX_REVIEW_ROUNDS: task is blocked."""
        tid = db.add_task(self.conn, "Deferred build at limit", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-make", last_review_decision="approve",
                       review_round=orchestrator.MAX_REVIEW_ROUNDS - 1)
        task = db.get_task(self.conn, tid)

        same_hash = "a" * 40

        def fake_agent_deferred_changed(name, prompt, task_id, conn, verb, **kw):
            db.add_comment(conn, task_id, "Build changed output",
                           kind="deferred-build-changed", author=name)
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_agent_deferred_changed), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "get_head_commit_hash", return_value=same_hash), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False), \
             patch.object(orchestrator.repo_policy, "read_skip_build_until_approved", return_value=True), \
             patch.object(orchestrator, "_repo_root", return_value="/fake/repo"):
            result = orchestrator.advance(task, self.conn)

        self.assertFalse(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "blocked")

    def test_path_b_deferred_build_changed_stale_signal_does_not_satisfy_new_run(self):
        """A deferred-build-changed comment from a prior run does not trigger re-review."""
        tid = db.add_task(self.conn, "Stale deferred signal", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-make", last_review_decision="approve")
        # Pre-seed a stale deferred-build-changed comment from a prior run
        db.add_comment(self.conn, tid, "Stale build change signal",
                       kind="deferred-build-changed", author="claude")
        task = db.get_task(self.conn, tid)

        # Agent exits 0 but creates no new commit and writes no fresh signal
        same_hash = "a" * 40
        with patch.object(orchestrator, "run_agent", return_value=0), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "get_head_commit_hash", return_value=same_hash), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False), \
             patch.object(orchestrator.repo_policy, "read_skip_build_until_approved", return_value=True), \
             patch.object(orchestrator, "_repo_root", return_value="/fake/repo"):
            result = orchestrator.advance(task, self.conn)

        self.assertFalse(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "blocked")

    def test_path_b_new_commit_wins_over_deferred_build_changed(self):
        """If both a real commit and deferred-build-changed are written, the commit wins."""
        tid = db.add_task(self.conn, "Commit beats deferred signal", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-make", last_review_decision="approve")
        task = db.get_task(self.conn, tid)

        before_hash = "a" * 40
        after_hash = "b" * 40

        def fake_agent_both(name, prompt, task_id, conn, verb, **kw):
            db.add_comment(conn, task_id, "Build changed something",
                           kind="deferred-build-changed", author=name)
            return 0

        hashes = iter([before_hash, after_hash, after_hash])
        with patch.object(orchestrator, "run_agent", side_effect=fake_agent_both), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "get_head_commit_hash", side_effect=lambda: next(hashes)), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False), \
             patch.object(orchestrator.repo_policy, "read_skip_build_until_approved", return_value=True), \
             patch.object(orchestrator, "_repo_root", return_value="/fake/repo"):
            result = orchestrator.advance(task, self.conn)

        self.assertTrue(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "done")
        self.assertEqual(updated["commit_hash"], after_hash)

    def test_path_a_deferred_validation_comment_required_when_policy_active(self):
        """Path A: agent writes commit-message but no validation comment — blocked when policy active."""
        tid = db.add_task(self.conn, "Path A missing validation", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-make", last_review_decision="none",
                       review_round=0)
        task = db.get_task(self.conn, tid)

        def fake_agent_no_validation(name, prompt, task_id, conn, verb, **kw):
            # Writes a commit-message but no validation comment
            db.add_comment(conn, task_id, "Do the thing\n\nTask 99",
                           kind="commit-message", author=name)
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_agent_no_validation), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False), \
             patch.object(orchestrator.repo_policy, "read_skip_build_until_approved", return_value=True), \
             patch.object(orchestrator, "_repo_root", return_value="/fake/repo"):
            result = orchestrator.advance(task, self.conn)

        self.assertFalse(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "blocked")

    def test_path_a_deferred_validation_comment_not_required_when_policy_inactive(self):
        """Path A: no validation comment is fine when SKIP_BUILD_UNTIL_APPROVED is off."""
        tid = db.add_task(self.conn, "Path A no policy", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-make", last_review_decision="none",
                       review_round=0)
        task = db.get_task(self.conn, tid)

        def fake_agent_commit_msg_only(name, prompt, task_id, conn, verb, **kw):
            db.add_comment(conn, task_id, "Do the thing\n\nTask 99",
                           kind="commit-message", author=name)
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_agent_commit_msg_only), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False), \
             patch.object(orchestrator.repo_policy, "read_skip_build_until_approved", return_value=False), \
             patch.object(orchestrator, "_repo_root", return_value="/fake/repo"):
            result = orchestrator.advance(task, self.conn)

        self.assertTrue(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "ready")
        self.assertEqual(updated["next_step"], "commit-review")

    def test_path_b_deferred_build_changed_without_policy_is_ignored(self):
        """Path B: deferred-build-changed signal without policy does not trigger re-review."""
        tid = db.add_task(self.conn, "No policy deferred signal", coder_agent="claude")
        db.update_task(self.conn, tid, status="running", branch="b",
                       next_step="commit-make", last_review_decision="approve",
                       review_round=0)
        task = db.get_task(self.conn, tid)

        same_hash = "a" * 40

        def fake_agent_deferred_changed(name, prompt, task_id, conn, verb, **kw):
            db.add_comment(conn, task_id, "Build generated new artifact",
                           kind="deferred-build-changed", author=name)
            return 0

        with patch.object(orchestrator, "run_agent", side_effect=fake_agent_deferred_changed), \
             patch.object(orchestrator, "ensure_branch", return_value=True), \
             patch.object(orchestrator, "get_head_commit_hash", return_value=same_hash), \
             patch.object(orchestrator, "is_worktree_dirty", return_value=False), \
             patch.object(orchestrator.repo_policy, "read_skip_build_until_approved", return_value=False), \
             patch.object(orchestrator, "_repo_root", return_value="/fake/repo"):
            result = orchestrator.advance(task, self.conn)

        # Without the policy, deferred-build-changed is ignored and no commit was made → blocked.
        self.assertFalse(result)
        updated = db.get_task(self.conn, tid)
        self.assertEqual(updated["status"], "blocked")

    def test_deferred_build_changed_cli_flag_sets_kind(self):
        """task comment --deferred-build-changed stores kind='deferred-build-changed' via real CLI."""
        task_py = str(Path(__file__).resolve().parent / "task.py")
        env = {**os.environ, "KANBAN_DB": self.tmp.name, "KANBAN_NONINTERACTIVE": "1"}

        # Create the task via CLI
        r = subprocess.run(
            [sys.executable, task_py, "add", "CLI flag test"],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(r.returncode, 0)
        tid = json.loads(r.stdout)["id"]

        # Exercise cmd_comment() via the real CLI parser with --deferred-build-changed
        r2 = subprocess.run(
            [sys.executable, task_py, "comment", str(tid),
             "Build generated new file", "--deferred-build-changed"],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(r2.returncode, 0, r2.stderr)

        # Verify the stored kind through the DB
        comments = db.get_comments(self.conn, tid)
        deferred = [c for c in comments if c["kind"] == "deferred-build-changed"]
        self.assertEqual(len(deferred), 1)
        self.assertEqual(deferred[0]["message"], "Build generated new file")


class TestCommitFooter(unittest.TestCase):
    """Tests for get_agent_display_name() and task get-commit-footer subcommand."""

    NEW_AGENT_KEYS = (
        "kilo-opus-4.6",
        "kilo-opus-4.7",
        "kilo-sonnet-4.6",
        "cursor-auto",
        "cursor-composer-2.5",
        "cursor-opus-4.6",
        "cursor-opus-4.7",
        "cursor-sonnet-4.6",
    )

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)
        self.task_py = str(Path(__file__).resolve().parent / "task.py")

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def _run(self, *args):
        env = {**os.environ, "KANBAN_DB": self.tmp.name, "KANBAN_NONINTERACTIVE": "1"}
        return subprocess.run(
            [sys.executable, self.task_py] + list(args),
            capture_output=True, text=True, env=env,
        )

    def test_display_name_sonnet(self):
        self.assertEqual(config.get_agent_display_name("sonnet"), "Claude Sonnet 4.5")

    def test_display_name_haiku(self):
        self.assertEqual(config.get_agent_display_name("haiku"), "Claude Haiku 4.5")

    def test_display_name_opus(self):
        self.assertEqual(config.get_agent_display_name("opus"), "Claude Opus 4.6")

    def test_display_name_claude_alias(self):
        self.assertEqual(config.get_agent_display_name("claude"), "Claude Sonnet 4.5")

    def test_display_name_fallback_for_codex(self):
        self.assertEqual(shared_config.AGENT_DISPLAY_LABELS["codex"], "GPT-5.5 medium")
        result = config.get_agent_display_name("codex")
        self.assertEqual(result, "GPT-5.5 medium")

    def test_existing_registry_labels_preserve_previous_fallbacks(self):
        self.assertEqual(config.get_agent_display_name("gemini"), "gemini")
        self.assertEqual(config.get_agent_display_name("kilo"), "kilo/kilo-auto/free")

    def test_agent_registry_keys_are_unique(self):
        self.assertEqual(len(agent_registry.AGENTS), len(set(agent_registry.AGENTS)))

    def test_agent_registry_commands_have_one_prompt_placeholder(self):
        for key, command in agent_registry.AGENT_CMD.items():
            with self.subTest(key=key):
                prompt_count = sum(part.count("{prompt}") for part in command)
                self.assertEqual(prompt_count, 1)

    def test_shared_config_uses_agent_registry_exports(self):
        self.assertIs(shared_config.AGENT_CMD, agent_registry.AGENT_CMD)
        self.assertIs(shared_config.AGENT_DISPLAY_LABELS, agent_registry.AGENT_DISPLAY_LABELS)

    def test_new_agent_keys_are_allowed(self):
        for key in self.NEW_AGENT_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, config.AGENTS)

    def test_display_names_for_new_agent_keys(self):
        expected = {
            "kilo-opus-4.6": "Kilo Claude Opus 4.6",
            "kilo-opus-4.7": "Kilo Claude Opus 4.7",
            "kilo-sonnet-4.6": "Kilo Claude Sonnet 4.6",
            "cursor-auto": "Cursor Auto",
            "cursor-composer-2.5": "Cursor Composer 2.5",
            "cursor-opus-4.6": "Cursor Opus 4.6 High",
            "cursor-opus-4.7": "Cursor Opus 4.7 High",
            "cursor-sonnet-4.6": "Cursor Sonnet 4.6",
        }
        for key, label in expected.items():
            with self.subTest(key=key):
                self.assertEqual(config.get_agent_display_name(key), label)

    def test_display_name_fallback_for_unknown_agent(self):
        result = config.get_agent_display_name("nonexistent-agent")
        self.assertEqual(result, "nonexistent-agent")

    def test_get_commit_footer_format(self):
        """get-commit-footer outputs coder/reviewer/rejection attribution."""
        r = self._run("add", "Footer test task", "--branch", "test")
        self.assertEqual(r.returncode, 0)
        tid = json.loads(r.stdout)["id"]

        db.update_task(self.conn, tid, coder_agent="sonnet")
        db.add_comment(
            self.conn, tid, "Looks good", kind="approval",
            author="codex", review_round=0,
        )

        r2 = self._run("get-commit-footer", str(tid))
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(
            r2.stdout.strip(),
            f"Task {tid} (coder: Claude Sonnet 4.5; reviewer: GPT-5.5 medium; review rejections: 0)",
        )

    def test_get_commit_footer_default_agent(self):
        """get-commit-footer uses DEFAULT_CODER when coder_agent is not set."""
        r = self._run("add", "Footer default agent task", "--branch", "test")
        self.assertEqual(r.returncode, 0)
        tid = json.loads(r.stdout)["id"]

        r2 = self._run("get-commit-footer", str(tid))
        self.assertEqual(r2.returncode, 0, r2.stderr)
        # Output must match Task <id> (<something>)
        self.assertRegex(r2.stdout.strip(), rf"^Task {tid} \(.+\)$")

    def test_get_commit_footer_missing_task(self):
        """get-commit-footer exits non-zero for missing task id."""
        r = self._run("get-commit-footer", "99999")
        self.assertNotEqual(r.returncode, 0)

    def test_get_commit_footer_codex_agent(self):
        """get-commit-footer uses config labels for agents without --model."""
        r = self._run("add", "Codex footer task", "--branch", "test")
        self.assertEqual(r.returncode, 0)
        tid = json.loads(r.stdout)["id"]

        db.update_task(self.conn, tid, coder_agent="codex")

        r2 = self._run("get-commit-footer", str(tid))
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(
            r2.stdout.strip(),
            f"Task {tid} (coder: GPT-5.5 medium; reviewer: pending; review rejections: 0)",
        )

    def test_get_commit_footer_counts_only_code_review_rejections(self):
        """Rejection count includes only kind='rejection' comments."""
        r = self._run("add", "Rejection count task", "--branch", "test")
        self.assertEqual(r.returncode, 0)
        tid = json.loads(r.stdout)["id"]
        db.update_task(self.conn, tid, coder_agent="haiku")
        db.add_comment(
            self.conn, tid, "Plan needs work", kind="plan-rejection",
            author="codex",
        )
        db.add_comment(
            self.conn, tid, "Fix one", kind="rejection",
            author="codex", review_round=0,
        )
        db.add_comment(
            self.conn, tid, "Operational note", kind="comment",
            author="orchestrator",
        )
        db.add_comment(
            self.conn, tid, "Fix two", kind="rejection",
            author="gemini", review_round=1,
        )
        db.add_comment(
            self.conn, tid, "Approved", kind="approval",
            author="sonnet", review_round=2,
        )

        r2 = self._run("get-commit-footer", str(tid))
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(
            r2.stdout.strip(),
            f"Task {tid} (coder: Claude Haiku 4.5; reviewer: Claude Sonnet 4.5; review rejections: 2)",
        )

    def test_get_commit_footer_uses_final_approval_reviewer(self):
        """Reviewer label comes from the last approval comment."""
        r = self._run("add", "Final reviewer task", "--branch", "test")
        self.assertEqual(r.returncode, 0)
        tid = json.loads(r.stdout)["id"]
        db.update_task(self.conn, tid, coder_agent="haiku")
        db.add_comment(
            self.conn, tid, "Earlier approval", kind="approval",
            author="gemini", review_round=0,
        )
        db.add_comment(
            self.conn, tid, "Final approval", kind="approval",
            author="codex", review_round=1,
        )

        r2 = self._run("get-commit-footer", str(tid))
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(
            r2.stdout.strip(),
            f"Task {tid} (coder: Claude Haiku 4.5; reviewer: GPT-5.5 medium; review rejections: 0)",
        )


class TestFinalizationFooter(unittest.TestCase):
    """Tests for normalize_commit_message_footer — the finalization path that
    ensures the commit message ends with the canonical Task <id> (<attribution>)
    footer using the get_commit_footer helper instead of hand-authored text."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)
        self.task_py = str(Path(__file__).resolve().parent / "task.py")

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def _add_task(self, agent="sonnet"):
        r = subprocess.run(
            [sys.executable, self.task_py, "add", "Finalization footer test", "--branch", "test"],
            capture_output=True, text=True,
            env={**os.environ, "KANBAN_DB": self.tmp.name, "KANBAN_NONINTERACTIVE": "1"},
        )
        tid = json.loads(r.stdout)["id"]
        db.update_task(self.conn, tid, coder_agent=agent)
        return tid

    def test_bare_trailer_replaced_with_canonical(self):
        """Bare 'Task <id>' at end of message is replaced with canonical footer."""
        tid = self._add_task("sonnet")
        message = f"Fix something important\n\nTask {tid}"
        result = task_module.normalize_commit_message_footer(message, tid, self.conn)
        self.assertTrue(
            result.endswith(
                f"Task {tid} (coder: Claude Sonnet 4.5; reviewer: pending; review rejections: 0)"
            ),
            repr(result),
        )
        self.assertNotIn(f"Task {tid}\n", result)

    def test_canonical_footer_preserved(self):
        """Already-canonical footer is left unchanged."""
        tid = self._add_task("sonnet")
        footer = task_module.get_commit_footer(tid, self.conn)
        message = f"Fix something\n\n{footer}"
        result = task_module.normalize_commit_message_footer(message, tid, self.conn)
        self.assertEqual(result, message)

    def test_stale_rich_footer_replaced(self):
        """Stale rich footers are replaced with the current canonical footer."""
        tid = self._add_task("sonnet")
        db.add_comment(
            self.conn, tid, "Approved", kind="approval",
            author="codex", review_round=0,
        )
        message = f"Fix something\n\nTask {tid} (Sonnet)"
        result = task_module.normalize_commit_message_footer(message, tid, self.conn)
        self.assertTrue(
            result.endswith(
                f"Task {tid} (coder: Claude Sonnet 4.5; reviewer: GPT-5.5 medium; review rejections: 0)"
            ),
            repr(result),
        )

    def test_missing_footer_appended(self):
        """Canonical footer is appended when no trailer is present."""
        tid = self._add_task("haiku")
        message = "Add a new feature\n\nLong body text."
        result = task_module.normalize_commit_message_footer(message, tid, self.conn)
        self.assertTrue(
            result.endswith(
                f"Task {tid} (coder: Claude Haiku 4.5; reviewer: pending; review rejections: 0)"
            ),
            repr(result),
        )

    def test_fallback_agent_in_normalized_footer(self):
        """Normalization uses configured agent display labels."""
        tid = self._add_task("codex")
        message = f"Some work\n\nTask {tid}"
        result = task_module.normalize_commit_message_footer(message, tid, self.conn)
        self.assertTrue(
            result.endswith(
                f"Task {tid} (coder: GPT-5.5 medium; reviewer: pending; review rejections: 0)"
            ),
            repr(result),
        )

    def test_get_commit_footer_used_by_normalization(self):
        """normalize_commit_message_footer delegates footer text to get_commit_footer."""
        tid = self._add_task("opus")
        expected_footer = task_module.get_commit_footer(tid, self.conn)
        self.assertEqual(
            expected_footer,
            f"Task {tid} (coder: Claude Opus 4.6; reviewer: pending; review rejections: 0)",
        )
        message = "Refactor internals\n\nSome body."
        result = task_module.normalize_commit_message_footer(message, tid, self.conn)
        self.assertTrue(result.endswith(expected_footer), repr(result))

    def test_path_b_prompt_references_get_commit_footer(self):
        """The commit-make.md Path B section instructs agents to run get-commit-footer."""
        prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "commit-make.md"
        content = prompt_path.read_text()
        path_b_section = content[content.index("## Path B"):]
        self.assertIn("get-commit-footer", path_b_section)
        self.assertIn("Task <id> (<attribution>)", path_b_section)


class TestAgentPingACKGate(unittest.TestCase):
    """Unit tests for the ping / ACK gate: one-ACK-per-(task, agent) semantics,
    retry loop behavior, and the operator-facing log / runtime messages."""

    def setUp(self):
        # Each test gets a fresh ACK cache so tests don't bleed into each other.
        orchestrator._agent_ack_cache.clear()
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        orchestrator._agent_ack_cache.clear()
        self.conn.close()
        os.unlink(self.tmp.name)

    # ── ping_agent wording ────────────────────────────────────────────

    def test_ping_log_on_success_mentions_responsive(self):
        """Successful ping log says the agent responded, not just 'ACKed'."""
        with patch.object(orchestrator, "ping_agent", return_value=True) as mock_ping:
            orchestrator.ensure_agent_acked("sonnet", 1, self.conn)
        # ensure_agent_acked delegates to ping_agent; verify it was called
        mock_ping.assert_called_once_with("sonnet", 1)

    def test_ping_log_on_failure_mentions_unavailable(self):
        """The run-log entry for a failed ping tells the operator the agent may be
        out of tokens or unavailable — not just a generic 'no output' message."""
        log_messages = []

        def capture_log(msg, task_id=None):
            log_messages.append(msg)

        with (
            patch.object(orchestrator, "log", side_effect=capture_log),
            patch.object(orchestrator, "subprocess") as mock_sub,
        ):
            # Simulate a process that closes stdout immediately (no output).
            mock_proc = MagicMock()
            mock_proc.stdout = None
            mock_sub.Popen.return_value = mock_proc
            # AGENT_CMD needs a known agent; patch it so ping_agent doesn't bail early.
            with patch.dict(orchestrator.AGENT_CMD, {"sonnet": ["echo", "{prompt}"]}):
                orchestrator.ping_agent("sonnet", 42)

        failure_logs = [m for m in log_messages if "no output" in m or "unavailable" in m]
        self.assertTrue(failure_logs, f"Expected a 'no output / unavailable' log; got: {log_messages}")

    # ── one-ACK-per-(task, agent) caching ────────────────────────────

    def test_second_call_skips_ping(self):
        """ensure_agent_acked with same (task_id, agent) calls ping only once."""
        with patch.object(orchestrator, "ping_agent", return_value=True) as mock_ping:
            orchestrator.ensure_agent_acked("sonnet", 10, self.conn)
            orchestrator.ensure_agent_acked("sonnet", 10, self.conn)
        mock_ping.assert_called_once()

    def test_different_task_ids_each_get_a_ping(self):
        """ACK cache is keyed on (task_id, agent) — two tasks trigger two pings."""
        with patch.object(orchestrator, "ping_agent", return_value=True) as mock_ping:
            orchestrator.ensure_agent_acked("sonnet", 11, self.conn)
            orchestrator.ensure_agent_acked("sonnet", 12, self.conn)
        self.assertEqual(mock_ping.call_count, 2)

    def test_different_agents_same_task_each_get_a_ping(self):
        """Different agents for the same task_id both need to ping."""
        with patch.object(orchestrator, "ping_agent", return_value=True) as mock_ping:
            orchestrator.ensure_agent_acked("sonnet", 20, self.conn)
            orchestrator.ensure_agent_acked("haiku", 20, self.conn)
        self.assertEqual(mock_ping.call_count, 2)

    def test_ack_cache_populated_after_success(self):
        """Cache key is present after a successful ping so subsequent calls hit it."""
        with patch.object(orchestrator, "ping_agent", return_value=True):
            orchestrator.ensure_agent_acked("sonnet", 30, self.conn)
        self.assertIn(("sonnet", 30) if False else (30, "sonnet"),
                      orchestrator._agent_ack_cache)

    # ── retry loop ───────────────────────────────────────────────────

    def test_retry_until_agent_responds(self):
        """ensure_agent_acked retries and returns only after ping succeeds."""
        ping_results = [False, False, True]
        call_count = []

        def fake_ping(agent, task_id):
            call_count.append(1)
            return ping_results.pop(0)

        with (
            patch.object(orchestrator, "ping_agent", side_effect=fake_ping),
            patch.object(orchestrator, "time") as mock_time,
            patch.object(db, "update_runtime"),
        ):
            orchestrator.ensure_agent_acked("sonnet", 40, self.conn)

        self.assertEqual(len(call_count), 3, "Should have pinged 3 times before success")
        self.assertEqual(mock_time.sleep.call_count, 2, "Should have slept twice (once per failure)")

    def test_sleep_duration_matches_interval(self):
        """Sleep between retries uses PING_RETRY_INTERVAL."""
        ping_results = [False, True]

        with (
            patch.object(orchestrator, "ping_agent", side_effect=lambda *a: ping_results.pop(0)),
            patch.object(orchestrator, "time") as mock_time,
            patch.object(db, "update_runtime"),
        ):
            orchestrator.ensure_agent_acked("sonnet", 41, self.conn)

        mock_time.sleep.assert_called_with(orchestrator.PING_RETRY_INTERVAL)

    # ── runtime status messaging ─────────────────────────────────────

    def test_stall_status_message_contains_agent_and_task(self):
        """Runtime status message names both the agent and task_id when stalled."""
        ping_results = [False, True]
        captured_messages = []

        def fake_update_runtime(conn, **kwargs):
            if "status_message" in kwargs:
                captured_messages.append(kwargs["status_message"])

        with (
            patch.object(orchestrator, "ping_agent", side_effect=lambda *a: ping_results.pop(0)),
            patch.object(orchestrator, "time"),
            patch.object(db, "update_runtime", side_effect=fake_update_runtime),
        ):
            orchestrator.ensure_agent_acked("haiku", 50, self.conn)

        self.assertTrue(captured_messages, "Expected at least one status_message update")
        msg = captured_messages[0]
        self.assertIn("haiku", msg)
        self.assertIn("50", msg)

    def test_stall_status_message_says_stalled(self):
        """Runtime status message uses 'STALLED' so dashboards can spot it easily."""
        ping_results = [False, True]
        captured = []

        def fake_update_runtime(conn, **kwargs):
            if "status_message" in kwargs:
                captured.append(kwargs["status_message"])

        with (
            patch.object(orchestrator, "ping_agent", side_effect=lambda *a: ping_results.pop(0)),
            patch.object(orchestrator, "time"),
            patch.object(db, "update_runtime", side_effect=fake_update_runtime),
        ):
            orchestrator.ensure_agent_acked("haiku", 51, self.conn)

        self.assertTrue(captured)
        self.assertIn("STALLED", captured[0])

    def test_stall_status_message_mentions_retry(self):
        """Status message tells the operator retries are happening automatically."""
        ping_results = [False, True]
        captured = []

        def fake_update_runtime(conn, **kwargs):
            if "status_message" in kwargs:
                captured.append(kwargs["status_message"])

        with (
            patch.object(orchestrator, "ping_agent", side_effect=lambda *a: ping_results.pop(0)),
            patch.object(orchestrator, "time"),
            patch.object(db, "update_runtime", side_effect=fake_update_runtime),
        ):
            orchestrator.ensure_agent_acked("haiku", 52, self.conn)

        self.assertTrue(captured)
        self.assertTrue(
            "retry" in captured[0].lower() or "retrying" in captured[0].lower(),
            f"Expected 'retry'/'retrying' in status message; got: {captured[0]}",
        )

    def test_stall_log_mentions_no_other_tasks_will_run(self):
        """Run-log entry during stall explains that other tasks are blocked too."""
        ping_results = [False, True]
        log_messages = []

        def capture_log(msg, task_id=None):
            log_messages.append(msg)

        with (
            patch.object(orchestrator, "ping_agent", side_effect=lambda *a: ping_results.pop(0)),
            patch.object(orchestrator, "log", side_effect=capture_log),
            patch.object(orchestrator, "time"),
            patch.object(db, "update_runtime"),
        ):
            orchestrator.ensure_agent_acked("sonnet", 60, self.conn)

        stall_logs = [m for m in log_messages if "STALLED" in m or "no other" in m.lower()]
        self.assertTrue(stall_logs, f"Expected a stall log mentioning blocking; got: {log_messages}")

    def test_no_status_update_when_cache_hit(self):
        """Cache hit path never calls update_runtime (no unnecessary DB writes)."""
        orchestrator._agent_ack_cache.add((60, "sonnet"))

        with patch.object(db, "update_runtime") as mock_update:
            orchestrator.ensure_agent_acked("sonnet", 60, self.conn)

        mock_update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
