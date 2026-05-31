#!/usr/bin/env python3
"""Manage Orchestra instances for a private list of work repositories."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import config


ORCHESTRA_ROOT = Path(os.environ.get("ORCHESTRA_DIR", Path(__file__).resolve().parents[2])).resolve()
DEFAULT_CONFIG_PATH = Path("~/.config/orchestra/fleet.repos").expanduser()


@dataclass(frozen=True)
class FleetRepo:
    label: str
    path: Path
    root: Path | None
    error: str | None = None
    managed: bool = True

    @property
    def session(self) -> str:
        return f"orch-{self.label}"

    @property
    def lock_path(self) -> Path | None:
        if self.root is None:
            return None
        return self.root / "kanban-orchestra.lock"

    @property
    def runtime_root(self) -> Path | None:
        if self.root is None:
            return None
        return self.root / ".kanban-orchestra"

    @property
    def log_path(self) -> Path | None:
        if self.runtime_root is None:
            return None
        return self.runtime_root / "orchestrator.log"

    @property
    def dashboard_metadata_path(self) -> Path | None:
        if self.runtime_root is None:
            return None
        return self.runtime_root / "dashboard.json"


def config_path() -> Path:
    return Path(os.environ.get("ORCHESTRA_FLEET_REPOS", DEFAULT_CONFIG_PATH)).expanduser()


def run(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def die(message: str, code: int = 1) -> None:
    print(f"ko-fleet: {message}", file=sys.stderr)
    raise SystemExit(code)


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        die(f"required tool not found on PATH: {name}")


def orchestra_bin(name: str) -> Path:
    path = ORCHESTRA_ROOT / "bin" / name
    if not path.exists():
        die(f"expected executable not found: {path}")
    if not os.access(path, os.X_OK):
        die(f"expected executable is not executable: {path}")
    return path


def label_for(path: Path) -> str:
    label = re.sub(r"[^A-Za-z0-9_.-]+", "-", path.name).strip("-")
    return label or "repo"


def parse_config_lines(text: str) -> list[str]:
    paths: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        paths.append(line)
    return paths


def expand_repo_path(raw_path: str) -> Path:
    return Path(os.path.expandvars(raw_path)).expanduser()


def display_path(path: Path | str | None) -> str:
    """Return a display-only path, abbreviating paths under HOME with ~."""
    if path is None:
        return "-"
    resolved = Path(path).expanduser().resolve()
    home = Path.home().resolve()
    try:
        relative = resolved.relative_to(home)
    except ValueError:
        return str(resolved)
    if str(relative) == ".":
        return "~"
    return f"~/{relative}"


def discover_repo(raw_path: str) -> FleetRepo:
    path = expand_repo_path(raw_path)
    label = label_for(path)
    if not path.exists():
        return FleetRepo(label, path, None, "path does not exist")
    if not path.is_dir():
        return FleetRepo(label, path, None, "path is not a directory")

    result = run(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        return FleetRepo(label, path, None, "not inside a git repo")

    root = Path(result.stdout.strip()).resolve()
    physical_path = path.resolve()
    label = label_for(root)
    if physical_path != root:
        return FleetRepo(label, physical_path, root, f"configured path is not the git root: {root}")
    return FleetRepo(label, physical_path, root)


def load_repos() -> list[FleetRepo]:
    path = config_path()
    if not path.exists():
        die(f"fleet config not found: {path}\nRun `ko-fleet init` or `ko-fleet add .`.")
    return [discover_repo(raw) for raw in parse_config_lines(path.read_text(encoding="utf-8"))]


def load_status_repos() -> list[FleetRepo]:
    path = config_path()
    if not path.exists():
        return []
    return [discover_repo(raw) for raw in parse_config_lines(path.read_text(encoding="utf-8"))]


def repo_matches(repo: FleetRepo, query: str) -> bool:
    return query in {repo.label, repo.path.name, str(repo.path), str(repo.root or "")}


def select_repos(queries: list[str]) -> list[FleetRepo]:
    repos = load_repos()
    if not queries:
        return repos

    selected: list[FleetRepo] = []
    unmatched: list[str] = []
    for query in queries:
        matches = [repo for repo in repos if repo_matches(repo, query)]
        if not matches:
            unmatched.append(query)
            continue
        for repo in matches:
            if repo not in selected:
                selected.append(repo)
    if unmatched:
        die(f"unknown repo selector(s): {', '.join(unmatched)}")
    return selected


def pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_key_value_or_json(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        payload = {}
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                payload[key.strip()] = value.strip()
        return payload


def metadata_matches_repo(payload: dict, repo: FleetRepo) -> bool:
    """Return whether runtime metadata belongs to the selected repo instance."""
    if repo.root is None:
        return False
    repo_root = payload.get("repo_root")
    if not repo_root:
        return True
    try:
        return Path(str(repo_root)).expanduser().resolve() == repo.root.resolve()
    except OSError:
        return False


def metadata_pid(path: Path | None, role: str, repo: FleetRepo | None = None) -> int | None:
    payload = read_key_value_or_json(path)
    if payload.get("role") != role:
        return None
    if repo is not None and not metadata_matches_repo(payload, repo):
        return None
    try:
        return int(payload["pid"])
    except (KeyError, TypeError, ValueError):
        return None


def repo_runtime_status(repo: FleetRepo) -> dict | None:
    """Read the repo runtime row without creating or migrating its database."""
    if repo.root is None:
        return None
    db_path = repo.root / "kanban-orchestra.db"
    if not db_path.exists():
        return None
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM orchestrator_runtime WHERE singleton = 1").fetchone()
    except sqlite3.Error:
        return None
    finally:
        if conn is not None:
            conn.close()
    return dict(row) if row else None


def runtime_activity(runtime: dict | None) -> str | None:
    if not runtime:
        return None

    current_step = runtime.get("current_step")
    try:
        active_agents = int(runtime.get("active_agents") or 0)
    except (TypeError, ValueError):
        active_agents = 0

    if runtime.get("current_task_id") or (current_step and current_step != "none") or active_agents > 0:
        return "busy"

    status = runtime.get("status")
    if status == "idle":
        return "idle"
    if status == "running":
        return "busy"
    if status in {"starting", "stopping", "hard-break", "error"}:
        return str(status)
    return None


def running_status(activity: str | None) -> str:
    return f"running/{activity}" if activity else "running"


def status_is_running(status: str) -> bool:
    return status == "running" or status.startswith("running/")


def dashboard_port_for_index(index: int) -> int:
    """Return Fleet's preferred dashboard port for a repo position."""
    return config.DASHBOARD_PORT_BASE + index


def request_dashboard_start(repo: FleetRepo, *, preferred_port: int | None = None) -> Path:
    if repo.runtime_root is None:
        die(f"{repo.label}: invalid config ({repo.error or 'missing runtime root'})")
    path = repo.runtime_root / config.DASHBOARD_START_REQUEST_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    content = "start\n"
    if preferred_port is not None:
        content += f"port={preferred_port}\n"
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return path


def dashboard_endpoint_ready(payload: dict, *, timeout: float = 0.25) -> bool:
    host = payload.get("host")
    port = payload.get("port")
    if not host or port is None:
        return False
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False
    try:
        with socket.create_connection((str(host), port), timeout=timeout):
            return True
    except OSError:
        return False


def dashboard_status_url(repo: FleetRepo) -> str:
    orch_pid = metadata_pid(repo.lock_path, "orchestrator", repo)
    if not pid_alive(orch_pid):
        return "-"

    payload = read_key_value_or_json(repo.dashboard_metadata_path)
    url = payload.get("url")
    if not url or not metadata_matches_repo(payload, repo):
        return "-"
    pid = metadata_pid(repo.dashboard_metadata_path, "dashboard", repo)
    if not pid_alive(pid):
        return "-"
    return str(url)


def wait_dashboard_ready(repo: FleetRepo, *, timeout: float = 12.0) -> bool:
    """Wait for Fleet-visible dashboard metadata for one running repo."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if dashboard_status_url(repo) != "-":
            return True
        time.sleep(0.2)
    return dashboard_status_url(repo) != "-"


def tmux_has_session(session: str) -> bool:
    if shutil.which("tmux") is None:
        return False
    return subprocess.run(
        ["tmux", "has-session", "-t", session],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def process_table() -> list[tuple[int, str]]:
    result = run(["ps", "-Ao", "pid=,command="])
    if result.returncode != 0:
        return []

    processes = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        processes.append((pid, command.strip()))
    return processes


def is_orchestrator_command(command: str) -> bool:
    return re.search(r"(^|\s)\S*orchestrator\.py(\s|$)", command) is not None


def process_cwd(pid: int) -> Path | None:
    proc_cwd = Path("/proc") / str(pid) / "cwd"
    if proc_cwd.exists():
        try:
            return proc_cwd.resolve()
        except OSError:
            return None

    if shutil.which("lsof") is None:
        return None
    result = run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("n") and len(line) > 1:
            try:
                return Path(line[1:]).resolve()
            except OSError:
                return None
    return None


def repo_root_for_process_cwd(cwd: Path) -> Path | None:
    result = run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"])
    if result.returncode == 0:
        return Path(result.stdout.strip()).resolve()
    fallback = cwd.resolve()
    if (fallback / "kanban-orchestra.lock").exists():
        return fallback
    return None


def running_orchestrator_process_roots() -> list[tuple[int, Path]]:
    roots = []
    current_pid = os.getpid()
    for pid, command in process_table():
        if pid == current_pid or not is_orchestrator_command(command):
            continue
        cwd = process_cwd(pid)
        if cwd is None:
            continue
        root = repo_root_for_process_cwd(cwd)
        if root is not None:
            roots.append((pid, root))
    return roots


def discover_running_repos() -> list[FleetRepo]:
    repos = []
    seen_roots: set[Path] = set()
    for pid, root in running_orchestrator_process_roots():
        if root in seen_roots:
            continue
        repo = FleetRepo(label_for(root), root, root, managed=False)
        if metadata_pid(repo.lock_path, "orchestrator", repo) != pid:
            continue
        if not pid_alive(pid):
            continue
        repos.append(repo)
        seen_roots.add(root)
    return repos


def validated_orchestrator_pid(repo: FleetRepo) -> int | None:
    """Return a live repo-owned orchestrator PID only after process validation."""
    if repo.root is None:
        return None
    lock_pid = metadata_pid(repo.lock_path, "orchestrator", repo)
    if not pid_alive(lock_pid):
        return None
    for pid, root in running_orchestrator_process_roots():
        if pid == lock_pid and root.resolve() == repo.root.resolve() and pid_alive(pid):
            return pid
    return None


def status_repos(queries: list[str]) -> list[FleetRepo]:
    configured = load_status_repos()
    managed_roots = {repo.root.resolve() for repo in configured if repo.root is not None}
    unmanaged = [
        repo
        for repo in discover_running_repos()
        if repo.root is not None and repo.root.resolve() not in managed_roots
    ]
    repos = [
        *configured,
        *unmanaged,
    ]
    if not queries:
        return repos

    selected: list[FleetRepo] = []
    unmatched: list[str] = []
    for query in queries:
        matches = [repo for repo in repos if repo_matches(repo, query)]
        if not matches:
            unmatched.append(query)
            continue
        for repo in matches:
            if repo not in selected:
                selected.append(repo)
    if unmatched:
        die(f"unknown repo selector(s): {', '.join(unmatched)}")
    return selected


def repo_process_state(repo: FleetRepo) -> tuple[str, str, str, str]:
    if repo.error:
        return "invalid", "-", "-", "-"

    session_alive = tmux_has_session(repo.session)
    orch_pid = metadata_pid(repo.lock_path, "orchestrator", repo)
    dashboard_pid = metadata_pid(repo.dashboard_metadata_path, "dashboard", repo)
    orch_alive = pid_alive(orch_pid)

    if orch_alive:
        status = running_status(runtime_activity(repo_runtime_status(repo)))
    else:
        status = "stopped"
        dashboard_pid = None

    return (
        status,
        str(orch_pid) if orch_pid is not None else "-",
        str(dashboard_pid) if dashboard_pid is not None else "-",
        repo.session if session_alive else "-",
    )


def dirty_lines(repo: FleetRepo) -> list[str]:
    if repo.root is None:
        return []
    result = run(["git", "-C", str(repo.root), "status", "--porcelain", "--untracked-files=normal"])
    if result.returncode != 0:
        return [f"?? could not read git status: {result.stderr.strip()}"]
    return [line for line in result.stdout.splitlines() if line.strip()]


def invalid_repos(repos: list[FleetRepo]) -> list[FleetRepo]:
    return [repo for repo in repos if repo.error]


def require_startable(repos: list[FleetRepo]) -> None:
    invalid = invalid_repos(repos)
    dirty = []
    for repo in repos:
        if repo.error:
            continue
        lines = dirty_lines(repo)
        if lines:
            dirty.append((repo, lines))
    if not invalid and not dirty:
        return

    print("Fleet precheck failed; not starting anything.", file=sys.stderr)
    for repo in invalid:
        print(f"\n{repo.label}: {display_path(repo.path)}", file=sys.stderr)
        print(f"  {repo.error}", file=sys.stderr)
    for repo, lines in dirty:
        print(f"\n{repo.label}: {display_path(repo.root)}", file=sys.stderr)
        for line in lines[:12]:
            print(f"  {line}", file=sys.stderr)
        if len(lines) > 12:
            print(f"  ... {len(lines) - 12} more", file=sys.stderr)
    raise SystemExit(1)


def print_status(repos: list[FleetRepo]) -> None:
    rows = []
    for repo in repos:
        status, orch_pid, dashboard_pid, tmux = repo_process_state(repo)
        dashboard_url = "-" if repo.error else dashboard_status_url(repo)
        owner = "managed" if repo.managed else "unmanaged"
        root_display = display_path(repo.root or repo.path)
        if repo.error:
            root_display = f"{root_display} ({repo.error})"
        rows.append(
            (
                repo.label,
                status,
                f"{orch_pid}/{dashboard_pid}",
                tmux,
                dashboard_url,
                owner,
                root_display,
            )
        )

    widths = [
        max(len("repo"), *(len(row[0]) for row in rows)),
        max(len("status"), *(len(row[1]) for row in rows)),
        max(len("orch/dash"), *(len(row[2]) for row in rows)),
        max(len("tmux"), *(len(row[3]) for row in rows)),
        max(len("dash_url"), *(len(row[4]) for row in rows)),
        max(len("owner"), *(len(row[5]) for row in rows)),
    ]
    print(
        f"{'repo':<{widths[0]}}  {'status':<{widths[1]}}  "
        f"{'orch/dash':>{widths[2]}}  {'tmux':<{widths[3]}}  "
        f"{'dash_url':<{widths[4]}}  {'owner':<{widths[5]}}  root"
    )
    print(
        f"{'-' * widths[0]}  {'-' * widths[1]}  "
        f"{'-' * widths[2]}  {'-' * widths[3]}  "
        f"{'-' * widths[4]}  {'-' * widths[5]}  ----"
    )
    for label, status, process_pids, tmux, dashboard_url, owner, root in rows:
        print(
            f"{label:<{widths[0]}}  {status:<{widths[1]}}  "
            f"{process_pids:>{widths[2]}}  {tmux:<{widths[3]}}  "
            f"{dashboard_url:<{widths[4]}}  {owner:<{widths[5]}}  {root}"
        )


def precheck(repos: list[FleetRepo]) -> int:
    rows = []
    dirty = []
    exit_code = 0
    for repo in repos:
        if repo.error:
            rows.append((repo.label, "invalid", "-", repo.error))
            exit_code = 1
            continue
        lines = dirty_lines(repo)
        if lines:
            rows.append((repo.label, "dirty", str(len(lines)), display_path(repo.root)))
            dirty.append((repo, lines))
            exit_code = 1
        else:
            rows.append((repo.label, "clean", "0", display_path(repo.root)))

    widths = [
        max(len("repo"), *(len(row[0]) for row in rows)),
        max(len("state"), *(len(row[1]) for row in rows)),
        max(len("dirty"), *(len(row[2]) for row in rows)),
    ]
    print(f"{'repo':<{widths[0]}}  {'state':<{widths[1]}}  {'dirty':>{widths[2]}}  root/error")
    print(f"{'-' * widths[0]}  {'-' * widths[1]}  {'-' * widths[2]}  ----------")
    for label, state, dirty_count, detail in rows:
        print(f"{label:<{widths[0]}}  {state:<{widths[1]}}  {dirty_count:>{widths[2]}}  {detail}")

    if dirty:
        print("\nDirty details:")
        for repo, lines in dirty:
            print(f"\n{repo.label}: {display_path(repo.root)}")
            for line in lines[:12]:
                print(f"  {line}")
            if len(lines) > 12:
                print(f"  ... {len(lines) - 12} more")
    return exit_code


def start(repos: list[FleetRepo], *, precheck: bool = True) -> None:
    require_tool("tmux")
    orchestrator = orchestra_bin("ko-orchestrator")
    invalid = invalid_repos(repos)
    states = []
    repos_to_start = []
    for repo in repos:
        if repo.error:
            continue
        state = repo_process_state(repo)
        states.append((repo, state))
        status, _, _, session = state
        if not status_is_running(status) and session == "-":
            repos_to_start.append(repo)
    if precheck:
        require_startable([*invalid, *repos_to_start])
    elif invalid:
        for repo in invalid:
            print(f"{repo.label}: invalid config ({repo.error})", file=sys.stderr)
        raise SystemExit(1)
    for index, (repo, state) in enumerate(states):
        preferred_port = dashboard_port_for_index(index)
        status, orch_pid, dashboard_pid, session = state
        if status_is_running(status):
            if dashboard_pid == "-":
                request_dashboard_start(repo, preferred_port=preferred_port)
                if wait_dashboard_ready(repo):
                    print(f"{repo.label}: dashboard started (orchestrator {orch_pid})")
                else:
                    print(f"{repo.label}: dashboard start requested (orchestrator {orch_pid}); still pending")
            else:
                print(f"{repo.label}: already running (orchestrator {orch_pid})")
            continue
        if session != "-":
            print(f"{repo.label}: tmux session already exists without a live orchestrator ({session})")
            continue
        subprocess.run(
            [
                "tmux", "new-session", "-d", "-s", repo.session, "-c", str(repo.root),
                str(orchestrator), "--dashboard-port", str(preferred_port),
            ],
            check=True,
        )
        if wait_dashboard_ready(repo):
            print(f"{repo.label}: started ({repo.session})")
        else:
            print(f"{repo.label}: started ({repo.session}); dashboard still pending")
    time.sleep(0.5)
    print()
    print_status(repos)


def stop(repos: list[FleetRepo]) -> None:
    require_tool("tmux")
    invalid = invalid_repos(repos)
    if invalid:
        for repo in invalid:
            print(f"{repo.label}: invalid config ({repo.error})")
        raise SystemExit(1)
    for repo in repos:
        if tmux_has_session(repo.session):
            subprocess.run(["tmux", "send-keys", "-t", repo.session, "C-c"], check=False)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and tmux_has_session(repo.session):
                time.sleep(0.1)
            if tmux_has_session(repo.session):
                subprocess.run(["tmux", "kill-session", "-t", repo.session], check=False)
            print(f"{repo.label}: stopped tmux session")
        else:
            status, orch_pid, _, _ = repo_process_state(repo)
            if status_is_running(status):
                print(f"{repo.label}: running outside fleet tmux session (orchestrator {orch_pid}); left alone")
            else:
                print(f"{repo.label}: not running")


def stop_pid(pid: int, *, timeout: float = 2.0) -> bool:
    try:
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


def stop_all(repos: list[FleetRepo]) -> None:
    for repo in repos:
        owner = "managed" if repo.managed else "unmanaged"
        prefix = f"{repo.label} [{owner}]"
        if repo.error:
            print(f"{prefix}: invalid config ({repo.error})")
            continue

        if repo.managed and tmux_has_session(repo.session):
            subprocess.run(["tmux", "send-keys", "-t", repo.session, "C-c"], check=False)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and tmux_has_session(repo.session):
                time.sleep(0.1)
            if tmux_has_session(repo.session):
                subprocess.run(["tmux", "kill-session", "-t", repo.session], check=False)
            print(f"{prefix}: stopped fleet tmux session")
            continue

        status, orch_pid, _, _ = repo_process_state(repo)
        if not status_is_running(status):
            print(f"{prefix}: not running")
            continue

        pid = validated_orchestrator_pid(repo)
        if pid is None:
            print(f"{prefix}: running orchestrator {orch_pid} could not be validated; left alone")
            continue

        if stop_pid(pid):
            print(f"{prefix}: stopped orchestrator {pid}")
        else:
            print(f"{prefix}: could not stop orchestrator {pid}")


def wait_stopped(repos: list[FleetRepo], *, timeout: float = 12.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        running = []
        for repo in repos:
            if repo.error:
                continue
            orch_pid = metadata_pid(repo.lock_path, "orchestrator", repo)
            if pid_alive(orch_pid):
                running.append((repo, orch_pid))
        if not running:
            return
        if time.monotonic() >= deadline:
            labels = ", ".join(f"{repo.label} ({pid})" for repo, pid in running)
            die(f"timed out waiting for orchestrator shutdown: {labels}")
        time.sleep(0.2)


def one_repo(queries: list[str]) -> FleetRepo:
    if len(queries) != 1:
        die("this command requires exactly one repo label or path")
    repos = select_repos(queries)
    if len(repos) != 1:
        die("selector matched more than one repo")
    if repos[0].error:
        die(f"{repos[0].label}: {repos[0].error}")
    return repos[0]


def attach(repo: FleetRepo) -> None:
    require_tool("tmux")
    if not tmux_has_session(repo.session):
        die(f"no fleet tmux session for {repo.label}")
    os.execvp("tmux", ["tmux", "attach", "-t", repo.session])


def logs(repo: FleetRepo) -> None:
    if repo.log_path is None or not repo.log_path.exists():
        die(f"no orchestrator log found for {repo.label}: {repo.log_path}")
    os.execvp("tail", ["tail", "-f", str(repo.log_path)])


def open_dashboard(repo: FleetRepo) -> None:
    payload = read_key_value_or_json(repo.dashboard_metadata_path)
    url = payload.get("url")
    if not url:
        die(f"no dashboard metadata found for {repo.label}")
    if not metadata_matches_repo(payload, repo):
        die(f"dashboard metadata belongs to a different repo for {repo.label}")
    pid = metadata_pid(repo.dashboard_metadata_path, "dashboard", repo)
    if not pid_alive(pid):
        die(f"dashboard is not running for {repo.label}")
    if not dashboard_endpoint_ready(payload):
        die(f"dashboard is not accepting connections for {repo.label}: {url}")
    if sys.platform == "darwin" and shutil.which("open"):
        subprocess.run(["open", url], check=False)
    print(url)


def init_config() -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        die(f"config already exists: {path}")
    path.write_text(
        "# One git repo root per line. Blank lines and # comments are ignored.\n"
        "# ~/Documents/work/my-repo\n",
        encoding="utf-8",
    )
    print(path)


def repo_root_for_add(raw_path: str) -> Path:
    path = expand_repo_path(raw_path)
    result = run(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        die(f"not inside a git repo: {path}")
    return Path(result.stdout.strip()).resolve()


def add_repo(raw_path: str) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    root = repo_root_for_add(raw_path)
    existing = []
    if path.exists():
        existing = [discover_repo(raw).root for raw in parse_config_lines(path.read_text(encoding="utf-8"))]
    if root in existing:
        print(f"already present: {root}")
        return
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{root}\n")
    print(f"added: {root}")


def remove_repo(selector: str) -> None:
    path = config_path()
    if not path.exists():
        die(f"fleet config not found: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    kept: list[str] = []
    removed: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            kept.append(line)
            continue
        repo = discover_repo(stripped)
        if repo_matches(repo, selector):
            removed.append(str(repo.root or repo.path))
        else:
            kept.append(line)
    if not removed:
        die(f"no repo matched: {selector}")
    path.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
    for item in removed:
        print(f"removed: {item}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")

    for name in ("status", "precheck", "start", "stop", "stop-all", "restart"):
        p = sub.add_parser(name)
        p.add_argument("repos", nargs="*", help="optional repo labels or paths")

    for name in ("attach", "logs", "dashboard", "dashboard-open"):
        p = sub.add_parser(name)
        p.add_argument("repo", nargs=1, help="repo label or path")

    sub.add_parser("init")
    p_add = sub.add_parser("add")
    p_add.add_argument("path")
    p_remove = sub.add_parser("remove")
    p_remove.add_argument("repo")
    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["status"]

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "status":
        print_status(status_repos(args.repos))
    elif args.command == "precheck":
        return precheck(select_repos(args.repos))
    elif args.command == "start":
        start(select_repos(args.repos))
    elif args.command == "stop":
        stop(select_repos(args.repos))
    elif args.command == "stop-all":
        stop_all(status_repos(args.repos))
    elif args.command == "restart":
        repos = select_repos(args.repos)
        require_startable(repos)
        stop(repos)
        wait_stopped(repos)
        start(repos, precheck=False)
    elif args.command == "attach":
        attach(one_repo(args.repo))
    elif args.command == "logs":
        logs(one_repo(args.repo))
    elif args.command in {"dashboard", "dashboard-open"}:
        open_dashboard(one_repo(args.repo))
    elif args.command == "init":
        init_config()
    elif args.command == "add":
        add_repo(args.path)
    elif args.command == "remove":
        remove_repo(args.repo)
    else:
        parser.print_help()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
