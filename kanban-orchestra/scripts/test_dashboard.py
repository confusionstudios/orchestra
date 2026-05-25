#!/usr/bin/env python3
"""
Tests for Kanban Orchestra dashboard helpers.

Covers the pure helper functions and the fragment renderers.
Does not start an HTTP server; tests import dashboard directly.
"""

import asyncio
import errno as errno_mod
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
import dashboard


def _fresh_conn():
    """Return a connection to a fresh in-memory-ish temp DB."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = db.connect(tmp.name)
    return conn, tmp.name


class TestHelpers(unittest.TestCase):
    """Tests for pure helper functions."""

    def test_age_seconds(self):
        ts = (datetime.now(timezone.utc) - timedelta(seconds=45)).strftime("%Y-%m-%d %H:%M:%S")
        result = dashboard._age(ts)
        self.assertIn("s ago", result)

    def test_age_minutes(self):
        ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        result = dashboard._age(ts)
        self.assertIn("m ago", result)

    def test_age_hours(self):
        ts = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        result = dashboard._age(ts)
        self.assertIn("h ago", result)

    def test_age_none(self):
        self.assertEqual(dashboard._age(None), "unknown")

    def test_client_timestamp_is_utc_iso(self):
        self.assertEqual(
            dashboard._client_timestamp("2026-03-30 12:34:56"),
            "2026-03-30T12:34:56Z",
        )

    def test_display_timestamp_trims_to_minutes(self):
        utc_dt = datetime(2026, 3, 30, 12, 34, 56, tzinfo=timezone.utc)
        expected = utc_dt.astimezone().strftime("%Y-%m-%d %H:%M")
        self.assertEqual(
            dashboard._display_timestamp("2026-03-30 12:34:56"),
            expected,
        )

    def test_server_tz_label_returns_nonempty(self):
        label = dashboard._server_tz_label()
        self.assertIsInstance(label, str)
        self.assertGreater(len(label), 0)

    def test_dashboard_metadata_is_written_when_requested(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            metadata_path = Path(tmpdir) / "dashboard.json"
            identity = {
                "repo_root": tmpdir,
                "repo_label": "repo",
                "db_path": str(Path(tmpdir) / "kanban-orchestra.db"),
                "runtime_root": str(Path(tmpdir) / ".kanban-orchestra"),
                "lock_path": str(Path(tmpdir) / "kanban-orchestra.lock"),
            }
            with patch.dict(os.environ, {"KO_DASHBOARD_METADATA_PATH": str(metadata_path)}, clear=False), \
                 patch.object(dashboard.db, "get_instance_identity", return_value=identity):
                dashboard._write_dashboard_metadata("127.0.0.1", 8430)

                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["role"], "dashboard")
                self.assertEqual(payload["host"], "127.0.0.1")
                self.assertEqual(payload["port"], 8430)
                self.assertEqual(payload["url"], "http://127.0.0.1:8430")

                dashboard._remove_dashboard_metadata()
                self.assertFalse(metadata_path.exists())
            self.assertEqual(payload["repo_root"], tmpdir)

    def test_abbreviate_home_replaces_home_prefix(self):
        with patch("pathlib.Path.home", return_value=Path("/Users/alex")):
            self.assertEqual(
                dashboard._abbreviate_home(Path("/Users/alex/project").resolve()),
                "~/project",
            )

    def test_abbreviate_home_leaves_external_path_absolute(self):
        with patch("pathlib.Path.home", return_value=Path("/Users/alex")):
            self.assertEqual(
                dashboard._abbreviate_home(Path("/opt/project").resolve()),
                "/opt/project",
            )

    def test_is_stale_fresh(self):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.assertFalse(dashboard._is_stale(ts))

    def test_is_stale_old(self):
        ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertTrue(dashboard._is_stale(ts))

    def test_is_stale_none(self):
        self.assertTrue(dashboard._is_stale(None))

    def test_short_hash(self):
        self.assertEqual(dashboard._short_hash("abcdef1234567890"), "abcdef12")

    def test_short_hash_none(self):
        self.assertEqual(dashboard._short_hash(None), "")

    def test_esc_xss(self):
        result = dashboard._esc("<script>alert('xss')</script>")
        self.assertNotIn("<script>", result)
        self.assertIn("&lt;", result)

    def test_esc_none(self):
        self.assertEqual(dashboard._esc(None), "")

    def test_format_skips(self):
        self.assertEqual(
            dashboard._format_skips(["commit-plan", "commit-review"]),
            "commit-plan, commit-review",
        )
        self.assertEqual(dashboard._format_skips([]), "")

    def test_live_age_embeds_machine_timestamp(self):
        html = dashboard._live_age("2026-03-30 12:34:56", css_class="heartbeat-age")
        self.assertIn('data-relative-time="true"', html)
        self.assertIn('data-timestamp="2026-03-30T12:34:56Z"', html)
        self.assertIn('class="heartbeat-age"', html)

    def test_absolute_timestamp_html_is_server_rendered(self):
        html = dashboard._absolute_timestamp_html("2026-03-30 12:34:56", css_class="stamp")
        utc_dt = datetime(2026, 3, 30, 12, 34, 56, tzinfo=timezone.utc)
        expected_ts = utc_dt.astimezone().strftime("%Y-%m-%d %H:%M")
        self.assertIn('class="timestamp-absolute stamp"', html)
        self.assertIn(expected_ts, html)
        self.assertNotIn("data-local-time", html)
        self.assertNotIn("data-timestamp", html)

    def test_display_review_round_text_converts_round_labels(self):
        self.assertEqual(
            dashboard._display_review_round_text("Round 0: gemini reviewing; Review round 2 approved."),
            "Round 1: gemini reviewing; Review round 3 approved.",
        )


class TestPageShell(unittest.TestCase):
    """Tests for shared page shell timestamp formatting."""

    def test_page_shell_only_rewrites_relative_times(self):
        html = dashboard._page_shell("Title", "<p>Body</p>")
        self.assertIn("formatRelativeAge", html)
        self.assertIn("updateRelativeTimes", html)
        self.assertNotIn("formatLocalTimestamp", html)
        self.assertNotIn("updateAbsoluteTimes", html)
        self.assertNotIn("data-local-time", html)
        self.assertNotIn("Intl.DateTimeFormat", html)
        self.assertNotIn("timeZoneName", html)


class TestHealthCard(unittest.TestCase):
    """Tests for render_health_card."""

    def test_no_runtime(self):
        html = dashboard.render_health_card(None)
        self.assertIn("no runtime row", html)
        self.assertIn("health-card", html)

    def test_idle_fresh(self):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        runtime = {
            "status": "idle",
            "last_heartbeat_at": ts,
            "status_message": "Waiting for ready tasks",
            "current_task_id": None,
        }
        html = dashboard.render_health_card(runtime)
        self.assertIn("idle", html)
        self.assertIn("Waiting for ready tasks", html)
        self.assertNotIn("stale", html)

    def test_stale_heartbeat_shows_stale(self):
        ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S")
        runtime = {
            "status": "running",
            "last_heartbeat_at": ts,
            "status_message": None,
            "current_task_id": 5,
            "current_step": "commit-make",
            "current_branch": "feat-x",
            "review_round": 0,
        }
        html = dashboard.render_health_card(runtime)
        self.assertIn("stale", html)

    def test_operator_control_states_are_not_mislabeled_stale(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S")
        for status in ("starting", "stopped", "hard-break"):
            with self.subTest(status=status):
                runtime = {
                    "status": status,
                    "last_heartbeat_at": old_ts,
                    "status_message": f"{status} message",
                    "current_task_id": None,
                }
                html = dashboard.render_health_card(runtime)
                self.assertIn(status, html)
                self.assertNotIn('badge-stale">stale', html)

    def test_running_with_task(self):
        utc_dt = datetime.now(timezone.utc)
        ts = utc_dt.strftime("%Y-%m-%d %H:%M:%S")
        local_display = utc_dt.astimezone().strftime("%Y-%m-%d %H:%M")
        runtime = {
            "status": "running",
            "last_heartbeat_at": ts,
            "started_at": ts,
            "status_message": "Building commit",
            "current_task_id": 7,
            "current_step": "commit-make",
            "current_branch": "feat-y",
            "review_round": 1,
        }
        html = dashboard.render_health_card(runtime)
        self.assertIn("/task/7", html)
        self.assertIn("feat-y", html)
        self.assertIn("Building commit", html)
        self.assertIn('data-relative-time="true"', html)
        self.assertIn("Started:", html)
        self.assertIn(local_display, html)
        self.assertIn("Review round:</strong> 2", html)
        self.assertNotIn('data-local-time="true"', html)

    def test_review_status_shown(self):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        runtime = {
            "status": "running",
            "last_heartbeat_at": ts,
            "status_message": "Round 0: gemini reviewing",
            "current_task_id": 3,
            "current_step": "commit-review",
            "current_branch": "feat-z",
            "review_round": 0,
        }
        html = dashboard.render_health_card(runtime)
        self.assertIn("in progress", html)
        self.assertIn("Review round:</strong> 1", html)
        self.assertIn("Round 1: gemini reviewing", html)
        self.assertNotIn("Round 0: gemini reviewing", html)


class TestCurrentTaskCard(unittest.TestCase):
    """Tests for render_current_task_card."""

    def setUp(self):
        self.conn, self.db_path = _fresh_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_no_active_task(self):
        html = dashboard.render_current_task_card(None, self.conn)
        self.assertIn("idle", html)
        self.assertIn("current-task-card", html)

    def test_active_task_shown(self):
        tid = db.add_task(
            self.conn,
            "Implement feature X",
            branch="feat-x",
            coder_agent="claude",
            reviewer_agent="gemini",
            skips=["commit-plan-review"],
        )
        db.update_task(self.conn, tid, status="running")
        db.add_run_log(self.conn, tid, "Working on it", author="claude")
        utc_dt = datetime.now(timezone.utc)
        ts = utc_dt.strftime("%Y-%m-%d %H:%M:%S")
        local_display = utc_dt.astimezone().strftime("%Y-%m-%d %H:%M")
        runtime = {
            "status": "running",
            "last_heartbeat_at": ts,
            "current_task_id": tid,
            "current_step": "commit-make",
        }
        html = dashboard.render_current_task_card(runtime, self.conn)
        self.assertIn("Implement feature X", html)
        self.assertIn("feat-x", html)
        self.assertIn("Reviewer:</strong> gemini", html)
        self.assertIn("Working on it", html)
        self.assertIn(f"/task/{tid}", html)
        self.assertIn(f"Task {tid}: Implement feature X", html)
        self.assertIn("Review round:</strong> 1", html)
        self.assertIn("Skips:</strong> commit-plan-review", html)
        self.assertIn(local_display, html)
        self.assertNotIn('data-local-time="true"', html)

    def test_log_snippet_limited_to_10(self):
        tid = db.add_task(self.conn, "Many logs", branch="b")
        db.update_task(self.conn, tid, status="running")
        for i in range(15):
            db.add_run_log(self.conn, tid, f"Log entry {i}")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        runtime = {"status": "running", "last_heartbeat_at": ts, "current_task_id": tid, "current_step": "commit-make"}
        html = dashboard.render_current_task_card(runtime, self.conn)
        # Count log entries: last 10 of 15 should be entries 5-14
        self.assertIn("Log entry 14", html)
        self.assertNotIn("Log entry 4", html)

    def test_active_child_shows_supertask_context(self):
        parent_id = db.add_task(self.conn, "Parent supertask", branch="feat-parent", kind="supertask")
        db.update_task(self.conn, parent_id, status="pending_subtasks", next_step="none")
        child_id = db.add_task(
            self.conn,
            "Child implementation",
            branch="feat-parent",
            parent_task_id=parent_id,
            sequence_index=100,
        )
        db.update_task(self.conn, child_id, status="running")
        runtime = {
            "status": "running",
            "last_heartbeat_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "current_task_id": child_id,
            "current_step": "commit-make",
        }

        html = dashboard.render_current_task_card(runtime, self.conn)
        self.assertIn("Supertask:", html)
        self.assertIn("Parent supertask", html)
        self.assertIn("Position:</strong> 1 of 1", html)
        self.assertIn("Queue state:</strong> active now", html)

    def test_active_supertask_with_no_children_reads_cleanly(self):
        tid = db.add_task(self.conn, "Plan a supertask", branch="feat-super", kind="supertask")
        db.update_task(self.conn, tid, status="running", next_step="commit-make-supertask")
        runtime = {
            "status": "running",
            "last_heartbeat_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "current_task_id": tid,
            "current_step": "commit-make-supertask",
        }

        html = dashboard.render_current_task_card(runtime, self.conn)
        self.assertIn("Kind:</strong> supertask plan in progress", html)
        self.assertIn("Children:</strong> none yet", html)
        self.assertNotIn("0/0 children done", html)


class TestReadyQueue(unittest.TestCase):
    """Tests for render_ready_queue."""

    def setUp(self):
        self.conn, self.db_path = _fresh_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_empty(self):
        html = dashboard.render_ready_queue(self.conn)
        self.assertIn("No ready work", html)

    def test_tasks_listed(self):
        t1 = db.add_task(
            self.conn,
            "Task Alpha",
            branch="feat-a",
            coder_agent="codex",
            reviewer_agent="opus",
            skips=["commit-plan", "commit-review"],
        )
        db.update_task(self.conn, t1, status="ready")
        t2 = db.add_task(self.conn, "Task Beta", branch="feat-b", coder_agent="gemini")
        db.update_task(self.conn, t2, status="ready")
        html = dashboard.render_ready_queue(self.conn)
        self.assertIn("Task Alpha", html)
        self.assertIn("Task Beta", html)
        self.assertIn("feat-a", html)
        self.assertIn("codex", html)
        self.assertIn("opus", html)
        self.assertIn("commit-plan, commit-review", html)
        self.assertNotIn("Queued Behind Supertask", html)

    def test_non_ready_not_shown(self):
        t1 = db.add_task(self.conn, "Running task", branch="b")
        db.update_task(self.conn, t1, status="running")
        html = dashboard.render_ready_queue(self.conn)
        self.assertIn("No ready work", html)

    def test_no_conn(self):
        html = dashboard.render_ready_queue(None)
        self.assertIn("not available", html)

    def test_not_limited_to_20_tasks(self):
        for i in range(25):
            tid = db.add_task(self.conn, f"Ready task {i}", branch=f"feat-{i}")
            db.update_task(self.conn, tid, status="ready")
        html = dashboard.render_ready_queue(self.conn)
        self.assertIn("Ready task 24", html)
        self.assertIn("/task/25", html)

    def test_ready_queue_orders_by_sequence_index(self):
        first_id = db.add_task(self.conn, "Later ready", branch="feat-late", sequence_index=200)
        second_id = db.add_task(self.conn, "Earlier ready", branch="feat-early", sequence_index=100)
        db.update_task(self.conn, first_id, status="ready")
        db.update_task(self.conn, second_id, status="ready")

        html = dashboard.render_ready_queue(self.conn)
        self.assertLess(html.find("Earlier ready"), html.find("Later ready"))

    def test_child_ready_task_waiting_for_plan_review_is_gated(self):
        parent_id = db.add_task(self.conn, "Parent supertask", branch="feat-parent", kind="supertask")
        db.update_task(self.conn, parent_id, status="ready", next_step="commit-review-supertask")
        child_id = db.add_task(
            self.conn,
            "Child task",
            branch="feat-parent",
            parent_task_id=parent_id,
            sequence_index=100,
        )
        db.update_task(self.conn, child_id, status="ready")

        html = dashboard.render_ready_queue(self.conn)
        self.assertIn("Runnable Now", html)
        self.assertIn("Queued Behind Supertask", html)
        self.assertIn("waiting for supertask plan review", html)
        self.assertIn("Child task", html)

    def test_later_child_waits_for_earlier_sibling(self):
        parent_id = db.add_task(self.conn, "Sequenced supertask", branch="feat-seq", kind="supertask")
        db.update_task(self.conn, parent_id, status="pending_subtasks", next_step="none")
        first_child = db.add_task(
            self.conn,
            "First child",
            branch="feat-seq",
            parent_task_id=parent_id,
            sequence_index=100,
        )
        second_child = db.add_task(
            self.conn,
            "Second child",
            branch="feat-seq",
            parent_task_id=parent_id,
            sequence_index=200,
        )
        db.update_task(self.conn, first_child, status="ready")
        db.update_task(self.conn, second_child, status="ready")

        html = dashboard.render_ready_queue(self.conn)
        self.assertIn("First child", html)
        self.assertIn("Second child", html)
        self.assertIn(f"waiting for Task {first_child}", html)

    def test_gated_children_render_in_sequence_order(self):
        parent_id = db.add_task(self.conn, "Reviewing supertask", branch="feat-seq", kind="supertask")
        db.update_task(self.conn, parent_id, status="ready", next_step="commit-review-supertask")
        later_child = db.add_task(
            self.conn,
            "Second child",
            branch="feat-seq",
            parent_task_id=parent_id,
            sequence_index=200,
        )
        earlier_child = db.add_task(
            self.conn,
            "First child",
            branch="feat-seq",
            parent_task_id=parent_id,
            sequence_index=100,
        )
        db.update_task(self.conn, later_child, status="ready")
        db.update_task(self.conn, earlier_child, status="ready")

        html = dashboard.render_ready_queue(self.conn)
        self.assertLess(html.find("First child"), html.find("Second child"))

    def test_current_task_is_not_listed_in_ready_queue(self):
        tid = db.add_task(self.conn, "Current ready task", branch="feat-current")
        other_id = db.add_task(self.conn, "Other ready task", branch="feat-other")
        db.update_task(self.conn, tid, status="ready")
        db.update_task(self.conn, other_id, status="ready")

        html = dashboard.render_ready_queue(
            self.conn,
            runtime={"current_task_id": tid, "status": "running"},
        )
        self.assertNotIn("Current ready task", html)
        self.assertIn("Other ready task", html)


class TestActiveSupertasks(unittest.TestCase):
    """Tests for render_active_supertasks."""

    def setUp(self):
        self.conn, self.db_path = _fresh_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_empty(self):
        html = dashboard.render_active_supertasks(self.conn)
        self.assertIn("No active supertasks", html)

    def test_pending_supertask_is_listed_with_progress(self):
        parent_id = db.add_task(self.conn, "Parent supertask", branch="feat-parent", kind="supertask")
        db.update_task(self.conn, parent_id, status="pending_subtasks", next_step="none")
        child_done = db.add_task(
            self.conn,
            "Child done",
            branch="feat-parent",
            parent_task_id=parent_id,
            sequence_index=100,
        )
        child_ready = db.add_task(
            self.conn,
            "Child ready",
            branch="feat-parent",
            parent_task_id=parent_id,
            sequence_index=200,
        )
        child_running = db.add_task(
            self.conn,
            "Child running",
            branch="feat-parent",
            parent_task_id=parent_id,
            sequence_index=300,
        )
        child_blocked = db.add_task(
            self.conn,
            "Child blocked",
            branch="feat-parent",
            parent_task_id=parent_id,
            sequence_index=400,
        )
        db.update_task(self.conn, child_done, status="done")
        db.update_task(self.conn, child_ready, status="ready")
        db.update_task(self.conn, child_running, status="running")
        db.update_task(self.conn, child_blocked, status="blocked")

        html = dashboard.render_active_supertasks(self.conn)
        self.assertIn("Active Supertasks", html)
        self.assertIn("Parent supertask", html)
        self.assertIn("feat-parent", html)
        self.assertIn(">1/4<", html)
        self.assertGreaterEqual(html.count(">1<"), 3)


class TestIcebox(unittest.TestCase):
    """Tests for render_icebox."""

    def setUp(self):
        self.conn, self.db_path = _fresh_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_empty(self):
        html = dashboard.render_icebox(self.conn)
        self.assertIn("No parked tasks", html)

    def test_none_tasks_listed(self):
        db.add_task(self.conn, "Idea Alpha", branch="feat-a", coder_agent="codex")
        t2 = db.add_task(self.conn, "Idea Beta", branch="feat-b", coder_agent="gemini")
        db.update_task(self.conn, t2, status="ready")
        html = dashboard.render_icebox(self.conn)
        self.assertIn("Idea Alpha", html)
        self.assertIn("feat-a", html)
        self.assertIn("codex", html)
        self.assertNotIn("Idea Beta", html)

    def test_updated_column_uses_updated_at(self):
        tid = db.add_task(self.conn, "Freshly parked", branch="feat-r")
        self.conn.execute(
            "UPDATE tasks SET created_at = datetime('now', '-2 days'), "
            "updated_at = datetime('now', '-2 days') WHERE id = ?",
            (tid,),
        )
        self.conn.commit()
        db.update_task(self.conn, tid, coder_agent="codex")
        html = dashboard.render_icebox(self.conn)
        self.assertIn("0s ago", html)

    def test_no_conn(self):
        html = dashboard.render_icebox(None)
        self.assertIn("not available", html)

    def test_not_limited_to_20_tasks(self):
        for i in range(25):
            db.add_task(self.conn, f"Icebox task {i}", branch=f"feat-{i}")
        html = dashboard.render_icebox(self.conn)
        self.assertIn("Icebox task 24", html)
        self.assertIn("/task/25", html)


class TestBlockedTasks(unittest.TestCase):
    """Tests for render_blocked_tasks."""

    def setUp(self):
        self.conn, self.db_path = _fresh_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_empty(self):
        html = dashboard.render_blocked_tasks(self.conn)
        self.assertIn("No blocked tasks", html)

    def test_blocked_task_shown(self):
        tid = db.add_task(self.conn, "Stuck task", branch="feat-s", skips=["commit-review"])
        db.update_task(self.conn, tid, status="blocked")
        db.add_comment(self.conn, tid, "Cannot resolve dependency")
        html = dashboard.render_blocked_tasks(self.conn)
        self.assertIn("Stuck task", html)
        self.assertIn("Cannot resolve dependency", html)
        self.assertIn("commit-review", html)

    def test_long_comment_truncated(self):
        tid = db.add_task(self.conn, "Long comment task", branch="b")
        db.update_task(self.conn, tid, status="blocked")
        db.add_comment(self.conn, tid, "x" * 200)
        html = dashboard.render_blocked_tasks(self.conn)
        self.assertIn("...", html)

    def test_updated_column_uses_updated_at(self):
        tid = db.add_task(self.conn, "Recently blocked", branch="feat-r")
        self.conn.execute(
            "UPDATE tasks SET created_at = datetime('now', '-2 days'), "
            "updated_at = datetime('now', '-2 days') WHERE id = ?",
            (tid,),
        )
        self.conn.commit()
        db.update_task(self.conn, tid, status="blocked")
        db.add_comment(self.conn, tid, "Fresh blocker")
        html = dashboard.render_blocked_tasks(self.conn)
        self.assertIn("0s ago", html)

    def test_not_limited_to_20_tasks(self):
        for i in range(25):
            tid = db.add_task(self.conn, f"Blocked task {i}", branch=f"feat-{i}")
            db.update_task(self.conn, tid, status="blocked")
            db.add_comment(self.conn, tid, f"Blocker {i}")
        html = dashboard.render_blocked_tasks(self.conn)
        self.assertIn("Blocked task 24", html)
        self.assertIn("Blocker 24", html)


class TestRecentlyDone(unittest.TestCase):
    """Tests for render_recently_done."""

    def setUp(self):
        self.conn, self.db_path = _fresh_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_empty(self):
        html = dashboard.render_recently_done(self.conn)
        self.assertIn("No completed tasks", html)

    def test_done_tasks_shown(self):
        for i in range(3):
            tid = db.add_task(self.conn, f"Done task {i}", branch=f"feat-{i}")
            db.update_task(self.conn, tid, status="done", commit_hash=f"abc{i:04x}00000000")
        html = dashboard.render_recently_done(self.conn)
        self.assertIn("Done task 0", html)
        self.assertIn("Done task 2", html)

    def test_done_tasks_show_commit_hash(self):
        tid = db.add_task(self.conn, "Done task", branch="feat-done")
        db.update_task(
            self.conn,
            tid,
            status="done",
            commit_hash="22222222bbbbbbbb",
            coder_agent="claude",
            reviewer_agent="opus",
        )
        db.add_comment(self.conn, tid, "LGTM", kind="approval", author="gemini", review_round=0)
        html = dashboard.render_recently_done(self.conn)
        self.assertIn("Done task", html)
        self.assertIn("22222222", html)
        self.assertIn("claude", html)
        self.assertIn("opus", html)
        self.assertIn("gemini", html)

    def test_recently_done_initially_hides_rows_after_first_five(self):
        for i in range(7):
            tid = db.add_task(self.conn, f"Done {i}", branch="b")
            db.update_task(self.conn, tid, status="done")
        html = dashboard.render_recently_done(self.conn)
        self.assertIn("/task/1", html)
        self.assertIn("/task/7", html)
        self.assertIn('data-row-index="5" hidden', html)
        self.assertIn('data-row-index="6" hidden', html)
        self.assertIn("Show More", html)
        self.assertIn("Showing 5 of 7", html)

    def test_recently_done_no_show_more_when_five_or_fewer(self):
        for i in range(5):
            tid = db.add_task(self.conn, f"Done {i}", branch="b")
            db.update_task(self.conn, tid, status="done")
        html = dashboard.render_recently_done(self.conn)
        self.assertNotIn("Show More", html)
        self.assertNotIn("data-show-more-controls", html)

    def test_recent_done_orders_by_updated_at(self):
        older_id = db.add_task(self.conn, "Older task", branch="feat-old")
        newer_id = db.add_task(self.conn, "Newer task", branch="feat-new")
        db.update_task(self.conn, older_id, status="done")
        db.update_task(self.conn, newer_id, status="done")
        self.conn.execute(
            "UPDATE tasks SET updated_at = datetime('now', '-2 days') WHERE id = ?",
            (newer_id,),
        )
        self.conn.commit()
        db.update_task(self.conn, older_id, status="done")
        html = dashboard.render_recently_done(self.conn)
        self.assertLess(html.find("Older task"), html.find("Newer task"))


class TestTaskHeader(unittest.TestCase):
    """Tests for render_task_header."""

    def test_fields_present(self):
        created_utc = datetime(2026, 3, 30, 12, 34, 56, tzinfo=timezone.utc)
        updated_utc = datetime(2026, 3, 30, 13, 35, 57, tzinfo=timezone.utc)
        task = {
            "id": 42,
            "koid": "ko-abc123456789",
            "title": "My Feature",
            "description": "Does stuff",
            "status": "running",
            "next_step": "commit-review",
            "branch": "feat-42",
            "coder_agent": "claude",
            "reviewer_agent": "gemini",
            "review_round": 2,
            "last_review_decision": "reject",
            "commit_hash": None,
            "created_at": "2026-03-30 12:34:56",
            "updated_at": "2026-03-30 13:35:57",
            "skips": ["commit-plan", "commit-review"],
        }
        html = dashboard.render_task_header(task)
        self.assertIn("My Feature", html)
        self.assertIn("Task 42: My Feature", html)
        self.assertIn("Does stuff", html)
        self.assertIn('action="/task/42/edit"', html)
        self.assertIn("task-title-editor-42", html)
        self.assertIn("task-description-editor-42", html)
        self.assertIn("Description Source (Markdown)", html)
        self.assertIn("feat-42", html)
        self.assertIn("claude", html)
        self.assertIn("<th>Reviewer</th><td>gemini</td>", html)
        self.assertIn("commit-plan, commit-review", html)
        self.assertIn("<th>Review round</th><td>3</td>", html)
        self.assertIn("badge-running", html)
        self.assertIn("Created", html)
        self.assertIn("Updated", html)
        self.assertIn(created_utc.astimezone().strftime("%Y-%m-%d %H:%M"), html)
        self.assertIn(updated_utc.astimezone().strftime("%Y-%m-%d %H:%M"), html)
        self.assertNotIn("koid:", html)
        self.assertNotIn('data-local-time="true"', html)

    def test_xss_escaped_in_title(self):
        task = {
            "id": 1,
            "koid": "ko-x",
            "title": "<b>evil</b>",
            "description": None,
            "status": "none",
            "next_step": "commit-make",
            "branch": None,
            "coder_agent": None,
            "review_round": 0,
            "last_review_decision": "none",
            "commit_hash": None,
        }
        html = dashboard.render_task_header(task)
        self.assertNotIn("<b>evil</b>", html)
        self.assertIn("&lt;b&gt;", html)

    def test_markdown_description_renders_in_header_and_preserves_source(self):
        task = {
            "id": 7,
            "koid": "ko-markdown",
            "title": "Markdown task",
            "description": "**Bold** item",
            "status": "none",
            "next_step": "commit-make",
            "branch": None,
            "coder_agent": None,
            "review_round": 0,
            "last_review_decision": "none",
            "commit_hash": None,
        }
        html = dashboard.render_task_header(task)
        self.assertIn("<strong>Bold</strong>", html)
        self.assertIn("**Bold** item", html)

    def test_raw_html_in_description_is_escaped(self):
        task = {
            "id": 8,
            "koid": "ko-safe",
            "title": "Safe markdown",
            "description": "<script>alert(1)</script>",
            "status": "none",
            "next_step": "commit-make",
            "branch": None,
            "coder_agent": None,
            "review_round": 0,
            "last_review_decision": "none",
            "commit_hash": None,
        }
        html = dashboard.render_task_header(task)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_validation_error_opens_inline_title_editor(self):
        task = {
            "id": 10,
            "koid": "ko-error",
            "title": "   ",
            "description": "keep this",
            "status": "running",
            "next_step": "commit-make",
            "branch": "feat-error",
            "coder_agent": "codex",
            "review_round": 0,
            "last_review_decision": "none",
            "commit_hash": None,
        }
        html = dashboard.render_task_header(task, edit_error="Title is required.")
        self.assertIn("Title is required.", html)
        self.assertIn('data-inline-display hidden', html)
        self.assertIn('class="edit-form inline-edit-form title-edit-form" data-inline-form', html)

    def test_commit_field_uses_commit_hash(self):
        task = {
            "id": 9,
            "koid": "ko-commits",
            "title": "Done task",
            "description": None,
            "status": "done",
            "next_step": "none",
            "branch": "feat-done",
            "coder_agent": "codex",
            "review_round": 0,
            "last_review_decision": "approve",
            "commit_hash": "bbbbbbbb22222222",
        }
        html = dashboard.render_task_header(task)
        self.assertIn("bbbbbbbb", html)

    def test_commit_field_shows_dash_when_no_commit_hash(self):
        task = {
            "id": 10,
            "koid": "ko-commits",
            "title": "In-progress task",
            "description": None,
            "status": "in_progress",
            "next_step": "commit-make",
            "branch": "feat-wip",
            "coder_agent": "sonnet",
            "review_round": 0,
            "last_review_decision": "none",
            "commit_hash": None,
        }
        html = dashboard.render_task_header(task)
        self.assertIn(">-<", html)


class TestTaskHeaderHierarchy(unittest.TestCase):
    """Tests for task hierarchy rendering in the task header."""

    def setUp(self):
        self.conn, self.db_path = _fresh_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_supertask_header_shows_child_progress(self):
        parent_id = db.add_task(self.conn, "Parent supertask", branch="feat-parent", kind="supertask")
        child_done = db.add_task(
            self.conn,
            "Child done",
            branch="feat-parent",
            parent_task_id=parent_id,
            sequence_index=100,
        )
        child_ready = db.add_task(
            self.conn,
            "Child ready",
            branch="feat-parent",
            parent_task_id=parent_id,
            sequence_index=200,
        )
        db.update_task(self.conn, parent_id, status="pending_subtasks", next_step="none")
        db.update_task(self.conn, child_done, status="done", next_step="none")
        db.update_task(self.conn, child_ready, status="ready")

        task = db.get_task(self.conn, parent_id)
        html = dashboard.render_task_header(task, self.conn)
        self.assertIn("This is a supertask", html)
        self.assertIn("badge-pending-subtasks", html)
        self.assertIn("Children:</strong> 1/2 done", html)
        self.assertIn("Child done", html)
        self.assertIn("Child ready", html)

    def test_child_header_shows_parent_and_sequence(self):
        parent_id = db.add_task(self.conn, "Parent supertask", branch="feat-parent", kind="supertask")
        first_child = db.add_task(
            self.conn,
            "First child",
            branch="feat-parent",
            parent_task_id=parent_id,
            sequence_index=100,
        )
        second_child = db.add_task(
            self.conn,
            "Second child",
            branch="feat-parent",
            parent_task_id=parent_id,
            sequence_index=200,
        )
        db.update_task(self.conn, parent_id, status="pending_subtasks", next_step="none")
        db.update_task(self.conn, first_child, status="done", next_step="none")
        db.update_task(self.conn, second_child, status="ready")

        task = db.get_task(self.conn, second_child)
        html = dashboard.render_task_header(task, self.conn)
        self.assertIn("Parent supertask:", html)
        self.assertIn("Task 1: Parent supertask", html)
        self.assertIn("Sequence:</strong> 2 of 2", html)
        self.assertIn("Queue state:</strong> runnable now", html)

    def test_running_child_header_shows_active_now(self):
        parent_id = db.add_task(self.conn, "Parent supertask", branch="feat-parent", kind="supertask")
        child_id = db.add_task(
            self.conn,
            "Running child",
            branch="feat-parent",
            parent_task_id=parent_id,
            sequence_index=100,
        )
        db.update_task(self.conn, parent_id, status="pending_subtasks", next_step="none")
        db.update_task(self.conn, child_id, status="running", next_step="commit-make")

        task = db.get_task(self.conn, child_id)
        html = dashboard.render_task_header(task, self.conn)
        self.assertIn("Queue state:</strong> active now", html)


class TestCommentsPanel(unittest.TestCase):
    """Tests for render_comments_panel."""

    def setUp(self):
        self.conn, self.db_path = _fresh_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_no_comments(self):
        tid = db.add_task(self.conn, "No comments")
        html = dashboard.render_comments_panel(tid, self.conn)
        self.assertIn("No comments yet", html)

    def test_comments_rendered_in_order(self):
        tid = db.add_task(self.conn, "Comment order")
        db.add_comment(self.conn, tid, "First note", kind="comment", author="human")
        db.add_comment(self.conn, tid, "Approve it", kind="approval", author="gemini")
        db.add_comment(self.conn, tid, "Reject it", kind="rejection", author="codex")
        html = dashboard.render_comments_panel(tid, self.conn)
        idx_first = html.find("First note")
        idx_approve = html.find("Approve it")
        idx_reject = html.find("Reject it")
        self.assertLess(idx_first, idx_approve)
        self.assertLess(idx_approve, idx_reject)

    def test_comment_timestamps_are_server_rendered(self):
        tid = db.add_task(self.conn, "Comment timestamps")
        db.add_comment(self.conn, tid, "Timestamped note", kind="comment", author="human")
        html = dashboard.render_comments_panel(tid, self.conn)
        self.assertIn("timestamp-absolute", html)
        self.assertNotIn('data-local-time="true"', html)
        self.assertNotIn("UTC ", html)

    def test_kind_css_classes(self):
        tid = db.add_task(self.conn, "CSS test")
        db.add_comment(self.conn, tid, "ok", kind="approval")
        db.add_comment(self.conn, tid, "no", kind="rejection")
        db.add_comment(self.conn, tid, "msg", kind="commit-message")
        html = dashboard.render_comments_panel(tid, self.conn)
        self.assertIn("comment-approval", html)
        self.assertIn("comment-rejection", html)
        self.assertIn("comment-commit-msg", html)

    def test_review_rounds_render_as_one_based_in_comment_meta_and_body(self):
        tid = db.add_task(self.conn, "Review comment display")
        db.add_comment(
            self.conn,
            tid,
            "Starting commit-review round 0 with reviewer: gemini.",
            kind="comment",
            author="orchestrator",
            review_round=0,
        )
        html = dashboard.render_comments_panel(tid, self.conn)
        self.assertIn("Round 1", html)
        self.assertIn("Starting commit-review round 1 with reviewer: gemini.", html)
        self.assertNotIn("Starting commit-review round 0 with reviewer: gemini.", html)


class TestRunLogPanel(unittest.TestCase):
    """Tests for render_run_log_panel."""

    def setUp(self):
        self.conn, self.db_path = _fresh_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_empty(self):
        tid = db.add_task(self.conn, "Empty log")
        html = dashboard.render_run_log_panel(tid, self.conn)
        self.assertIn("No run log entries", html)

    def test_entries_shown(self):
        tid = db.add_task(self.conn, "Log task")
        db.add_run_log(self.conn, tid, "Starting build", author="claude")
        db.add_run_log(self.conn, tid, "Build done", author="claude")
        html = dashboard.render_run_log_panel(tid, self.conn)
        self.assertIn("Starting build", html)
        self.assertIn("Build done", html)
        self.assertIn("claude", html)
        self.assertIn("timestamp-absolute", html)
        self.assertIn('data-stick-to-bottom="true"', html)
        self.assertNotIn('data-local-time="true"', html)
        self.assertNotIn("UTC ", html)

    def test_entries_render_in_chronological_order(self):
        tid = db.add_task(self.conn, "Log task")
        db.add_run_log(self.conn, tid, "Starting build", author="claude")
        db.add_run_log(self.conn, tid, "Build done", author="claude")

        html = dashboard.render_run_log_panel(tid, self.conn)

        self.assertLess(html.index("Starting build"), html.index("Build done"))

    def test_picked_up_entries_use_blue_lifecycle_class(self):
        tid = db.add_task(self.conn, "Log task")
        db.add_run_log(self.conn, tid, "Picked up: 'Log task' (step=commit-make)", author="orchestrator")

        html = dashboard.render_run_log_panel(tid, self.conn)

        self.assertIn('class="log-row log-row-picked-up"', html)

    def test_error_warning_and_done_log_classes_are_preserved(self):
        tid = db.add_task(self.conn, "Log task")
        db.add_run_log(self.conn, tid, "Task done", author="orchestrator")
        db.add_run_log(self.conn, tid, "Warning: retrying", author="orchestrator")
        db.add_run_log(self.conn, tid, "Picked up after error", author="orchestrator")

        html = dashboard.render_run_log_panel(tid, self.conn)

        self.assertIn('class="log-row log-row-done"', html)
        self.assertIn('class="log-row log-row-warning"', html)
        self.assertIn('class="log-row log-row-error"', html)
        self.assertNotIn('class="log-row log-row-picked-up"', html)


class TestCurrentTaskCardRunLog(unittest.TestCase):
    """Tests for render_current_task_card run-log rendering."""

    def setUp(self):
        self.conn, self.db_path = _fresh_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_run_log_snippet_uses_chronological_order_and_bottom_stick(self):
        tid = db.add_task(self.conn, "Active task")
        db.add_run_log(self.conn, tid, "Starting build", author="claude")
        db.add_run_log(self.conn, tid, "Build done", author="claude")
        runtime = {"current_task_id": tid, "current_step": "commit-make"}

        html = dashboard.render_current_task_card(runtime, self.conn)

        self.assertIn("Recent Run Log</h3>", html)
        self.assertNotIn("Most Recent First", html)
        self.assertIn('data-stick-to-bottom="true"', html)
        self.assertLess(html.index("Starting build"), html.index("Build done"))

    def test_run_log_snippet_highlights_picked_up_lines(self):
        tid = db.add_task(self.conn, "Active task")
        db.add_run_log(self.conn, tid, "Picked up: 'Active task' (step=commit-make)", author="orchestrator")
        runtime = {"current_task_id": tid, "current_step": "commit-make"}

        html = dashboard.render_current_task_card(runtime, self.conn)

        self.assertIn('class="log-row log-row-picked-up"', html)


class TestGlobalRunLogPanel(unittest.TestCase):
    """Tests for render_global_run_log_panel."""

    def setUp(self):
        self.conn, self.db_path = _fresh_conn()
        self.log_path = db.get_orchestrator_log_path(self.db_path)
        if self.log_path.exists():
            self.log_path.unlink()

    def tearDown(self):
        self.conn.close()
        if self.log_path.exists():
            self.log_path.unlink()
        os.unlink(self.db_path)

    def test_empty_when_log_missing(self):
        html = dashboard.render_global_run_log_panel(self.conn)
        self.assertIn("Orchestrator Output", html)
        self.assertIn("No orchestrator output yet", html)

    def test_reads_orchestrator_log_tail_from_repo_runtime_file(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text("[12:00:00] first\n[12:00:01] second\n", encoding="utf-8")

        html = dashboard.render_global_run_log_panel(self.conn)

        self.assertIn("Orchestrator Output", html)
        self.assertIn("[12:00:00] first", html)
        self.assertIn("[12:00:01] second", html)
        self.assertIn('data-stick-to-bottom="true"', html)
        self.assertNotIn("No orchestrator output yet", html)

    def test_shows_only_lines_from_most_recent_orchestrator_start(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text(
            "[11:59:58] older line\n"
            "[11:59:59] Kanban Orchestra started. Polling for ready tasks...\n"
            "[12:00:00] old run output\n"
            "[12:00:10] Kanban Orchestra started. Polling for ready tasks...\n"
            "[12:00:11] current run output\n",
            encoding="utf-8",
        )

        html = dashboard.render_global_run_log_panel(self.conn)

        self.assertNotIn("[11:59:58] older line", html)
        self.assertNotIn("[12:00:00] old run output", html)
        self.assertIn("[12:00:10] Kanban Orchestra started. Polling for ready tasks...", html)
        self.assertIn("[12:00:11] current run output", html)

    def test_global_log_highlights_picked_up_lines(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text(
            "[12:00:10] Kanban Orchestra started. Polling for ready tasks...\n"
            "[12:00:11] Picked up: 'Allowed task' (step=commit-make)\n",
            encoding="utf-8",
        )

        html = dashboard.render_global_run_log_panel(self.conn)

        self.assertIn('class="log-output-line log-row-picked-up"', html)


class TestTaskRuntimePanel(unittest.TestCase):
    """Tests for render_task_runtime_panel."""

    def test_inactive_task(self):
        task = {"id": 5, "status": "ready", "next_step": "commit-make"}
        runtime = {"current_task_id": 9, "status": "running"}
        html = dashboard.render_task_runtime_panel(task, runtime)
        self.assertIn("not the active task", html)

    def test_no_runtime(self):
        task = {"id": 5, "status": "none", "next_step": "commit-make"}
        html = dashboard.render_task_runtime_panel(task, None)
        self.assertIn("not the active task", html)

    def test_active_task_details(self):
        utc_dt = datetime.now(timezone.utc)
        ts = utc_dt.strftime("%Y-%m-%d %H:%M:%S")
        task = {"id": 5, "status": "running", "next_step": "commit-review"}
        runtime = {
            "current_task_id": 5,
            "status": "running",
            "current_step": "commit-review",
            "status_message": "Review round 0: reviewing",
            "last_heartbeat_at": ts,
        }
        html = dashboard.render_task_runtime_panel(task, runtime)
        self.assertIn("commit-review", html)
        self.assertIn("in progress", html)
        self.assertIn("Review round 1: reviewing", html)
        self.assertNotIn("Review round 0: reviewing", html)
        self.assertIn('data-relative-time="true"', html)
        self.assertNotIn('data-local-time="true"', html)


class TestTaskDetailLiveHeader(unittest.TestCase):
    """Tests that task detail keeps the header live over SSE."""

    def setUp(self):
        self.conn, self.db_path = _fresh_conn()
        os.environ["KANBAN_DB"] = self.db_path
        self.tid = db.add_task(self.conn, "Live header task", branch="feat-live", coder_agent="claude")
        db.update_task(self.conn, self.tid, status="running", next_step="commit-review", review_round=1)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("KANBAN_DB", None)

    def test_task_detail_page_listens_for_task_header_event(self):
        from fastapi.testclient import TestClient

        client = TestClient(dashboard.app)
        resp = client.get(f"/task/{self.tid}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('task-header-wrap', resp.text)
        self.assertNotIn('task-edit-wrap', resp.text)
        self.assertIn('task-title-editor-', resp.text)
        self.assertIn('task_header', resp.text)
        self.assertIn("headerHasOpenEditor", resp.text)
        self.assertIn("updateRelativeTimes", resp.text)
        note = f"Times shown in server local time ({dashboard._server_tz_label()})."
        self.assertIn(note, resp.text)
        self.assertLess(resp.text.index('task-log-wrap'), resp.text.index(note))

    def test_task_events_stream_emits_task_header(self):
        response = dashboard.events_task(self.tid)

        async def read_first_chunk():
            return await anext(response.body_iterator)

        first_chunk = asyncio.run(read_first_chunk())
        self.assertIn("event: task_header", first_chunk)
        self.assertIn("Live header task", first_chunk)


class TestTaskEditingRoutes(unittest.TestCase):
    """Tests for task detail edit flow."""

    def setUp(self):
        self.conn, self.db_path = _fresh_conn()
        os.environ["KANBAN_DB"] = self.db_path
        self.tid = db.add_task(
            self.conn,
            "Editable task",
            description="**Bold**\n\n- item",
            branch="feat-edit",
            coder_agent="codex",
        )

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("KANBAN_DB", None)

    def test_task_detail_page_shows_inline_editors(self):
        from fastapi.testclient import TestClient

        client = TestClient(dashboard.app)
        resp = client.get(f"/task/{self.tid}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text.count(f'action="/task/{self.tid}/edit"'), 2)
        self.assertIn("**Bold**\n\n- item", resp.text)
        self.assertIn("<strong>Bold</strong>", resp.text)
        self.assertIn("Cancel", resp.text)
        self.assertNotIn("Edit Task Text", resp.text)

    def test_post_edit_updates_title_and_description(self):
        from fastapi.testclient import TestClient

        client = TestClient(dashboard.app)
        resp = client.post(
            f"/task/{self.tid}/edit",
            data={"title": "Updated title", "description": "## Heading\n\nNew text"},
            headers={"origin": "http://127.0.0.1:8427"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], f"/task/{self.tid}")

        fresh = db.connect(self.db_path)
        try:
            task = db.get_task(fresh, self.tid)
        finally:
            fresh.close()
        self.assertEqual(task["title"], "Updated title")
        self.assertEqual(task["description"], "## Heading\n\nNew text")

    def test_post_edit_blank_description_clears_description(self):
        from fastapi.testclient import TestClient

        client = TestClient(dashboard.app)
        resp = client.post(
            f"/task/{self.tid}/edit",
            data={"title": "Editable task", "description": ""},
            headers={"origin": "http://127.0.0.1:8427"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)

        fresh = db.connect(self.db_path)
        try:
            task = db.get_task(fresh, self.tid)
        finally:
            fresh.close()
        self.assertIsNone(task["description"])

    def test_post_edit_rejects_blank_title(self):
        from fastapi.testclient import TestClient

        client = TestClient(dashboard.app)
        resp = client.post(
            f"/task/{self.tid}/edit",
            data={"title": "   ", "description": "keep this"},
            headers={"origin": "http://127.0.0.1:8427"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Title is required.", resp.text)
        self.assertIn("keep this", resp.text)
        self.assertNotIn('task-edit-wrap', resp.text)


    def test_post_edit_rejects_cross_origin(self):
        from fastapi.testclient import TestClient

        client = TestClient(dashboard.app)
        resp = client.post(
            f"/task/{self.tid}/edit",
            data={"title": "evil", "description": "injected"},
            headers={"origin": "http://evil.example.com"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_post_edit_rejects_missing_origin(self):
        from fastapi.testclient import TestClient

        client = TestClient(dashboard.app)
        resp = client.post(
            f"/task/{self.tid}/edit",
            data={"title": "evil", "description": "injected"},
        )
        self.assertEqual(resp.status_code, 403)


class TestOverviewPage(unittest.TestCase):
    """Tests for overview page layout."""

    def setUp(self):
        self.conn, self.db_path = _fresh_conn()
        os.environ["KANBAN_DB"] = self.db_path

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("KANBAN_DB", None)

    def test_overview_shows_running_directory(self):
        from fastapi.testclient import TestClient

        client = TestClient(dashboard.app)
        with patch("dashboard.db.get_repo_root", return_value=Path.home().resolve() / "work-repo"):
            resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Running against <code>~/work-repo</code>", resp.text)
        self.assertNotIn("Overview is read-only", resp.text)

    def test_overview_timezone_note_moves_to_bottom(self):
        from fastapi.testclient import TestClient

        client = TestClient(dashboard.app)
        resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        note = f"Times shown in server local time ({dashboard._server_tz_label()})."
        self.assertIn(note, resp.text)
        self.assertLess(resp.text.index('done-wrap'), resp.text.index(note))


class TestFindFreePort(unittest.TestCase):
    """Tests for _find_free_port: port-probe logic and error propagation."""

    def _busy_mock(self):
        """Return a mock socket whose bind() raises EADDRINUSE."""
        m = MagicMock()
        m.bind.side_effect = OSError(errno_mod.EADDRINUSE, "address already in use")
        m.__enter__ = lambda s: s
        m.__exit__ = MagicMock(return_value=False)
        return m

    def _free_mock(self):
        """Return a mock socket whose bind() succeeds."""
        m = MagicMock()
        m.__enter__ = lambda s: s
        m.__exit__ = MagicMock(return_value=False)
        return m

    def test_returns_preferred_port_when_free(self):
        """When the preferred port is available, return it directly."""
        mock_sock = self._free_mock()
        with patch("dashboard._socket.socket", return_value=mock_sock):
            port = dashboard._find_free_port("127.0.0.1", 9000)
        self.assertEqual(port, 9000)
        mock_sock.bind.assert_called_once_with(("127.0.0.1", 9000))

    def test_falls_back_on_eaddrinuse(self):
        """When preferred port is busy, return the next port."""
        busy = self._busy_mock()
        free = self._free_mock()
        with patch("dashboard._socket.socket", side_effect=[busy, free]):
            port = dashboard._find_free_port("127.0.0.1", 9000)
        self.assertEqual(port, 9001)
        free.bind.assert_called_once_with(("127.0.0.1", 9001))

    def test_increments_through_multiple_busy_ports(self):
        """Keeps incrementing port until a free one is found."""
        busy1, busy2, free = self._busy_mock(), self._busy_mock(), self._free_mock()
        with patch("dashboard._socket.socket", side_effect=[busy1, busy2, free]):
            port = dashboard._find_free_port("127.0.0.1", 9000)
        self.assertEqual(port, 9002)

    def test_non_eaddrinuse_error_propagates(self):
        """Non-EADDRINUSE bind errors are not swallowed."""
        mock_sock = self._free_mock()
        mock_sock.bind.side_effect = OSError(errno_mod.EACCES, "Permission denied")
        with patch("dashboard._socket.socket", return_value=mock_sock):
            with self.assertRaises(OSError) as ctx:
                dashboard._find_free_port("127.0.0.1", 80)
        self.assertEqual(ctx.exception.errno, errno_mod.EACCES)


class TestRunDashboard(unittest.TestCase):
    """Tests for _run_dashboard: startup-flow wiring and final-port reporting."""

    def _make_mock_uvicorn(self):
        """Return a minimal mock that stands in for the uvicorn module."""
        uv = MagicMock()
        uv.run = MagicMock()
        return uv

    def test_preferred_port_used_when_free(self):
        """When the preferred port is free, uvicorn is started on that port."""
        uv = self._make_mock_uvicorn()

        with patch("dashboard._find_free_port", return_value=9000) as find_mock, \
             patch("builtins.print") as mock_print:
            dashboard._run_dashboard("127.0.0.1", 9000, _uvicorn=uv)

        find_mock.assert_called_once_with("127.0.0.1", 9000)
        uv.run.assert_called_once()
        _, kwargs = uv.run.call_args
        self.assertEqual(kwargs["port"], 9000)
        self.assertNotIn("fd", kwargs)
        # No fallback message should be printed
        for call in mock_print.call_args_list:
            args = call[0]
            self.assertFalse(
                any("in use" in str(a) for a in args),
                msg=f"Unexpected fallback message printed: {call}",
            )

    def test_fallback_port_passed_to_uvicorn(self):
        """When _find_free_port returns a different port, uvicorn uses it."""
        uv = self._make_mock_uvicorn()

        with patch("dashboard._find_free_port", return_value=9001) as find_mock, \
             patch("builtins.print") as mock_print:
            dashboard._run_dashboard("127.0.0.1", 9000, _uvicorn=uv)

        find_mock.assert_called_once_with("127.0.0.1", 9000)
        _, kwargs = uv.run.call_args
        # Uvicorn must receive the actual free port, not the preferred one
        self.assertEqual(kwargs["port"], 9001)
        self.assertNotIn("fd", kwargs)
        # User-facing message must mention both ports
        printed = " ".join(str(a) for call in mock_print.call_args_list for a in call[0])
        self.assertIn("9001", printed)
        self.assertIn("9000", printed)

    def test_fallback_message_contains_actual_port(self):
        """The printed message reports the final bound port, not just the preferred one."""
        uv = self._make_mock_uvicorn()

        with patch("dashboard._find_free_port", return_value=8430), \
             patch("builtins.print") as mock_print:
            dashboard._run_dashboard("127.0.0.1", 8427, _uvicorn=uv)

        printed = " ".join(str(a) for call in mock_print.call_args_list for a in call[0])
        self.assertIn("8430", printed)   # actual port present
        self.assertIn("8427", printed)   # preferred port present (for context)


if __name__ == "__main__":
    unittest.main()
