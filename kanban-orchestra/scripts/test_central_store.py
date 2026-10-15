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

    def test_activation_verification_refuses_sqlite_errors(self):
        with patch.object(db, "_peek_database_kind", return_value="current"), patch.object(
            db.sqlite3, "connect", side_effect=sqlite3.OperationalError("unavailable")
        ):
            self.assertIn("Cannot verify migration activation", db.unfinished_activation_reason(self.db_path))
        broken = MagicMock()
        broken.execute.side_effect = sqlite3.OperationalError("unavailable")
        with self.assertRaisesRegex(RuntimeError, "Cannot verify migration activation"):
            db.assert_activation_finished(broken)

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

        historical = db.get_run_log(conn_linked, child)
        self.assertIn("child log", [row["message"] for row in historical])
        self.assertTrue(all(
            row["queue_id"] == self.conn_a.orchestra_queue_id for row in historical
        ))
        source_messages = [row["message"] for row in db.get_orchestrator_run_log(self.conn_a)]
        self.assertIn("child log", source_messages)
        self.assertNotIn(
            "child log",
            [row["message"] for row in db.get_orchestrator_run_log(conn_linked)],
        )
        transcript_message = next(
            row["message"] for row in historical if row["message"].startswith("Transcript:")
        )
        recorded_path = Path(transcript_message.split("Transcript: ", 1)[1])
        self.assertEqual(recorded_path, transcript)
        self.assertEqual(transcript.read_text(encoding="utf-8"), "trace\n")
        self.assertIn(self.conn_a.orchestra_queue_id, str(transcript))

        db.add_run_log(conn_linked, child, "ran on destination", author="orchestrator")
        new_rows = [
            row for row in db.get_run_log(conn_linked, child)
            if row["message"] == "ran on destination"
        ]
        self.assertEqual(len(new_rows), 1)
        self.assertEqual(new_rows[0]["queue_id"], conn_linked.orchestra_queue_id)
        self.assertIn(
            "ran on destination",
            [row["message"] for row in db.get_orchestrator_run_log(conn_linked)],
        )

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

        moved = db.reassign_task_queue(self.conn_a, child, conn_linked.orchestra_queue_id)
        self.assertEqual(moved["queue_id"], conn_linked.orchestra_queue_id)
        self.assertEqual(
            db.get_task(conn_linked, parent)["queue_id"], conn_linked.orchestra_queue_id,
        )

        source_rows = db.get_run_log(conn_linked, parent)
        self.assertIn("parent log", [row["message"] for row in source_rows])
        self.assertTrue(all(
            row["queue_id"] == self.conn_a.orchestra_queue_id for row in source_rows
        ))
        self.assertIn(
            "parent log",
            [row["message"] for row in db.get_orchestrator_run_log(self.conn_a)],
        )
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

    def test_reassign_refuses_a_running_family(self):
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
        child = db.add_task(self.conn_a, "Child", parent_task_id=parent, branch="a")
        db.update_task(self.conn_a, child, status="running")

        with self.assertRaises(ValueError) as ctx:
            db.reassign_task_queue(self.conn_a, parent, conn_linked.orchestra_queue_id)

        self.assertIn("running", str(ctx.exception).lower())
        self.assertEqual(db.get_task(self.conn_a, child)["status"], "running")
        self.assertEqual(db.get_task(self.conn_a, child)["queue_id"], self.conn_a.orchestra_queue_id)
        self.assertEqual(db.get_task(self.conn_a, parent)["queue_id"], self.conn_a.orchestra_queue_id)

    def test_dump_restore_round_trips_two_queues(self):
        parent = db.add_task(self.conn_a, "Parent", kind="supertask", branch="a")
        child = db.add_task(self.conn_a, "Child", parent_task_id=parent, branch="a")
        follow = db.add_task(self.conn_a, "Follow", branch="a")
        db.update_task(self.conn_a, parent, follow_up_task_id=follow)
        other = db.add_task(
            self.conn_b, "Other", branch="b", status="ready", skips=["commit-plan"],
        )
        task_py = str(Path(__file__).resolve().parent / "task.py")
        env = os.environ.copy()
        env["KANBAN_DB"] = str(self.db_path)
        env["ORCHESTRA_STATE_DIR"] = str(self.db_path.parent)
        env.pop("ORCHESTRA_QUEUE_ID", None)
        env.pop("ORCHESTRA_REPO_ROOT", None)
        env["KANBAN_NONINTERACTIVE"] = "1"
        dumped = subprocess.run(
            [sys.executable, task_py, "dump"],
            cwd=self.repo_a, env=env, capture_output=True, text=True,
        )
        self.assertEqual(dumped.returncode, 0, dumped.stderr)
        sql_path = self.db_path.parent / "orchestra.sql"
        self.assertIn(str(sql_path), dumped.stdout)
        self.assertNotIn("kanban-orchestra.sql", dumped.stdout)
        self.assertIn("BEGIN TRANSACTION;", sql_path.read_text(encoding="utf-8"))

        self.conn_a.close()
        self.conn_b.close()
        for suffix in ("-wal", "-shm"):
            Path(str(self.db_path) + suffix).unlink(missing_ok=True)
        self.db_path.rename(self.db_path.with_suffix(".aside"))

        restored = subprocess.run(
            [sys.executable, task_py, "restore"],
            cwd=self.repo_a, env=env, capture_output=True, text=True,
        )
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertNotIn("Traceback", restored.stderr)
        self.assertTrue(self.db_path.is_file())
        self.assertIn(str(sql_path), restored.stdout)

        check = sqlite3.connect(self.db_path)
        check.row_factory = sqlite3.Row
        try:
            self.assertGreaterEqual(
                check.execute("SELECT COUNT(*) FROM queues").fetchone()[0], 2,
            )
            parent_row = check.execute(
                "SELECT id, queue_id, follow_up_task_id FROM tasks WHERE title = 'Parent'"
            ).fetchone()
            child_row = check.execute(
                "SELECT parent_task_id, queue_id FROM tasks WHERE id = ?",
                (child,),
            ).fetchone()
            follow_row = check.execute(
                "SELECT id, queue_id FROM tasks WHERE id = ?",
                (follow,),
            ).fetchone()
            other_row = check.execute(
                "SELECT queue_id, status FROM tasks WHERE id = ?",
                (other,),
            ).fetchone()
            self.assertEqual(child_row["parent_task_id"], parent)
            self.assertEqual(child_row["queue_id"], parent_row["queue_id"])
            self.assertEqual(parent_row["follow_up_task_id"], follow)
            self.assertEqual(follow_row["queue_id"], parent_row["queue_id"])
            self.assertNotEqual(other_row["queue_id"], parent_row["queue_id"])
            self.assertEqual(other_row["status"], "ready")
            self.assertEqual(
                check.execute("PRAGMA integrity_check").fetchone()[0], "ok",
            )
            self.assertEqual(check.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            check.close()

        def listed(cwd: Path) -> str:
            result = subprocess.run(
                [sys.executable, task_py, "list"],
                cwd=cwd, env=env, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            return result.stdout

        from_a = listed(self.repo_a)
        from_b = listed(self.repo_b)
        self.assertIn("Parent", from_a)
        self.assertIn("Child", from_a)
        self.assertNotIn("Other", from_a)
        self.assertIn("Other", from_b)
        self.assertNotIn("Parent", from_b)

        bad_db = self.root / "bad-restore" / "orchestra.db"
        bad_db.parent.mkdir()
        (bad_db.parent / "orchestra.sql").write_text("this is not sql;\n", encoding="utf-8")
        bad_env = dict(env)
        bad_env["KANBAN_DB"] = str(bad_db)
        bad_env["ORCHESTRA_STATE_DIR"] = str(bad_db.parent)
        failed = subprocess.run(
            [sys.executable, task_py, "restore"],
            cwd=self.repo_a, env=bad_env, capture_output=True, text=True,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("Error:", failed.stderr)
        self.assertNotIn("Traceback", failed.stderr)
        self.assertFalse(bad_db.exists())

    def test_recreated_worktree_does_not_inherit_or_dispatch_stale_work(self):
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=self.repo_a, check=True, capture_output=True, text=True,
        )
        worktree = self.root / "wt"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "feat", str(worktree)],
            cwd=self.repo_a, check=True, capture_output=True, text=True,
        )
        conn = db.connect(str(self.db_path), repo_root=worktree)
        old_queue = conn.orchestra_queue_id
        task_id = db.add_task(
            conn, "Old ready", branch="feat", status="ready", skips=["commit-plan"],
        )
        conn.close()
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=self.repo_a, check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "other", str(worktree)],
            cwd=self.repo_a, check=True, capture_output=True, text=True,
        )

        hinted = db.connect(str(self.db_path), queue_id=old_queue)
        self.addCleanup(hinted.close)
        self.assertIsNotNone(db.queue_dispatch_block_reason(hinted))
        self.assertIsNone(db.find_ready_task(hinted))
        self.assertIsNone(db.claim_ready_task(hinted, task_id))
        self.assertEqual(db.get_task(hinted, task_id)["status"], "ready")
        self.assertEqual(db.get_task(hinted, task_id)["queue_id"], old_queue)

        task_py = str(Path(__file__).resolve().parent / "task.py")
        env = os.environ.copy()
        env["KANBAN_DB"] = str(self.db_path)
        env["ORCHESTRA_STATE_DIR"] = str(self.db_path.parent)
        env["ORCHESTRA_QUEUE_ID"] = old_queue
        env["ORCHESTRA_REPO_ROOT"] = str(worktree)
        env["KANBAN_NONINTERACTIVE"] = "1"
        stale = subprocess.run(
            [sys.executable, task_py, "list", "--status", "ready"],
            cwd=worktree, env=env, capture_output=True, text=True,
        )
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("does not carry queue", stale.stderr)
        self.assertNotIn("Old ready", stale.stdout)
        self.assertNotIn("Traceback", stale.stderr)

        fresh_env = dict(env)
        fresh_env.pop("ORCHESTRA_QUEUE_ID")
        fresh_env.pop("ORCHESTRA_REPO_ROOT")
        fresh = subprocess.run(
            [sys.executable, task_py, "list", "--status", "ready"],
            cwd=worktree, env=fresh_env, capture_output=True, text=True,
        )
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        self.assertEqual(json.loads(fresh.stdout), [])

        conn_new = db.connect(str(self.db_path), repo_root=worktree)
        self.addCleanup(conn_new.close)
        self.assertNotEqual(conn_new.orchestra_queue_id, old_queue)
        self.assertEqual(conn_new.orchestra_repository_id, self.conn_a.orchestra_repository_id)
        retired = conn_new.execute(
            "SELECT state FROM queues WHERE id = ?",
            (old_queue,),
        ).fetchone()
        self.assertEqual(retired["state"], "retired")
        self.assertEqual(db.get_task(conn_new, task_id)["queue_id"], old_queue)
        self.assertEqual(db.list_tasks(conn_new, status="ready"), [])

    def test_live_owner_blocks_a_reused_path_until_the_identity_moves(self):
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=self.repo_a, check=True, capture_output=True, text=True,
        )
        worktree = self.root / "wt"
        moved = self.root / "wt-moved"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "feat", str(worktree)],
            cwd=self.repo_a, check=True, capture_output=True, text=True,
        )
        conn = db.connect(str(self.db_path), repo_root=worktree)
        old_queue = conn.orchestra_queue_id
        task_id = db.add_task(conn, "Keep with the checkout", branch="feat")
        conn.close()
        subprocess.run(
            ["git", "worktree", "move", str(worktree), str(moved)],
            cwd=self.repo_a, check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "other", str(worktree)],
            cwd=self.repo_a, check=True, capture_output=True, text=True,
        )

        with self.assertRaises(RuntimeError) as ctx:
            db.connect(str(self.db_path), repo_root=worktree)
        self.assertIn(old_queue, str(ctx.exception))
        holder = self.conn_b.execute(
            "SELECT state, path FROM queues WHERE id = ?",
            (old_queue,),
        ).fetchone()
        self.assertEqual(holder["state"], "enabled")

        conn_moved = db.connect(str(self.db_path), repo_root=moved)
        self.addCleanup(conn_moved.close)
        self.assertEqual(conn_moved.orchestra_queue_id, old_queue)
        self.assertEqual(db.get_task(conn_moved, task_id)["title"], "Keep with the checkout")
        self.assertEqual(
            conn_moved.execute("SELECT path FROM queues WHERE id = ?", (old_queue,)).fetchone()["path"],
            str(moved.resolve()),
        )

        conn_new = db.connect(str(self.db_path), repo_root=worktree)
        self.addCleanup(conn_new.close)
        self.assertNotEqual(conn_new.orchestra_queue_id, old_queue)
        self.assertEqual(db.list_tasks(conn_new), [])
        self.assertEqual(db.get_task(conn_new, task_id)["queue_id"], old_queue)

    def test_copied_checkout_does_not_steal_and_a_move_keeps_the_queue(self):
        task_id = db.add_task(self.conn_a, "Stay", branch="a", status="ready", skips=["commit-plan"])
        queue_id = self.conn_a.orchestra_queue_id
        copied = self.root / "repo-a-copy"
        shutil.copytree(self.repo_a, copied, symlinks=True)

        with self.assertRaises(RuntimeError) as ctx:
            db.connect(str(self.db_path), repo_root=copied)
        self.assertIn(str(self.repo_a.resolve()), str(ctx.exception))
        held = self.conn_b.execute(
            "SELECT path, state FROM queues WHERE id = ?",
            (queue_id,),
        ).fetchone()
        self.assertEqual(held["path"], str(self.repo_a.resolve()))
        self.assertEqual(held["state"], "enabled")

        moved = self.root / "repo-a-moved"
        self.repo_a.rename(moved)
        conn_moved = db.connect(str(self.db_path), repo_root=moved)
        self.addCleanup(conn_moved.close)
        self.assertEqual(conn_moved.orchestra_queue_id, queue_id)
        self.assertEqual(conn_moved.orchestra_repository_id, self.conn_a.orchestra_repository_id)
        self.assertEqual(db.get_task(conn_moved, task_id)["title"], "Stay")
        self.assertEqual(
            conn_moved.execute("SELECT path FROM queues WHERE id = ?", (queue_id,)).fetchone()["path"],
            str(moved.resolve()),
        )

    def test_moved_primary_keeps_its_queue_when_a_new_repo_reuses_the_path(self):
        task_id = db.add_task(
            self.conn_a, "Keep with the primary", branch="a", status="ready", skips=["commit-plan"],
        )
        old_queue = self.conn_a.orchestra_queue_id
        repository_id = self.conn_a.orchestra_repository_id
        old_path = self.conn_a.execute(
            "SELECT path FROM queues WHERE id = ?",
            (old_queue,),
        ).fetchone()["path"]
        moved = self.root / "repo-a-moved"
        self.repo_a.rename(moved)
        self.repo_a.mkdir()
        _git_repo(self.repo_a)

        with self.assertRaises(RuntimeError) as ctx:
            db.connect(str(self.db_path), repo_root=self.repo_a)
        self.assertIn(old_queue, str(ctx.exception))
        self.assertIn("cannot prove", str(ctx.exception))
        self.assertIn("retire-queue", str(ctx.exception))
        holder = self.conn_b.execute(
            "SELECT state, path FROM queues WHERE id = ?",
            (old_queue,),
        ).fetchone()
        self.assertEqual(holder["state"], "enabled")
        self.assertEqual(holder["path"], old_path)

        hinted = db.connect(str(self.db_path), queue_id=old_queue)
        self.addCleanup(hinted.close)
        self.assertIsNotNone(db.queue_dispatch_block_reason(hinted))
        self.assertIsNone(db.claim_ready_task(hinted, task_id))
        self.assertEqual(db.get_task(hinted, task_id)["status"], "ready")
        self.assertEqual(db.get_task(hinted, task_id)["queue_id"], old_queue)

        conn_moved = db.connect(str(self.db_path), repo_root=moved)
        self.addCleanup(conn_moved.close)
        self.assertEqual(conn_moved.orchestra_queue_id, old_queue)
        self.assertEqual(conn_moved.orchestra_repository_id, repository_id)
        self.assertEqual(db.get_task(conn_moved, task_id)["title"], "Keep with the primary")
        self.assertEqual(db.get_task(conn_moved, task_id)["status"], "ready")
        self.assertEqual(
            conn_moved.execute(
                "SELECT path, state FROM queues WHERE id = ?",
                (old_queue,),
            ).fetchone()["path"],
            str(moved.resolve()),
        )
        self.assertEqual(
            conn_moved.execute("SELECT state FROM queues WHERE id = ?", (old_queue,)).fetchone()["state"],
            "enabled",
        )
        self.assertIsNone(db.queue_dispatch_block_reason(conn_moved))

        conn_new = db.connect(str(self.db_path), repo_root=self.repo_a)
        self.addCleanup(conn_new.close)
        self.assertNotEqual(conn_new.orchestra_queue_id, old_queue)
        self.assertEqual(db.list_tasks(conn_new, status="ready"), [])
        self.assertEqual(db.get_task(conn_new, task_id)["queue_id"], old_queue)
        self.assertIsNone(db.find_ready_task(conn_new))

    def test_reclone_at_reused_primary_path_refuses_until_the_old_queue_is_retired(self):
        task_id = db.add_task(
            self.conn_a, "Old primary", branch="a", status="ready", skips=["commit-plan"],
        )
        old_queue = self.conn_a.orchestra_queue_id
        shutil.rmtree(self.repo_a)
        self.repo_a.mkdir()
        _git_repo(self.repo_a)

        with self.assertRaises(RuntimeError) as ctx:
            db.connect(str(self.db_path), repo_root=self.repo_a)
        self.assertIn(old_queue, str(ctx.exception))
        self.assertIn("cannot prove", str(ctx.exception))
        self.assertIn("retire-queue", str(ctx.exception))
        held = self.conn_b.execute(
            "SELECT state FROM queues WHERE id = ?",
            (old_queue,),
        ).fetchone()
        self.assertEqual(held["state"], "enabled")
        self.assertEqual(db.get_task(self.conn_b, task_id)["queue_id"], old_queue)
        self.assertEqual(db.get_task(self.conn_b, task_id)["status"], "ready")

        db.retire_queue(self.conn_b, old_queue)
        conn = db.connect(str(self.db_path), repo_root=self.repo_a)
        self.addCleanup(conn.close)
        self.assertNotEqual(conn.orchestra_queue_id, old_queue)
        self.assertNotEqual(conn.orchestra_repository_id, self.conn_a.orchestra_repository_id)
        retired = conn.execute(
            "SELECT state FROM queues WHERE id = ?",
            (old_queue,),
        ).fetchone()
        self.assertEqual(retired["state"], "retired")
        self.assertEqual(db.list_tasks(conn, status="ready"), [])
        self.assertEqual(db.get_task(conn, task_id)["queue_id"], old_queue)
        self.assertEqual(db.get_task(conn, task_id)["status"], "ready")
        self.assertIsNone(db.find_ready_task(conn))

    def test_explicit_database_ignores_unrelated_inherited_queue_context(self):
        unrelated = self.root / "live-checkout"
        unrelated.mkdir()
        _git_repo(unrelated)
        (unrelated / "AGENTS.md").write_text("ALLOW_TASKS_ON_MASTER\n", encoding="utf-8")
        fresh = self.root / "isolated" / "orchestra.db"
        task_py = str(Path(__file__).resolve().parent / "task.py")
        env = os.environ.copy()
        env["KANBAN_DB"] = str(fresh)
        env["ORCHESTRA_STATE_DIR"] = str(fresh.parent)
        env["ORCHESTRA_QUEUE_ID"] = "11111111-1111-4111-8111-111111111111"
        env["ORCHESTRA_REPO_ROOT"] = str(unrelated.resolve())
        env["KANBAN_NONINTERACTIVE"] = "1"

        rejected = subprocess.run(
            [sys.executable, task_py, "add", "On master", "--branch", "master"],
            cwd=self.repo_a, env=env, capture_output=True, text=True,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertNotIn("Unknown queue", rejected.stderr)
        self.assertNotIn("Traceback", rejected.stderr)
        self.assertIn("ALLOW_TASKS_ON_MASTER", rejected.stderr)

        added = subprocess.run(
            [sys.executable, task_py, "add", "Isolated task", "--branch", "feat"],
            cwd=self.repo_a, env=env, capture_output=True, text=True,
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertNotIn("Unknown queue", added.stderr)
        conn = db.connect(str(fresh), repo_root=self.repo_a)
        self.addCleanup(conn.close)
        self.assertEqual(
            [task["title"] for task in db.list_tasks(conn)],
            ["Isolated task"],
        )
        self.assertNotEqual(conn.orchestra_queue_id, env["ORCHESTRA_QUEUE_ID"])
        self.assertNotEqual(db.connection_worktree(conn), unrelated.resolve())

    def test_dispatch_fails_closed_without_queue_context(self):
        raw = sqlite3.connect(self.db_path)
        raw.row_factory = sqlite3.Row
        try:
            reason = db.queue_dispatch_block_reason(raw)
        finally:
            raw.close()
        self.assertIsNotNone(reason)
        self.assertIn("unavailable", reason.lower())

    def test_main_loop_refuses_the_state_directory(self):
        import orchestrator
        conn = db.connect()
        self.addCleanup(conn.close)
        with self.assertRaises(RuntimeError) as ctx:
            orchestrator.main_loop(conn)
        self.assertIn("central state directory", str(ctx.exception).lower())
