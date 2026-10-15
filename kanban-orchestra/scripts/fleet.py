#!/usr/bin/env python3
"""Manage Orchestra instances for a private list of work repositories."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
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
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import config
import dashboard_tailscale
import db


ORCHESTRA_ROOT = Path(os.environ.get("ORCHESTRA_DIR", Path(__file__).resolve().parents[2])).resolve()
DEFAULT_CONFIG_PATH = Path("~/.config/orchestra/fleet.repos").expanduser()
FLEET_DASHBOARD_PORT = config.DASHBOARD_PORT_BASE - 1
FLEET_DASHBOARD_METADATA_NAME = "fleet-dashboard.json"
UNOWNED_SESSION = "unowned"


@dataclass(frozen=True)
class FleetRepo:
    """One Fleet instance.

    ``label`` is the readable display name. ``selector`` is the stable identity
    used for commands and dashboard play. ``session`` is the tmux name derived
    from that selector.
    """

    label: str
    path: Path
    root: Path | None
    error: str | None = None
    managed: bool = True
    kind: str = "primary"
    parent: Path | None = None

    @property
    def selector(self) -> str:
        if self.root is None:
            return self.label
        return instance_token(self.root)

    @property
    def session(self) -> str:
        if self.root is None:
            return f"orch-{sanitize_token(self.label)}"
        return f"orch-{self.selector}"

    @property
    def lock_path(self) -> Path | None:
        row = _registered_queue(self.root)
        if row is None:
            return None
        return db.get_lock_path(queue_id=row["id"])

    @property
    def runtime_root(self) -> Path | None:
        row = _registered_queue(self.root)
        if row is None:
            return None
        return db.get_runtime_root(queue_id=row["id"])

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


def fleet_dashboard_metadata_path() -> Path:
    """Return the fleet-scoped dashboard metadata path beside fleet.repos."""
    override = os.environ.get("KO_FLEET_DASHBOARD_METADATA_PATH")
    if override:
        return Path(override).expanduser().resolve()
    return config_path().with_name(FLEET_DASHBOARD_METADATA_NAME)


def fleet_dashboard_start_lock_path() -> Path:
    """Return the user-scoped lock serializing Fleet Dashboard launches."""
    return fleet_dashboard_metadata_path().with_suffix(".start.lock")


@contextmanager
def _fleet_dashboard_start_lock():
    path = fleet_dashboard_start_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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


def sanitize_token(name: str) -> str:
    """Return a tmux-safe token. Session targets treat '.' and ':' specially."""
    token = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")
    return token or "repo"


def canonical_path(path: Path) -> Path:
    return path.expanduser().resolve()


def instance_token(root: Path) -> str:
    """Readable basename plus a short hash of the canonical root.

    The hash follows the directory path, so a branch change does not rename
    the instance or its tmux session.
    """
    canonical = canonical_path(root)
    digest = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()[:8]
    return f"{sanitize_token(canonical.name)}-{digest}"


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
    return FleetRepo(label, physical_path, root, kind="primary")


@dataclass(frozen=True)
class _WorktreeEntry:
    path: Path
    missing: bool


def _unquote_git_path(text: str) -> str:
    if len(text) < 2 or not text.startswith('"') or not text.endswith('"'):
        return text
    body = text[1:-1]
    out: list[str] = []
    index = 0
    while index < len(body):
        if body[index] != "\\":
            out.append(body[index])
            index += 1
            continue
        index += 1
        if index >= len(body):
            break
        escaped = body[index]
        mapping = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}
        if escaped in mapping:
            out.append(mapping[escaped])
            index += 1
        elif escaped in "01234567":
            octal = body[index:index + 3]
            if len(octal) == 3 and octal.isdigit():
                out.append(chr(int(octal, 8)))
                index += 3
            else:
                out.append(escaped)
                index += 1
        else:
            out.append(escaped)
            index += 1
    return "".join(out)


def _parse_worktree_porcelain(text: str) -> list[_WorktreeEntry]:
    entries: list[_WorktreeEntry] = []
    path: Path | None = None
    prunable = False

    def finish() -> None:
        nonlocal path, prunable
        if path is not None:
            missing = prunable or not path.exists()
            entries.append(_WorktreeEntry(path, missing))
        path = None
        prunable = False

    for line in text.splitlines():
        if not line:
            finish()
            continue
        if line.startswith("worktree "):
            if path is not None:
                finish()
            path = Path(_unquote_git_path(line[len("worktree "):]))
            prunable = False
        elif line.startswith("prunable"):
            prunable = True
    finish()
    return entries


def list_worktree_entries(root: Path) -> list[_WorktreeEntry]:
    result = run(["git", "-C", str(root), "worktree", "list", "--porcelain"])
    if result.returncode != 0:
        return []
    return _parse_worktree_porcelain(result.stdout)


def is_linked_worktree(path: Path) -> bool:
    """Linked worktrees store a .git file; the main worktree stores a directory."""
    try:
        return (path / ".git").is_file()
    except OSError:
        return False


def _annotate_legacy(repo: FleetRepo) -> FleetRepo:
    """Attach a migration diagnostic when a checkout still has a legacy database."""
    if repo.root is None or repo.error:
        return repo
    legacy = db.legacy_database_file(repo.root)
    if legacy is None or _registered_queue(repo.root) is not None:
        return repo
    return replace(repo, error=db.migration_required_message(legacy))


def _registered_queue(root: Path | None) -> dict | None:
    """Return the enabled central-store queue for a worktree, if one exists."""
    if root is None:
        return None
    try:
        return db.lookup_queue_by_path(root)
    except (RuntimeError, OSError):
        return None


def orchestra_database_usable(root: Path) -> bool:
    """Return whether this checkout has an enabled queue in the central store.

    A legacy per-worktree database does not count: Fleet must not treat it as
    a live queue. Callers surface migration-required separately.
    """
    if db.legacy_database_file(root) is not None:
        return False
    return _registered_queue(root) is not None


def configured_primary_repos(*, missing_ok: bool = False) -> list[FleetRepo]:
    path = config_path()
    if not path.exists():
        if missing_ok:
            return []
        die(f"fleet config not found: {path}\nRun `ko-fleet init` or `ko-fleet add .`.")
    return [discover_repo(raw) for raw in parse_config_lines(path.read_text(encoding="utf-8"))]


def expand_configured_repos(primaries: list[FleetRepo]) -> list[FleetRepo]:
    """Return configured primaries plus initialized linked worktrees.

    Invalid primary config is preserved. Missing linked worktrees are omitted.
    Canonical roots are listed once; a configured primary wins over a worktree
    entry for the same path. This list is derived from Git metadata and is not
    a second registration database.
    """
    instances: list[FleetRepo] = []
    seen: set[Path] = set()
    valid: list[FleetRepo] = []
    for primary in primaries:
        if primary.error or primary.root is None:
            instances.append(primary)
            continue
        try:
            root = canonical_path(primary.root)
        except OSError:
            instances.append(replace(primary, error=primary.error or "path could not be resolved"))
            continue
        if root in seen:
            continue
        seen.add(root)
        annotated = _annotate_legacy(replace(primary, path=root, root=root, kind="primary", parent=None))
        if annotated.error is None:
            identity_error = db.queue_registration_error(root)
            if identity_error:
                annotated = replace(annotated, error=identity_error)
        instances.append(annotated)
        valid.append(primary)

    for primary in valid:
        if primary.root is None:
            continue
        try:
            parent = canonical_path(primary.root)
        except OSError:
            continue
        for entry in list_worktree_entries(parent):
            if entry.missing:
                continue
            try:
                root = canonical_path(entry.path)
            except OSError:
                continue
            if root in seen or root == parent:
                continue
            if not is_linked_worktree(root):
                continue
            legacy = db.legacy_database_file(root)
            if legacy is not None:
                seen.add(root)
                instances.append(
                    FleetRepo(
                        label_for(root),
                        root,
                        root,
                        error=db.migration_required_message(legacy),
                        kind="worktree",
                        parent=parent,
                    )
                )
                continue
            identity_error = db.queue_registration_error(root)
            if identity_error:
                seen.add(root)
                instances.append(
                    FleetRepo(
                        label_for(root),
                        root,
                        root,
                        error=identity_error,
                        kind="worktree",
                        parent=parent,
                    )
                )
                continue
            if not orchestra_database_usable(root):
                continue
            seen.add(root)
            instances.append(
                FleetRepo(
                    label_for(root),
                    root,
                    root,
                    kind="worktree",
                    parent=parent,
                )
            )
    return instances


def load_repos() -> list[FleetRepo]:
    return expand_configured_repos(configured_primary_repos())


def load_status_repos() -> list[FleetRepo]:
    return expand_configured_repos(configured_primary_repos(missing_ok=True))


def repo_matches(repo: FleetRepo, query: str) -> bool:
    candidates = {repo.label, repo.selector, repo.path.name, str(repo.path)}
    if repo.root is not None:
        candidates.add(str(repo.root))
        try:
            candidates.add(str(canonical_path(repo.root)))
        except OSError:
            pass
    return query in candidates


def select_instances(repos: list[FleetRepo], queries: list[str]) -> list[FleetRepo]:
    if not queries:
        return repos

    selected: list[FleetRepo] = []
    unmatched: list[str] = []
    for query in queries:
        matches = [repo for repo in repos if repo_matches(repo, query)]
        if not matches:
            unmatched.append(query)
            continue
        if len(matches) > 1:
            rendered = ", ".join(repo.selector for repo in matches)
            die(f"ambiguous repo selector: {query} ({rendered})")
        if matches[0] not in selected:
            selected.append(matches[0])
    if unmatched:
        die(f"unknown repo selector(s): {', '.join(unmatched)}")
    return selected


def select_repos(queries: list[str]) -> list[FleetRepo]:
    return select_instances(load_repos(), queries)


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
    """Read this queue's runtime row without creating or migrating a database."""
    if repo.root is None:
        return None
    queue = _registered_queue(repo.root)
    if queue is None:
        return None
    db_path = Path(db.get_db_path())
    if not db_path.is_file():
        return None
    conn = None
    try:
        conn = sqlite3.connect(db._readonly_uri(db_path), uri=True)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM orchestrator_runtime WHERE queue_id = ?",
            (queue["id"],),
        ).fetchone()
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
    if status == "waiting-dirty":
        return "waiting-dirty"
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


def tailscale_dashboard_url(local_url: str) -> str | None:
    """Return the HTTPS Serve URL proxying one loopback dashboard, if present."""
    return dashboard_tailscale.lookup_dashboard_url(local_url)


def preferred_dashboard_url(local_url: str, *, prefer_local: bool = False) -> str:
    """Return the operator-facing URL for one localhost dashboard."""
    return dashboard_tailscale.preferred_dashboard_url(
        local_url,
        prefer_local=prefer_local,
    )


def wait_dashboard_ready(repo: FleetRepo, *, timeout: float = 12.0) -> bool:
    """Wait for Fleet-visible dashboard metadata for one running repo."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if dashboard_status_url(repo) != "-":
            return True
        time.sleep(0.2)
    return dashboard_status_url(repo) != "-"


def tmux_target(session: str) -> str:
    """Force an exact tmux session match. Plain names can match a unique prefix."""
    return f"={session}"


def tmux_format_target(session: str) -> str:
    """Exact session target that resolves to the session's current pane.

    tmux 3.7 ``display-message -t =name`` matches the session and exits 0, but
    ``#{session_path}`` and ``#{pane_current_path}`` expand to empty. The
    trailing colon selects that session's current window and pane, which
    returns the paths used to prove ownership.
    """
    return f"{tmux_target(session)}:"


def tmux_has_session(session: str) -> bool:
    if shutil.which("tmux") is None or not session:
        return False
    return run(["tmux", "has-session", "-t", tmux_target(session)]).returncode == 0


def _tmux_format(session: str, fmt: str) -> str | None:
    if shutil.which("tmux") is None:
        return None
    result = run(["tmux", "display-message", "-p", "-t", tmux_format_target(session), fmt])
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    return text or None


def tmux_recorded_path(session: str) -> Path | None:
    recorded = _tmux_format(session, "#{session_path}")
    if recorded is None:
        recorded = _tmux_format(session, "#{pane_current_path}")
    if recorded is None:
        return None
    try:
        return canonical_path(Path(recorded))
    except OSError:
        return None


def tmux_session_matches_root(session: str, root: Path) -> bool:
    """Return whether this exact session was started in the selected root."""
    recorded = tmux_recorded_path(session)
    if recorded is None:
        return False
    try:
        return recorded == canonical_path(root)
    except OSError:
        return False


def reported_tmux_session(repo: FleetRepo) -> str:
    """Return the owned session name, ``unowned``, or ``-``.

    Status display maps ``unowned`` back to ``-``. Start and stop use the
    sentinel so a name collision is not treated as a free session.
    """
    if repo.root is None or not tmux_has_session(repo.session):
        return "-"
    if not tmux_session_matches_root(repo.session, repo.root):
        return UNOWNED_SESSION
    return repo.session


def session_is_owned(repo: FleetRepo) -> bool:
    """Exact session name plus a recorded path for this canonical root."""
    if repo.root is None or not tmux_has_session(repo.session):
        return False
    return tmux_session_matches_root(repo.session, repo.root)


def session_runs_orchestrator(session: str) -> bool:
    """Return whether a pane process is the Fleet-launched orchestrator."""
    if shutil.which("tmux") is None:
        return False
    result = run(["tmux", "list-panes", "-t", tmux_target(session), "-F", "#{pane_pid}"])
    if result.returncode != 0:
        return False
    commands = dict(process_table())
    for line in result.stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            pid = int(text)
        except ValueError:
            continue
        command = commands.get(pid, "")
        if "ko-orchestrator" in command or is_orchestrator_command(command):
            return True
    return False


def interrupt_tmux_session(session: str) -> None:
    """Ask the pane to exit, then kill the session if it is still present.

    ``send-keys`` needs the pane-qualified exact target. A bare ``=name``
    session target is not a pane on tmux 3.7. Session checks and ``kill-session``
    keep the exact session target.
    """
    subprocess.run(
        ["tmux", "send-keys", "-t", tmux_format_target(session), "C-c"],
        check=False,
    )
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and tmux_has_session(session):
        time.sleep(0.1)
    if tmux_has_session(session):
        subprocess.run(
            ["tmux", "kill-session", "-t", tmux_target(session)],
            check=False,
        )


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
        kind = "worktree" if is_linked_worktree(root) else "primary"
        repo = FleetRepo(label_for(root), root, root, managed=False, kind=kind)
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
    return select_instances(repos, queries)


def repo_process_state(repo: FleetRepo) -> tuple[str, str, str, str]:
    if repo.error:
        return "invalid", "-", "-", "-"

    session_name = reported_tmux_session(repo)
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
        session_name,
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
    if not invalid:
        return

    print("Fleet precheck failed; not starting anything.", file=sys.stderr)
    for repo in invalid:
        print(f"\n{repo.label}: {display_path(repo.path)}", file=sys.stderr)
        print(f"  {repo.error}", file=sys.stderr)
    raise SystemExit(1)


def current_repo_root() -> Path | None:
    """Return the git root containing the current directory, if any."""
    result = run(["git", "rev-parse", "--show-toplevel"], cwd=Path.cwd())
    if result.returncode != 0:
        return None
    try:
        return Path(result.stdout.strip()).resolve()
    except OSError:
        return None


def current_fleet_repo(repos: list[FleetRepo]) -> FleetRepo | None:
    """Return the configured fleet repo containing the current directory."""
    root = current_repo_root()
    if root is None:
        return None
    for repo in repos:
        if repo.managed and repo.root is not None and repo.root.resolve() == root:
            return repo
    return None


def print_status(repos: list[FleetRepo]) -> None:
    rows = []
    for repo in repos:
        status, orch_pid, dashboard_pid, tmux = repo_process_state(repo)
        if tmux == UNOWNED_SESSION:
            tmux = "-"
        dashboard_url = "-" if repo.error else dashboard_status_url(repo)
        owner = "managed" if repo.managed else "unmanaged"
        root_display = display_path(repo.root or repo.path)
        if repo.parent is not None:
            root_display = f"{root_display} parent {display_path(repo.parent)}"
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
                repo.kind,
                repo.selector,
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
        max(len("kind"), *(len(row[6]) for row in rows)),
        max(len("selector"), *(len(row[7]) for row in rows)),
    ]
    print(
        f"{'repo':<{widths[0]}}  {'status':<{widths[1]}}  "
        f"{'orch/dash':>{widths[2]}}  {'tmux':<{widths[3]}}  "
        f"{'dash_url':<{widths[4]}}  {'owner':<{widths[5]}}  "
        f"{'kind':<{widths[6]}}  {'selector':<{widths[7]}}  root"
    )
    print(
        f"{'-' * widths[0]}  {'-' * widths[1]}  "
        f"{'-' * widths[2]}  {'-' * widths[3]}  "
        f"{'-' * widths[4]}  {'-' * widths[5]}  "
        f"{'-' * widths[6]}  {'-' * widths[7]}  ----"
    )
    for label, status, process_pids, tmux, dashboard_url, owner, kind, selector, root in rows:
        print(
            f"{label:<{widths[0]}}  {status:<{widths[1]}}  "
            f"{process_pids:>{widths[2]}}  {tmux:<{widths[3]}}  "
            f"{dashboard_url:<{widths[4]}}  {owner:<{widths[5]}}  "
            f"{kind:<{widths[6]}}  {selector:<{widths[7]}}  {root}"
        )

    current_repo = current_fleet_repo(repos)
    if current_repo is not None:
        status, _, _, _ = repo_process_state(current_repo)
        dashboard_url = dashboard_status_url(current_repo) if not current_repo.error else "-"
        if dashboard_url != "-":
            dashboard_display = preferred_dashboard_url(dashboard_url)
        else:
            dashboard_display = "not running"
        print()
        print(
            f"This repo ({current_repo.label}) is {status}. "
            f"{dashboard_tailscale.format_dashboard_line(dashboard_display)}"
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


def start_tmux_session(repo: FleetRepo, *, preferred_port: int, orchestrator: Path) -> bool:
    """Create the fleet tmux session for one stopped repo. Return dashboard-ready."""
    subprocess.run(
        [
            "tmux", "new-session", "-d", "-s", repo.session, "-c", str(repo.root),
            str(orchestrator), "--dashboard-port", str(preferred_port),
        ],
        check=True,
    )
    return wait_dashboard_ready(repo)


def try_start_repo(repo: FleetRepo, *, preferred_port: int | None = None) -> str | None:
    """Start one configured repo without exiting.

    Return None on success, or a concise failure reason.
    """
    if preferred_port is None:
        preferred_port = dashboard_port_for_index(0)
    if not repo.managed:
        return "Unmanaged repo"
    if repo.error or repo.root is None:
        return "Invalid config"

    status, _, dashboard_pid, session = repo_process_state(repo)
    if status_is_running(status):
        if dashboard_pid == "-":
            request_dashboard_start(repo, preferred_port=preferred_port)
            wait_dashboard_ready(repo)
        return None
    if session == UNOWNED_SESSION:
        return (
            f"tmux session {repo.session} is in use without proof it belongs to this repo; "
            "left untouched"
        )
    if session != "-":
        return f"tmux session already exists without a live orchestrator ({session})"
    if shutil.which("tmux") is None:
        return "required tool not found on PATH: tmux"

    orchestrator = ORCHESTRA_ROOT / "bin" / "ko-orchestrator"
    if not orchestrator.exists() or not os.access(orchestrator, os.X_OK):
        return "orchestrator executable not found"
    try:
        start_tmux_session(repo, preferred_port=preferred_port, orchestrator=orchestrator)
    except (OSError, subprocess.CalledProcessError):
        return "Start failed"
    return None


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
        if invalid:
            require_startable(invalid)
    elif invalid:
        for repo in invalid:
            print(f"{repo.label}: invalid config ({repo.error})", file=sys.stderr)
        raise SystemExit(1)
    startable_roots = {repo.root for repo in repos_to_start}
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
        if session == UNOWNED_SESSION:
            print(
                f"{repo.label}: tmux session {repo.session} is in use without proof it belongs "
                "to this repo; left untouched"
            )
            continue
        if session != "-":
            print(f"{repo.label}: tmux session already exists without a live orchestrator ({session})")
            continue
        if repo.root not in startable_roots:
            continue
        if start_tmux_session(repo, preferred_port=preferred_port, orchestrator=orchestrator):
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
        if session_is_owned(repo):
            interrupt_tmux_session(repo.session)
            print(f"{repo.label}: stopped tmux session")
            continue
        blocked = unowned_session_reason(repo)
        if blocked:
            print(f"{repo.label}: {blocked}")
            continue
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

        if repo.managed and session_is_owned(repo):
            interrupt_tmux_session(repo.session)
            print(f"{prefix}: stopped fleet tmux session")
            continue
        if repo.managed:
            blocked = unowned_session_reason(repo)
            if blocked:
                print(f"{prefix}: {blocked}")
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


def reap_disappeared_worktree_sessions(primaries: list[FleetRepo]) -> None:
    """Stop leftover sessions for linked worktrees Git still lists as missing.

    A session is reaped only when all of these are true: the worktree path is
    absent, the exact session name is the fleet token for that canonical path,
    tmux's recorded path is that path, and a pane process is the orchestrator.
    Anything less is left untouched. In particular, Fleet does not scan every
    tmux session, and it cannot prove ownership after ``git worktree prune``
    has dropped the path or after the orchestrator process has already exited.
    Missing primary-config entries are not reaped here; they stay visible as
    configuration errors.
    """
    if shutil.which("tmux") is None:
        return
    for primary in primaries:
        if primary.error or primary.root is None:
            continue
        try:
            parent = canonical_path(primary.root)
        except OSError:
            continue
        for entry in list_worktree_entries(parent):
            if not entry.missing:
                continue
            try:
                missing = canonical_path(entry.path)
            except OSError:
                continue
            if missing == parent:
                continue
            session = f"orch-{instance_token(missing)}"
            if not tmux_has_session(session):
                continue
            if not tmux_session_matches_root(session, missing):
                continue
            if not session_runs_orchestrator(session):
                continue
            interrupt_tmux_session(session)
            print(f"reaped disappeared worktree session {session}")


def one_repo(queries: list[str]) -> FleetRepo:
    if len(queries) != 1:
        die("this command requires exactly one repo label, unique selector, or path")
    repos = select_repos(queries)
    if len(repos) != 1:
        die("selector matched more than one repo")
    if repos[0].error:
        die(f"{repos[0].label}: {repos[0].error}")
    return repos[0]


def unowned_session_reason(repo: FleetRepo) -> str | None:
    """Return a refusal when the exact session name exists without root proof."""
    if repo.root is None or not tmux_has_session(repo.session):
        return None
    if tmux_session_matches_root(repo.session, repo.root):
        return None
    return (
        f"tmux session {repo.session} is in use without proof it belongs to this repo; "
        "left untouched"
    )


def attach(repo: FleetRepo) -> None:
    require_tool("tmux")
    if not session_is_owned(repo):
        blocked = unowned_session_reason(repo)
        if blocked:
            die(blocked)
        die(f"no fleet tmux session for {repo.label}")
    os.execvp("tmux", ["tmux", "attach", "-t", tmux_target(repo.session)])


def logs(repo: FleetRepo) -> None:
    if repo.log_path is None or not repo.log_path.exists():
        die(f"no orchestrator log found for {repo.label}: {repo.log_path}")
    os.execvp("tail", ["tail", "-f", str(repo.log_path)])


def _open_dashboard_url(url: str) -> None:
    if sys.platform == "darwin" and shutil.which("open"):
        subprocess.run(["open", url], check=False)
    print(dashboard_tailscale.format_dashboard_line(url))


def open_dashboard(repo: FleetRepo, *, prefer_local: bool = False) -> None:
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
    _open_dashboard_url(preferred_dashboard_url(str(url), prefer_local=prefer_local))


def fleet_dashboard_live_payload() -> dict | None:
    """Return live Fleet Dashboard metadata when the process and endpoint are up."""
    path = fleet_dashboard_metadata_path()
    payload = read_key_value_or_json(path)
    if payload.get("role") != "fleet-dashboard":
        return None
    try:
        pid = int(payload["pid"])
    except (KeyError, TypeError, ValueError):
        return None
    if not pid_alive(pid):
        return None
    url = payload.get("url")
    if not url or not dashboard_endpoint_ready(payload):
        return None
    return payload


def wait_fleet_dashboard_ready(*, timeout: float = 12.0) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = fleet_dashboard_live_payload()
        if payload is not None:
            return payload
        time.sleep(0.2)
    return fleet_dashboard_live_payload()


def start_fleet_dashboard_process() -> subprocess.Popen:
    """Spawn the Fleet Dashboard server as a detached background process."""
    script = Path(__file__).resolve().parent / "fleet_dashboard.py"
    if not script.exists():
        die(f"expected script not found: {script}")
    metadata_path = fleet_dashboard_metadata_path()
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["KO_FLEET_DASHBOARD_METADATA_PATH"] = str(metadata_path)
    env["KO_FLEET_DASH_PORT"] = str(FLEET_DASHBOARD_PORT)
    return subprocess.Popen(
        [sys.executable, str(script)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def open_or_start_fleet_dashboard(*, prefer_local: bool = False) -> None:
    """Open a running Fleet Dashboard, or start one and print its preferred URL."""
    with _fleet_dashboard_start_lock():
        payload = fleet_dashboard_live_payload()
        if payload is None:
            start_fleet_dashboard_process()
            payload = wait_fleet_dashboard_ready()
            if payload is None:
                die("fleet dashboard did not become ready")
    url = payload.get("url")
    if not url:
        die("fleet dashboard metadata is missing a URL")
    _open_dashboard_url(preferred_dashboard_url(str(url), prefer_local=prefer_local))


def init_config() -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        die(f"config already exists: {path}")
    path.write_text(
        "# One primary git repo root per line. Blank lines and # comments are ignored.\n"
        "# Initialized linked worktrees of these primaries are discovered automatically.\n"
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
        p.add_argument("repos", nargs="*", help="optional repo labels, unique selectors, or paths")

    for name in ("attach", "logs"):
        p = sub.add_parser(name)
        p.add_argument("repo", nargs=1, help="repo label, unique selector, or path")

    local_help = (
        "open the localhost dashboard URL instead of the preferred Tailscale URL"
    )
    p_dash = sub.add_parser(
        "dashboard",
        help="start or open the Fleet Dashboard, or a repo dashboard when a selector is given",
        description=(
            "With no repo argument, start or open the Fleet Dashboard and print "
            "its preferred Dashboard URL. With a repo selector, open that repo's "
            "preferred dashboard. Pass --local to open localhost."
        ),
    )
    p_dash.add_argument(
        "repo",
        nargs="?",
        help="repo label or path; omit to start or open the Fleet Dashboard",
    )
    p_dash.add_argument("--local", action="store_true", help=local_help)
    p_open = sub.add_parser(
        "dashboard-open",
        help="open a repo dashboard (requires a repo selector)",
    )
    p_open.add_argument("repo", nargs=1, help="repo label, unique selector, or path")
    p_open.add_argument("--local", action="store_true", help=local_help)

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

    if args.command in {"status", "precheck", "start", "stop", "stop-all", "restart"}:
        reap_disappeared_worktree_sessions(configured_primary_repos(missing_ok=True))

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
    elif args.command == "dashboard":
        if args.repo:
            open_dashboard(one_repo([args.repo]), prefer_local=args.local)
        else:
            open_or_start_fleet_dashboard(prefer_local=args.local)
    elif args.command == "dashboard-open":
        open_dashboard(one_repo(args.repo), prefer_local=args.local)
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
