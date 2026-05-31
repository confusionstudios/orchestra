#!/usr/bin/env python3
"""Focused tests for ko-fleet operator replacement flows."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet


class TestFleetOperatorFlows(unittest.TestCase):
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
            repo = fleet.FleetRepo("repo", root, root)
            out = io.StringIO()

            with patch.object(fleet, "tmux_has_session", return_value=False), redirect_stdout(out):
                fleet.print_status([repo])

            text = out.getvalue()
            self.assertIn("dash_url", text)
            self.assertIn("http://127.0.0.1:8427", text)

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

    def test_status_prints_dash_without_dashboard_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            repo = fleet.FleetRepo("repo", root, root)
            out = io.StringIO()

            with patch.object(fleet, "tmux_has_session", return_value=False), redirect_stdout(out):
                fleet.print_status([repo])

            lines = out.getvalue().splitlines()
            self.assertIn("dash_url", lines[0])
            columns = lines[2].split()
            self.assertEqual(columns[-3], "-")
            self.assertEqual(columns[-2], "managed")
            self.assertEqual(columns[-1], str(root))

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
             patch.object(fleet, "require_startable") as require_startable, \
             patch.object(fleet, "orchestra_bin", return_value=Path("/opt/orchestra/bin/ko-orchestrator")), \
             patch.object(fleet, "repo_process_state", return_value=("stopped", "-", "-", "-")), \
             patch.object(fleet.subprocess, "run") as run, \
             patch.object(fleet, "wait_dashboard_ready", return_value=True), \
             patch.object(fleet.time, "sleep"), \
             patch.object(fleet, "print_status"):
            fleet.start([repo])

        require_tool.assert_called_once_with("tmux")
        require_startable.assert_called_once_with([repo])
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
             patch.object(fleet, "require_startable"), \
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
             patch.object(fleet, "require_startable") as require_startable, \
             patch.object(fleet, "orchestra_bin", return_value=Path("/opt/orchestra/bin/ko-orchestrator")), \
             patch.object(
                 fleet,
                 "repo_process_state",
                 side_effect=[
                     ("running", "123", "456", "orch-running"),
                     ("stopped", "-", "-", "-"),
                 ],
             ), \
             patch.object(fleet.subprocess, "run") as run, \
             patch.object(fleet, "wait_dashboard_ready", return_value=True), \
             patch.object(fleet.time, "sleep"), \
             patch.object(fleet, "print_status"):
            fleet.start([running, stopped])

        require_startable.assert_called_once_with([stopped])
        run.assert_called_once()

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
             patch.object(fleet, "require_startable"), \
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
             patch.object(fleet, "require_startable"), \
             patch.object(fleet, "orchestra_bin", return_value=Path("/opt/orchestra/bin/ko-orchestrator")), \
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
             patch.object(fleet, "repo_process_state", return_value=("running", "123", "-", "-")), \
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

        with patch.object(fleet, "repo_process_state", return_value=("running", "123", "-", "-")), \
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
             patch.object(fleet, "repo_process_state", return_value=("running", "234", "-", "-")), \
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

        with patch.object(fleet, "repo_process_state", return_value=("running", "999", "-", "-")), \
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
        open_dashboard.assert_called_once_with(repo)

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
