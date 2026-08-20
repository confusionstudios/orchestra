#!/usr/bin/env python3
"""Focused tests for ko-fleet operator replacement flows."""

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet


def write_runtime(root, **overrides):
    fields = {
        "singleton": 1,
        "status": "idle",
        "current_task_id": None,
        "current_step": "none",
        "active_agents": 0,
    }
    fields.update(overrides)
    conn = sqlite3.connect(root / "kanban-orchestra.db")
    try:
        conn.execute(
            """
            CREATE TABLE orchestrator_runtime (
                singleton INTEGER PRIMARY KEY,
                status TEXT,
                current_task_id INTEGER,
                current_step TEXT,
                active_agents INTEGER
            )
            """
        )
        conn.execute(
            """
            INSERT INTO orchestrator_runtime
                (singleton, status, current_task_id, current_step, active_agents)
            VALUES
                (:singleton, :status, :current_task_id, :current_step, :active_agents)
            """,
            fields,
        )
        conn.commit()
    finally:
        conn.close()


class TestFleetOperatorFlows(unittest.TestCase):
    def test_tailscale_dashboard_url_matches_existing_https_proxy(self):
        status = {
            "TCP": {"8427": {"HTTPS": True}},
            "Web": {
                "node.example.ts.net:8427": {
                    "Handlers": {"/": {"Proxy": "http://127.0.0.1:8427"}}
                }
            },
        }
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(status), stderr="")

        with patch.object(fleet.dashboard_tailscale.shutil, "which", return_value="/usr/bin/tailscale"), \
             patch.object(fleet.dashboard_tailscale, "_run", return_value=result):
            remote = fleet.tailscale_dashboard_url("http://127.0.0.1:8427")

        self.assertEqual(remote, "https://node.example.ts.net:8427/")

    def test_tailscale_dashboard_url_requires_matching_https_proxy(self):
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

        with patch.object(fleet.dashboard_tailscale.shutil, "which", return_value="/usr/bin/tailscale"), \
             patch.object(fleet.dashboard_tailscale, "_run", return_value=result):
            remote = fleet.tailscale_dashboard_url("http://127.0.0.1:8427")

        self.assertIsNone(remote)

    def test_tailscale_dashboard_url_is_absent_without_cli(self):
        with patch.object(fleet.dashboard_tailscale.shutil, "which", return_value=None), \
             patch.object(fleet.dashboard_tailscale, "_run") as run_mock:
            remote = fleet.tailscale_dashboard_url("http://127.0.0.1:8427")

        self.assertIsNone(remote)
        run_mock.assert_not_called()

    def test_tailscale_dashboard_url_is_absent_without_serve_config(self):
        for stdout in ("null", "{}"):
            with self.subTest(stdout=stdout), \
                 patch.object(fleet.dashboard_tailscale.shutil, "which", return_value="/usr/bin/tailscale"), \
                 patch.object(
                     fleet.dashboard_tailscale,
                     "_run",
                     return_value=subprocess.CompletedProcess([], 0, stdout=stdout, stderr=""),
                 ):
                remote = fleet.tailscale_dashboard_url("http://127.0.0.1:8427")

            self.assertIsNone(remote)

    def test_parser_exposes_operator_replacement_commands(self):
        parser = fleet.build_parser()

        for command in (
            "status",
            "precheck",
            "start",
            "stop",
            "stop-all",
            "restart",
            "attach",
            "logs",
            "dashboard",
            "dashboard-open",
        ):
            with self.subTest(command=command):
                argv = [command]
                if command in {"attach", "logs", "dashboard", "dashboard-open"}:
                    argv.append("repo")

                args = parser.parse_args(argv)

                self.assertEqual(args.command, command)

    def test_process_state_ignores_metadata_for_a_different_repo_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            lock_path = root / "kanban-orchestra.lock"
            lock_path.write_text(
                f"role=orchestrator\npid={os.getpid()}\nrepo_root={root / 'other'}\n",
                encoding="utf-8",
            )
            repo = fleet.FleetRepo("repo", root, root)

            with patch.object(fleet, "tmux_has_session", return_value=False):
                state, orch_pid, dashboard_pid, session = fleet.repo_process_state(repo)

            self.assertEqual(state, "stopped")
            self.assertEqual(orch_pid, "-")
            self.assertEqual(dashboard_pid, "-")
            self.assertEqual(session, "-")

    def test_status_prints_dashboard_url_when_metadata_is_live(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            runtime = root / ".kanban-orchestra"
            runtime.mkdir()
            (root / "kanban-orchestra.lock").write_text(
                f"role=orchestrator\npid={os.getpid()}\nrepo_root={root}\n",
                encoding="utf-8",
            )
            (runtime / "dashboard.json").write_text(
                json.dumps(
                    {
                        "role": "dashboard",
                        "pid": os.getpid(),
                        "repo_root": str(root),
                        "host": "127.0.0.1",
                        "port": 8427,
                        "url": "http://127.0.0.1:8427",
                    }
                ),
                encoding="utf-8",
            )
            write_runtime(root, status="idle", current_step="none", active_agents=0)
            repo = fleet.FleetRepo("repo", root, root)
            out = io.StringIO()

            with patch.object(fleet, "tmux_has_session", return_value=False), redirect_stdout(out):
                fleet.print_status([repo])

            text = out.getvalue()
            self.assertIn("orch/dash", text.splitlines()[0])
            self.assertIn("tmux", text.splitlines()[0])
            self.assertIn(f"{os.getpid()}/{os.getpid()}", text)
            self.assertIn("running/idle", text)
            self.assertIn("dash_url", text)
            self.assertIn("http://127.0.0.1:8427", text)

    def test_status_summarizes_the_current_managed_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            (root / "kanban-orchestra.lock").write_text(
                f"role=orchestrator\npid={os.getpid()}\nrepo_root={root}\n",
                encoding="utf-8",
            )
            runtime = root / ".kanban-orchestra"
            runtime.mkdir()
            (runtime / "dashboard.json").write_text(
                json.dumps(
                    {
                        "role": "dashboard",
                        "pid": os.getpid(),
                        "repo_root": str(root),
                        "url": "http://127.0.0.1:8427",
                    }
                ),
                encoding="utf-8",
            )
            write_runtime(root, status="idle", current_step="none", active_agents=0)
            repo = fleet.FleetRepo("repo", root, root)
            out = io.StringIO()

            with patch.object(fleet, "tmux_has_session", return_value=False), \
                 patch.object(fleet, "current_repo_root", return_value=root), \
                 patch.object(fleet, "preferred_dashboard_url", side_effect=lambda url, **kwargs: url), \
                 redirect_stdout(out):
                fleet.print_status([repo])

            self.assertIn(
                "This repo (repo) is running/idle. Dashboard: http://127.0.0.1:8427",
                out.getvalue(),
            )
            self.assertNotIn("Remote:", out.getvalue())
            self.assertNotIn("Dash:", out.getvalue())

    def test_status_summarizes_current_repo_preferred_dashboard_below_table(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            (root / "kanban-orchestra.lock").write_text(
                f"role=orchestrator\npid={os.getpid()}\nrepo_root={root}\n",
                encoding="utf-8",
            )
            runtime = root / ".kanban-orchestra"
            runtime.mkdir()
            (runtime / "dashboard.json").write_text(
                json.dumps(
                    {
                        "role": "dashboard",
                        "pid": os.getpid(),
                        "repo_root": str(root),
                        "url": "http://127.0.0.1:8427",
                    }
                ),
                encoding="utf-8",
            )
            write_runtime(root, status="idle", current_step="none", active_agents=0)
            repo = fleet.FleetRepo("repo", root, root)
            out = io.StringIO()

            with patch.object(fleet, "tmux_has_session", return_value=False), \
                 patch.object(fleet, "current_repo_root", return_value=root), \
                 patch.object(
                     fleet,
                     "preferred_dashboard_url",
                     return_value="https://node.example.ts.net:8427/",
                 ), \
                 redirect_stdout(out):
                fleet.print_status([repo])

            lines = out.getvalue().splitlines()
            self.assertNotIn("Remote", lines[0])
            self.assertIn("http://127.0.0.1:8427", lines[2])
            self.assertIn(
                "This repo (repo) is running/idle. Dashboard: https://node.example.ts.net:8427/",
                out.getvalue(),
            )
            self.assertNotIn("Remote:", out.getvalue())
            self.assertNotIn("Dash:", out.getvalue())

    def test_status_does_not_summarize_an_unmanaged_current_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            repo = fleet.FleetRepo("repo", root, root, managed=False)
            out = io.StringIO()

            with patch.object(fleet, "current_repo_root", return_value=root), redirect_stdout(out):
                fleet.print_status([repo])

            self.assertNotIn("This repo", out.getvalue())

    def test_process_state_reports_running_busy_from_runtime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            (root / "kanban-orchestra.lock").write_text(
                f"role=orchestrator\npid={os.getpid()}\nrepo_root={root}\n",
                encoding="utf-8",
            )
            write_runtime(
                root,
                status="running",
                current_task_id=7,
                current_step="commit-make",
                active_agents=1,
            )
            repo = fleet.FleetRepo("repo", root, root)

            with patch.object(fleet, "tmux_has_session", return_value=False):
                state, orch_pid, dashboard_pid, session = fleet.repo_process_state(repo)

            self.assertEqual(state, "running/busy")
            self.assertEqual(orch_pid, str(os.getpid()))
            self.assertEqual(dashboard_pid, "-")
            self.assertEqual(session, "-")

    def test_status_hides_dashboard_url_when_orchestrator_is_stopped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            runtime = root / ".kanban-orchestra"
            runtime.mkdir()
            (runtime / "dashboard.json").write_text(
                json.dumps(
                    {
                        "role": "dashboard",
                        "pid": os.getpid(),
                        "repo_root": str(root),
                        "url": "http://127.0.0.1:8427",
                    }
                ),
                encoding="utf-8",
            )
            repo = fleet.FleetRepo("repo", root, root)
            out = io.StringIO()

            with patch.object(fleet, "tmux_has_session", return_value=False), redirect_stdout(out):
                fleet.print_status([repo])

            line = out.getvalue().splitlines()[2]
            self.assertIn("stopped", line)
            self.assertNotIn("http://127.0.0.1:8427", line)

    def test_status_prints_missing_dashboard_in_combined_process_column(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            (root / "kanban-orchestra.lock").write_text(
                f"role=orchestrator\npid={os.getpid()}\nrepo_root={root}\n",
                encoding="utf-8",
            )
            write_runtime(root, status="idle", current_step="none", active_agents=0)
            repo = fleet.FleetRepo("repo", root, root)
            out = io.StringIO()

            with patch.object(fleet, "tmux_has_session", return_value=False), redirect_stdout(out):
                fleet.print_status([repo])

            lines = out.getvalue().splitlines()
            self.assertIn("orch/dash", lines[0])
            self.assertIn("dash_url", lines[0])
            self.assertIn("running/idle", lines[2])
            self.assertIn(f"{os.getpid()}/-", lines[2])
            columns = lines[2].split()
            self.assertEqual(columns[-3], "-")
            self.assertEqual(columns[-2], "managed")
            self.assertEqual(columns[-1], str(root))

    def test_status_prints_invalid_repo_error_outside_tmux_column(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir).resolve() / "missing"
            repo = fleet.FleetRepo("missing", path, None, "path does not exist")
            out = io.StringIO()

            with redirect_stdout(out):
                fleet.print_status([repo])

            lines = out.getvalue().splitlines()
            self.assertIn("tmux", lines[0])
            self.assertIn("invalid", lines[2])
            self.assertIn("-/-", lines[2])
            self.assertIn("path does not exist", lines[2])
            self.assertNotIn("path does not exist", lines[2].split()[3])

    def test_discover_running_repos_marks_live_processes_as_unmanaged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            (root / "kanban-orchestra.lock").write_text(
                f"role=orchestrator\npid={os.getpid()}\nrepo_root={root}\n",
                encoding="utf-8",
            )

            with patch.object(
                fleet,
                "running_orchestrator_process_roots",
                return_value=[(os.getpid(), root)],
            ):
                repos = fleet.discover_running_repos()

            self.assertEqual(len(repos), 1)
            self.assertEqual(repos[0].root, root)
            self.assertFalse(repos[0].managed)

    def test_status_repos_adds_unmanaged_running_instances_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir).resolve()
            managed_root = base / "managed"
            unmanaged_root = base / "unmanaged"
            managed_root.mkdir()
            unmanaged_root.mkdir()
            managed = fleet.FleetRepo("managed", managed_root, managed_root)
            unmanaged = fleet.FleetRepo(
                "unmanaged",
                unmanaged_root,
                unmanaged_root,
                managed=False,
            )
            duplicate_running = fleet.FleetRepo(
                "managed",
                managed_root,
                managed_root,
                managed=False,
            )

            with patch.object(fleet, "load_status_repos", return_value=[managed]), \
                 patch.object(fleet, "discover_running_repos", return_value=[duplicate_running, unmanaged]):
                repos = fleet.status_repos([])

            self.assertEqual([repo.root for repo in repos], [managed_root, unmanaged_root])
            self.assertTrue(repos[0].managed)
            self.assertFalse(repos[1].managed)

    def test_status_prints_unmanaged_owner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            (root / "kanban-orchestra.lock").write_text(
                f"role=orchestrator\npid={os.getpid()}\nrepo_root={root}\n",
                encoding="utf-8",
            )
            repo = fleet.FleetRepo("repo", root, root, managed=False)
            out = io.StringIO()

            with patch.object(fleet, "tmux_has_session", return_value=False), redirect_stdout(out):
                fleet.print_status([repo])

            text = out.getvalue()
            self.assertIn("owner", text.splitlines()[0])
            self.assertIn("unmanaged", text.splitlines()[2])

    def test_request_dashboard_start_creates_presence_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            repo = fleet.FleetRepo("repo", root, root)

            request_path = fleet.request_dashboard_start(repo)

            self.assertEqual(request_path.name, "dashboard-start-request")
            self.assertEqual(request_path.read_text(encoding="utf-8"), "start\n")

    def test_request_dashboard_start_can_include_preferred_port(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            repo = fleet.FleetRepo("repo", root, root)

            request_path = fleet.request_dashboard_start(repo, preferred_port=8433)

            self.assertEqual(
                request_path.read_text(encoding="utf-8"),
                "start\nport=8433\n",
            )

    def test_process_state_reports_running_without_dashboard_for_external_orchestrator(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            (root / "kanban-orchestra.lock").write_text(
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

    def test_status_abbreviates_home_repo_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir).resolve()
            root = home / "Documents" / "repo"
            root.mkdir(parents=True)
            repo = fleet.FleetRepo("repo", root, root)
            out = io.StringIO()

            with patch.dict(os.environ, {"HOME": str(home)}), \
                 patch.object(fleet, "tmux_has_session", return_value=False), \
                 redirect_stdout(out):
                fleet.print_status([repo])

            text = out.getvalue()
            self.assertIn("~/Documents/repo", text)
            self.assertNotIn(str(root), text)

    def test_start_launches_orchestrator_from_selected_repo_root(self):
        repo = fleet.FleetRepo("repo", Path("/tmp/repo"), Path("/tmp/repo"))

        with patch.object(fleet, "require_tool") as require_tool, \
             patch.object(fleet, "dirty_lines", return_value=[]), \
             patch.object(fleet, "orchestra_bin", return_value=Path("/opt/orchestra/bin/ko-orchestrator")), \
             patch.object(fleet, "repo_process_state", return_value=("stopped", "-", "-", "-")), \
             patch.object(fleet.subprocess, "run") as run, \
             patch.object(fleet, "wait_dashboard_ready", return_value=True), \
             patch.object(fleet.time, "sleep"), \
             patch.object(fleet, "print_status"):
            fleet.start([repo])

        require_tool.assert_called_once_with("tmux")
        run.assert_called_once_with(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                "orch-repo",
                "-c",
                "/tmp/repo",
                "/opt/orchestra/bin/ko-orchestrator",
                "--dashboard-port",
                "8427",
            ],
            check=True,
        )

    def test_start_assigns_distinct_dashboard_ports(self):
        repos = [
            fleet.FleetRepo("one", Path("/tmp/one"), Path("/tmp/one")),
            fleet.FleetRepo("two", Path("/tmp/two"), Path("/tmp/two")),
        ]

        with patch.object(fleet, "require_tool"), \
             patch.object(fleet, "dirty_lines", return_value=[]), \
             patch.object(fleet, "orchestra_bin", return_value=Path("/opt/orchestra/bin/ko-orchestrator")), \
             patch.object(fleet, "repo_process_state", return_value=("stopped", "-", "-", "-")), \
             patch.object(fleet.subprocess, "run") as run, \
             patch.object(fleet, "wait_dashboard_ready", return_value=True), \
             patch.object(fleet.time, "sleep"), \
             patch.object(fleet, "print_status"):
            fleet.start(repos)

        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["--dashboard-port", "8427"], [cmd[-2:] for cmd in commands])
        self.assertIn(["--dashboard-port", "8428"], [cmd[-2:] for cmd in commands])

    def test_start_prechecks_only_repos_that_will_launch(self):
        running = fleet.FleetRepo("running", Path("/tmp/running"), Path("/tmp/running"))
        stopped = fleet.FleetRepo("stopped", Path("/tmp/stopped"), Path("/tmp/stopped"))

        with patch.object(fleet, "require_tool"), \
             patch.object(fleet, "dirty_lines", return_value=[]) as dirty_lines, \
             patch.object(fleet, "orchestra_bin", return_value=Path("/opt/orchestra/bin/ko-orchestrator")), \
             patch.object(
                 fleet,
                 "repo_process_state",
                 side_effect=[
                     ("running/idle", "123", "456", "orch-running"),
                     ("stopped", "-", "-", "-"),
                 ],
             ), \
             patch.object(fleet.subprocess, "run") as run, \
             patch.object(fleet, "wait_dashboard_ready", return_value=True), \
             patch.object(fleet.time, "sleep"), \
             patch.object(fleet, "print_status"):
            fleet.start([running, stopped])

        dirty_lines.assert_called_once_with(stopped)
        run.assert_called_once()

    def test_start_skips_dirty_stopped_repos_and_launches_clean_ones(self):
        dirty = fleet.FleetRepo("dirty", Path("/tmp/dirty"), Path("/tmp/dirty"))
        clean = fleet.FleetRepo("clean", Path("/tmp/clean"), Path("/tmp/clean"))
        err = io.StringIO()

        def fake_dirty_lines(repo):
            return [" M file.txt"] if repo.label == "dirty" else []

        with patch.object(fleet, "require_tool"), \
             patch.object(fleet, "dirty_lines", side_effect=fake_dirty_lines), \
             patch.object(fleet, "orchestra_bin", return_value=Path("/opt/orchestra/bin/ko-orchestrator")), \
             patch.object(fleet, "repo_process_state", return_value=("stopped", "-", "-", "-")), \
             patch.object(fleet.subprocess, "run") as run, \
             patch.object(fleet, "wait_dashboard_ready", return_value=True), \
             patch.object(fleet.time, "sleep"), \
             patch.object(fleet, "print_status"), \
             redirect_stderr(err):
            fleet.start([dirty, clean])

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][commands[0].index("-c") + 1], "/tmp/clean")
        self.assertIn("skipped dirty repo", err.getvalue())
        self.assertIn("dirty", err.getvalue())
        self.assertIn("M file.txt", err.getvalue())

    def test_start_reports_all_invalid_repos_before_launching(self):
        repos = [
            fleet.FleetRepo("missing-one", Path("/tmp/missing-one"), None, "path does not exist"),
            fleet.FleetRepo("missing-two", Path("/tmp/missing-two"), None, "path is not a directory"),
        ]
        err = io.StringIO()

        with patch.object(fleet, "require_tool"), \
             patch.object(fleet, "orchestra_bin", return_value=Path("/opt/orchestra/bin/ko-orchestrator")), \
             patch.object(fleet, "repo_process_state") as repo_process_state, \
             redirect_stderr(err), \
             self.assertRaises(SystemExit):
            fleet.start(repos)

        repo_process_state.assert_not_called()
        self.assertIn("missing-one", err.getvalue())
        self.assertIn("missing-two", err.getvalue())

    def test_start_waits_for_each_dashboard_before_next_repo(self):
        repos = [
            fleet.FleetRepo("one", Path("/tmp/one"), Path("/tmp/one")),
            fleet.FleetRepo("two", Path("/tmp/two"), Path("/tmp/two")),
        ]
        events = []

        def fake_run(args, check=False):
            events.append(f"run:{args[args.index('-c') + 1]}")

        def fake_wait(repo):
            events.append(f"wait:{repo.label}")
            return True

        with patch.object(fleet, "require_tool"), \
             patch.object(fleet, "dirty_lines", return_value=[]), \
             patch.object(fleet, "orchestra_bin", return_value=Path("/opt/orchestra/bin/ko-orchestrator")), \
             patch.object(fleet, "repo_process_state", return_value=("stopped", "-", "-", "-")), \
             patch.object(fleet.subprocess, "run", side_effect=fake_run), \
             patch.object(fleet, "wait_dashboard_ready", side_effect=fake_wait), \
             patch.object(fleet.time, "sleep"), \
             patch.object(fleet, "print_status"):
            fleet.start(repos)

        self.assertEqual(events, ["run:/tmp/one", "wait:one", "run:/tmp/two", "wait:two"])

    def test_start_requests_dashboard_for_running_repo_without_dashboard(self):
        repo = fleet.FleetRepo("repo", Path("/tmp/repo"), Path("/tmp/repo"))
        out = io.StringIO()

        with patch.object(fleet, "require_tool"), \
             patch.object(fleet, "orchestra_bin", return_value=Path("/opt/orchestra/bin/ko-orchestrator")), \
             patch.object(fleet, "repo_process_state", return_value=("running/idle", "123", "-", "orch-repo")), \
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

    def test_stop_stops_fleet_owned_session(self):
        repo = fleet.FleetRepo("repo", Path("/tmp/repo"), Path("/tmp/repo"))

        with patch.object(fleet, "require_tool"), \
             patch.object(fleet, "tmux_has_session", side_effect=[True, False, False]), \
             patch.object(fleet.subprocess, "run") as run:
            fleet.stop([repo])

        run.assert_called_once_with(["tmux", "send-keys", "-t", "orch-repo", "C-c"], check=False)

    def test_stop_reports_external_repo_instance_without_killing_it(self):
        repo = fleet.FleetRepo("repo", Path("/tmp/repo"), Path("/tmp/repo"))
        out = io.StringIO()

        with patch.object(fleet, "require_tool"), \
             patch.object(fleet, "tmux_has_session", return_value=False), \
             patch.object(fleet, "repo_process_state", return_value=("running/busy", "123", "-", "-")), \
             patch.object(fleet.subprocess, "run") as run, \
             redirect_stdout(out):
            fleet.stop([repo])

        run.assert_not_called()
        self.assertIn("running outside fleet tmux session", out.getvalue())

    def test_stop_reports_stale_lock_pid_as_not_running(self):
        repo = fleet.FleetRepo("repo", Path("/tmp/repo"), Path("/tmp/repo"))
        out = io.StringIO()

        with patch.object(fleet, "require_tool"), \
             patch.object(fleet, "tmux_has_session", return_value=False), \
             patch.object(fleet, "repo_process_state", return_value=("stopped", "123", "-", "-")), \
             patch.object(fleet.subprocess, "run") as run, \
             redirect_stdout(out):
            fleet.stop([repo])

        run.assert_not_called()
        self.assertIn("repo: not running", out.getvalue())
        self.assertNotIn("running outside fleet tmux session", out.getvalue())

    def test_stop_all_stops_managed_fleet_session(self):
        repo = fleet.FleetRepo("repo", Path("/tmp/repo"), Path("/tmp/repo"))
        out = io.StringIO()

        with patch.object(fleet, "tmux_has_session", side_effect=[True, False, False]), \
             patch.object(fleet.subprocess, "run") as run, \
             redirect_stdout(out):
            fleet.stop_all([repo])

        run.assert_called_once_with(["tmux", "send-keys", "-t", "orch-repo", "C-c"], check=False)
        self.assertIn("repo [managed]: stopped fleet tmux session", out.getvalue())

    def test_stop_all_stops_unmanaged_running_instance(self):
        repo = fleet.FleetRepo("repo", Path("/tmp/repo"), Path("/tmp/repo"), managed=False)
        out = io.StringIO()

        with patch.object(fleet, "repo_process_state", return_value=("running/busy", "123", "-", "-")), \
             patch.object(fleet, "validated_orchestrator_pid", return_value=123), \
             patch.object(fleet, "stop_pid", return_value=True) as stop_pid, \
             redirect_stdout(out):
            fleet.stop_all([repo])

        stop_pid.assert_called_once_with(123)
        self.assertIn("repo [unmanaged]: stopped orchestrator 123", out.getvalue())

    def test_stop_all_handles_mixed_managed_and_unmanaged_repos(self):
        managed = fleet.FleetRepo("managed", Path("/tmp/managed"), Path("/tmp/managed"))
        unmanaged = fleet.FleetRepo(
            "unmanaged",
            Path("/tmp/unmanaged"),
            Path("/tmp/unmanaged"),
            managed=False,
        )
        out = io.StringIO()

        with patch.object(fleet, "tmux_has_session", side_effect=[True, False, False]), \
             patch.object(fleet, "repo_process_state", return_value=("running/busy", "234", "-", "-")), \
             patch.object(fleet, "validated_orchestrator_pid", return_value=234), \
             patch.object(fleet, "stop_pid", return_value=True), \
             patch.object(fleet.subprocess, "run"), \
             redirect_stdout(out):
            fleet.stop_all([managed, unmanaged])

        text = out.getvalue()
        self.assertIn("managed [managed]: stopped fleet tmux session", text)
        self.assertIn("unmanaged [unmanaged]: stopped orchestrator 234", text)

    def test_stop_all_leaves_unvalidated_running_pid_alone(self):
        repo = fleet.FleetRepo("repo", Path("/tmp/repo"), Path("/tmp/repo"), managed=False)
        out = io.StringIO()

        with patch.object(fleet, "repo_process_state", return_value=("running/busy", "999", "-", "-")), \
             patch.object(fleet, "validated_orchestrator_pid", return_value=None), \
             patch.object(fleet, "stop_pid") as stop_pid, \
             redirect_stdout(out):
            fleet.stop_all([repo])

        stop_pid.assert_not_called()
        self.assertIn("repo [unmanaged]: running orchestrator 999 could not be validated; left alone", out.getvalue())

    def test_stop_all_dispatches_to_status_repos(self):
        repo = fleet.FleetRepo("repo", Path("/tmp/repo"), Path("/tmp/repo"), managed=False)

        with patch.object(fleet, "status_repos", return_value=[repo]) as status_repos, \
             patch.object(fleet, "stop_all") as stop_all:
            exit_code = fleet.main(["stop-all"])

        self.assertEqual(exit_code, 0)
        status_repos.assert_called_once_with([])
        stop_all.assert_called_once_with([repo])

    def test_attach_uses_selected_repo_session(self):
        repo = fleet.FleetRepo("repo", Path("/tmp/repo"), Path("/tmp/repo"))

        with patch.object(fleet, "require_tool"), \
             patch.object(fleet, "tmux_has_session", return_value=True), \
             patch.object(fleet.os, "execvp", side_effect=RuntimeError("stop")) as execvp, \
             self.assertRaisesRegex(RuntimeError, "stop"):
            fleet.attach(repo)

        execvp.assert_called_once_with("tmux", ["tmux", "attach", "-t", "orch-repo"])

    def test_logs_tails_repo_local_orchestrator_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            runtime = root / ".kanban-orchestra"
            runtime.mkdir()
            log_path = runtime / "orchestrator.log"
            log_path.write_text("started\n", encoding="utf-8")
            repo = fleet.FleetRepo("repo", root, root)

            with patch.object(fleet.os, "execvp", side_effect=RuntimeError("stop")) as execvp, \
                 self.assertRaisesRegex(RuntimeError, "stop"):
                fleet.logs(repo)

            execvp.assert_called_once_with("tail", ["tail", "-f", str(log_path)])

    def test_dashboard_open_alias_dispatches_to_repo_dashboard(self):
        repo = fleet.FleetRepo("repo", Path("/tmp/repo"), Path("/tmp/repo"))

        with patch.object(fleet, "one_repo", return_value=repo) as one_repo, \
             patch.object(fleet, "open_dashboard") as open_dashboard:
            exit_code = fleet.main(["dashboard-open", "repo"])

        self.assertEqual(exit_code, 0)
        one_repo.assert_called_once_with(["repo"])
        open_dashboard.assert_called_once_with(repo, prefer_local=False)

    def test_no_arg_dashboard_dispatches_to_fleet_dashboard(self):
        with patch.object(fleet, "open_or_start_fleet_dashboard") as open_fleet, \
             patch.object(fleet, "open_dashboard") as open_dashboard:
            exit_code = fleet.main(["dashboard"])

        self.assertEqual(exit_code, 0)
        open_fleet.assert_called_once_with(prefer_local=False)
        open_dashboard.assert_not_called()

    def test_repo_arg_dashboard_still_opens_repo_dashboard(self):
        repo = fleet.FleetRepo("repo", Path("/tmp/repo"), Path("/tmp/repo"))

        with patch.object(fleet, "one_repo", return_value=repo) as one_repo, \
             patch.object(fleet, "open_dashboard") as open_dashboard, \
             patch.object(fleet, "open_or_start_fleet_dashboard") as open_fleet:
            exit_code = fleet.main(["dashboard", "repo"])

        self.assertEqual(exit_code, 0)
        one_repo.assert_called_once_with(["repo"])
        open_dashboard.assert_called_once_with(repo, prefer_local=False)
        open_fleet.assert_not_called()

    def test_dashboard_open_without_repo_is_rejected(self):
        stderr = io.StringIO()

        with patch.object(fleet, "open_dashboard") as open_dashboard, \
             patch.object(fleet, "open_or_start_fleet_dashboard") as open_fleet, \
             redirect_stderr(stderr), \
             self.assertRaises(SystemExit):
            fleet.main(["dashboard-open"])

        open_dashboard.assert_not_called()
        open_fleet.assert_not_called()
        self.assertIn("repo", stderr.getvalue())

    def test_parser_accepts_dashboard_without_repo(self):
        args = fleet.build_parser().parse_args(["dashboard"])

        self.assertEqual(args.command, "dashboard")
        self.assertIsNone(args.repo)
        self.assertFalse(args.local)

    def test_parser_accepts_dashboard_local_flag(self):
        parser = fleet.build_parser()

        fleet_args = parser.parse_args(["dashboard", "--local"])
        self.assertTrue(fleet_args.local)
        self.assertIsNone(fleet_args.repo)

        repo_args = parser.parse_args(["dashboard", "repo", "--local"])
        self.assertTrue(repo_args.local)
        self.assertEqual(repo_args.repo, "repo")

        open_args = parser.parse_args(["dashboard-open", "--local", "repo"])
        self.assertTrue(open_args.local)
        self.assertEqual(open_args.repo, ["repo"])

    def test_dashboard_local_flag_dispatches_to_openers(self):
        repo = fleet.FleetRepo("repo", Path("/tmp/repo"), Path("/tmp/repo"))

        with patch.object(fleet, "open_or_start_fleet_dashboard") as open_fleet, \
             patch.object(fleet, "open_dashboard") as open_dashboard:
            self.assertEqual(fleet.main(["dashboard", "--local"]), 0)
        open_fleet.assert_called_once_with(prefer_local=True)
        open_dashboard.assert_not_called()

        with patch.object(fleet, "one_repo", return_value=repo), \
             patch.object(fleet, "open_dashboard") as open_dashboard, \
             patch.object(fleet, "open_or_start_fleet_dashboard") as open_fleet:
            self.assertEqual(fleet.main(["dashboard", "--local", "repo"]), 0)
        open_dashboard.assert_called_once_with(repo, prefer_local=True)
        open_fleet.assert_not_called()

        with patch.object(fleet, "one_repo", return_value=repo), \
             patch.object(fleet, "open_dashboard") as open_dashboard:
            self.assertEqual(fleet.main(["dashboard-open", "repo", "--local"]), 0)
        open_dashboard.assert_called_once_with(repo, prefer_local=True)

    def test_parser_help_exposes_no_arg_fleet_dashboard(self):
        parser = fleet.build_parser()
        help_text = parser.format_help()
        stdout = io.StringIO()

        with redirect_stdout(stdout), self.assertRaises(SystemExit):
            parser.parse_args(["dashboard", "--help"])

        dash_help = stdout.getvalue()
        self.assertIn("Fleet Dashboard", help_text)
        self.assertIn("Fleet Dashboard", dash_help)
        self.assertIn("omit to start or open the Fleet Dashboard", dash_help)

    def test_fleet_dashboard_metadata_path_sits_beside_fleet_repos(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repos = Path(tmpdir) / "fleet.repos"
            env = {
                key: value
                for key, value in os.environ.items()
                if key != "KO_FLEET_DASHBOARD_METADATA_PATH"
            }
            env["ORCHESTRA_FLEET_REPOS"] = str(repos)
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(
                    fleet.fleet_dashboard_metadata_path(),
                    repos.with_name("fleet-dashboard.json"),
                )

    def test_start_fleet_dashboard_uses_port_outside_repo_range(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repos = Path(tmpdir) / "fleet.repos"
            env = {
                key: value
                for key, value in os.environ.items()
                if key != "KO_FLEET_DASHBOARD_METADATA_PATH"
            }
            env["ORCHESTRA_FLEET_REPOS"] = str(repos)
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(fleet.subprocess, "Popen") as popen:
                fleet.start_fleet_dashboard_process()

        child_env = popen.call_args.kwargs["env"]
        port = int(child_env["KO_FLEET_DASH_PORT"])
        self.assertLess(port, fleet.config.DASHBOARD_PORT_BASE)
        self.assertEqual(
            Path(child_env["KO_FLEET_DASHBOARD_METADATA_PATH"]),
            Path(tmpdir) / "fleet-dashboard.json",
        )
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_open_or_start_fleet_dashboard_reuses_live_url(self):
        payload = {
            "role": "fleet-dashboard",
            "pid": os.getpid(),
            "host": "127.0.0.1",
            "port": 8426,
            "url": "http://127.0.0.1:8426",
        }

        with patch.object(fleet, "fleet_dashboard_live_payload", return_value=payload), \
             patch.object(fleet, "start_fleet_dashboard_process") as start, \
             patch.object(
                 fleet,
                 "preferred_dashboard_url",
                 return_value="https://node.example.ts.net:8426/",
             ) as preferred, \
             patch.object(fleet, "_open_dashboard_url") as open_url:
            fleet.open_or_start_fleet_dashboard()

        start.assert_not_called()
        preferred.assert_called_once_with("http://127.0.0.1:8426", prefer_local=False)
        open_url.assert_called_once_with("https://node.example.ts.net:8426/")

    def test_open_or_start_fleet_dashboard_starts_when_not_running(self):
        payload = {"url": "http://127.0.0.1:8426"}

        with patch.object(fleet, "fleet_dashboard_live_payload", return_value=None), \
             patch.object(fleet, "start_fleet_dashboard_process") as start, \
             patch.object(fleet, "wait_fleet_dashboard_ready", return_value=payload), \
             patch.object(
                 fleet,
                 "preferred_dashboard_url",
                 side_effect=lambda url, **kwargs: url,
             ) as preferred, \
             patch.object(fleet, "_open_dashboard_url") as open_url:
            fleet.open_or_start_fleet_dashboard()

        start.assert_called_once_with()
        preferred.assert_called_once_with("http://127.0.0.1:8426", prefer_local=False)
        open_url.assert_called_once_with("http://127.0.0.1:8426")

    def test_open_or_start_fleet_dashboard_local_flag_skips_preferred_lookup(self):
        payload = {"url": "http://127.0.0.1:8426"}

        with patch.object(fleet, "fleet_dashboard_live_payload", return_value=payload), \
             patch.object(
                 fleet,
                 "preferred_dashboard_url",
                 return_value="http://127.0.0.1:8426",
             ) as preferred, \
             patch.object(fleet, "_open_dashboard_url") as open_url:
            fleet.open_or_start_fleet_dashboard(prefer_local=True)

        preferred.assert_called_once_with("http://127.0.0.1:8426", prefer_local=True)
        open_url.assert_called_once_with("http://127.0.0.1:8426")

    def test_fleet_dashboard_live_payload_requires_live_endpoint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            meta = Path(tmpdir) / "fleet-dashboard.json"
            meta.write_text(
                json.dumps(
                    {
                        "role": "fleet-dashboard",
                        "pid": os.getpid(),
                        "host": "127.0.0.1",
                        "port": 1,
                        "url": "http://127.0.0.1:1",
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(fleet, "fleet_dashboard_metadata_path", return_value=meta), \
                 patch.object(fleet, "dashboard_endpoint_ready", return_value=False):
                self.assertIsNone(fleet.fleet_dashboard_live_payload())

    def test_fleet_dashboard_live_payload_returns_ready_metadata(self):
        payload = {
            "role": "fleet-dashboard",
            "pid": os.getpid(),
            "host": "127.0.0.1",
            "port": 8426,
            "url": "http://127.0.0.1:8426",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            meta = Path(tmpdir) / "fleet-dashboard.json"
            meta.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(fleet, "fleet_dashboard_metadata_path", return_value=meta), \
                 patch.object(fleet, "pid_alive", return_value=True), \
                 patch.object(fleet, "dashboard_endpoint_ready", return_value=True):
                self.assertEqual(fleet.fleet_dashboard_live_payload(), payload)

    def test_wait_fleet_dashboard_ready_returns_when_live(self):
        payload = {"url": "http://127.0.0.1:8426"}

        with patch.object(fleet, "fleet_dashboard_live_payload", side_effect=[None, payload]), \
             patch.object(fleet.time, "sleep") as sleep:
            result = fleet.wait_fleet_dashboard_ready(timeout=1)

        self.assertEqual(result, payload)
        sleep.assert_called_once_with(0.2)

    def test_open_dashboard_rejects_metadata_for_a_different_repo_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            runtime = root / ".kanban-orchestra"
            runtime.mkdir()
            (runtime / "dashboard.json").write_text(
                json.dumps(
                    {
                        "role": "dashboard",
                        "pid": os.getpid(),
                        "repo_root": str(root / "other"),
                        "host": "127.0.0.1",
                        "port": 8427,
                        "url": "http://127.0.0.1:8427",
                    }
                ),
                encoding="utf-8",
            )
            repo = fleet.FleetRepo("repo", root, root)

            with self.assertRaises(SystemExit):
                fleet.open_dashboard(repo)


if __name__ == "__main__":
    unittest.main()
