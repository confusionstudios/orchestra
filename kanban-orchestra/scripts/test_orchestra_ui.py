#!/usr/bin/env python3
"""Tests for the Textual orchestra-ui quit flow."""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "kanban-orchestra" / "scripts"
UI_PATH = REPO_ROOT / "orchestra-ui" / "orchestra-ui.py"

os.environ["ORCHESTRA_DIR"] = str(REPO_ROOT)
sys.path.insert(0, str(SCRIPT_DIR))


def _load_ui_module():
    spec = importlib.util.spec_from_file_location("orchestra_ui_test_module", UI_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["orchestra_ui_test_module"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


orchestra_ui = _load_ui_module()


class TestLogColorizer(unittest.TestCase):
    def test_picked_up_lines_are_blue(self):
        colored = orchestra_ui._colorize("Picked up: 'Allowed task' (step=commit-make)")

        self.assertIn("[blue]Picked up: 'Allowed task' (step=commit-make)[/blue]", colored)

    def test_warning_error_and_done_colors_are_preserved(self):
        self.assertIn("[red]Picked up after error[/red]", orchestra_ui._colorize("Picked up after error"))
        self.assertIn("[yellow]Picked up warning[/yellow]", orchestra_ui._colorize("Picked up warning"))
        self.assertIn("[green]Task done[/green]", orchestra_ui._colorize("Task done"))


class TestGitBranchReadout(unittest.TestCase):
    def test_current_git_branch_uses_show_current(self):
        with patch.object(orchestra_ui.subprocess, "run", return_value=SimpleNamespace(stdout="feature-x\n")) as run:
            branch = orchestra_ui._current_git_branch(Path("/repo"))

        self.assertEqual(branch, "feature-x")
        run.assert_called_once_with(
            ["git", "branch", "--show-current"],
            cwd=Path("/repo"),
            capture_output=True,
            text=True,
            check=True,
        )

    def test_current_git_branch_falls_back_when_detached(self):
        with patch.object(orchestra_ui.subprocess, "run", return_value=SimpleNamespace(stdout="\n")):
            self.assertEqual(orchestra_ui._current_git_branch(Path("/repo")), "unknown")

    def test_current_git_branch_falls_back_on_git_failure(self):
        error = orchestra_ui.subprocess.CalledProcessError(1, ["git", "branch", "--show-current"])

        with patch.object(orchestra_ui.subprocess, "run", side_effect=error):
            self.assertEqual(orchestra_ui._current_git_branch(Path("/repo")), "unknown")

    def test_current_git_branch_label_is_compact(self):
        with patch.object(orchestra_ui, "_current_git_branch", return_value="feature-x"):
            self.assertEqual(orchestra_ui._current_git_branch_label(Path("/repo")), "branch: feature-x")


class FakeProcess:
    def __init__(self, status="stopped", cmd=None, name="orchestrator", pid=111):
        self.status = status
        self.cmd = cmd
        self.name = name
        self.pid = pid if status == "running" else None
        self.status_detail = f"PID {self.pid}" if self.pid else ""
        self.killed = False
        self.started = False
        self.browser_url = None

    def kill(self):
        self.killed = True
        self.status = "stopped"
        self.pid = None
        self.status_detail = ""

    def start(self):
        self.started = True
        self.status = "running"
        self.pid = self.pid or 222
        self.status_detail = f"PID {self.pid}"


class AppHarness:
    def __init__(self, processes):
        self.processes = processes
        self.exited = False
        self.pushed_screen = None
        self.pushed_callback = None

    def exit(self):
        self.exited = True

    def push_screen(self, screen, callback):
        self.pushed_screen = screen
        self.pushed_callback = callback


class TestQuitModal(unittest.TestCase):
    def test_cancel_button_dismisses_without_choice(self):
        modal = orchestra_ui.QuitModal()
        event = SimpleNamespace(button=SimpleNamespace(id="cancel-quit"))

        with patch.object(modal, "dismiss") as dismiss:
            modal.on_button_pressed(event)

        dismiss.assert_called_once_with(None)

    def test_existing_quit_choices_are_preserved(self):
        modal = orchestra_ui.QuitModal()

        for button_id, expected_result in (
            ("kill-quit", "kill"),
            ("leave-quit", "leave"),
        ):
            event = SimpleNamespace(button=SimpleNamespace(id=button_id))
            with self.subTest(button_id=button_id), patch.object(modal, "dismiss") as dismiss:
                modal.on_button_pressed(event)
                dismiss.assert_called_once_with(expected_result)


class TestOrchestraAppQuitFlow(unittest.TestCase):
    def test_quit_exits_immediately_when_no_managed_processes_are_running(self):
        app = AppHarness(
            [
                FakeProcess("stopped", cmd=["orchestrator"]),
                FakeProcess("exited(0)", cmd=["dashboard"]),
                FakeProcess("running", cmd=None),
            ]
        )

        orchestra_ui.OrchestraApp._do_quit(app)

        self.assertTrue(app.exited)
        self.assertIsNone(app.pushed_screen)

    def test_quit_dialog_cancel_keeps_running_processes_alive(self):
        orchestrator = FakeProcess("running", cmd=["orchestrator"])
        dashboard = FakeProcess("stopped", cmd=["dashboard"])
        app = AppHarness([orchestrator, dashboard])

        orchestra_ui.OrchestraApp._do_quit(app)

        self.assertFalse(app.exited)
        self.assertIsInstance(app.pushed_screen, orchestra_ui.QuitModal)

        app.pushed_callback(None)

        self.assertFalse(app.exited)
        self.assertFalse(orchestrator.killed)
        self.assertFalse(dashboard.killed)

    def test_quit_dialog_kill_and_leave_choices_still_exit(self):
        for choice in ("kill", "leave"):
            with self.subTest(choice=choice):
                orchestrator = FakeProcess("running", cmd=["orchestrator"])
                app = AppHarness([orchestrator])

                orchestra_ui.OrchestraApp._do_quit(app)
                app.pushed_callback(choice)

                self.assertTrue(app.exited)
                self.assertEqual(orchestrator.killed, choice == "kill")


class TestOrchestratorControl(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "kanban.db")
        self.conn = orchestra_ui.kanban_db.connect(self.db_path)
        self.old_kanban_db = os.environ.get("KANBAN_DB")
        os.environ["KANBAN_DB"] = self.db_path

    def tearDown(self):
        self.conn.close()
        if self.old_kanban_db is None:
            os.environ.pop("KANBAN_DB", None)
        else:
            os.environ["KANBAN_DB"] = self.old_kanban_db
        self.tmpdir.cleanup()

    def _app(self, orchestrator_status="stopped"):
        app = orchestra_ui.OrchestraApp(auto_start=False)
        app.processes = [
            FakeProcess(orchestrator_status, cmd=["orchestrator"], name="orchestrator"),
            FakeProcess("stopped", cmd=["dashboard"], name="dashboard"),
        ]
        return app

    def test_start_refuses_duplicate_supervised_orchestrator(self):
        app = self._app("running")

        response = app._execute_control_command("start")

        self.assertFalse(response["ok"])
        self.assertIn("already running", response["message"])

    def test_start_launches_one_supervised_orchestrator(self):
        app = self._app("stopped")

        with patch.object(orchestra_ui.orchestrator_control, "singleton_lock_available", return_value=True):
            response = app._execute_control_command("start")

        self.assertTrue(response["ok"])
        self.assertTrue(app.processes[0].started)
        runtime = orchestra_ui.kanban_db.get_runtime(self.conn)
        self.assertEqual(runtime["status"], "starting")
        self.assertEqual(runtime["current_step"], "none")

    def test_start_refuses_when_task_is_still_running_after_stop(self):
        task_id = orchestra_ui.kanban_db.add_task(self.conn, "Interrupted task", branch="feat-x")
        orchestra_ui.kanban_db.update_task(self.conn, task_id, status="running")
        app = self._app("stopped")

        response = app._execute_control_command("start")

        self.assertFalse(response["ok"])
        self.assertIn("status=running", response["message"])
        self.assertFalse(app.processes[0].started)

    def test_stop_preserves_task_status_and_runtime_active_fields(self):
        task_id = orchestra_ui.kanban_db.add_task(self.conn, "Paused task", branch="feat-pause")
        orchestra_ui.kanban_db.update_task(self.conn, task_id, status="running", next_step="commit-make")
        orchestra_ui.kanban_db.upsert_runtime(
            self.conn,
            status="running",
            pid=111,
            started_at="CURRENT_TIMESTAMP",
            last_heartbeat_at="CURRENT_TIMESTAMP",
            current_task_id=task_id,
            current_step="commit-make",
            current_branch="feat-pause",
            review_round=0,
            active_agents=1,
            status_message="Working",
        )
        app = self._app("running")

        with patch.object(orchestra_ui.orchestrator_control, "kill_active_agents", return_value=[{"pid": 444}]):
            response = app._execute_control_command("stop")

        self.assertTrue(response["ok"])
        self.assertTrue(app.processes[0].killed)
        task = orchestra_ui.kanban_db.get_task(self.conn, task_id)
        self.assertEqual(task["status"], "running")
        runtime = orchestra_ui.kanban_db.get_runtime(self.conn)
        self.assertEqual(runtime["status"], "stopped")
        self.assertEqual(runtime["current_task_id"], task_id)
        self.assertEqual(runtime["current_step"], "commit-make")
        self.assertIn("preserved", runtime["status_message"])

    def test_break_blocks_running_child_clears_runtime_and_markers(self):
        parent_id = orchestra_ui.kanban_db.add_task(self.conn, "Parent", kind="supertask")
        child_id = orchestra_ui.kanban_db.add_task(
            self.conn,
            "Child",
            branch="feat-break",
            parent_task_id=parent_id,
            sequence_index=100,
        )
        orchestra_ui.kanban_db.update_task(self.conn, parent_id, status="pending_subtasks", next_step="none")
        orchestra_ui.kanban_db.update_task(self.conn, child_id, status="running", next_step="commit-make")
        orchestra_ui.kanban_db.upsert_runtime(
            self.conn,
            status="running",
            pid=111,
            started_at="CURRENT_TIMESTAMP",
            last_heartbeat_at="CURRENT_TIMESTAMP",
            current_task_id=child_id,
            current_step="commit-make",
            current_branch="feat-break",
            review_round=2,
            active_agents=1,
            status_message="Working",
        )
        stop_marker = Path(self.db_path).resolve().parent / "KANBAN_ORCHESTRATOR_STOP_AFTER_TASK"
        stop_marker.write_text("", encoding="utf-8")
        app = self._app("running")

        with patch.object(orchestra_ui.orchestrator_control, "kill_active_agents", return_value=[{"pid": 444}]):
            response = app._execute_control_command("break")

        self.assertTrue(response["ok"])
        self.assertTrue(app.processes[0].killed)
        self.assertFalse(stop_marker.exists())
        child = orchestra_ui.kanban_db.get_task(self.conn, child_id)
        parent = orchestra_ui.kanban_db.get_task(self.conn, parent_id)
        self.assertEqual(child["status"], "blocked")
        self.assertEqual(child["next_step"], "none")
        self.assertEqual(parent["status"], "blocked")
        comments = orchestra_ui.kanban_db.get_comments(self.conn, child_id)
        self.assertTrue(any("git/worktree changes were left untouched" in c["message"] for c in comments))
        runtime = orchestra_ui.kanban_db.get_runtime(self.conn)
        self.assertEqual(runtime["status"], "hard-break")
        self.assertIsNone(runtime["current_task_id"])
        self.assertEqual(runtime["current_step"], "none")
        self.assertEqual(runtime["active_agents"], 0)


if __name__ == "__main__":
    unittest.main()
