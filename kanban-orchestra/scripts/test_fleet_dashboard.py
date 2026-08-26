#!/usr/bin/env python3
"""Behavioral tests for Fleet Dashboard card-state collection and HTML page."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

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

        with patch.object(fleet.dashboard_tailscale.shutil, "which", return_value="/usr/bin/tailscale"), \
             patch.object(fleet.dashboard_tailscale, "_run", return_value=result):
            card = _collect(self.repo)

        self.assertEqual(card.dashboard_url, "http://127.0.0.1:8427")
        self.assertEqual(card.tailscale_url, "https://node.example.ts.net:8427/")

    def test_unavailable_tailscale_returns_none_without_warnings(self):
        _write_lock(self.root)
        _insert_runtime(self.conn, status="idle")
        _write_dashboard_metadata(self.root, "http://127.0.0.1:8427")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch.object(fleet.dashboard_tailscale.shutil, "which", return_value=None), \
             patch.object(fleet.dashboard_tailscale, "_run") as run_mock, \
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

        with patch.object(fleet.dashboard_tailscale.shutil, "which", return_value="/usr/bin/tailscale"), \
             patch.object(fleet.dashboard_tailscale, "_run", return_value=result), \
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
                with patch.object(fleet.dashboard_tailscale.shutil, "which", return_value="/usr/bin/tailscale"), \
                     patch.object(fleet.dashboard_tailscale, "_run", return_value=result), \
                     redirect_stdout(stdout), \
                     redirect_stderr(stderr):
                    card = _collect(self.repo)

                self.assertIsNone(card.tailscale_url)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "")


def _page_card(**fields) -> fleet_dashboard.FleetCard:
    payload = {
        "name": "midi",
        "path": "~/dans-data/midi",
        "branch": "feature/device-sync",
        "status": "idle",
        "invalid": False,
        "current_task": None,
        "ready_count": 0,
        "recently_done_count": 4,
        "icebox_count": 7,
        "dashboard_url": "http://127.0.0.1:8428",
        "tailscale_url": None,
        "last_start_failure": None,
    }
    payload.update(fields)
    return fleet_dashboard.FleetCard(**payload)


_LOCAL_VIEW_HEADERS = {
    "host": "127.0.0.1:8426",
    "origin": "http://127.0.0.1:8426",
}
_TAILSCALE_VIEW_HEADERS = {
    "host": "node.example.ts.net:8426",
    "origin": "https://node.example.ts.net:8426",
    "x-forwarded-host": "node.example.ts.net:8426",
    "x-forwarded-proto": "https",
}


def _get_fleet_page(
    *cards: fleet_dashboard.FleetCard,
    config_display: str = "~/.config/orchestra/fleet.repos",
    headers: dict[str, str] | None = None,
):
    from fastapi.testclient import TestClient

    request_headers = dict(_LOCAL_VIEW_HEADERS if headers is None else headers)
    with patch.object(fleet_dashboard, "collect_cards", return_value=list(cards)), \
         patch.object(fleet_dashboard, "_config_display", return_value=config_display):
        return TestClient(fleet_dashboard.app).get("/", headers=request_headers)


def _visible_text(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


class TestFleetDashboardPage(unittest.TestCase):
    def test_get_index_serves_utf8_html(self):
        response = _get_fleet_page(_page_card(branch="feature/unicodé-sync"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("charset=utf-8", response.headers["content-type"].lower())
        self.assertIn('<meta charset="utf-8">', response.text)
        self.assertIn("feature/unicodé-sync", response.text)

    def test_page_uses_early_accent_preference_and_accessible_picker(self):
        html = _get_fleet_page(_page_card()).text
        cookie = dashboard.accent_cookie_name(dashboard.fleet_accent_identity())
        repo_cookie = dashboard.accent_cookie_name(dashboard.repo_accent_identity())

        self.assertNotEqual(cookie, repo_cookie)
        self.assertLess(html.index(cookie), html.index("<style>"))
        self.assertIn(f"{cookie}=", html)
        self.assertNotIn(f"{repo_cookie}=", html)
        self.assertNotIn("orchestra_accent=", html)
        self.assertIn(dashboard.accent_picker_html(), html)
        self.assertIn(
            'id="orchestra-accent-picker" role="radiogroup" aria-label="Accent"',
            html,
        )
        self.assertNotIn("<select", html)
        self.assertNotIn("<option", html)
        self.assertIn("; Path=/; Max-Age=31536000; SameSite=Lax", html)
        self.assertNotIn("; Domain=", html)
        self.assertNotIn("localStorage", html)
        self.assertIn("flex-wrap: wrap", html)
        self.assertIn("var(--accent-soft)", html)
        self.assertIn("var(--accent-hover)", html)
        self.assertIn(".accent-chit input:checked + .accent-chit-swatch", html)
        self.assertIn(".accent-chit input:checked + .accent-chit-swatch::after", html)
        self.assertIn(".accent-chit input:focus-visible + .accent-chit-swatch", html)
        self.assertIn("input.checked = input.value === name", html)
        self.assertEqual(html.count('class="accent-chit"'), len(dashboard.ACCENT_PALETTE))
        for name, accent in dashboard.ACCENT_PALETTE.items():
            with self.subTest(accent=name):
                self.assertIn(
                    f'<label class="accent-chit" title="{accent["label"]}" '
                    f'style="--chit-color: {accent["color"]}">',
                    html,
                )
                self.assertIn(
                    f'<input type="radio" name="accent" value="{name}" '
                    f'aria-label="{accent["label"]}">',
                    html,
                )
                self.assertNotIn(f'>{accent["label"]}<', html)

    def test_fleet_accent_does_not_replace_semantic_status_tokens(self):
        html = _get_fleet_page(_page_card(status="blocked")).text

        self.assertIn(".badge-blocked", html)
        self.assertIn("color: var(--red)", html)
        self.assertIn(".badge-running", html)
        self.assertIn("color: var(--blue)", html)
        self.assertIn(".badge-pending-subtasks", html)
        self.assertIn("color: var(--orange)", html)
        self.assertIn(".queue-box-ready .queue-count", html)
        self.assertIn("color: var(--green)", html)

    def test_cards_match_collector_data_and_layout(self):
        task = fleet_dashboard.FleetCurrentTask(214, "Device synchronization")
        response = _get_fleet_page(
            _page_card(
                name="midi",
                path="~/dans-data/midi",
                branch="feature/device-sync",
                status="running",
                current_task=task,
                ready_count=3,
                recently_done_count=2,
                icebox_count=5,
                dashboard_url="http://127.0.0.1:8428",
            )
        )
        html = response.text

        self.assertIn('class="card repo-record"', html)
        self.assertIn('class="repo-list"', html)
        self.assertIn("midi", html)
        self.assertIn("~/dans-data/midi", html)
        self.assertIn("feature/device-sync", html)
        self.assertIn("badge-running", html)
        self.assertIn(">running</span>", html)
        self.assertIn("#214 Device synchronization", html)
        self.assertIn(">3</span>", html)
        self.assertIn(">Ready</span>", html)
        self.assertIn(">2</span>", html)
        self.assertIn(">Recently Done</span>", html)
        self.assertIn(">5</span>", html)
        self.assertIn(">Icebox</span>", html)
        self.assertIn(">Dashboard</a>", html)
        self.assertIn('href="http://127.0.0.1:8428"', html)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", html)
        self.assertIn(".dashboard-links {\n  display: grid;\n  grid-template-columns: 1fr", html)
        self.assertNotIn("aspect-ratio", html)
        self.assertIn("min-height: 232px", html)
        self.assertIn("@media (max-width: 960px)", html)
        self.assertIn("@media (max-width: 560px)", html)

    def test_absent_current_task_renders_none(self):
        html = _get_fleet_page(_page_card(current_task=None)).text

        self.assertIn("Current task", html)
        self.assertIn(">None</span>", html)

    def test_omits_subtitle_summary_heartbeat_and_raw_dashboard_urls(self):
        html = _get_fleet_page(
            _page_card(
                dashboard_url="http://127.0.0.1:8428",
                tailscale_url="https://node.example.ts.net:8428/",
            )
        ).text
        visible = _visible_text(html)

        self.assertNotIn('class="lede"', html)
        self.assertNotIn("subtitle", html.lower())
        self.assertNotIn("heartbeat-age", html)
        self.assertNotIn("heartbeat", visible.lower())
        self.assertNotIn("health-wrap", html)
        self.assertNotIn("aggregate", html.lower())
        self.assertNotIn("http://127.0.0.1:8428", visible)
        self.assertNotIn("https://node.example.ts.net:8428/", visible)
        self.assertIn(">Dashboard</a>", html)
        self.assertNotIn("Via Tailscale", html)
        self.assertEqual(html.count('<div class="dashboard-links">'), 1)

    def test_local_dashboard_action_uses_local_url_with_or_without_mapping(self):
        with_mapping = _get_fleet_page(
            _page_card(tailscale_url="https://node.example.ts.net:8428/")
        ).text
        without_mapping = _get_fleet_page(_page_card(tailscale_url=None)).text

        for html in (with_mapping, without_mapping):
            self.assertIn(">Dashboard</a>", html)
            self.assertIn('href="http://127.0.0.1:8428"', html)
            self.assertNotIn("Via Tailscale", html)
        self.assertNotIn('href="https://node.example.ts.net:8428/"', with_mapping)

    def test_stopped_card_shows_play_button_and_last_start_reason(self):
        html = _get_fleet_page(
            _page_card(
                name="clip-library",
                status="stopped",
                dashboard_url=None,
                last_start_failure="Worktree dirty",
            )
        ).text

        self.assertRegex(
            html,
            r'badge-stopped">stopped</span>\s*'
            r'<button class="start-button" type="button" '
            r'data-start-repo="clip-library" '
            r'aria-label="Start clip-library"></button>',
        )
        self.assertIn("Last start failed", html)
        self.assertIn("Worktree dirty", html)
        self.assertIn(".start-button::before", html)
        self.assertIn("border-left: 7px solid currentColor", html)
        self.assertNotIn("▶", html)
        self.assertNotIn("►", html)
        running_html = _get_fleet_page(_page_card(status="running")).text
        self.assertNotIn('class="start-button"', running_html)
        self.assertNotIn('data-start-repo="midi"', running_html)

    def test_error_and_invalid_cards_omit_play_button(self):
        error_html = _get_fleet_page(
            _page_card(status="error", dashboard_url=None)
        ).text
        invalid_html = _get_fleet_page(
            _page_card(status="error", invalid=True, dashboard_url=None)
        ).text
        stopped_invalid_html = _get_fleet_page(
            _page_card(status="stopped", invalid=True, dashboard_url=None)
        ).text

        for html in (error_html, invalid_html, stopped_invalid_html):
            self.assertNotIn('class="start-button"', html)
            self.assertNotIn('data-start-repo="midi"', html)

    def test_page_exposes_starting_success_and_failure_client_state(self):
        html = _get_fleet_page(_page_card(status="stopped", dashboard_url=None)).text

        self.assertIn("is-starting", html)
        self.assertIn('method: "POST"', html)
        self.assertIn("/repos/", html)
        self.assertIn("encodeURIComponent", html)
        self.assertIn("outerHTML", html)
        self.assertIn("payload.html", html)
        self.assertIn("showFailure", html)
        self.assertIn("Last start failed", html)
        self.assertIn("start-reason", html)
        self.assertIn("Start failed", html)

    def test_local_origin_shows_one_local_dashboard_action_when_mapped(self):
        html = _get_fleet_page(
            _page_card(
                dashboard_url="http://127.0.0.1:8428",
                tailscale_url="https://node.example.ts.net:8428/",
            )
        ).text

        self.assertIn(">Dashboard</a>", html)
        self.assertIn('href="http://127.0.0.1:8428"', html)
        self.assertNotIn("Via Tailscale", html)
        self.assertNotIn('href="https://node.example.ts.net:8428/"', html)
        self.assertEqual(html.count('<div class="dashboard-links">'), 1)

    def test_local_origin_without_mapping_shows_local_dashboard_action(self):
        html = _get_fleet_page(_page_card(tailscale_url=None)).text

        self.assertIn(">Dashboard</a>", html)
        self.assertIn('href="http://127.0.0.1:8428"', html)
        self.assertNotIn("Via Tailscale", html)

    def test_loopback_host_without_origin_is_treated_as_local(self):
        html = _get_fleet_page(
            _page_card(tailscale_url="https://node.example.ts.net:8428/"),
            headers={"host": "127.0.0.1:8426"},
        ).text

        self.assertIn(">Dashboard</a>", html)
        self.assertIn('href="http://127.0.0.1:8428"', html)
        self.assertNotIn("Via Tailscale", html)

    def test_tailscale_origin_shows_one_remote_dashboard_action(self):
        html = _get_fleet_page(
            _page_card(
                current_task=fleet_dashboard.FleetCurrentTask(214, "Device synchronization"),
                dashboard_url="http://127.0.0.1:8428",
                tailscale_url="https://node.example.ts.net:8428/",
            ),
            headers=_TAILSCALE_VIEW_HEADERS,
        ).text

        self.assertNotIn('href="http://127.0.0.1:8428"', html)
        self.assertNotIn("http://127.0.0.1:8428/task/214", html)
        self.assertIn(">Dashboard</a>", html)
        self.assertNotIn("Via Tailscale", html)
        self.assertIn('href="https://node.example.ts.net:8428/"', html)
        self.assertIn('href="https://node.example.ts.net:8428/task/214"', html)
        self.assertEqual(html.count('<div class="dashboard-links">'), 1)
        self.assertIn('class="repo-name"', html)
        self.assertNotIn("Dashboard unavailable", html)

    def test_tailscale_origin_without_mapping_is_quiet(self):
        html = _get_fleet_page(
            _page_card(
                current_task=fleet_dashboard.FleetCurrentTask(214, "Device synchronization"),
                tailscale_url=None,
            ),
            headers=_TAILSCALE_VIEW_HEADERS,
        ).text

        self.assertNotIn(">Dashboard</a>", html)
        self.assertNotIn("Via Tailscale", html)
        self.assertNotIn("Dashboard unavailable", html)
        self.assertNotIn("http://127.0.0.1:8428", html)
        self.assertIn('<span class="repo-name">midi</span>', html)
        self.assertIn("<span>#214 Device synchronization</span>", html)

    def test_tailscale_stopped_card_still_reports_unavailable(self):
        html = _get_fleet_page(
            _page_card(status="stopped", dashboard_url=None, tailscale_url=None),
            headers=_TAILSCALE_VIEW_HEADERS,
        ).text

        self.assertIn("Dashboard unavailable", html)
        self.assertNotIn(">Dashboard</a>", html)
        self.assertNotIn("Via Tailscale", html)

    def test_forwarded_https_on_loopback_host_is_treated_as_proxied(self):
        html = _get_fleet_page(
            _page_card(tailscale_url="https://node.example.ts.net:8428/"),
            headers={
                "host": "127.0.0.1:8426",
                "origin": "http://127.0.0.1:8426",
                "x-forwarded-host": "node.example.ts.net:8426",
                "x-forwarded-proto": "https",
            },
        ).text

        self.assertIn(">Dashboard</a>", html)
        self.assertNotIn("Via Tailscale", html)
        self.assertIn('href="https://node.example.ts.net:8428/"', html)

    def test_repo_dashboard_actions_open_in_a_new_tab(self):
        html = fleet_dashboard.render_card(
            _page_card(
                current_task=fleet_dashboard.FleetCurrentTask(214, "Device synchronization"),
                tailscale_url="https://node.example.ts.net:8428/",
            )
        )

        for href in (
            "http://127.0.0.1:8428",
            "http://127.0.0.1:8428/task/214",
        ):
            self.assertRegex(
                html,
                rf'<a[^>]*href="{re.escape(href)}"[^>]*target="_blank"[^>]*rel="noopener noreferrer"',
            )
        self.assertNotIn("https://node.example.ts.net:8428/", html)
        self.assertEqual(html.count('target="_blank"'), 3)
        self.assertEqual(html.count('rel="noopener noreferrer"'), 3)
        self.assertNotIn('target="_blank"', _get_fleet_page(_page_card()).text.split("<main>")[0])

    def test_play_start_action_stays_in_place(self):
        html = _get_fleet_page(
            _page_card(status="stopped", dashboard_url=None)
        ).text
        match = re.search(r'<button class="start-button"[^>]*>', html)

        self.assertIsNotNone(match)
        self.assertIn('data-start-repo="midi"', match.group(0))
        self.assertNotIn("target=", match.group(0))
        self.assertNotIn("window.open", html)


def _post_start(label: str, headers: dict[str, str] | None = None):
    from fastapi.testclient import TestClient

    request_headers = dict(_LOCAL_VIEW_HEADERS if headers is None else headers)
    return TestClient(fleet_dashboard.app).post(
        f"/repos/{label}/start",
        headers=request_headers,
    )


class TestFleetDashboardStart(FleetDashboardRepoTest):
    def test_start_rejects_cross_origin_and_missing_origin_before_lookup(self):
        cases = (
            {"host": "127.0.0.1:8426"},
            {
                "host": "127.0.0.1:8426",
                "origin": "https://attacker.example",
            },
            {
                "host": "127.0.0.1:8426",
                "origin": "https://attacker.example",
                "x-forwarded-host": "node.example.ts.net:8426",
                "x-forwarded-proto": "https",
            },
        )
        for headers in cases:
            with self.subTest(headers=headers), \
                 patch.object(fleet, "load_repos") as load_mock, \
                 patch.object(fleet, "try_start_repo") as start_mock:
                response = _post_start("midi", headers=headers)

            self.assertEqual(response.status_code, 403)
            self.assertFalse(response.json()["ok"])
            self.assertEqual(
                response.json()["error"],
                "Forbidden: cross-origin request",
            )
            load_mock.assert_not_called()
            start_mock.assert_not_called()

    def test_start_accepts_configured_label_only(self):
        with patch.object(fleet, "load_repos", return_value=[self.repo]) as load_mock, \
             patch.object(fleet, "try_start_repo", return_value=None) as start_mock, \
             patch.object(fleet, "tmux_has_session", return_value=False):
            allowed = _post_start("midi")
            unknown = _post_start("other")
            path_selector = _post_start(str(self.root))

        load_mock.assert_called()
        start_mock.assert_called_once_with(self.repo)
        self.assertEqual(allowed.status_code, 200)
        self.assertTrue(allowed.json()["ok"])
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json()["error"], "Unknown repo")
        self.assertFalse(unknown.json()["ok"])
        self.assertEqual(path_selector.status_code, 404)
        self.assertNotIn("card", unknown.json())

    def test_start_rejects_unknown_unmanaged_and_invalid_labels(self):
        unmanaged = fleet.FleetRepo("shadow", self.root, self.root, managed=False)
        invalid = fleet.FleetRepo(
            "missing-repo",
            self.home / "missing-repo",
            None,
            "path does not exist",
        )

        with patch.object(fleet, "load_repos", return_value=[self.repo, unmanaged, invalid]), \
             patch.object(fleet, "try_start_repo") as start_mock:
            unknown = _post_start("not-configured")
            unmanaged_resp = _post_start("shadow")
            invalid_resp = _post_start("missing-repo")

        start_mock.assert_not_called()
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json()["error"], "Unknown repo")
        self.assertEqual(unmanaged_resp.status_code, 400)
        self.assertEqual(unmanaged_resp.json()["error"], "Unmanaged repo")
        self.assertEqual(invalid_resp.status_code, 400)
        self.assertEqual(invalid_resp.json()["error"], "Invalid config")

    def test_dirty_start_reports_worktree_dirty_without_launching(self):
        (self.root / "dirty.txt").write_text("unstaged\n", encoding="utf-8")

        with patch.object(fleet, "load_repos", return_value=[self.repo]), \
             patch.object(fleet, "start_tmux_session") as launch_mock, \
             patch.object(fleet, "tmux_has_session", return_value=False):
            response = _post_start("midi")

        launch_mock.assert_not_called()
        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "Worktree dirty")
        self.assertEqual(payload["card"]["status"], "stopped")
        self.assertEqual(payload["card"]["last_start_failure"], "Worktree dirty")
        self.assertIn("Worktree dirty", payload["html"])
        self.assertIn("Last start failed", payload["html"])
        self.assertIn("data-start-repo", payload["html"])

    def test_successful_start_refreshes_card_from_collector_state(self):
        task_id = db.add_task(
            self.conn, "Device synchronization", branch="feature/device-sync"
        )
        db.update_task(self.conn, task_id, status="running")
        _insert_runtime(
            self.conn,
            status="running",
            current_task_id=task_id,
            current_step="commit-make",
            active_agents=1,
        )

        def fake_launch(repo, *, preferred_port, orchestrator):
            _write_lock(repo.root)
            _write_dashboard_metadata(repo.root, "http://127.0.0.1:8427")
            return True

        with patch.object(fleet, "load_repos", return_value=[self.repo]), \
             patch.object(fleet.shutil, "which", return_value="/usr/bin/tmux"), \
             patch.object(fleet, "start_tmux_session", side_effect=fake_launch) as launch_mock, \
             patch.object(
                 fleet,
                 "tailscale_dashboard_url",
                 return_value="https://node.example.ts.net:8427/",
             ), \
             patch.object(fleet, "tmux_has_session", return_value=False):
            response = _post_start("midi")

        launch_mock.assert_called_once()
        launched_repo = launch_mock.call_args.args[0]
        self.assertEqual(launched_repo.label, "midi")
        self.assertEqual(launch_mock.call_args.kwargs["preferred_port"], 8427)
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        card = payload["card"]
        self.assertEqual(card["status"], "running")
        self.assertEqual(card["dashboard_url"], "http://127.0.0.1:8427")
        self.assertEqual(card["tailscale_url"], "https://node.example.ts.net:8427/")
        self.assertEqual(card["current_task"]["id"], task_id)
        self.assertEqual(card["current_task"]["title"], "Device synchronization")
        self.assertIsNone(card["last_start_failure"])
        self.assertIn("badge-running", payload["html"])
        self.assertIn("http://127.0.0.1:8427", payload["html"])
        self.assertIn(">Dashboard</a>", payload["html"])
        self.assertNotIn("Via Tailscale", payload["html"])
        self.assertNotIn("https://node.example.ts.net:8427/", payload["html"])
        self.assertIn("#%s Device synchronization" % task_id, payload["html"])
        self.assertNotIn("data-start-repo", payload["html"])
        self.assertNotIn("error", payload)

    def test_start_html_follows_tailscale_origin(self):
        def fake_launch(repo, *, preferred_port, orchestrator):
            _write_lock(repo.root)
            _write_dashboard_metadata(repo.root, "http://127.0.0.1:8427")
            return True

        with patch.object(fleet, "load_repos", return_value=[self.repo]), \
             patch.object(fleet.shutil, "which", return_value="/usr/bin/tmux"), \
             patch.object(fleet, "start_tmux_session", side_effect=fake_launch), \
             patch.object(
                 fleet,
                 "tailscale_dashboard_url",
                 return_value="https://node.example.ts.net:8427/",
             ), \
             patch.object(fleet, "tmux_has_session", return_value=False):
            response = _post_start(
                "midi",
                headers={
                    "host": "127.0.0.1:8426",
                    "origin": "https://node.example.ts.net:8426",
                    "x-forwarded-host": "node.example.ts.net:8426",
                    "x-forwarded-proto": "https",
                },
            )

        html = response.json()["html"]
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("http://127.0.0.1:8427", html)
        self.assertIn(">Dashboard</a>", html)
        self.assertNotIn("Via Tailscale", html)
        self.assertIn('href="https://node.example.ts.net:8427/"', html)
        self.assertIn('target="_blank"', html)
        self.assertIn('rel="noopener noreferrer"', html)

    def test_start_response_exposes_starting_success_and_failure(self):
        with patch.object(fleet, "load_repos", return_value=[self.repo]), \
             patch.object(fleet, "try_start_repo", return_value=None), \
             patch.object(fleet, "tmux_has_session", return_value=False):
            success = _post_start("midi")

        (self.root / "dirty.txt").write_text("unstaged\n", encoding="utf-8")
        with patch.object(fleet, "load_repos", return_value=[self.repo]), \
             patch.object(fleet, "tmux_has_session", return_value=False):
            failure = _post_start("midi")

        page = _get_fleet_page(_page_card(status="stopped", dashboard_url=None)).text
        self.assertIn('classList.add("is-starting")', page)
        self.assertTrue(success.json()["ok"])
        self.assertIn("html", success.json())
        self.assertIn("card", success.json())
        self.assertFalse(failure.json()["ok"])
        self.assertEqual(failure.json()["error"], "Worktree dirty")
        self.assertIn("html", failure.json())


class FleetDashboardServerTests(unittest.TestCase):
    def setUp(self):
        self.publish_patch = patch.object(
            fleet_dashboard.dashboard_tailscale,
            "schedule_publish_dashboard",
            return_value=None,
        )
        self.publish_mock = self.publish_patch.start()
        self.addCleanup(self.publish_patch.stop)
        self.fallback_patch = patch.object(
            fleet_dashboard.dashboard_tailscale,
            "schedule_startup_dashboard_fallback",
            return_value=None,
        )
        self.fallback_mock = self.fallback_patch.start()
        self.addCleanup(self.fallback_patch.stop)

    def test_write_dashboard_metadata_uses_fleet_config_sidecar(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            meta = Path(tmpdir) / "fleet-dashboard.json"
            with patch.object(fleet, "fleet_dashboard_metadata_path", return_value=meta):
                fleet_dashboard._write_dashboard_metadata("127.0.0.1", 8426)
                payload = json.loads(meta.read_text(encoding="utf-8"))
                self.assertEqual(payload["role"], "fleet-dashboard")
                self.assertEqual(payload["host"], "127.0.0.1")
                self.assertEqual(payload["port"], 8426)
                self.assertEqual(payload["url"], "http://127.0.0.1:8426")
                self.assertEqual(payload["pid"], os.getpid())
                self.assertEqual(payload["owner"], fleet_dashboard._METADATA_OWNER)
                self.assertIsNone(payload["remote_url"])
                fleet_dashboard._remove_dashboard_metadata()
                self.assertFalse(meta.exists())

    def test_remove_dashboard_metadata_preserves_new_owner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            meta = Path(tmpdir) / "fleet-dashboard.json"
            meta.write_text(
                json.dumps(
                    {
                        "role": "fleet-dashboard",
                        "pid": os.getpid(),
                        "owner": "newer-process-owner",
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(fleet, "fleet_dashboard_metadata_path", return_value=meta):
                fleet_dashboard._remove_dashboard_metadata()

            self.assertTrue(meta.exists())

    def test_remove_dashboard_metadata_serializes_owner_check_and_delete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            meta = Path(tmpdir) / "fleet-dashboard.json"
            owned_payload = {
                "role": "fleet-dashboard",
                "pid": os.getpid(),
                "owner": fleet_dashboard._METADATA_OWNER,
            }
            newer_payload = {
                "role": "fleet-dashboard",
                "pid": os.getpid() + 1,
                "owner": "newer-process-owner",
            }
            meta.write_text(json.dumps(owned_payload), encoding="utf-8")
            replacement_attempted = threading.Event()
            replacement_finished = threading.Event()

            def replace_metadata():
                replacement_attempted.set()
                with fleet_dashboard._dashboard_metadata_lock(meta):
                    meta.write_text(json.dumps(newer_payload), encoding="utf-8")
                replacement_finished.set()

            replacement_thread = None

            def read_while_replacement_waits(_path):
                nonlocal replacement_thread
                replacement_thread = threading.Thread(target=replace_metadata)
                replacement_thread.start()
                self.assertTrue(replacement_attempted.wait(timeout=1))
                self.assertFalse(replacement_finished.wait(timeout=0.1))
                return owned_payload

            with patch.object(fleet, "fleet_dashboard_metadata_path", return_value=meta), \
                 patch.object(fleet, "read_key_value_or_json", side_effect=read_while_replacement_waits):
                fleet_dashboard._remove_dashboard_metadata()
                self.assertIsNotNone(replacement_thread)
                replacement_thread.join(timeout=1)

            self.assertFalse(replacement_thread.is_alive())
            self.assertEqual(json.loads(meta.read_text(encoding="utf-8")), newer_payload)

    def test_run_dashboard_uses_free_port_and_fleet_app(self):
        uv = MagicMock()
        uv.run = MagicMock()

        with patch.object(dashboard, "_find_free_port", return_value=8426) as find_mock, \
             patch.object(fleet_dashboard, "_write_dashboard_metadata") as write_mock, \
             patch("builtins.print") as mock_print:
            fleet_dashboard._run_dashboard("127.0.0.1", 8426, _uvicorn=uv)

        find_mock.assert_called_once_with("127.0.0.1", 8426)
        write_mock.assert_called_once_with("127.0.0.1", 8426, remote_url=None)
        _, kwargs = uv.run.call_args
        self.assertEqual(kwargs["host"], "127.0.0.1")
        self.assertEqual(kwargs["port"], 8426)
        self.assertEqual(uv.run.call_args.args[0], "fleet_dashboard:app")
        for call in mock_print.call_args_list:
            self.assertFalse(any("in use" in str(arg) for arg in call[0]))

    def test_run_dashboard_reports_fallback_port(self):
        uv = MagicMock()
        uv.run = MagicMock()

        with patch.object(dashboard, "_find_free_port", return_value=8419), \
             patch.object(fleet_dashboard, "_write_dashboard_metadata"), \
             patch("builtins.print") as mock_print:
            fleet_dashboard._run_dashboard("127.0.0.1", 8426, _uvicorn=uv)

        _, kwargs = uv.run.call_args
        self.assertEqual(kwargs["port"], 8419)
        printed = " ".join(str(arg) for call in mock_print.call_args_list for arg in call[0])
        self.assertIn("8426", printed)
        self.assertIn("8419", printed)


if __name__ == "__main__":
    unittest.main()
