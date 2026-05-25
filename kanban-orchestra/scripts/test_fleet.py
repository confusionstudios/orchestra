#!/usr/bin/env python3
"""Focused tests for ko-fleet operator replacement flows."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
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
            self.assertEqual(columns[-2], "-")
            self.assertEqual(columns[-1], str(root))

    def test_start_launches_orchestrator_from_selected_repo_root(self):
        repo = fleet.FleetRepo("repo", Path("/tmp/repo"), Path("/tmp/repo"))

        with patch.object(fleet, "require_tool") as require_tool, \
             patch.object(fleet, "require_startable") as require_startable, \
             patch.object(fleet, "orchestra_bin", return_value=Path("/opt/orchestra/bin/ko-orchestrator")), \
             patch.object(fleet, "repo_process_state", return_value=("stopped", "-", "-", "-")), \
             patch.object(fleet.subprocess, "run") as run, \
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
            ],
            check=True,
        )

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
             patch.object(fleet, "repo_process_state", return_value=("running-external", "123", "-", "-")), \
             patch.object(fleet.subprocess, "run") as run, \
             redirect_stdout(out):
            fleet.stop([repo])

        run.assert_not_called()
        self.assertIn("running outside fleet tmux session", out.getvalue())

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
