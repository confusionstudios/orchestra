#!/usr/bin/env python3
"""
Tests for get_kanban_update.py.
"""

import os
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
import get_kanban_update


def _fresh_conn():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = db.connect(tmp.name)
    return conn, tmp.name


class TestBuildUpdate(unittest.TestCase):
    def setUp(self):
        self.conn, self.db_path = _fresh_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_recently_done_shows_commit_hash(self):
        tid = db.add_task(self.conn, "Done task", branch="feat-done")
        db.update_task(
            self.conn,
            tid,
            status="done",
            commit_hash="22222222bbbbbbbb",
        )

        update = get_kanban_update.build_update(self.conn)

        self.assertIn("RECENTLY DONE (1 shown):", update)
        self.assertIn("#1 Done task  commit: 22222222", update)

    def test_update_shows_skips_for_active_ready_and_blocked_tasks(self):
        active_id = db.add_task(
            self.conn,
            "Active task",
            branch="feat-active",
            coder_agent="claude",
            reviewer_agent="gemini",
            skips=["commit-plan-review", "commit-review"],
        )
        ready_id = db.add_task(self.conn, "Ready task", branch="feat-ready", coder_agent="sonnet", reviewer_agent="opus", skips=["commit-plan"])
        blocked_id = db.add_task(self.conn, "Blocked task", branch="feat-blocked", coder_agent="haiku", reviewer_agent="codex", skips=["commit-review"])
        done_id = db.add_task(self.conn, "Done task", branch="feat-done", reviewer_agent="opus", skips=["commit-plan"])

        db.update_task(self.conn, active_id, status="running", review_round=1)
        db.update_task(self.conn, ready_id, status="ready")
        db.update_task(self.conn, blocked_id, status="blocked", next_step="commit-review")
        db.update_task(self.conn, done_id, status="done", commit_hash="33333333aaaaaaaa",
                       coder_agent="claude")
        db.add_comment(self.conn, done_id, "LGTM", kind="approval", author="gemini", review_round=0)
        db.add_comment(self.conn, blocked_id, "Needs human input")
        db.upsert_runtime(
            self.conn,
            status="running",
            current_task_id=active_id,
            current_step="commit-review",
            current_branch="feat-active",
            review_round=1,
        )

        update = get_kanban_update.build_update(self.conn)

        self.assertIn("  skips: commit-plan-review, commit-review", update)
        self.assertIn("branch: feat-active  coder: claude  reviewer: gemini", update)
        self.assertIn("#2 [commit-make] Ready task  branch: feat-ready  coder: sonnet  reviewer: opus  skips: commit-plan", update)
        self.assertIn("#3 Blocked task  coder: haiku  reviewer: codex  — Needs human input  skips: commit-review", update)
        self.assertIn("#4 Done task  commit: 33333333  coder: claude  configured reviewer: opus  approver: gemini", update)

    def test_operator_control_statuses_render_distinctly(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S")
        for status, attention in (
            ("starting", "orchestrator is starting"),
            ("stopped", "orchestrator is stopped"),
            ("hard-break", "hard BREAK completed"),
        ):
            with self.subTest(status=status):
                db.upsert_runtime(
                    self.conn,
                    status=status,
                    pid=None,
                    started_at=None,
                    last_heartbeat_at=old_ts,
                    current_task_id=None,
                    current_step="none",
                    active_agents=0,
                    status_message=f"{status} message",
                )

                update = get_kanban_update.build_update(self.conn)

                self.assertIn(f"ORCHESTRATOR: {status}", update)
                self.assertNotIn("ORCHESTRATOR: stale", update)
                self.assertIn(attention, update)

    def test_running_with_old_heartbeat_renders_stale(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S")
        db.upsert_runtime(
            self.conn,
            status="running",
            pid=123,
            started_at=None,
            last_heartbeat_at=old_ts,
            current_task_id=None,
            current_step="none",
            active_agents=0,
            status_message="old heartbeat",
        )

        update = get_kanban_update.build_update(self.conn)

        self.assertIn("ORCHESTRATOR: stale", update)
        self.assertIn("orchestrator heartbeat is stale", update)

    def test_process_pids_ignore_metadata_for_other_repo_identity(self):
        identity = db.get_instance_identity(self.db_path)
        lock_path = Path(identity["lock_path"])
        lock_path.write_text(
            "\n".join(
                [
                    "role=orchestrator",
                    f"pid={os.getpid()}",
                    f"repo_root={lock_path.parent / 'other-repo'}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        dashboard_path = Path(identity["runtime_root"]) / "dashboard.json"
        dashboard_path.parent.mkdir(parents=True, exist_ok=True)
        dashboard_path.write_text(
            json.dumps(
                {
                    "role": "dashboard",
                    "pid": os.getpid(),
                    "repo_root": str(lock_path.parent / "other-repo"),
                }
            ),
            encoding="utf-8",
        )
        db.upsert_runtime(
            self.conn,
            status="idle",
            pid=os.getpid(),
            started_at=None,
            last_heartbeat_at=datetime.now(timezone.utc).isoformat(),
            current_task_id=None,
            current_step="none",
            active_agents=0,
            status_message="idle",
        )

        update = get_kanban_update.build_update(self.conn)

        self.assertIn("processes: none found", update)
