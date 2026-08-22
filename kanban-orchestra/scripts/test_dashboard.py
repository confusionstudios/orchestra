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

    def test_format_duration_hhmmss(self):
        self.assertEqual(dashboard._format_duration_hhmmss(3661), "01:01:01")

    def test_format_done_recency_recent_uses_relative_words(self):
        ts = (datetime.now(timezone.utc) - timedelta(hours=2, minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(dashboard._format_done_recency(ts), "2 hours ago")

    def test_format_done_recency_just_under_cutoff_stays_relative(self):
        ts = (
            datetime.now(timezone.utc) - dashboard.DONE_RECENCY_CUTOFF + timedelta(hours=12)
        ).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(dashboard._format_done_recency(ts), "6 days ago")

    def test_format_done_recency_at_cutoff_uses_calendar_date(self):
        done_at = datetime.now(timezone.utc) - dashboard.DONE_RECENCY_CUTOFF
        ts = done_at.strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(dashboard._format_done_recency(ts), done_at.strftime("%Y-%m-%d"))

    def test_format_done_recency_older_uses_calendar_date(self):
        self.assertEqual(
            dashboard._format_done_recency("2026-01-15 09:30:00"),
            "2026-01-15",
        )

    def test_format_done_recency_missing_or_invalid_is_empty(self):
        self.assertEqual(dashboard._format_done_recency(None), "")
        self.assertEqual(dashboard._format_done_recency(""), "")
        self.assertEqual(dashboard._format_done_recency("not-a-timestamp"), "")

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
                self.assertIsNone(payload["remote_url"])

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
            dashboard._display_review_round_text("Round 0: antigravity reviewing; Review round 2 approved."),
            "Round 1: antigravity reviewing; Review round 3 approved.",
        )

    def test_display_review_round_count_converts_stored_zero_based_values(self):
        self.assertEqual(dashboard._display_review_round_count(0), 1)
        self.assertEqual(dashboard._display_review_round_count(2), 3)
        self.assertIsNone(dashboard._display_review_round_count(None))
        self.assertIsNone(dashboard._display_review_round_count("legacy"))


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

    def test_page_shell_shows_running_directory_in_nav(self):
        with patch("dashboard.db.get_repo_root", return_value=Path.home().resolve() / "work-repo"):
            html = dashboard._page_shell("Title", "<p>Body</p>")

        self.assertIn('<a class="nav-title" href="/">Kanban Orchestra</a>', html)
        self.assertIn('<span class="nav-repo-path" title="~/work-repo">~/work-repo</span>', html)
        self.assertNotIn("Running against", html)

    def test_palette_is_allowlisted_and_dark_safe_for_normal_text(self):
        self.assertEqual(dashboard._validated_accent("violet"), "violet")
        self.assertEqual(dashboard._validated_accent("green"), dashboard.DEFAULT_ACCENT)
        self.assertEqual(dashboard._validated_accent("#ffffff"), dashboard.DEFAULT_ACCENT)
        self.assertEqual(dashboard._validated_accent(None), dashboard.DEFAULT_ACCENT)
        self.assertGreater(len(dashboard.ACCENT_PALETTE), 1)

        for name, accent in dashboard.ACCENT_PALETTE.items():
            with self.subTest(accent=name):
                self.assertGreaterEqual(dashboard._contrast_ratio(accent["color"], "#000000"), 4.5)
                self.assertGreaterEqual(dashboard._contrast_ratio(accent["color"], "#0c0c0c"), 4.5)

    def test_accent_identities_are_stable_hashed_and_distinct(self):
        repo_a = Path.home() / "accent-repo-a"
        repo_b = Path.home() / "accent-repo-b"
        identity_a = dashboard.repo_accent_identity(repo_a)
        identity_b = dashboard.repo_accent_identity(repo_b)
        fleet_identity = dashboard.fleet_accent_identity()

        self.assertEqual(identity_a, dashboard.repo_accent_identity(str(repo_a)))
        self.assertNotEqual(identity_a, identity_b)
        self.assertNotEqual(identity_a, fleet_identity)
        self.assertNotEqual(identity_b, fleet_identity)
        self.assertEqual(fleet_identity, dashboard.fleet_accent_identity())
        self.assertRegex(identity_a, r"^[0-9a-f]{64}$")
        self.assertRegex(fleet_identity, r"^[0-9a-f]{64}$")

        cookie_a = dashboard.accent_cookie_name(identity_a)
        self.assertTrue(cookie_a.startswith(dashboard.ACCENT_COOKIE_PREFIX))
        self.assertNotIn(str(repo_a), cookie_a)
        self.assertNotIn(str(repo_a.resolve()), cookie_a)
        self.assertNotIn("/", cookie_a)
        self.assertNotIn(":", cookie_a)
        with self.assertRaises(ValueError):
            dashboard.accent_cookie_name(str(repo_a))
        with self.assertRaises(ValueError):
            dashboard.accent_cookie_name("orchestra_accent")

    def test_page_shell_applies_allowlisted_cookie_before_styles_and_renders_accessible_picker(self):
        html = dashboard._page_shell("Title", "<p>Body</p>")
        cookie = dashboard.accent_cookie_name(dashboard.repo_accent_identity())

        self.assertLess(html.index(cookie), html.index("<style>"))
        self.assertIn('Object.prototype.hasOwnProperty.call(palette, requested)', html)
        self.assertIn(f'? requested : "{dashboard.DEFAULT_ACCENT}"', html)
        self.assertIn('<label for="orchestra-accent-picker">Accent</label>', html)
        self.assertIn('<select id="orchestra-accent-picker" name="accent">', html)
        self.assertNotIn('type="color"', html)
        self.assertNotIn("orchestra_accent=", html)
        for name, accent in dashboard.ACCENT_PALETTE.items():
            self.assertIn(f'<option value="{name}">{accent["label"]}</option>', html)

    def test_picker_persists_host_only_cross_port_cookie(self):
        html = dashboard._page_shell("Title", "<p>Body</p>")
        cookie = dashboard.accent_cookie_name(dashboard.repo_accent_identity())

        self.assertIn(f"{cookie}=", html)
        self.assertIn("; Path=/; Max-Age=31536000; SameSite=Lax", html)
        self.assertNotIn("; Domain=", html)
        self.assertNotIn("localStorage", html)
        self.assertIn("window.location.reload()", html)

    def test_page_shell_scopes_accent_cookie_to_repo_identity(self):
        repo_a = Path.home() / "accent-scope-a"
        repo_b = Path.home() / "accent-scope-b"
        cookie_a = dashboard.accent_cookie_name(dashboard.repo_accent_identity(repo_a))
        cookie_b = dashboard.accent_cookie_name(dashboard.repo_accent_identity(repo_b))
        fleet_cookie = dashboard.accent_cookie_name(dashboard.fleet_accent_identity())
        identity_a = {
            "repo_root": str(repo_a.resolve()),
            "repo_label": repo_a.name,
            "db_path": str(repo_a / "kanban-orchestra.db"),
            "runtime_root": str(repo_a / ".kanban-orchestra"),
            "lock_path": str(repo_a / "kanban-orchestra.lock"),
        }
        identity_b = {
            **identity_a,
            "repo_root": str(repo_b.resolve()),
            "repo_label": repo_b.name,
            "db_path": str(repo_b / "kanban-orchestra.db"),
            "runtime_root": str(repo_b / ".kanban-orchestra"),
            "lock_path": str(repo_b / "kanban-orchestra.lock"),
        }

        with patch.object(dashboard.db, "get_instance_identity", return_value=identity_a):
            html_a = dashboard._page_shell("Title", "<p>Body</p>")
        with patch.object(dashboard.db, "get_instance_identity", return_value=identity_b):
            html_b = dashboard._page_shell("Title", "<p>Body</p>")

        self.assertNotEqual(cookie_a, cookie_b)
        self.assertIn(f"{cookie_a}=", html_a)
        self.assertNotIn(f"{cookie_b}=", html_a)
        self.assertIn(f"{cookie_b}=", html_b)
        self.assertNotIn(f"{cookie_a}=", html_b)
        self.assertNotIn(f"{fleet_cookie}=", html_a)
        self.assertNotIn("orchestra_accent=", html_a)
        self.assertNotIn(str(repo_a.resolve()), cookie_a)

    def test_generic_accent_and_semantic_status_colors_are_separate(self):
        self.assertIn("--accent-rgb: 0 204 68", dashboard.COMMON_CSS)
        self.assertIn("rgb(var(--accent-rgb) / 0.28)", dashboard.COMMON_CSS)
        self.assertIn(".badge-ready", dashboard.COMMON_CSS)
        self.assertIn("color: var(--green)", dashboard.COMMON_CSS)
        self.assertIn(".badge-running", dashboard.COMMON_CSS)
        self.assertIn("color: var(--blue)", dashboard.COMMON_CSS)
        self.assertIn(".badge-blocked", dashboard.COMMON_CSS)
        self.assertIn("color: var(--red)", dashboard.COMMON_CSS)
        self.assertIn(".badge-pending-subtasks", dashboard.COMMON_CSS)
        self.assertIn("color: var(--orange)", dashboard.COMMON_CSS)


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

    def test_runtime_lifecycle_states_are_not_mislabeled_stale(self):
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
            "status_message": "Round 0: antigravity reviewing",
            "current_task_id": 3,
            "current_step": "commit-review",
            "current_branch": "feat-z",
            "review_round": 0,
        }
        html = dashboard.render_health_card(runtime)
        self.assertIn("in progress", html)
        self.assertIn("Review round:</strong> 1", html)
        self.assertIn("Round 1: antigravity reviewing", html)
        self.assertNotIn("Round 0: antigravity reviewing", html)

    def test_idle_with_ready_work_blocked_by_blocked_gate_displays_blocked(self):
        conn, db_path = _fresh_conn()
        try:
            blocked_id = db.add_task(conn, "Blocked task", branch="feat-blocked")
            ready_id = db.add_task(conn, "Ready task", branch="feat-ready")
            db.update_task(conn, blocked_id, status="blocked")
            db.update_task(conn, ready_id, status="ready")
            runtime = {
                "status": "idle",
                "last_heartbeat_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "status_message": "Waiting for ready tasks",
                "current_task_id": None,
            }

            html = dashboard.render_health_card(runtime, conn)

            self.assertIn("badge-blocked", html)
            self.assertIn(">blocked<", html)
            self.assertIn("Waiting for ready tasks", html)
        finally:
            conn.close()
            os.unlink(db_path)


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
            reviewer_agent="antigravity",
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
        self.assertIn("Reviewer:</strong> antigravity", html)
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
        self.assertIn("Type:</strong> supertask plan in progress", html)
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
        t2 = db.add_task(self.conn, "Task Beta", branch="feat-b", coder_agent="antigravity")
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

    def test_child_ready_task_waiting_for_final_review_is_gated(self):
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
        self.assertIn("waiting for final supertask review", html)
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
        t2 = db.add_task(self.conn, "Idea Beta", branch="feat-b", coder_agent="antigravity")
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
        db.add_comment(self.conn, tid, "LGTM", kind="approval", author="antigravity", review_round=0)
        db.add_comment(self.conn, tid, "Fix this", kind="rejection", author="opus", review_round=0)
        db.add_comment(self.conn, tid, "Still failing", kind="rejection", author="opus", review_round=1)
        html = dashboard.render_recently_done(self.conn)
        self.assertIn("Done task", html)
        self.assertIn("22222222", html)
        self.assertIn("claude", html)
        self.assertIn("antigravity", html)
        self.assertIn('class="task-record-reviewer">(reviewed by antigravity)</span>', html)
        self.assertNotIn("task-record-rejections", html)
        self.assertNotIn("2 rejections", html)
        self.assertNotIn("Configured Reviewer", html)
        self.assertNotIn("Approver", html)

    def test_recently_done_shows_first_review_as_one_round(self):
        tid = db.add_task(self.conn, "First-round approval", branch="feat-round-one")
        db.update_task(
            self.conn,
            tid,
            status="done",
            review_round=0,
            coder_agent="grok",
            reviewer_agent="codex",
        )
        db.add_comment(
            self.conn, tid, "LGTM", kind="approval", author="codex", review_round=0
        )

        html = dashboard.render_recently_done(self.conn)

        self.assertIn(
            '<span class="task-record-meta-item task-record-review-rounds">'
            '<span class="task-record-label">review rounds</span> 1</span>',
            html,
        )
        self.assertNotIn("1 review round", html)
        self.assertNotIn("0 review round", html)
        self.assertNotIn("task-record-rejections", html)
        self.assertNotIn("rejection", html)

    def test_recently_done_shows_multiple_review_rounds_without_rejection_count(self):
        tid = db.add_task(self.conn, "Multi-round task", branch="feat-round-multi")
        db.update_task(
            self.conn,
            tid,
            status="done",
            review_round=2,
            coder_agent="grok",
            reviewer_agent="codex",
        )
        db.add_comment(
            self.conn, tid, "Fix this", kind="rejection", author="codex", review_round=0
        )
        db.add_comment(
            self.conn, tid, "Still failing", kind="rejection", author="codex", review_round=1
        )
        db.add_comment(
            self.conn, tid, "LGTM", kind="approval", author="codex", review_round=2
        )

        html = dashboard.render_recently_done(self.conn)

        self.assertIn(
            '<span class="task-record-meta-item task-record-review-rounds">'
            '<span class="task-record-label">review rounds</span> 3</span>',
            html,
        )
        self.assertNotIn("3 review rounds", html)
        self.assertNotIn("2 review rounds", html)
        self.assertNotIn("task-record-rejections", html)
        self.assertNotIn("2 rejections", html)
        self.assertNotIn("3 rejections", html)
        self.assertNotIn("rejection", html)

    def test_recently_done_omits_review_rounds_when_not_applicable(self):
        other_id = db.add_task(
            self.conn, "Legacy other task", branch="feat-other", kind="other"
        )
        db.update_task(self.conn, other_id, status="done", review_round=None)
        pr_id = db.add_task(
            self.conn, "Legacy pull request", branch="feat-pr", kind="pull_request"
        )
        db.update_task(self.conn, pr_id, status="done")
        self.conn.execute("UPDATE tasks SET review_round = NULL WHERE id = ?", (pr_id,))
        self.conn.commit()

        html = dashboard.render_recently_done(self.conn)

        self.assertIn("Legacy other task", html)
        self.assertIn("Legacy pull request", html)
        self.assertNotIn("task-record-review-rounds", html)
        self.assertNotIn("task-record-label\">review rounds", html)
        self.assertNotIn("review round", html)
        self.assertNotIn("task-record-rejections", html)
        self.assertNotIn("rejection", html)

    def test_recently_done_omits_review_rounds_for_default_round_without_decision(self):
        tid = db.add_task(self.conn, "Skipped review task", branch="feat-skip")
        db.update_task(self.conn, tid, status="done")
        task = db.get_task(self.conn, tid)

        html = dashboard.render_recently_done(self.conn)

        self.assertEqual(task["review_round"], 0)
        self.assertIn("Skipped review task", html)
        self.assertNotIn("task-record-review-rounds", html)
        self.assertNotIn("task-record-label\">review rounds", html)
        self.assertNotIn("review round", html)
        self.assertNotIn("task-record-rejections", html)
        self.assertNotIn("rejection", html)

    def test_recently_done_shows_elapsed_runtime(self):
        tid = db.add_task(self.conn, "Runtime task", branch="feat-runtime")
        db.update_task(self.conn, tid, status="done")
        self.conn.execute(
            "UPDATE tasks SET last_ready_at = ?, done_at = ? WHERE id = ?",
            ("2026-05-31 10:00:00", "2026-05-31 12:03:04", tid),
        )
        self.conn.commit()

        html = dashboard.render_recently_done(self.conn)

        self.assertIn("task-record-runtime", html)
        self.assertIn("02:03:04", html)

    def test_recently_done_elapsed_runtime_uses_latest_ready_time(self):
        tid = db.add_task(self.conn, "Requeued runtime task", branch="feat-runtime")
        db.update_task(self.conn, tid, status="ready")
        self.conn.execute(
            "UPDATE tasks SET ready_at = ?, last_ready_at = ? WHERE id = ?",
            ("2026-05-31 08:00:00", "2026-05-31 08:00:00", tid),
        )
        self.conn.commit()
        db.update_task(self.conn, tid, status="running")
        db.update_task(self.conn, tid, status="ready")
        self.conn.execute(
            "UPDATE tasks SET ready_at = ?, last_ready_at = ? WHERE id = ?",
            ("2026-05-31 10:00:00", "2026-05-31 10:00:00", tid),
        )
        self.conn.commit()
        db.update_task(self.conn, tid, status="done")
        self.conn.execute(
            "UPDATE tasks SET done_at = ? WHERE id = ?",
            ("2026-05-31 10:10:05", tid),
        )
        self.conn.commit()

        html = dashboard.render_recently_done(self.conn)

        self.assertIn("00:10:05", html)
        self.assertNotIn("02:10:05", html)

    def test_recently_done_elapsed_runtime_missing_timestamp_fallback(self):
        tid = db.add_task(self.conn, "Missing runtime task", branch="feat-runtime")
        db.update_task(self.conn, tid, status="done")
        self.conn.execute(
            "UPDATE tasks SET last_ready_at = NULL WHERE id = ?",
            (tid,),
        )
        self.conn.commit()

        html = dashboard.render_recently_done(self.conn)

        self.assertIn("Missing runtime task", html)
        self.assertNotIn("task-record-runtime", html)
        self.assertNotIn(">unknown<", html)

    def test_recently_done_shows_finished_after_runtime_from_done_at(self):
        tid = db.add_task(self.conn, "Finished recency task", branch="feat-finished")
        db.update_task(self.conn, tid, status="done")
        done_at = (datetime.now(timezone.utc) - timedelta(hours=2, minutes=5)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        self.conn.execute(
            "UPDATE tasks SET last_ready_at = ?, done_at = ?, updated_at = ? WHERE id = ?",
            ("2026-01-01 00:00:00", done_at, "2026-01-01 00:00:00", tid),
        )
        self.conn.commit()

        html = dashboard.render_recently_done(self.conn)

        runtime_at = html.find("task-record-runtime")
        finished_at = html.find("task-record-finished")
        self.assertNotEqual(runtime_at, -1)
        self.assertNotEqual(finished_at, -1)
        self.assertLess(runtime_at, finished_at)
        self.assertIn("2 hours ago", html)
        self.assertNotIn("2026-01-01", html)

    def test_recently_done_finished_missing_timestamp_is_omitted(self):
        tid = db.add_task(self.conn, "Missing finished task", branch="feat-finished")
        db.update_task(self.conn, tid, status="done")
        self.conn.execute(
            "UPDATE tasks SET last_ready_at = ?, done_at = NULL WHERE id = ?",
            ("2026-05-31 10:00:00", tid),
        )
        self.conn.commit()

        html = dashboard.render_recently_done(self.conn)

        self.assertIn("Missing finished task", html)
        self.assertNotIn("task-record-finished", html)
        self.assertNotIn(">unknown<", html)

    def test_recently_done_reviewer_falls_back_to_configured_reviewer(self):
        tid = db.add_task(self.conn, "Done task", branch="feat-done", reviewer_agent="opus")
        db.update_task(self.conn, tid, status="done")

        html = dashboard.render_recently_done(self.conn)

        self.assertIn("Done task", html)
        self.assertIn("opus", html)
        self.assertNotIn("task-record-rejections", html)

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


class TestTaskRecordLists(unittest.TestCase):
    """Focused coverage for compact responsive task-record list markup."""

    def setUp(self):
        self.conn, self.db_path = _fresh_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_ready_queue_uses_wrapping_task_records(self):
        long_agent = "cursor:provider/model-with-a-very-long-spec-name"
        tid = db.add_task(
            self.conn,
            "Ready record",
            branch="feat-ready",
            coder_agent=long_agent,
            reviewer_agent="codex",
            skips=["commit-plan"],
        )
        db.update_task(self.conn, tid, status="ready", next_step="commit-make")

        html = dashboard.render_ready_queue(self.conn)

        self.assertIn('class="task-record-list"', html)
        self.assertIn('class="task-record"', html)
        self.assertIn(f'href="/task/{tid}"', html)
        self.assertIn('class="task-record-title">Ready record</span>', html)
        self.assertIn(f'class="task-record-coder">{long_agent}</span>', html)
        self.assertIn("(reviewed by codex)", html)
        self.assertIn("task-record-step", html)
        self.assertIn("commit-make", html)
        self.assertIn("commit-plan", html)
        self.assertNotIn("<table>", html)
        self.assertIn("task-record-meta", dashboard.COMMON_CSS)
        self.assertIn("overflow-wrap: anywhere", dashboard.COMMON_CSS)

    def test_icebox_and_blocked_use_task_records(self):
        ice_id = db.add_task(
            self.conn,
            "Parked idea",
            branch="feat-ice",
            coder_agent="antigravity",
            reviewer_agent="opus",
        )
        blocked_id = db.add_task(
            self.conn,
            "Stuck work",
            branch="feat-block",
            coder_agent="claude",
            reviewer_agent="codex",
            skips=["commit-review"],
        )
        db.update_task(self.conn, blocked_id, status="blocked")
        db.add_comment(self.conn, blocked_id, "Waiting on dependency")

        ice_html = dashboard.render_icebox(self.conn)
        blocked_html = dashboard.render_blocked_tasks(self.conn)

        self.assertIn('class="task-record-list"', ice_html)
        self.assertIn(f'href="/task/{ice_id}"', ice_html)
        self.assertIn("Parked idea", ice_html)
        self.assertIn("by <span class=\"task-record-coder\">antigravity</span>", ice_html)
        self.assertIn("(reviewed by opus)", ice_html)
        self.assertNotIn("<table>", ice_html)

        self.assertIn('class="task-record-list"', blocked_html)
        self.assertIn(f'href="/task/{blocked_id}"', blocked_html)
        self.assertIn("Stuck work", blocked_html)
        self.assertIn("Waiting on dependency", blocked_html)
        self.assertIn("commit-review", blocked_html)
        self.assertNotIn("<table>", blocked_html)

    def test_recently_done_record_metadata_and_show_more(self):
        for i in range(6):
            tid = db.add_task(
                self.conn,
                f"Done record {i}",
                branch=f"feat-{i}",
                coder_agent="cursor:grok-4.5-high",
                reviewer_agent="codex",
            )
            db.update_task(
                self.conn,
                tid,
                status="done",
                commit_hash=f"{i:08x}deadbeef",
            )
            self.conn.execute(
                "UPDATE tasks SET last_ready_at = ?, done_at = ? WHERE id = ?",
                ("2026-05-31 10:00:00", "2026-05-31 10:05:00", tid),
            )
        self.conn.commit()
        first_id = 1
        db.add_comment(self.conn, first_id, "Fix", kind="rejection", author="codex", review_round=0)

        html = dashboard.render_recently_done(self.conn)

        self.assertIn('class="task-record-list"', html)
        self.assertIn("task-record-agents", html)
        self.assertIn("cursor:grok-4.5-high", html)
        self.assertIn("(reviewed by codex)", html)
        self.assertIn("task-record-hash", html)
        self.assertIn(
            '<span class="task-record-meta-item task-record-review-rounds">'
            '<span class="task-record-label">review rounds</span> 1</span>',
            html,
        )
        self.assertIn(
            '<span class="task-record-meta-item task-record-runtime">'
            '<span class="task-record-label">runtime</span> 00:05:00</span>',
            html,
        )
        self.assertIn("task-record-finished", html)
        self.assertIn('<span class="task-record-label">finished</span>', html)
        self.assertIn("2026-05-31", html)
        self.assertNotIn("1 review round", html)
        self.assertNotIn("1 rejection", html)
        self.assertNotIn("task-record-rejections", html)
        review_at = html.find("task-record-review-rounds")
        self.assertNotEqual(review_at, -1)
        self.assertLess(review_at, html.find("task-record-runtime", review_at))
        self.assertLess(html.find("task-record-runtime"), html.find("task-record-finished"))
        self.assertIn('data-show-more-row data-row-index="5" hidden', html)
        self.assertIn("Show More", html)
        self.assertNotIn("<thead>", html)
        self.assertNotIn("<table>", html)

    def test_active_supertasks_remain_comparative_table(self):
        parent_id = db.add_task(self.conn, "Parent", branch="feat-parent", kind="supertask")
        db.update_task(self.conn, parent_id, status="pending_subtasks")
        child_id = db.add_task(
            self.conn,
            "Child",
            branch="feat-parent",
            parent_task_id=parent_id,
            sequence_index=100,
        )
        db.update_task(self.conn, child_id, status="ready")

        html = dashboard.render_active_supertasks(self.conn)

        self.assertIn("<table>", html)
        self.assertIn("<th class='col-count'>Done</th>", html)
        self.assertNotIn('class="task-record-list"', html)


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
            "reviewer_agent": "antigravity",
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
        self.assertIn("<th>Reviewer</th><td>antigravity</td>", html)
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
        db.add_comment(self.conn, tid, "Approve it", kind="approval", author="antigravity")
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
            "Starting commit-review round 0 with reviewer: antigravity.",
            kind="comment",
            author="orchestrator",
            review_round=0,
        )
        html = dashboard.render_comments_panel(tid, self.conn)
        self.assertIn("Round 1", html)
        self.assertIn("Starting commit-review round 1 with reviewer: antigravity.", html)
        self.assertNotIn("Starting commit-review round 0 with reviewer: antigravity.", html)


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


class TestCurrentAgentOutputPanel(unittest.TestCase):
    """Tests for current agent output selection and rendering."""

    def setUp(self):
        self.conn, self.db_path = _fresh_conn()

    def tearDown(self):
        artifacts = db.get_artifacts_root(self.db_path)
        if artifacts.exists():
            for path in sorted(artifacts.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            artifacts.rmdir()
        self.conn.close()
        os.unlink(self.db_path)

    def _write_transcript(self, task_id, name, text, mtime):
        task_dir = db.get_artifacts_root(self.db_path) / f"task-{task_id}"
        task_dir.mkdir(parents=True, exist_ok=True)
        path = task_dir / name
        path.write_text(text, encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def test_idle_state_has_clear_empty_message(self):
        state = dashboard.current_agent_output_state(None, self.conn)

        self.assertEqual(state["state"], "idle")
        self.assertIn("no active task", state["message"])

    def test_pending_state_when_active_phase_has_no_transcript(self):
        tid = db.add_task(self.conn, "Active output", coder_agent="codex")
        runtime = {"current_task_id": tid, "current_step": "commit-make"}

        state = dashboard.current_agent_output_state(runtime, self.conn)

        self.assertEqual(state["state"], "pending")
        self.assertEqual(state["task_id"], tid)
        self.assertEqual(state["step"], "commit-make")
        self.assertIn("No agent output file", state["message"])

    def test_selects_latest_transcript_for_current_step_only(self):
        tid = db.add_task(self.conn, "Active output", coder_agent="codex")
        older = self._write_transcript(
            tid,
            "20260603-010000-000000-commit-make-codex.log",
            "older coder output\n",
            100,
        )
        latest = self._write_transcript(
            tid,
            "20260603-010001-000000-commit-make-codex.log",
            "latest coder output\n",
            200,
        )
        self._write_transcript(
            tid,
            "20260603-010002-000000-commit-review-cursor-composer-2.5.log",
            "review output\n",
            300,
        )
        runtime = {"current_task_id": tid, "current_step": "commit-make"}

        state = dashboard.current_agent_output_state(runtime, self.conn)

        self.assertEqual(state["state"], "ready")
        self.assertEqual(state["path"], latest)
        self.assertNotEqual(state["path"], older)
        self.assertEqual(state["lines"], ["latest coder output"])

    def test_follows_phase_when_current_step_changes(self):
        tid = db.add_task(self.conn, "Active output", coder_agent="codex")
        self._write_transcript(
            tid,
            "20260603-010000-000000-commit-make-codex.log",
            "coder output\n",
            100,
        )
        review = self._write_transcript(
            tid,
            "20260603-010001-000000-commit-review-cursor-composer-2.5.log",
            "review output\n",
            200,
        )
        runtime = {"current_task_id": tid, "current_step": "commit-review"}

        state = dashboard.current_agent_output_state(runtime, self.conn)

        self.assertEqual(state["path"], review)
        self.assertEqual(state["step"], "commit-review")
        self.assertEqual(state["lines"], ["review output"])

    def test_render_includes_metadata_and_tail(self):
        tid = db.add_task(self.conn, "Active output", coder_agent="codex")
        path = self._write_transcript(
            tid,
            "20260603-010000-000000-other-make-codex.log",
            "first\nsecond\n",
            100,
        )
        runtime = {"current_task_id": tid, "current_step": "other-make"}

        html = dashboard.render_current_agent_output_panel(runtime, self.conn)

        self.assertIn("Agent Output", html)
        self.assertIn(f">#{tid}</a>", html)
        self.assertIn("other-make", html)
        self.assertIn(path.name, html)
        self.assertIn("first", html)
        self.assertIn("second", html)
        self.assertIn('data-stick-to-bottom="true"', html)


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
        self.assertIn("Set to ready", html)
        self.assertNotIn("data-confirm-inferred-next-step", html)

    def test_blocked_task_shows_set_ready_action(self):
        task = {"id": 5, "status": "blocked", "next_step": "commit-review", "kind": "commit"}
        html = dashboard.render_task_runtime_panel(task, None)
        self.assertIn('action="/task/5/set-ready"', html)
        self.assertIn("Set to ready", html)
        self.assertNotIn("continue-review-cap", html)

    def test_structured_review_cap_shows_continue_form(self):
        task = {
            "id": 5,
            "status": "blocked",
            "next_step": "none",
            "kind": "commit",
            "block_reason": db.BLOCK_REASON_REVIEW_CAP,
            "resume_next_step": "commit-make",
        }
        html = dashboard.render_task_runtime_panel(task, None)
        self.assertIn('action="/task/5/continue-review-cap"', html)
        self.assertIn('name="add_review_rounds"', html)
        self.assertIn('min="1"', html)
        self.assertIn('value="3"', html)
        self.assertIn("Continue with additional rounds", html)
        self.assertNotIn('action="/task/5/set-ready"', html)
        self.assertNotIn("Set to ready", html)

    def test_legacy_blocked_without_metadata_keeps_set_ready(self):
        task = {
            "id": 5,
            "status": "blocked",
            "next_step": "commit-review",
            "kind": "commit",
            "block_reason": None,
            "resume_next_step": None,
        }
        html = dashboard.render_task_runtime_panel(task, None)
        self.assertIn('action="/task/5/set-ready"', html)
        self.assertIn("Set to ready", html)
        self.assertNotIn("continue-review-cap", html)

    def test_missing_next_step_prompts_for_inferred_default(self):
        task = {"id": 5, "status": "blocked", "next_step": "none", "kind": "pull_request"}
        html = dashboard.render_task_runtime_panel(task, None)
        self.assertIn('data-confirm-inferred-next-step="pull-request-make"', html)
        self.assertIn("Set to ready", html)

    def test_unknown_kind_displays_ready_error_without_action(self):
        task = {"id": 5, "status": "blocked", "next_step": "none", "kind": "mystery"}
        html = dashboard.render_task_runtime_panel(task, None)
        self.assertIn("not recognized", html)
        self.assertNotIn("Set to ready", html)

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


class TestReadyActionHelpers(unittest.TestCase):
    """Tests for dashboard Set to ready helper behavior."""

    def setUp(self):
        self.conn, self.db_path = _fresh_conn()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_infers_defaults_by_normalized_kind(self):
        cases = [
            (None, "commit-make"),
            ("task", "commit-make"),
            ("commit", "commit-make"),
            ("pull_request", "pull-request-make"),
            ("other", "other-make"),
            ("supertask", "commit-make-supertask"),
        ]
        for kind, expected in cases:
            with self.subTest(kind=kind):
                task = {
                    "id": 1,
                    "status": "none",
                    "next_step": "none",
                    "kind": kind,
                    "branch": "feat-ready",
                }
                self.assertEqual(dashboard._infer_ready_next_step(task), expected)

    def test_unknown_kind_refused(self):
        task = {
            "id": 1,
            "status": "none",
            "next_step": "none",
            "kind": "unknown",
            "branch": "feat-ready",
        }
        with self.assertRaisesRegex(dashboard.ReadyActionError, "not recognized"):
            dashboard._ready_update_fields(self.conn, task, confirmed_next_step="commit-make")

    def test_preserves_existing_meaningful_next_step(self):
        task = {
            "id": 1,
            "status": "blocked",
            "next_step": "commit-review",
            "kind": "commit",
            "branch": "feat-ready",
        }

        fields = dashboard._ready_update_fields(self.conn, task)

        self.assertEqual(fields["status"], "ready")
        self.assertNotIn("next_step", fields)

    def test_missing_next_step_requires_confirmed_default(self):
        for missing_value in (None, "", "none"):
            with self.subTest(next_step=missing_value):
                task = {
                    "id": 1,
                    "status": "blocked",
                    "next_step": missing_value,
                    "kind": "commit",
                    "branch": "feat-ready",
                }

                with self.assertRaisesRegex(dashboard.ReadyActionNeedsConfirmation, "commit-make"):
                    dashboard._ready_update_fields(self.conn, task)

                fields = dashboard._ready_update_fields(
                    self.conn,
                    task,
                    confirmed_next_step="commit-make",
                )
                self.assertEqual(fields["next_step"], "commit-make")
                self.assertEqual(fields["status"], "ready")

    def test_invalid_existing_next_step_is_refused(self):
        task = {
            "id": 1,
            "status": "blocked",
            "next_step": "other-make",
            "kind": "commit",
            "branch": "feat-ready",
        }
        with self.assertRaisesRegex(dashboard.ReadyActionError, "not valid for commit"):
            dashboard._ready_update_fields(self.conn, task)

    def test_idle_dirty_worktree_refused(self):
        task = {
            "id": 1,
            "status": "blocked",
            "next_step": "commit-make",
            "kind": "commit",
            "branch": "feat-ready",
        }
        with patch.object(dashboard.task_cli, "_is_orchestrator_idle", return_value=True), \
             patch.object(dashboard.task_cli, "_is_worktree_dirty", return_value=True), \
             patch.object(dashboard.task_cli, "_repo_root_for_policy", return_value=Path("/tmp/repo")):
            with self.assertRaisesRegex(dashboard.ReadyActionError, "worktree is dirty"):
                dashboard._ready_update_fields(self.conn, task)

    def test_structured_review_cap_refused_by_helper(self):
        task = {
            "id": 1,
            "status": "blocked",
            "next_step": "none",
            "kind": "commit",
            "branch": "feat-ready",
            "block_reason": db.BLOCK_REASON_REVIEW_CAP,
            "resume_next_step": "commit-make",
        }
        with self.assertRaisesRegex(dashboard.ReadyActionError, "review cap"):
            dashboard._ready_update_fields(self.conn, task)
        state = dashboard._ready_action_state(task)
        self.assertFalse(state["available"])
        self.assertIn("Continue with additional", state["error"])


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

    def test_post_edit_accepts_same_origin_proxy_host(self):
        from fastapi.testclient import TestClient

        client = TestClient(dashboard.app)
        resp = client.post(
            f"/task/{self.tid}/edit",
            data={"title": "Tailscale title", "description": "Proxy origin"},
            headers={
                "host": "192.0.2.1:8427",
                "origin": "http://192.0.2.1:8427",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)

        fresh = db.connect(self.db_path)
        try:
            task = db.get_task(fresh, self.tid)
        finally:
            fresh.close()
        self.assertEqual(task["title"], "Tailscale title")
        self.assertEqual(task["description"], "Proxy origin")

    def test_post_edit_accepts_same_origin_referer_proxy_host(self):
        from fastapi.testclient import TestClient

        client = TestClient(dashboard.app)
        resp = client.post(
            f"/task/{self.tid}/edit",
            data={"title": "Referer title", "description": "Referer origin"},
            headers={
                "host": "192.0.2.1:8427",
                "referer": f"http://192.0.2.1:8427/task/{self.tid}",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)

        fresh = db.connect(self.db_path)
        try:
            task = db.get_task(fresh, self.tid)
        finally:
            fresh.close()
        self.assertEqual(task["title"], "Referer title")
        self.assertEqual(task["description"], "Referer origin")

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


class TestTaskSetReadyRoutes(unittest.TestCase):
    """Tests for task detail Set to ready flow."""

    def setUp(self):
        self.conn, self.db_path = _fresh_conn()
        os.environ["KANBAN_DB"] = self.db_path

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("KANBAN_DB", None)

    def _post_ready(self, task_id, data=None):
        from fastapi.testclient import TestClient

        client = TestClient(dashboard.app)
        return client.post(
            f"/task/{task_id}/set-ready",
            data=data or {},
            headers={"origin": "http://127.0.0.1:8427"},
            follow_redirects=False,
        )

    def test_set_ready_from_none(self):
        tid = db.add_task(self.conn, "Parked task", branch="feat-ready")
        db.update_task(self.conn, tid, next_step="commit-make")

        resp = self._post_ready(tid)

        self.assertEqual(resp.status_code, 303)
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["next_step"], "commit-make")
        self.assertIsNotNone(task["ready_at"])

    def test_set_ready_from_blocked(self):
        tid = db.add_task(self.conn, "Blocked task", branch="feat-ready")
        db.update_task(self.conn, tid, status="blocked", next_step="commit-review")

        resp = self._post_ready(tid)

        self.assertEqual(resp.status_code, 303)
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["next_step"], "commit-review")

    def test_existing_next_step_does_not_require_confirmation(self):
        tid = db.add_task(self.conn, "Blocked task", branch="feat-ready")
        db.update_task(self.conn, tid, status="blocked", next_step="commit-review")

        resp = self._post_ready(tid)

        self.assertEqual(resp.status_code, 303)
        self.assertEqual(db.get_task(self.conn, tid)["next_step"], "commit-review")

    def test_missing_next_step_requires_confirmation_then_applies_default(self):
        tid = db.add_task(self.conn, "Blocked task", branch="feat-ready")
        db.update_task(self.conn, tid, status="blocked", next_step="none")

        resp = self._post_ready(tid)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Confirm setting next_step to", resp.text)
        self.assertEqual(db.get_task(self.conn, tid)["status"], "blocked")

        resp2 = self._post_ready(tid, {"confirmed_next_step": "commit-make"})
        self.assertEqual(resp2.status_code, 303)
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["next_step"], "commit-make")

    def test_legacy_kind_uses_commit_default(self):
        tid = db.add_task(self.conn, "Legacy task", branch="feat-ready")
        self.conn.execute("UPDATE tasks SET kind = 'task' WHERE id = ?", (tid,))
        self.conn.commit()
        db.update_task(self.conn, tid, status="blocked", next_step="none")

        resp = self._post_ready(tid, {"confirmed_next_step": "commit-make"})

        self.assertEqual(resp.status_code, 303)
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["next_step"], "commit-make")

    def test_error_display_for_master_branch_refusal(self):
        tid = db.add_task(self.conn, "Master task")
        db.update_task(self.conn, tid, branch="master", next_step="commit-make")

        resp = self._post_ready(tid)

        self.assertEqual(resp.status_code, 400)
        self.assertIn("tasks on master/main are disabled", resp.text)
        self.assertIn("Set to ready", resp.text)
        self.assertEqual(db.get_task(self.conn, tid)["status"], "none")

    def test_set_ready_rejects_cross_origin(self):
        from fastapi.testclient import TestClient

        tid = db.add_task(self.conn, "Parked task", branch="feat-ready")
        client = TestClient(dashboard.app)
        resp = client.post(
            f"/task/{tid}/set-ready",
            data={},
            headers={"origin": "http://evil.example.com"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_set_ready_rejects_structured_review_cap(self):
        tid = db.add_task(self.conn, "Cap blocked", branch="feat-ready")
        db.update_task(
            self.conn,
            tid,
            status="blocked",
            next_step="none",
            block_reason=db.BLOCK_REASON_REVIEW_CAP,
            resume_next_step="commit-make",
            max_review_rounds=3,
            review_round=3,
        )

        resp = self._post_ready(tid)

        self.assertEqual(resp.status_code, 400)
        self.assertIn("review cap", resp.text.lower())
        self.assertIn("Continue with additional", resp.text)
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(task["block_reason"], db.BLOCK_REASON_REVIEW_CAP)
        self.assertEqual(task["resume_next_step"], "commit-make")
        self.assertEqual(task["max_review_rounds"], 3)


class TestTaskContinueReviewCapRoutes(unittest.TestCase):
    """Tests for task detail review-cap continuation flow."""

    def setUp(self):
        self.conn, self.db_path = _fresh_conn()
        os.environ["KANBAN_DB"] = self.db_path

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("KANBAN_DB", None)

    def _block_at_review_cap(self, *, stash_ref="stash@{0}", max_rounds=3, review_round=None):
        tid = db.add_task(self.conn, "Cap blocked", branch="feat-continue")
        if review_round is None:
            review_round = max_rounds
        db.update_task(
            self.conn,
            tid,
            status="blocked",
            next_step="none",
            review_round=review_round,
            last_review_decision="reject",
            stash_ref=stash_ref,
            block_reason=db.BLOCK_REASON_REVIEW_CAP,
            resume_next_step="commit-make",
            max_review_rounds=max_rounds,
        )
        return tid, review_round, max_rounds

    def _post_continue(self, task_id, data=None):
        from fastapi.testclient import TestClient

        client = TestClient(dashboard.app)
        return client.post(
            f"/task/{task_id}/continue-review-cap",
            data=data if data is not None else {"add_review_rounds": "3"},
            headers={"origin": "http://127.0.0.1:8427"},
            follow_redirects=False,
        )

    def test_continue_review_cap_success(self):
        tid, review_round, max_rounds = self._block_at_review_cap()

        resp = self._post_continue(tid, {"add_review_rounds": "2"})

        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], f"/task/{tid}")
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["next_step"], "commit-make")
        self.assertEqual(task["review_round"], review_round)
        self.assertEqual(task["stash_ref"], "stash@{0}")
        self.assertEqual(task["max_review_rounds"], max_rounds + 2)
        self.assertIsNone(task["block_reason"])
        self.assertIsNone(task["resume_next_step"])
        comments = db.get_comments(self.conn, tid)
        self.assertTrue(
            any(
                c["author"] == "operator" and "additional review round" in c["message"]
                for c in comments
            ),
        )

    def test_continue_invalid_rounds_leaves_task_unchanged(self):
        tid, review_round, max_rounds = self._block_at_review_cap()

        for payload in (
            {},
            {"add_review_rounds": ""},
            {"add_review_rounds": "abc"},
            {"add_review_rounds": "0"},
            {"add_review_rounds": "-1"},
        ):
            with self.subTest(payload=payload):
                resp = self._post_continue(tid, payload)
                self.assertEqual(resp.status_code, 400)
                self.assertIn("positive integer", resp.text.lower())
                self.assertIn("continue-review-cap", resp.text)
                task = db.get_task(self.conn, tid)
                self.assertEqual(task["status"], "blocked")
                self.assertEqual(task["block_reason"], db.BLOCK_REASON_REVIEW_CAP)
                self.assertEqual(task["resume_next_step"], "commit-make")
                self.assertEqual(task["review_round"], review_round)
                self.assertEqual(task["max_review_rounds"], max_rounds)
                self.assertEqual(task["stash_ref"], "stash@{0}")

    def test_continue_restores_parent_supertask(self):
        parent_id = db.add_task(
            self.conn, "Parent", kind="supertask", branch="feat-parent",
        )
        child_id = db.add_task(
            self.conn, "Child", branch="feat-parent", parent_task_id=parent_id,
        )
        db.update_task(
            self.conn, child_id,
            status="blocked", next_step="none",
            review_round=3,
            block_reason=db.BLOCK_REASON_REVIEW_CAP,
            resume_next_step="commit-make",
            stash_ref="stash@{3}",
            max_review_rounds=3,
        )
        db.update_task(self.conn, parent_id, status="blocked")

        resp = self._post_continue(child_id, {"add_review_rounds": "3"})

        self.assertEqual(resp.status_code, 303)
        child = db.get_task(self.conn, child_id)
        self.assertEqual(child["status"], "ready")
        self.assertEqual(child["stash_ref"], "stash@{3}")
        parent = db.get_task(self.conn, parent_id)
        self.assertEqual(parent["status"], "pending_subtasks")

    def test_continue_rejects_cross_origin(self):
        from fastapi.testclient import TestClient

        tid, _, _ = self._block_at_review_cap()
        client = TestClient(dashboard.app)
        resp = client.post(
            f"/task/{tid}/continue-review-cap",
            data={"add_review_rounds": "3"},
            headers={"origin": "http://evil.example.com"},
        )
        self.assertEqual(resp.status_code, 403)
        task = db.get_task(self.conn, tid)
        self.assertEqual(task["status"], "blocked")


class TestOverviewPage(unittest.TestCase):
    """Tests for overview page layout."""

    def setUp(self):
        self.conn, self.db_path = _fresh_conn()
        os.environ["KANBAN_DB"] = self.db_path

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        os.environ.pop("KANBAN_DB", None)

    def test_overview_shows_running_directory_in_header_only(self):
        from fastapi.testclient import TestClient

        client = TestClient(dashboard.app)
        with patch("dashboard.db.get_repo_root", return_value=Path.home().resolve() / "work-repo"):
            resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('<span class="nav-repo-path" title="~/work-repo">~/work-repo</span>', resp.text)
        self.assertNotIn("Running against", resp.text)
        self.assertNotIn("Overview is read-only", resp.text)

    def test_task_detail_shows_running_directory_in_header(self):
        from fastapi.testclient import TestClient

        tid = db.add_task(self.conn, "Task detail path", branch="feat-path")
        client = TestClient(dashboard.app)
        with patch("dashboard.db.get_repo_root", return_value=Path.home().resolve() / "work-repo"):
            resp = client.get(f"/task/{tid}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('<span class="nav-repo-path" title="~/work-repo">~/work-repo</span>', resp.text)
        self.assertNotIn("Running against", resp.text)

    def test_accent_picker_renders_on_overview_and_task_detail(self):
        from fastapi.testclient import TestClient

        tid = db.add_task(self.conn, "Task with accent picker", branch="feat-accent")
        client = TestClient(dashboard.app)

        cookie = dashboard.accent_cookie_name(dashboard.repo_accent_identity())
        for path in ("/", f"/task/{tid}"):
            with self.subTest(path=path):
                resp = client.get(path)
                self.assertEqual(resp.status_code, 200)
                self.assertIn('id="orchestra-accent-picker"', resp.text)
                self.assertIn(f"{cookie}=", resp.text)
                self.assertNotIn("orchestra_accent=", resp.text)

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

    def setUp(self):
        self.publish_patch = patch.object(
            dashboard.dashboard_tailscale,
            "schedule_publish_dashboard",
            return_value=None,
        )
        self.publish_mock = self.publish_patch.start()
        self.addCleanup(self.publish_patch.stop)
        self.fallback_patch = patch.object(
            dashboard.dashboard_tailscale,
            "schedule_startup_dashboard_fallback",
            return_value=None,
        )
        self.fallback_mock = self.fallback_patch.start()
        self.addCleanup(self.fallback_patch.stop)

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
