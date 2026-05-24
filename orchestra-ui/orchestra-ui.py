#!/usr/bin/env python3
"""orchestra-ui: TUI manager for dashboard and orchestrator processes."""

import argparse
import collections
import datetime
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.containers import Container, Vertical, Horizontal
from textual.reactive import reactive
from textual.widgets import Button, Footer, Header, RichLog, Static, TextArea
from textual.screen import ModalScreen

ORCHESTRA_DIR = os.environ.get("ORCHESTRA_DIR")
if not ORCHESTRA_DIR:
    print("Error: ORCHESTRA_DIR environment variable is not set.", file=sys.stderr)
    sys.exit(1)

SCRIPTS_DIR = os.path.join(ORCHESTRA_DIR, "kanban-orchestra", "scripts")

if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
import db as kanban_db  # noqa: E402
import orchestrator_control  # noqa: E402

PROCESS_DEFS = [
    {"name": "orchestrator", "cmd": [sys.executable, os.path.join(SCRIPTS_DIR, "orchestrator.py")]},
    {"name": "dashboard",    "cmd": [sys.executable, os.path.join(SCRIPTS_DIR, "dashboard.py")]},
]

MAX_LOG_LINES = 2000
URL_PATTERN = re.compile(r"(https?://[^\s\])>\"']+)")
KO_DASH_PORT_PATTERN = re.compile(r'KO_DASH_PORT",\s*"(\d+)"')
BRANCH_REFRESH_INTERVAL = 5.0


def _launch_repo_root() -> Path:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(result.stdout.strip()).resolve()
    except Exception:
        return Path.cwd().resolve()


WORK_REPO_ROOT = _launch_repo_root()


def _work_repo_label(repo_root: Path = WORK_REPO_ROOT) -> str:
    return repo_root.name or str(repo_root)


def _current_git_branch(repo_root: Path = WORK_REPO_ROOT) -> str:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception:
        return "unknown"
    return result.stdout.strip() or "detached"


def _header_title(repo_root: Path = WORK_REPO_ROOT) -> str:
    return f"Orchestra UI - {_work_repo_label(repo_root)} ({_current_git_branch(repo_root)})"


# ── Log colorizer ─────────────────────────────────────────────────────────────

_ERROR_KEYWORDS    = ("error", "exception", "traceback", "critical", "fatal", "fail")
_WARN_KEYWORDS     = ("warn", "warning", "deprecated", "retry", "retrying", "timeout")
_OK_KEYWORDS       = ("success", "done", "complete", "started", "connected", "ready", "ok")
_PICKED_UP_KEYWORDS = ("picked up",)
_BLOCKING_APP_KEYWORDS = ("bind on address", "address already in use", "port in use")

def _colorize(line: str) -> str:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    lower = line.lower()
    if any(k in lower for k in _ERROR_KEYWORDS):
        content = f"[red]{line}[/red]"
    elif any(k in lower for k in _WARN_KEYWORDS):
        content = f"[yellow]{line}[/yellow]"
    elif any(k in lower for k in _PICKED_UP_KEYWORDS):
        content = f"[blue]{line}[/blue]"
    elif any(k in lower for k in _OK_KEYWORDS):
        content = f"[green]{line}[/green]"
    else:
        content = line
    return f"[dim]{ts}[/dim] {content}"

def _is_blocking_app_error(line: str) -> bool:
    lower = line.lower()
    return any(k in lower for k in _BLOCKING_APP_KEYWORDS)

def _kill_hint() -> str:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    return f"[dim]{ts}[/dim] [bold red]Critical error - use Kill Blocker in the action buttons[/bold red]"


def _process_cwd_from_proc(pid: int) -> Optional[Path]:
    proc_cwd = Path("/proc") / str(pid) / "cwd"
    try:
        return Path(os.readlink(proc_cwd)).resolve()
    except OSError:
        return None


def _process_cwds_from_lsof(pids: list[int]) -> dict[int, Path]:
    if not pids:
        return {}
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", ",".join(str(pid) for pid in pids), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            timeout=1,
        )
    except Exception:
        return {}

    if result.returncode != 0:
        return {}

    cwd_by_pid = {}
    current_pid = None
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            try:
                current_pid = int(line[1:])
            except ValueError:
                current_pid = None
        elif line.startswith("n") and current_pid is not None:
            cwd_by_pid[current_pid] = Path(line[1:]).resolve()
    return cwd_by_pid

# ── Process management ────────────────────────────────────────────────────────

class ManagedProcess:
    def __init__(self, name: str, cmd: list[str], cwd: Path = WORK_REPO_ROOT):
        self.name = name
        self.cmd = cmd
        self.cwd = cwd.resolve()
        self.proc: Optional[subprocess.Popen] = None
        self.lines: collections.deque = collections.deque(maxlen=MAX_LOG_LINES)
        self.browser_url: Optional[str] = self._infer_browser_url()
        self._lock = threading.Lock()
        self._reader_thread: Optional[threading.Thread] = None

    @property
    def pid(self) -> Optional[int]:
        return self.proc.pid if self.proc else None

    @property
    def status_detail(self) -> str:
        return f"PID {self.pid}" if self.pid else ""

    @property
    def status(self) -> str:
        if self.proc is None:
            return "stopped"
        rc = self.proc.poll()
        if rc is None:
            return "running"
        return f"exited({rc})"

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        cmd = self.cmd[:1] + ["-u"] + self.cmd[1:]
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            cwd=self.cwd,
        )
        self._reader_thread = threading.Thread(target=self._read_output, daemon=True)
        self._reader_thread.start()

    def kill(self):
        if self.proc and self.proc.poll() is None:
            self._terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._terminate(force=True)

    def _terminate(self, force: bool = False):
        if not self.proc or self.proc.poll() is not None:
            return
        sig = signal.SIGKILL if force else signal.SIGTERM
        if self.name == "orchestrator":
            try:
                pgid = os.getpgid(self.proc.pid)
                if pgid != os.getpgrp():
                    os.killpg(pgid, sig)
                    return
            except (ProcessLookupError, PermissionError):
                return
            except OSError:
                pass
        if force:
            self.proc.kill()
        else:
            self.proc.terminate()

    def restart(self):
        self.kill()
        self.start()

    def _read_output(self):
        try:
            for line in self.proc.stdout:
                self._capture_browser_url(line)
                with self._lock:
                    self.lines.append(line.rstrip("\n"))
        except Exception:
            pass

    def _capture_browser_url(self, line: str) -> None:
        match = URL_PATTERN.search(line)
        if match:
            self.browser_url = match.group(1).rstrip(").,")

    def _infer_browser_url(self) -> Optional[str]:
        if self.name != "dashboard":
            return None

        port = os.environ.get("KO_DASH_PORT")
        if port:
            return f"http://127.0.0.1:{port}"

        script_path = self.cmd[-1] if self.cmd else None
        if not script_path or not os.path.exists(script_path):
            return "http://127.0.0.1:8427"

        try:
            with open(script_path, "r", encoding="utf-8") as handle:
                script_text = handle.read()
        except OSError:
            return "http://127.0.0.1:8427"

        match = KO_DASH_PORT_PATTERN.search(script_text)
        port = match.group(1) if match else "8427"
        return f"http://127.0.0.1:{port}"

    def get_new_lines(self, since_index: int) -> tuple[list[str], int]:
        with self._lock:
            all_lines = list(self.lines)
        total = len(all_lines)
        return all_lines[since_index:], total

    def find_blocker_pids(self) -> list[int]:
        """Return same-repo PIDs of other processes running the same script."""
        script = self.cmd[-1]
        own_pid = self.pid
        if own_pid is None:
            return []
        try:
            result = subprocess.run(["ps", "-ax"], capture_output=True, text=True)
            candidate_pids = []
            for line in result.stdout.splitlines():
                if script not in line:
                    continue
                parts = line.split()
                if not parts:
                    continue
                try:
                    pid = int(parts[0])
                except ValueError:
                    continue
                if pid == own_pid:
                    continue
                candidate_pids.append(pid)

            cwd_by_pid = {}
            lsof_pids = []
            for pid in candidate_pids:
                proc_cwd = _process_cwd_from_proc(pid)
                if proc_cwd is None:
                    lsof_pids.append(pid)
                else:
                    cwd_by_pid[pid] = proc_cwd
            cwd_by_pid.update(_process_cwds_from_lsof(lsof_pids))
            return [pid for pid in candidate_pids if cwd_by_pid.get(pid) == self.cwd]
        except Exception:
            return []


class LatestRunLogTail(ManagedProcess):
    """
    Pseudo-process that tails the newest agent transcript under
    <repo>/.kanban-orchestra/artifacts/. When a newer .log file appears,
    it transitions to following that one and writes a banner with the path.
    """

    NAME = "latest run log"
    SCAN_INTERVAL = 1.5

    def __init__(self):
        super().__init__(self.NAME, [])
        self._stop_evt = threading.Event()
        self._artifacts_root: Optional[Path] = None
        self._current_file: Optional[Path] = None
        self._last_scan = 0.0

    @property
    def status(self) -> str:
        if self._reader_thread and self._reader_thread.is_alive():
            return "running"
        return "stopped"

    @property
    def pid(self) -> Optional[int]:
        return None

    @property
    def status_detail(self) -> str:
        if self._current_file is None:
            return "(no runs yet)"
        return self._current_file.parent.name  # e.g. "task-7"

    def current_path_display(self) -> Optional[str]:
        if self._current_file is None:
            return None
        try:
            return str(self._current_file.relative_to(WORK_REPO_ROOT))
        except ValueError:
            return str(self._current_file)

    def _resolve_artifacts_root(self) -> Optional[Path]:
        try:
            return kanban_db.get_artifacts_root()
        except Exception:
            return None

    def start(self):
        if self._reader_thread and self._reader_thread.is_alive():
            return
        self._stop_evt.clear()
        self._artifacts_root = self._resolve_artifacts_root()
        self._reader_thread = threading.Thread(target=self._tail_loop, daemon=True)
        self._reader_thread.start()

    def kill(self):
        self._stop_evt.set()
        if self._reader_thread:
            self._reader_thread.join(timeout=2)

    def restart(self):
        self.kill()
        with self._lock:
            self.lines.clear()
        self._current_file = None
        self.start()

    def find_blocker_pids(self) -> list[int]:
        return []

    def _find_latest(self) -> Optional[Path]:
        if not self._artifacts_root or not self._artifacts_root.exists():
            return None
        latest, latest_mtime = None, -1.0
        for p in self._artifacts_root.rglob("*.log"):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m > latest_mtime:
                latest_mtime, latest = m, p
        return latest

    def _banner(self, path: Path) -> str:
        try:
            shown = path.relative_to(WORK_REPO_ROOT)
        except ValueError:
            shown = path
        return f"── now following: {shown} ──"

    def _tail_loop(self):
        fh = None
        try:
            while not self._stop_evt.is_set():
                now = time.monotonic()
                if now - self._last_scan >= self.SCAN_INTERVAL:
                    self._last_scan = now
                    latest = self._find_latest()
                    if latest is not None and latest != self._current_file:
                        if fh is not None:
                            try:
                                fh.close()
                            except Exception:
                                pass
                            fh = None
                        self._current_file = latest
                        with self._lock:
                            self.lines.append(self._banner(latest))
                        try:
                            fh = open(latest, "r", encoding="utf-8", errors="replace")
                        except OSError:
                            fh = None

                if fh is None:
                    time.sleep(0.5)
                    continue

                line = fh.readline()
                if line:
                    with self._lock:
                        self.lines.append(line.rstrip("\n"))
                else:
                    time.sleep(0.3)
        finally:
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass


# ── Modals ────────────────────────────────────────────────────────────────────

class QuitModal(ModalScreen):
    def compose(self) -> ComposeResult:
        with Container(id="quit-modal"):
            yield Static("Quit orchestra-ui?", id="quit-title")
            yield Static("Kill running processes too?", id="quit-subtitle")
            with Horizontal(id="quit-buttons"):
                yield Button("Kill & Quit",         id="kill-quit",   variant="error")
                yield Button("Quit (leave running)", id="leave-quit",  variant="primary")
                yield Button("Cancel",              id="cancel-quit")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "kill-quit":
            self.dismiss("kill")
        elif event.button.id == "leave-quit":
            self.dismiss("leave")
        else:
            self.dismiss(None)


class BrowseModal(ModalScreen):
    """Snapshot of current log in a selectable TextArea."""

    CSS = """
    BrowseModal {
        align: center middle;
    }
    #browse-container {
        width: 90%;
        height: 90%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    #browse-title {
        text-style: bold;
        color: $accent;
        padding-bottom: 1;
    }
    #browse-text {
        height: 1fr;
    }
    #browse-close {
        margin-top: 1;
        width: 100%;
    }
    """

    def __init__(self, title: str, text: str):
        super().__init__()
        self._title = title
        self._text  = text

    def compose(self) -> ComposeResult:
        with Container(id="browse-container"):
            yield Static(f"Browse: {self._title}", id="browse-title")
            yield TextArea(self._text, read_only=True, id="browse-text")
            yield Button("Close", id="browse-close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss()


# ── Widgets ───────────────────────────────────────────────────────────────────

class StatusBar(Static):
    status = reactive("stopped")
    pid    = reactive(None)
    detail = reactive("")

    def __init__(self, process: ManagedProcess, **kwargs):
        super().__init__(**kwargs)
        self.process = process

    def render(self) -> str:
        detail_str = f"  {self.detail}" if self.detail else ""
        if self.status == "running":
            indicator = "[green]●[/green]"
        elif self.status.startswith("exited"):
            indicator = f"[red]● {self.status.upper()}[/red]"
        else:
            indicator = "[dim]○[/dim]"
        return f"{indicator} {self.process.name}{detail_str}"

    def refresh_status(self):
        self.status = self.process.status
        self.pid    = self.process.pid
        self.detail = self.process.status_detail


# ── App ───────────────────────────────────────────────────────────────────────

class OrchestraApp(App):
    TITLE = "Orchestra UI"

    CSS = """
    Screen {
        layout: vertical;
    }

    #topbar {
        height: auto;
        layout: vertical;
        border: solid $primary;
        padding: 0 1;
    }

    #process-tabs {
        height: 3;
        width: 100%;
        layout: horizontal;
    }

    .status-bar {
        height: 3;
        width: 1fr;
        border: solid $surface-lighten-2;
        padding: 0 1;
        margin: 0 1 0 0;
        content-align: center middle;
    }

    .status-bar.selected {
        border: solid $accent;
    }

    #topbar-buttons {
        height: 3;
        width: 100%;
        layout: horizontal;
    }

    #action-buttons-left {
        height: 3;
        width: 1fr;
        layout: horizontal;
        overflow: hidden hidden;
    }

    #action-buttons-left Button {
        width: auto;
        min-width: 8;
        margin: 0 1 0 0;
        content-align: center middle;
        text-align: center;
    }

    #topbar-buttons Button:disabled {
        text-style: bold;
        opacity: 60%;
    }

    #btn-start {
        width: 10;
    }

    #btn-quit {
        width: 8;
        height: 3;
        margin: 0;
    }

    #output-panel {
        height: 1fr;
        border: solid $primary;
        padding: 0 1;
    }

    #output-title {
        text-style: bold;
        color: $accent;
        padding: 1 0 0 0;
    }

    #process-log {
        height: 1fr;
    }

    #quit-modal {
        background: $surface;
        border: thick $primary;
        padding: 2 4;
        width: 68;
        max-width: 90%;
        height: auto;
        align: center middle;
    }

    #quit-title {
        text-style: bold;
        text-align: center;
        padding-bottom: 1;
    }

    #quit-subtitle {
        text-align: center;
        padding-bottom: 2;
    }

    #quit-buttons {
        layout: horizontal;
        height: auto;
        align: center middle;
        width: 100%;
    }

    #quit-buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, auto_start: bool = True):
        super().__init__()
        self.title = _header_title()
        self.processes     = [ManagedProcess(d["name"], d["cmd"]) for d in PROCESS_DEFS]
        self.processes.append(LatestRunLogTail())
        self.selected_index = 0
        self.auto_start    = auto_start
        self._log_indices  = [0] * len(self.processes)
        self._last_branch_refresh = 0.0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="topbar"):
            with Horizontal(id="process-tabs"):
                for i, proc in enumerate(self.processes):
                    yield StatusBar(
                        proc,
                        classes=f"status-bar{'  selected' if i == 0 else ''}",
                        id=f"status-{i}",
                    )
            with Horizontal(id="topbar-buttons"):
                with Horizontal(id="action-buttons-left"):
                    yield Button("Start",             id="btn-start",        variant="success")
                    yield Button("Kill",              id="btn-kill",         variant="error")
                    yield Button("Restart",           id="btn-restart",      variant="warning")
                    yield Button("Open Browser",      id="btn-open-browser", variant="primary")
                    yield Button("Kill Blocker",      id="btn-kill-blocker", variant="error")
                    yield Button("Browse Log",         id="btn-view-log")
                yield Button("Quit",              id="btn-quit")
        with Vertical(id="output-panel"):
            yield Static(f"Output: {self.processes[0].name}", id="output-title")
            yield RichLog(id="process-log", auto_scroll=True, markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        if self.auto_start:
            for proc in self.processes:
                proc.start()
        self._write_control_heartbeat()
        self.set_interval(0.5, self._poll)
        self.query_one("#process-log", RichLog).can_focus = False
        for button in self.query("#topbar-buttons Button"):
            button.can_focus = False
        self._refresh_action_buttons()
        self._refresh_header_title(force=True)
        self.set_focus(None)

    # ── Polling (output only — no ps -ax here) ────────────────────────────────

    def _poll(self) -> None:
        self._write_control_heartbeat()
        self._handle_control_request()
        log = self.query_one("#process-log", RichLog)
        for i, proc in enumerate(self.processes):
            self.query_one(f"#status-{i}", StatusBar).refresh_status()
            new_lines, total = proc.get_new_lines(self._log_indices[i])
            if not new_lines:
                continue
            if i == self.selected_index:
                for line in new_lines:
                    log.write(_colorize(line))
                    if _is_blocking_app_error(line):
                        log.write(_kill_hint())
            self._log_indices[i] = total
        self._refresh_action_buttons()
        self._refresh_output_title()
        self._refresh_header_title()

    def _refresh_header_title(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_branch_refresh < BRANCH_REFRESH_INTERVAL:
            return
        self._last_branch_refresh = now
        self.title = _header_title()

    def _refresh_output_title(self) -> None:
        proc  = self.processes[self.selected_index]
        title = f"Output: {proc.name}"
        if isinstance(proc, LatestRunLogTail):
            path = proc.current_path_display()
            if path:
                title = f"Output: {proc.name} — {path}"
        self.query_one("#output-title", Static).update(title)

    def _dashboard_process(self) -> ManagedProcess:
        for proc in self.processes:
            if proc.name == "dashboard":
                return proc
        return self.processes[-1]

    def _orchestrator_process(self) -> ManagedProcess:
        for proc in self.processes:
            if proc.name == "orchestrator":
                return proc
        return self.processes[0]

    def _runtime_snapshot(self) -> dict | None:
        try:
            conn = kanban_db.connect()
        except Exception:
            return None
        try:
            return kanban_db.get_runtime(conn)
        finally:
            conn.close()

    def _control_status_payload(self) -> dict:
        orchestrator = self._orchestrator_process()
        dashboard = self._dashboard_process()
        return {
            "instance": kanban_db.get_instance_identity(),
            "supervisor_pid": os.getpid(),
            "orchestrator": {
                "status": orchestrator.status,
                "pid": orchestrator.pid,
                "detail": orchestrator.status_detail,
            },
            "dashboard": {
                "status": dashboard.status,
                "pid": dashboard.pid,
                "detail": dashboard.status_detail,
                "browser_url": dashboard.browser_url,
            },
            "runtime": self._runtime_snapshot(),
        }

    def _write_control_heartbeat(self) -> None:
        try:
            orchestrator_control.write_supervisor_heartbeat(self._control_status_payload())
        except Exception:
            pass

    def _handle_control_request(self) -> None:
        request = orchestrator_control.read_control_request()
        if not request:
            return
        request_id = request.get("id") or "unknown"
        command = str(request.get("command") or "").strip().lower()
        try:
            if command not in orchestrator_control.CONTROL_COMMANDS:
                response = {
                    "ok": False,
                    "message": f"Unknown orchestrator control command: {command}",
                }
            else:
                response = self._execute_control_command(command)
        except Exception as exc:
            response = {
                "ok": False,
                "message": f"{command or 'control'} failed: {exc}",
            }
        response.update(
            {
                "id": request_id,
                "command": command,
                "handled_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
        )
        orchestrator_control.write_control_response(response)
        orchestrator_control.clear_control_request()

    def _execute_control_command(self, command: str) -> dict:
        if command == "status":
            return {"ok": True, "message": "Supervisor status read.", **self._control_status_payload()}
        if command == "start":
            return self._control_start_orchestrator()
        if command == "stop":
            return self._control_stop_orchestrator()
        if command == "break":
            return self._control_break_orchestrator()
        return {"ok": False, "message": f"Unknown orchestrator control command: {command}"}

    def _running_tasks(self, conn) -> list[dict]:
        rows = conn.execute(
            "SELECT id, title, next_step, branch, review_round FROM tasks WHERE status = 'running' ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]

    def _control_start_orchestrator(self) -> dict:
        proc = self._orchestrator_process()
        if proc.status == "running":
            return {
                "ok": False,
                "message": f"Refusing START: supervised orchestrator is already running (PID {proc.pid}).",
                **self._control_status_payload(),
            }

        conn = kanban_db.connect()
        try:
            running_tasks = self._running_tasks(conn)
            if running_tasks:
                ids = ", ".join(f"#{task['id']}" for task in running_tasks)
                return {
                    "ok": False,
                    "message": (
                        "Refusing START: task(s) still have status=running "
                        f"({ids}). Use BREAK or resolve the task state manually before restarting."
                    ),
                    **self._control_status_payload(),
                }
            if not orchestrator_control.singleton_lock_available():
                return {
                    "ok": False,
                    "message": "Refusing START: another orchestrator already holds the repo singleton lock.",
                    **self._control_status_payload(),
                }

            kanban_db.upsert_runtime(
                conn,
                status="starting",
                pid=None,
                started_at="CURRENT_TIMESTAMP",
                last_heartbeat_at="CURRENT_TIMESTAMP",
                current_task_id=None,
                current_step="none",
                current_branch=None,
                review_round=None,
                active_agents=0,
                status_message="Starting supervised orchestrator",
            )
        finally:
            conn.close()

        proc.start()
        return {
            "ok": True,
            "message": f"START accepted: supervised orchestrator launch requested (PID {proc.pid}).",
            **self._control_status_payload(),
        }

    def _control_stop_orchestrator(self) -> dict:
        proc = self._orchestrator_process()
        was_running = proc.status == "running"
        if was_running:
            proc.kill()
        killed_agents = orchestrator_control.kill_active_agents()

        conn = kanban_db.connect()
        try:
            runtime = kanban_db.get_runtime(conn)
            message = "Stopped by operator control."
            if runtime and runtime.get("current_task_id"):
                message += " Active task fields preserved for inspection."
            if runtime:
                kanban_db.update_runtime(
                    conn,
                    status="stopped",
                    pid=None,
                    active_agents=0,
                    status_message=message,
                    last_heartbeat_at="CURRENT_TIMESTAMP",
                )
            else:
                kanban_db.upsert_runtime(
                    conn,
                    status="stopped",
                    pid=None,
                    started_at=None,
                    last_heartbeat_at="CURRENT_TIMESTAMP",
                    current_task_id=None,
                    current_step="none",
                    current_branch=None,
                    review_round=None,
                    active_agents=0,
                    status_message=message,
                )
        finally:
            conn.close()

        return {
            "ok": True,
            "message": (
                f"STOP accepted: orchestrator was {'running' if was_running else 'already stopped'}; "
                f"terminated {len(killed_agents)} active agent process(es)."
            ),
            **self._control_status_payload(),
        }

    def _control_break_orchestrator(self) -> dict:
        proc = self._orchestrator_process()
        conn = kanban_db.connect()
        try:
            runtime = kanban_db.get_runtime(conn)
            snapshot = dict(runtime) if runtime else {}
            active_task_id = snapshot.get("current_task_id")
            original_step = snapshot.get("current_step") or "none"
            original_branch = snapshot.get("current_branch")
            original_round = snapshot.get("review_round")

            was_running = proc.status == "running"
            if was_running:
                proc.kill()
            killed_agents = orchestrator_control.kill_active_agents()
            removed_stop_marker = orchestrator_control.remove_stop_after_task_marker()

            blocked_task_id = None
            if active_task_id:
                task = kanban_db.get_task(conn, active_task_id)
                if task and task.get("status") == "running":
                    blocked_task_id = active_task_id
                    kanban_db.update_task(conn, active_task_id, status="blocked", next_step="none")
                    kanban_db.add_comment(
                        conn,
                        active_task_id,
                        "Hard BREAK requested by operator control. "
                        f"Interrupted active step={original_step}, branch={original_branch or 'unset'}, "
                        f"review_round={original_round if original_round is not None else 'unset'}. "
                        "The supervised orchestrator and recorded active agent children were stopped; "
                        "transient control markers were cleared; git/worktree changes were left untouched.",
                        kind="comment",
                        author="orchestrator",
                    )
                    parent_id = task.get("parent_task_id")
                    if parent_id:
                        parent = kanban_db.get_task(conn, parent_id)
                        if parent and parent.get("status") not in ("done", "blocked"):
                            kanban_db.update_task(conn, parent_id, status="blocked")
                            kanban_db.add_comment(
                                conn,
                                parent_id,
                                f"Child task {active_task_id} was blocked by a hard BREAK; supertask blocked.",
                                kind="comment",
                                author="orchestrator",
                            )
                elif task:
                    kanban_db.add_comment(
                        conn,
                        active_task_id,
                        "Hard BREAK requested by operator control, but the snapped active task was "
                        f"already status={task.get('status')}; task status was not changed. "
                        "Runtime active fields were cleared and git/worktree changes were left untouched.",
                        kind="comment",
                        author="orchestrator",
                    )

            kanban_db.add_run_log(
                conn,
                active_task_id,
                "Hard BREAK operator control executed; supervised orchestrator and recorded active agents stopped.",
                author="orchestrator",
            )

            if runtime:
                kanban_db.update_runtime(
                    conn,
                    status="hard-break",
                    pid=None,
                    current_task_id=None,
                    current_step="none",
                    current_branch=None,
                    review_round=None,
                    active_agents=0,
                    status_message=(
                        "Hard BREAK executed. Runtime active task fields cleared; "
                        f"{'task #' + str(blocked_task_id) + ' blocked' if blocked_task_id else 'no running task blocked'}."
                    ),
                    last_heartbeat_at="CURRENT_TIMESTAMP",
                )
            else:
                kanban_db.upsert_runtime(
                    conn,
                    status="hard-break",
                    pid=None,
                    started_at=None,
                    last_heartbeat_at="CURRENT_TIMESTAMP",
                    current_task_id=None,
                    current_step="none",
                    current_branch=None,
                    review_round=None,
                    active_agents=0,
                    status_message="Hard BREAK executed. Runtime active task fields cleared.",
                )
        finally:
            conn.close()

        return {
            "ok": True,
            "message": (
                f"BREAK accepted: orchestrator was {'running' if was_running else 'already stopped'}; "
                f"terminated {len(killed_agents)} active agent process(es); "
                f"stop-after-task marker {'removed' if removed_stop_marker else 'not present'}."
            ),
            **self._control_status_payload(),
        }

    def _refresh_action_buttons(self) -> None:
        proc = self.processes[self.selected_index]
        dash = self._dashboard_process()
        self.query_one("#btn-open-browser", Button).disabled = not bool(dash.browser_url)
        self.query_one("#btn-start",        Button).disabled = proc.status == "running"
        self.query_one("#btn-kill",         Button).disabled = proc.status != "running"
        self.query_one("#btn-restart",      Button).disabled = False

    # ── Button state ──────────────────────────────────────────────────────────

    # ── Process switching ─────────────────────────────────────────────────────

    def _select_process(self, index: int) -> None:
        if index == self.selected_index:
            return
        self.query_one(f"#status-{self.selected_index}", StatusBar).remove_class("selected")
        self.selected_index = index
        self.query_one(f"#status-{index}", StatusBar).add_class("selected")
        self._refresh_output_title()
        self._refresh_action_buttons()

        log = self.query_one("#process-log", RichLog)
        log.clear()
        proc = self.processes[index]
        with proc._lock:
            all_lines = list(proc.lines)
        self._log_indices[index] = len(all_lines)
        for line in all_lines:
            log.write(_colorize(line))
            if _is_blocking_app_error(line):
                log.write(_kill_hint())


    # ── Button handler ────────────────────────────────────────────────────────

    def on_status_bar_click(self, event) -> None:
        pass  # handled via on_click

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.set_focus(None)
        proc = self.processes[self.selected_index]
        log  = self.query_one("#process-log", RichLog)

        match event.button.id:
            case "btn-start":
                proc.start()
            case "btn-kill":
                proc.kill()
            case "btn-restart":
                proc.restart()
                log.clear()
                self._log_indices[self.selected_index] = 0
            case "btn-open-browser":
                self._open_browser(self._dashboard_process())
            case "btn-kill-blocker":
                self._kill_blocking_app(proc)
            case "btn-view-log":
                self._view_log(proc)
            case "btn-quit":
                self._do_quit()

    def on_click(self, event) -> None:
        for i in range(len(self.processes)):
            bar = self.query_one(f"#status-{i}", StatusBar)
            if bar.region.contains(event.screen_x, event.screen_y):
                self._select_process(i)
                return

    # ── Actions ───────────────────────────────────────────────────────────────

    def _kill_blocking_app(self, proc: ManagedProcess) -> None:
        log     = self.query_one("#process-log", RichLog)
        victims = proc.find_blocker_pids()
        if not victims:
            log.write(f"[yellow]No other instances of {proc.name} found.[/yellow]")
            return
        for pid in victims:
            os.kill(pid, signal.SIGTERM)
        log.write(f"[green]Killed PID(s) {', '.join(str(p) for p in victims)} ({proc.name}).[/green]")

    def _view_log(self, proc: ManagedProcess) -> None:
        with proc._lock:
            text = "\n".join(proc.lines)
        self.push_screen(BrowseModal(proc.name, text))

    def _open_browser(self, proc: ManagedProcess) -> None:
        log = self.query_one("#process-log", RichLog)
        if not proc.browser_url:
            log.write(f"[yellow]No browser URL known for {proc.name}.[/yellow]")
            return
        try:
            opened = webbrowser.open(proc.browser_url)
        except Exception as exc:
            log.write(f"[red]Failed to open {proc.browser_url}: {exc}[/red]")
            return
        if opened:
            log.write(f"[green]Opened {proc.browser_url}[/green]")
        else:
            log.write(f"[yellow]Browser launch was not confirmed for {proc.browser_url}[/yellow]")

    def _do_quit(self) -> None:
        managed_processes = [proc for proc in self.processes if proc.cmd]
        if all(proc.status != "running" for proc in managed_processes):
            self.exit()
            return

        def handle_result(result: Optional[str]) -> None:
            if result == "kill":
                for proc in managed_processes:
                    proc.kill()
                self.exit()
            elif result == "leave":
                self.exit()
        self.push_screen(QuitModal(), handle_result)

    def action_quit(self) -> None:
        self._do_quit()


def main():
    parser = argparse.ArgumentParser(description="Orchestra UI — process manager TUI")
    parser.add_argument("--doNotStart", action="store_true",
                        help="Open UI without starting processes")
    parser.add_argument(
        "--orchestrator-control",
        choices=sorted(orchestrator_control.CONTROL_COMMANDS),
        metavar="COMMAND",
        help="Send a local control command to the live orchestra-ui supervisor: status, start, stop, or break",
    )
    parser.add_argument(
        "--control-timeout",
        type=float,
        default=15.0,
        help="Seconds to wait for an orchestra-ui control response",
    )
    args = parser.parse_args()

    if args.orchestrator_control:
        try:
            response = orchestrator_control.submit_control_command(
                args.orchestrator_control,
                timeout=args.control_timeout,
            )
        except orchestrator_control.ControlError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(response, indent=2, sort_keys=True))
        return 0 if response.get("ok") else 1

    OrchestraApp(auto_start=not args.doNotStart).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
