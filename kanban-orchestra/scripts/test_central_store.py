"""Focused coverage for one central database with independent queues."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db


def _git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)


class TestCentralQueues(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state" / "orchestra.db"
        self.repo_a = self.root / "repo-a"
        self.repo_b = self.root / "repo-b"
        self.repo_a.mkdir()
        self.repo_b.mkdir()
        _git_repo(self.repo_a)
        _git_repo(self.repo_b)
        self.conn_a = db.connect(str(self.db_path), repo_root=self.repo_a)
        self.conn_b = db.connect(str(self.db_path), repo_root=self.repo_b)

    def tearDown(self):
        self.conn_a.close()
        self.conn_b.close()
        self.tmp.cleanup()

    def test_two_queues_keep_independent_ids_claims_and_runtime(self):
        self.assertNotEqual(self.conn_a.orchestra_queue_id, self.conn_b.orchestra_queue_id)
        self.assertNotEqual(self.conn_a.orchestra_repository_id, self.conn_b.orchestra_repository_id)

        ready_a = db.add_task(self.conn_a, "Queue A", branch="a", status="ready", skips=["commit-plan"])
        ready_b = db.add_task(self.conn_b, "Queue B", branch="b", status="ready", skips=["commit-plan"])
        self.assertNotEqual(ready_a, ready_b)
        self.assertEqual(db.find_ready_task(self.conn_a)["id"], ready_a)
        self.assertEqual(db.find_ready_task(self.conn_b)["id"], ready_b)

        claimed = db.claim_ready_task(self.conn_a, ready_a)
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(db.find_ready_task(self.conn_b)["id"], ready_b)
        self.assertIsNone(db.claim_ready_task(self.conn_b, ready_a))

        db.upsert_runtime(self.conn_a, status="running", pid=11, current_task_id=ready_a, current_step="commit-make")
        db.upsert_runtime(self.conn_b, status="idle", pid=22, current_task_id=None, current_step="none")
        self.assertEqual(db.get_runtime(self.conn_a)["pid"], 11)
        self.assertEqual(db.get_runtime(self.conn_b)["pid"], 22)
        self.assertEqual(db.get_runtime(self.conn_a)["current_task_id"], ready_a)

        global_task = db.get_task(self.conn_b, ready_a)
        self.assertEqual(global_task["title"], "Queue A")
        self.assertEqual(db.list_tasks(self.conn_b, all_queues=True).__len__(), 2)
        self.assertEqual(len(db.list_tasks(self.conn_b)), 1)

    def test_missing_and_retired_worktrees_do_not_release_tasks(self):
        task_id = db.add_task(self.conn_a, "Stay put", branch="a", status="ready", skips=["commit-plan"])
        missing = self.root / "missing-worktree"
        with self.assertRaises(RuntimeError) as missing_ctx:
            db.ensure_queue(self.conn_b, missing)
        self.assertIn("missing", str(missing_ctx.exception).lower())
        self.assertEqual(db.get_task(self.conn_b, task_id)["queue_id"], self.conn_a.orchestra_queue_id)

        db.retire_queue(self.conn_a)
        with self.assertRaises(RuntimeError) as retired_ctx:
            db.connect(str(self.db_path), repo_root=self.repo_a)
        self.assertIn("retired", str(retired_ctx.exception).lower())
        self.assertTrue((db.get_runtime_root(str(self.db_path), queue_id=self.conn_a.orchestra_queue_id)).is_dir())
        self.assertEqual(db.get_task(self.conn_b, task_id)["queue_id"], self.conn_a.orchestra_queue_id)
        with self.assertRaises(ValueError):
            db.reassign_task_queue(self.conn_b, task_id, self.conn_b.orchestra_queue_id)

    def test_cross_repository_parent_is_rejected(self):
        parent = db.add_task(self.conn_a, "Parent", kind="supertask", branch="a")
        with self.assertRaises(ValueError):
            db.add_task(self.conn_b, "Child", branch="b", parent_task_id=parent)

    def test_legacy_worktree_database_is_not_modified(self):
        legacy = self.repo_a / "kanban-orchestra.db"
        conn = sqlite3.connect(legacy)
        conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT)")
        conn.execute("INSERT INTO tasks (title) VALUES ('keep me')")
        conn.commit()
        conn.close()
        before = legacy.read_bytes()

        with self.assertRaises(db.MigrationRequired) as ctx:
            db.connect(str(self.db_path), repo_root=self.repo_a)
        self.assertIn("Migration required", str(ctx.exception))
        self.assertIn(str(legacy), str(ctx.exception))
        self.assertEqual(legacy.read_bytes(), before)
        self.assertFalse(list(legacy.parent.glob("kanban-orchestra.db-wal")))

    def test_default_state_root_stays_out_of_the_checkout(self):
        state = self.root / "platform-state"
        state.mkdir()
        previous_state = os.environ.get("ORCHESTRA_STATE_DIR")
        previous_db = os.environ.get("KANBAN_DB")
        os.environ["ORCHESTRA_STATE_DIR"] = str(state)
        os.environ.pop("KANBAN_DB", None)
        try:
            self.assertEqual(Path(db.get_db_path()).resolve(), (state / "orchestra.db").resolve())
            conn = db.connect()
            self.addCleanup(conn.close)
            with self.assertRaises(RuntimeError):
                db.execution_root(conn)
            self.assertTrue((state / "orchestra.db").is_file())
            self.assertFalse((self.repo_a / "kanban-orchestra.db").exists())
        finally:
            if previous_state is None:
                os.environ.pop("ORCHESTRA_STATE_DIR", None)
            else:
                os.environ["ORCHESTRA_STATE_DIR"] = previous_state
            if previous_db is None:
                os.environ.pop("KANBAN_DB", None)
            else:
                os.environ["KANBAN_DB"] = previous_db

    def test_mutations_require_the_current_queue(self):
        task_id = db.add_task(self.conn_b, "Owned by B", branch="master")
        with self.assertRaises(ValueError):
            db.update_task(self.conn_a, task_id, title="stolen")
        with self.assertRaises(ValueError):
            db.add_comment(self.conn_a, task_id, "nope")
        with self.assertRaises(ValueError):
            db.add_run_log(self.conn_a, task_id, "nope")
        with self.assertRaises(ValueError):
            db.delete_task(self.conn_a, task_id)
        self.assertEqual(db.get_task(self.conn_a, task_id)["title"], "Owned by B")
        self.assertEqual(db.get_comments(self.conn_a, task_id), [])

        selected = db.connect(str(self.db_path), queue_id=self.conn_b.orchestra_queue_id)
        self.addCleanup(selected.close)
        db.update_task(selected, task_id, title="explicit")
        self.assertEqual(db.get_task(self.conn_a, task_id)["title"], "explicit")

        (self.repo_b / "AGENTS.md").write_text("ALLOW_TASKS_ON_MASTER\n", encoding="utf-8")
        env = os.environ.copy()
        env.pop("KANBAN_DB", None)
        env.pop("ORCHESTRA_QUEUE_ID", None)
        env.pop("ORCHESTRA_REPO_ROOT", None)
        env["ORCHESTRA_STATE_DIR"] = str(self.db_path.parent)
        env["KANBAN_NONINTERACTIVE"] = "1"
        task_py = str(Path(__file__).resolve().parent / "task.py")
        rejected = subprocess.run(
            [sys.executable, task_py, "set", str(task_id), "--status", "ready"],
            cwd=self.repo_a,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("another queue", rejected.stderr)
        self.assertEqual(db.get_task(self.conn_a, task_id)["status"], "none")

        allowed = subprocess.run(
            [
                sys.executable, task_py, "set", str(task_id),
                "--queue-id", self.conn_b.orchestra_queue_id,
                "--status", "ready",
            ],
            cwd=self.repo_a,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        updated = db.get_task(self.conn_a, task_id)
        self.assertEqual(updated["status"], "ready")
        self.assertEqual(updated["branch"], "master")
        self.assertEqual(updated["queue_id"], self.conn_b.orchestra_queue_id)

    def test_claim_rejects_retired_queue_and_missing_worktree(self):
        ready = db.add_task(
            self.conn_a, "Still ready", branch="a", status="ready", skips=["commit-plan"],
        )
        db.retire_queue(self.conn_a)
        self.assertIsNone(db.find_ready_task(self.conn_a))
        self.assertIsNone(db.claim_ready_task(self.conn_a, ready))
        self.assertEqual(db.get_task(self.conn_b, ready)["status"], "ready")

        other = db.add_task(
            self.conn_b, "Missing tree", branch="b", status="ready", skips=["commit-plan"],
        )
        self.assertEqual(db.find_ready_task(self.conn_b)["id"], other)
        shutil.rmtree(self.repo_b)
        self.assertIsNone(db.find_ready_task(self.conn_b))
        self.assertIsNone(db.claim_ready_task(self.conn_b, other))
        self.assertEqual(db.get_task(self.conn_a, other)["status"], "ready")

    def test_reassign_moves_history_and_artifacts(self):
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=self.repo_a, check=True, capture_output=True, text=True,
        )
        linked = self.root / "repo-a-linked"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "linked", str(linked)],
            cwd=self.repo_a, check=True, capture_output=True, text=True,
        )
        conn_linked = db.connect(str(self.db_path), repo_root=linked)
        self.addCleanup(conn_linked.close)
        self.assertEqual(conn_linked.orchestra_repository_id, self.conn_a.orchestra_repository_id)

        parent = db.add_task(self.conn_a, "Parent", kind="supertask", branch="a")
        child = db.add_task(self.conn_a, "Child", parent_task_id=parent)
        db.add_run_log(self.conn_a, parent, "parent log", author="orchestrator")
        db.add_run_log(self.conn_a, child, "child log", author="orchestrator")
        transcript = db.new_agent_transcript_path(
            child, "commit-make", "grok", str(self.db_path),
            queue_id=self.conn_a.orchestra_queue_id,
        )
        transcript.write_text("trace\n", encoding="utf-8")
        db.add_run_log(
            self.conn_a, child, f"Transcript: {transcript}", author="orchestrator",
        )
        db.upsert_runtime(
            self.conn_a, status="running", pid=7, current_task_id=child,
            current_step="commit-make",
        )

        moved = db.reassign_task_queue(self.conn_a, child, conn_linked.orchestra_queue_id)
        self.assertEqual(moved["queue_id"], conn_linked.orchestra_queue_id)
        self.assertEqual(
            db.get_task(conn_linked, parent)["queue_id"], conn_linked.orchestra_queue_id,
        )
        self.assertIsNone(db.get_runtime(self.conn_a)["current_task_id"])

        source_messages = [row["message"] for row in db.get_orchestrator_run_log(self.conn_a)]
        dest_messages = [row["message"] for row in db.get_orchestrator_run_log(conn_linked)]
        self.assertNotIn("child log", source_messages)
        self.assertIn("child log", dest_messages)
        self.assertTrue(all(
            row["queue_id"] == conn_linked.orchestra_queue_id
            for row in db.get_run_log(conn_linked, child)
        ))
        transcript_message = next(
            row["message"]
            for row in db.get_run_log(conn_linked, child)
            if row["message"].startswith("Transcript:")
        )
        new_path = Path(transcript_message.split("Transcript: ", 1)[1])
        self.assertTrue(new_path.is_file())
        self.assertEqual(new_path.read_text(encoding="utf-8"), "trace\n")
        self.assertIn(conn_linked.orchestra_queue_id, str(new_path))
        self.assertFalse(transcript.exists())

    def test_status_entry_points_bind_the_invoking_worktree(self):
        title_a = "Visible from repo A"
        title_b = "Visible from repo B"
        db.add_task(
            self.conn_a, title_a, branch="a", status="ready", skips=["commit-plan"],
        )
        db.add_task(
            self.conn_b, title_b, branch="b", status="ready", skips=["commit-plan"],
        )
        env = os.environ.copy()
        env["KANBAN_DB"] = str(self.db_path)
        env["ORCHESTRA_STATE_DIR"] = str(self.db_path.parent)
        env.pop("ORCHESTRA_QUEUE_ID", None)
        env.pop("ORCHESTRA_REPO_ROOT", None)
        script = Path(__file__).resolve().parent / "get_kanban_update.py"
        update = subprocess.run(
            [sys.executable, str(script), "--db", str(self.db_path)],
            cwd=self.repo_a,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(update.returncode, 0, update.stderr)
        self.assertIn(title_a, update.stdout)
        self.assertNotIn(title_b, update.stdout)
        self.assertIn(str(self.repo_a), update.stdout)

        previous_cwd = os.getcwd()
        previous_db = os.environ.get("KANBAN_DB")
        previous_queue = os.environ.get("ORCHESTRA_QUEUE_ID")
        os.chdir(self.repo_b)
        os.environ["KANBAN_DB"] = str(self.db_path)
        os.environ.pop("ORCHESTRA_QUEUE_ID", None)
        try:
            import dashboard
            conn = dashboard._open_conn()
            self.assertIsNotNone(conn)
            self.addCleanup(conn.close)
            self.assertEqual(conn.orchestra_queue_id, self.conn_b.orchestra_queue_id)
            titles = [task["title"] for task in db.list_tasks(conn, status="ready")]
            self.assertEqual(titles, [title_b])
        finally:
            os.chdir(previous_cwd)
            if previous_db is None:
                os.environ.pop("KANBAN_DB", None)
            else:
                os.environ["KANBAN_DB"] = previous_db
            if previous_queue is None:
                os.environ.pop("ORCHESTRA_QUEUE_ID", None)
            else:
                os.environ["ORCHESTRA_QUEUE_ID"] = previous_queue

    def test_task_cli_binds_invoking_worktree_when_database_is_shared(self):
        title_a = "Listed from repo A"
        title_b = "Listed from repo B"
        db.add_task(
            self.conn_a, title_a, branch="a", status="ready", skips=["commit-plan"],
        )
        db.add_task(
            self.conn_b, title_b, branch="b", status="ready", skips=["commit-plan"],
        )
        task_py = str(Path(__file__).resolve().parent / "task.py")

        def run_list(cwd: Path) -> subprocess.CompletedProcess[str]:
            env = os.environ.copy()
            env["KANBAN_DB"] = str(self.db_path)
            env["ORCHESTRA_STATE_DIR"] = str(self.db_path.parent)
            env.pop("ORCHESTRA_QUEUE_ID", None)
            env.pop("ORCHESTRA_REPO_ROOT", None)
            env["KANBAN_NONINTERACTIVE"] = "1"
            return subprocess.run(
                [sys.executable, task_py, "list", "--status", "ready"],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
            )

        listed_a = run_list(self.repo_a)
        self.assertEqual(listed_a.returncode, 0, listed_a.stderr)
        self.assertNotIn("more than one queue", listed_a.stderr)
        self.assertIn(title_a, listed_a.stdout)
        self.assertNotIn(title_b, listed_a.stdout)

        listed_b = run_list(self.repo_b)
        self.assertEqual(listed_b.returncode, 0, listed_b.stderr)
        self.assertIn(title_b, listed_b.stdout)
        self.assertNotIn(title_a, listed_b.stdout)

    def test_explicit_db_from_second_worktree_does_not_use_the_only_queue(self):
        fresh = self.root / "one-queue.db"
        repo_only = self.root / "only-registered"
        repo_other = self.root / "second-worktree"
        outside = self.root / "not-a-repo"
        repo_only.mkdir()
        repo_other.mkdir()
        outside.mkdir()
        _git_repo(repo_only)
        _git_repo(repo_other)
        conn_only = db.connect(str(fresh), repo_root=repo_only)
        self.addCleanup(conn_only.close)
        title = "Belongs to the only registered queue"
        task_id = db.add_task(
            conn_only, title, branch="only", status="ready", skips=["commit-plan"],
        )
        task_py = str(Path(__file__).resolve().parent / "task.py")

        def run_task(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
            env = os.environ.copy()
            env["KANBAN_DB"] = str(fresh)
            env["ORCHESTRA_STATE_DIR"] = str(fresh.parent)
            env.pop("ORCHESTRA_QUEUE_ID", None)
            env.pop("ORCHESTRA_REPO_ROOT", None)
            env["KANBAN_NONINTERACTIVE"] = "1"
            return subprocess.run(
                [sys.executable, task_py, *args],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
            )

        outside_list = run_task(outside, "list", "--status", "ready")
        self.assertEqual(outside_list.returncode, 0, outside_list.stderr)
        self.assertIn(title, outside_list.stdout)

        listed = run_task(repo_other, "list", "--status", "ready")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertNotIn(title, listed.stdout)
        self.assertEqual(json.loads(listed.stdout), [])

        stolen = run_task(repo_other, "set", str(task_id), "--title", "stolen")
        self.assertNotEqual(stolen.returncode, 0)
        self.assertEqual(db.get_task(conn_only, task_id)["title"], title)

        added = run_task(repo_other, "add", "Created in the other worktree", "--branch", "other")
        self.assertEqual(added.returncode, 0, added.stderr)
        created_id = json.loads(added.stdout)["id"]
        conn_other = db.connect(str(fresh), repo_root=repo_other)
        self.addCleanup(conn_other.close)
        self.assertNotEqual(conn_other.orchestra_queue_id, conn_only.orchestra_queue_id)
        self.assertEqual(db.get_task(conn_other, created_id)["queue_id"], conn_other.orchestra_queue_id)
        self.assertEqual(db.get_task(conn_only, task_id)["queue_id"], conn_only.orchestra_queue_id)

    def test_reassign_keeps_artifacts_when_a_destination_exists(self):
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=self.repo_a, check=True, capture_output=True, text=True,
        )
        linked = self.root / "repo-a-linked"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "linked", str(linked)],
            cwd=self.repo_a, check=True, capture_output=True, text=True,
        )
        conn_linked = db.connect(str(self.db_path), repo_root=linked)
        self.addCleanup(conn_linked.close)

        parent = db.add_task(self.conn_a, "Parent", kind="supertask", branch="a")
        child = db.add_task(self.conn_a, "Child", parent_task_id=parent)
        db.add_run_log(self.conn_a, parent, "parent log", author="orchestrator")
        parent_log = db.new_agent_transcript_path(
            parent, "commit-make", "grok", str(self.db_path),
            queue_id=self.conn_a.orchestra_queue_id,
        )
        parent_log.write_text("parent trace\n", encoding="utf-8")
        child_log = db.new_agent_transcript_path(
            child, "commit-make", "grok", str(self.db_path),
            queue_id=self.conn_a.orchestra_queue_id,
        )
        child_log.write_text("child trace\n", encoding="utf-8")
        dest_root = db.get_artifacts_root(
            str(self.db_path), queue_id=conn_linked.orchestra_queue_id,
        )
        dest_child = dest_root / f"task-{child}"
        dest_child.mkdir(parents=True)
        (dest_child / "existing.log").write_text("keep\n", encoding="utf-8")

        with self.assertRaises(ValueError):
            db.reassign_task_queue(self.conn_a, child, conn_linked.orchestra_queue_id)

        self.assertEqual(
            db.get_task(self.conn_a, parent)["queue_id"], self.conn_a.orchestra_queue_id,
        )
        self.assertEqual(
            db.get_task(self.conn_a, child)["queue_id"], self.conn_a.orchestra_queue_id,
        )
        source_rows = db.get_run_log(self.conn_a, parent)
        self.assertIn("parent log", [row["message"] for row in source_rows])
        self.assertTrue(all(
            row["queue_id"] == self.conn_a.orchestra_queue_id for row in source_rows
        ))
        self.assertNotIn(
            "parent log",
            [row["message"] for row in db.get_orchestrator_run_log(conn_linked)],
        )
        self.assertEqual(parent_log.read_text(encoding="utf-8"), "parent trace\n")
        self.assertEqual(child_log.read_text(encoding="utf-8"), "child trace\n")
        self.assertFalse((dest_root / f"task-{parent}").exists())
        self.assertEqual((dest_child / "existing.log").read_text(encoding="utf-8"), "keep\n")

    def test_two_queues_scope_dashboard_log_and_start_requests(self):
        import dashboard
        import fleet
        import orchestrator

        marker_a = "queue-a orchestrator line"
        marker_b = "queue-b orchestrator line"
        log_a = db.get_orchestrator_log_path(
            str(self.db_path), queue_id=self.conn_a.orchestra_queue_id,
        )
        log_b = db.get_orchestrator_log_path(
            str(self.db_path), queue_id=self.conn_b.orchestra_queue_id,
        )
        log_a.write_text(
            "Kanban Orchestra started. Polling for ready tasks...\n"
            f"{marker_a}\n",
            encoding="utf-8",
        )
        log_b.write_text(
            "Kanban Orchestra started. Polling for ready tasks...\n"
            f"{marker_b}\n",
            encoding="utf-8",
        )

        html_a = dashboard.render_global_run_log_panel(self.conn_a)
        html_b = dashboard.render_global_run_log_panel(self.conn_b)
        self.assertIn(marker_a, html_a)
        self.assertNotIn(marker_b, html_a)
        self.assertIn(marker_b, html_b)
        self.assertNotIn(marker_a, html_b)

        task_a = db.add_task(self.conn_a, "Active A", branch="a")
        task_b = db.add_task(self.conn_b, "Active B", branch="b")
        transcript_a = db.new_agent_transcript_path(
            task_a, "commit-make", "grok", str(self.db_path),
            queue_id=self.conn_a.orchestra_queue_id,
        )
        transcript_b = db.new_agent_transcript_path(
            task_b, "commit-make", "grok", str(self.db_path),
            queue_id=self.conn_b.orchestra_queue_id,
        )
        transcript_a.write_text("output from queue A\n", encoding="utf-8")
        transcript_b.write_text("output from queue B\n", encoding="utf-8")
        panel_a = dashboard.render_current_agent_output_panel(
            {"current_task_id": task_a, "current_step": "commit-make"},
            self.conn_a,
        )
        panel_b = dashboard.render_current_agent_output_panel(
            {"current_task_id": task_b, "current_step": "commit-make"},
            self.conn_b,
        )
        self.assertIn("output from queue A", panel_a)
        self.assertNotIn("output from queue B", panel_a)
        self.assertIn("output from queue B", panel_b)
        self.assertNotIn("output from queue A", panel_b)

        with self.assertRaises(RuntimeError):
            orchestrator.request_dashboard_start(str(self.db_path))

        path_a = orchestrator.request_dashboard_start(
            str(self.db_path), queue_id=self.conn_a.orchestra_queue_id,
        )
        self.assertEqual(path_a.parent, log_a.parent)
        self.assertEqual(path_a.read_text(encoding="utf-8"), "start\n")

        previous_db = os.environ.get("KANBAN_DB")
        os.environ["KANBAN_DB"] = str(self.db_path)
        try:
            repo_b = fleet.FleetRepo("repo-b", self.repo_b, self.repo_b.resolve())
            path_b = fleet.request_dashboard_start(repo_b, preferred_port=8433)
        finally:
            if previous_db is None:
                os.environ.pop("KANBAN_DB", None)
            else:
                os.environ["KANBAN_DB"] = previous_db

        self.assertEqual(path_b.parent, log_b.parent)
        self.assertEqual(path_b.read_text(encoding="utf-8"), "start\nport=8433\n")
        self.assertNotEqual(path_a, path_b)

        started = []

        def fake_start(db_path, **kwargs):
            started.append((db_path, kwargs))
            process = MagicMock()
            process.pid = 99
            return process

        with patch.object(orchestrator, "start_dashboard", side_effect=fake_start), \
             patch.object(orchestrator.db, "update_runtime"), \
             patch.object(orchestrator, "log"):
            orchestrator.handle_dashboard_start_request(self.conn_a, str(self.db_path))

        self.assertFalse(path_a.exists())
        self.assertTrue(path_b.is_file())
        self.assertEqual(started[0][1]["queue_id"], self.conn_a.orchestra_queue_id)
        self.assertIsNone(started[0][1]["preferred_port"])

        with patch.object(orchestrator, "start_dashboard", side_effect=fake_start), \
             patch.object(orchestrator.db, "update_runtime"), \
             patch.object(orchestrator, "log"):
            orchestrator.handle_dashboard_start_request(self.conn_b, str(self.db_path))

        self.assertFalse(path_b.exists())
        self.assertEqual(started[1][1]["queue_id"], self.conn_b.orchestra_queue_id)
        self.assertEqual(started[1][1]["preferred_port"], 8433)

        self.addCleanup(orchestrator.release_singleton_lock)
        orchestrator.acquire_singleton_lock(
            str(self.db_path), queue_id=self.conn_a.orchestra_queue_id,
        )
        env = os.environ.copy()
        env["KANBAN_DB"] = str(self.db_path)
        env["ORCHESTRA_STATE_DIR"] = str(self.db_path.parent)
        env.pop("ORCHESTRA_QUEUE_ID", None)
        env.pop("ORCHESTRA_REPO_ROOT", None)
        scripts = str(Path(__file__).resolve().parent)
        duplicate = subprocess.run(
            [
                sys.executable, "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); import orchestrator; "
                "raise SystemExit(orchestrator.main([]))",
                scripts,
            ],
            cwd=self.repo_a,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
        self.assertEqual(path_a.read_text(encoding="utf-8"), "start\n")
        self.assertFalse(path_b.exists())
