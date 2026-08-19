#!/usr/bin/env python3
"""Behavioral tests for Fleet Dashboard card-state collection."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dashboard
import db
import fleet
import fleet_dashboard


def _git(root: Path, *args: str, **kwargs) -> subprocess.CompletedProcess[str]:
    env = kwargs.pop("env", None)
    if env is None:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        **kwargs,
    )


def _init_git_repo(root: Path, branch: str = "master") -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    (root / ".gitignore").write_text(
        "kanban-orchestra.db*\nkanban-orchestra.lock\n.kanban-orchestra/\n",
        encoding="utf-8",
    )
    _git(root, "add", ".gitignore")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init")
    _git(root, "branch", "-M", branch)


def _write_lock(root: Path, pid: int | None = None) -> None:
    pid = os.getpid() if pid is None else pid
    (root / "kanban-orchestra.lock").write_text(
        f"role=orchestrator\npid={pid}\nrepo_root={root}\n",
        encoding="utf-8",
    )


def _write_dashboard_metadata(root: Path, url: str = "http://127.0.0.1:8427") -> None:
    runtime = root / ".kanban-orchestra"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "dashboard.json").write_text(
        json.dumps(
            {
                "role": "dashboard",
                "pid": os.getpid(),
                "repo_root": str(root),
                "url": url,
            }
        ),
        encoding="utf-8",
    )


def _insert_runtime(conn, **fields) -> None:
    payload = {
        "singleton": 1,
        "status": "idle",
        "current_task_id": None,
        "current_step": "none",
        "active_agents": 0,
        "status_message": None,
        "current_branch": None,
        "review_round": 0,
    }
    payload.update(fields)
    conn.execute(
        """
        INSERT INTO orchestrator_runtime (
            singleton, status, current_task_id, current_step, active_agents,
            status_message, current_branch, review_round
        ) VALUES (
            :singleton, :status, :current_task_id, :current_step, :active_agents,
            :status_message, :current_branch, :review_round
        )
        """,
        payload,
    )
    conn.commit()


def _collect(repo: fleet.FleetRepo) -> fleet_dashboard.FleetCard:
    with patch.object(fleet, "tmux_has_session", return_value=False):
        return fleet_dashboard.collect_card(repo)


def _tailscale_status(local_port: int = 8427) -> dict:
    return {
        "TCP": {str(local_port): {"HTTPS": True}},
        "Web": {
            f"node.example.ts.net:{local_port}": {
                "Handlers": {"/": {"Proxy": f"http://127.0.0.1:{local_port}"}}
            }
        },
    }


class FleetDashboardRepoTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tmpdir.name).resolve()
        self.root = self.home / "dans-data" / "midi"
        _init_git_repo(self.root, "feature/device-sync")
        self.conn = db.connect(str(self.root / "kanban-orchestra.db"))
        self.repo = fleet.FleetRepo("midi", self.root, self.root)
        self.home_patch = patch.object(fleet.Path, "home", return_value=self.home)
        self.home_patch.start()

    def tearDown(self):
        self.home_patch.stop()
        self.conn.close()
        self.tmpdir.cleanup()


class TestCollectCardStatuses(FleetDashboardRepoTest):
    def test_stopped_card_uses_stopped_status(self):
        card = _collect(self.repo)

        self.assertEqual(card.status, "stopped")
        self.assertFalse(card.invalid)
        self.assertIsNone(card.last_start_failure)

    def test_stopped_dirty_worktree_reports_last_start_failure(self):
        (self.root / "dirty.txt").write_text("unstaged\n", encoding="utf-8")

        card = _collect(self.repo)

        self.assertEqual(card.status, "stopped")
        self.assertEqual(card.last_start_failure, "Worktree dirty")

    def test_idle_card_uses_idle_status(self):
        _write_lock(self.root)
        _insert_runtime(self.conn, status="idle")

        card = _collect(self.repo)

        self.assertEqual(card.status, "idle")
        self.assertFalse(card.invalid)

    def test_running_card_uses_running_status(self):
        task_id = db.add_task(self.conn, "Device synchronization", branch="feature/device-sync")
        db.update_task(self.conn, task_id, status="running")
        _write_lock(self.root)
        _insert_runtime(
            self.conn,
            status="running",
            current_task_id=task_id,
            current_step="commit-make",
            active_agents=1,
        )

        card = _collect(self.repo)

        self.assertEqual(card.status, "running")
        self.assertEqual(card.current_task.id, task_id)
        self.assertEqual(card.current_task.title, "Device synchronization")

    def test_blocked_idle_with_ready_work_uses_blocked_status(self):
        blocked_id = db.add_task(self.conn, "Blocked task", branch="feat-blocked")
        ready_id = db.add_task(self.conn, "Ready task", branch="feat-ready")
        db.update_task(self.conn, blocked_id, status="blocked")
        db.update_task(self.conn, ready_id, status="ready")
        _write_lock(self.root)
        _insert_runtime(self.conn, status="idle")

        card = _collect(self.repo)

        self.assertEqual(card.status, "blocked")

    def test_starting_card_uses_starting_status(self):
        _write_lock(self.root)
        _insert_runtime(self.conn, status="starting")

        card = _collect(self.repo)

        self.assertEqual(card.status, "starting")

    def test_runtime_error_card_uses_error_status(self):
        _write_lock(self.root)
        _insert_runtime(self.conn, status="error", status_message="boom")

        card = _collect(self.repo)

        self.assertEqual(card.status, "error")
        self.assertFalse(card.invalid)

    def test_invalid_config_is_error_card_not_stopped(self):
        missing = self.home / "missing-repo"
        repo = fleet.FleetRepo("missing-repo", missing, None, "path does not exist")

        card = _collect(repo)

        self.assertEqual(card.status, "error")
        self.assertTrue(card.invalid)
        self.assertIsNone(card.last_start_failure)
        self.assertIsNone(card.dashboard_url)
        self.assertIsNone(card.tailscale_url)


class TestCollectCardFields(FleetDashboardRepoTest):
    def test_card_fields_include_name_path_branch_and_empty_current_task(self):
        card = _collect(self.repo)

        self.assertEqual(card.name, "midi")
        self.assertEqual(card.path, "~/dans-data/midi")
        self.assertEqual(card.branch, "feature/device-sync")
        self.assertIsNone(card.current_task)

    def test_branch_is_read_as_utf8(self):
        _git(self.root, "checkout", "-q", "-b", "feature/unicodé-sync")

        card = _collect(self.repo)

        self.assertEqual(card.branch, "feature/unicodé-sync")

    def test_queue_counts_match_ready_recently_done_and_icebox(self):
        ready_a = db.add_task(self.conn, "Ready A", branch="feat-a")
        ready_b = db.add_task(self.conn, "Ready B", branch="feat-b")
        db.add_task(self.conn, "Parked", branch="feat-parked")
        recent = db.add_task(self.conn, "Recent done", branch="feat-recent")
        stale = db.add_task(self.conn, "Old done", branch="feat-old")
        db.update_task(self.conn, ready_a, status="ready")
        db.update_task(self.conn, ready_b, status="ready")
        db.update_task(self.conn, recent, status="done")
        db.update_task(self.conn, stale, status="done")
        stale_done_at = (
            datetime.now(timezone.utc) - dashboard.DONE_RECENCY_CUTOFF - timedelta(hours=1)
        ).strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            "UPDATE tasks SET done_at = ? WHERE id = ?",
            (stale_done_at, stale),
        )
        self.conn.commit()

        card = _collect(self.repo)

        self.assertEqual(card.ready_count, 2)
        self.assertEqual(card.recently_done_count, 1)
        self.assertEqual(card.icebox_count, 1)

    def test_collect_cards_uses_configured_repos_only(self):
        with patch.object(fleet, "load_repos", return_value=[self.repo]) as load_mock, \
             patch.object(fleet, "status_repos") as status_mock, \
             patch.object(fleet, "tmux_has_session", return_value=False):
            cards = fleet_dashboard.collect_cards()

        load_mock.assert_called_once_with()
        status_mock.assert_not_called()
        self.assertEqual([card.name for card in cards], ["midi"])


class TestCollectCardTailscale(FleetDashboardRepoTest):
    def test_exact_https_proxy_mapping_sets_tailscale_url(self):
        _write_lock(self.root)
        _insert_runtime(self.conn, status="idle")
        _write_dashboard_metadata(self.root, "http://127.0.0.1:8427")
        result = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps(_tailscale_status()), stderr=""
        )

        with patch.object(fleet.shutil, "which", return_value="/usr/bin/tailscale"), \
             patch.object(fleet, "run", return_value=result):
            card = _collect(self.repo)

        self.assertEqual(card.dashboard_url, "http://127.0.0.1:8427")
        self.assertEqual(card.tailscale_url, "https://node.example.ts.net:8427/")

    def test_unavailable_tailscale_returns_none_without_warnings(self):
        _write_lock(self.root)
        _insert_runtime(self.conn, status="idle")
        _write_dashboard_metadata(self.root, "http://127.0.0.1:8427")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch.object(fleet.shutil, "which", return_value=None), \
             patch.object(fleet, "run") as run_mock, \
             redirect_stdout(stdout), \
             redirect_stderr(stderr):
            card = _collect(self.repo)

        self.assertEqual(card.dashboard_url, "http://127.0.0.1:8427")
        self.assertIsNone(card.tailscale_url)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        run_mock.assert_not_called()

    def test_unmapped_https_proxy_returns_none_without_warnings(self):
        _write_lock(self.root)
        _insert_runtime(self.conn, status="idle")
        _write_dashboard_metadata(self.root, "http://127.0.0.1:8427")
        status = {
            "TCP": {"8427": {"HTTPS": True}, "8428": {"HTTPS": False}},
            "Web": {
                "node.example.ts.net:8427": {
                    "Handlers": {"/": {"Proxy": "http://127.0.0.1:9000"}}
                },
                "node.example.ts.net:8428": {
                    "Handlers": {"/": {"Proxy": "http://127.0.0.1:8427"}}
                },
            },
        }
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(status), stderr="")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch.object(fleet.shutil, "which", return_value="/usr/bin/tailscale"), \
             patch.object(fleet, "run", return_value=result), \
             redirect_stdout(stdout), \
             redirect_stderr(stderr):
            card = _collect(self.repo)

        self.assertIsNone(card.tailscale_url)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_nonzero_or_invalid_tailscale_json_returns_none_without_warnings(self):
        _write_lock(self.root)
        _insert_runtime(self.conn, status="idle")
        _write_dashboard_metadata(self.root, "http://127.0.0.1:8427")
        cases = (
            subprocess.CompletedProcess([], 1, stdout="", stderr="serve: failed"),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="{not-json", stderr=""),
        )

        for result in cases:
            with self.subTest(returncode=result.returncode, stdout=result.stdout):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with patch.object(fleet.shutil, "which", return_value="/usr/bin/tailscale"), \
                     patch.object(fleet, "run", return_value=result), \
                     redirect_stdout(stdout), \
                     redirect_stderr(stderr):
                    card = _collect(self.repo)

                self.assertIsNone(card.tailscale_url)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
