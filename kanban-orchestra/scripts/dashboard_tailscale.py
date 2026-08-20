#!/usr/bin/env python3
"""Best-effort Tailscale Serve publication for Orchestra dashboards.

Used by repo and Fleet dashboard startup after each process has chosen its
localhost port. Publication is scheduled in the background so Tailscale waits
cannot delay the localhost bind. Startup prints exactly one ``Dashboard:``
URL: the HTTPS mapping when publication resolves in time, otherwise localhost
from a background fallback timer. Lookup stays read-only so Fleet cards and
status commands can surface an existing exact HTTPS proxy without mutating
Serve config. Operator UX prefers that exact mapping through
``preferred_dashboard_url`` and falls back to localhost when it is absent.
"""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
COMMAND_TIMEOUT_SECONDS = 8.0
UP_TIMEOUT_SECONDS = 15.0
# One locked publication can run status, up, status, serve-status,
# serve-create, serve-status, and a fallback status. Wait at least that long
# per lock attempt so a holder finishing a legitimate sequence is not treated
# as a permanent failure. Retry enough times to cover a Fleet-sized burst.
PUBLISH_SEQUENCE_SECONDS = UP_TIMEOUT_SECONDS + 2.0 + (COMMAND_TIMEOUT_SECONDS * 6)
LOCK_WAIT_SECONDS = PUBLISH_SEQUENCE_SECONDS
LOCK_ATTEMPTS = 8
AUTH_BLOCKED_STATES = {"NeedsLogin", "NeedsMachineAuth"}
DEFAULT_LOCK_PATH = Path("~/.config/orchestra/dashboard-serve.lock")
# Bound how long a background timer waits for an in-flight Serve publication
# before announcing localhost. Shorter than up/serve/lock timeouts. The wait
# does not run on the server-starting thread, so a slow or hung Tailscale CLI
# cannot delay the localhost bind.
STARTUP_ANNOUNCE_WAIT_SECONDS = 1.0


def lookup_dashboard_url(local_url: str) -> str | None:
    """Return the HTTPS Serve URL proxying one loopback dashboard, if present."""
    local_port = _loopback_http_port(local_url)
    if local_port is None or shutil.which("tailscale") is None:
        return None
    payload = _serve_status_payload()
    if not payload:
        return None
    return lookup_in_payload(payload, local_port)


def preferred_dashboard_url(local_url: str, *, prefer_local: bool = False) -> str:
    """Return the operator-facing URL for one localhost dashboard.

    Prefers the exact HTTPS Tailscale Serve mapping when one exists. Falls
    back to *local_url* when lookup is skipped, unavailable, or unmapped.
    """
    if prefer_local:
        return local_url
    return lookup_dashboard_url(local_url) or local_url


def format_dashboard_line(url: str) -> str:
    """Return the canonical operator label for one dashboard URL."""
    return f"Dashboard: {url}"


def announce_startup_dashboard_url(local_url: str):
    """Return a one-shot callback that prints exactly one Dashboard URL.

    Call with a remote URL to announce it. Call with ``None`` or no argument to
    announce *local_url* when nothing has been printed yet. Later calls are
    ignored, so a late Serve mapping cannot add a second line.
    """
    state = {"printed": False}
    lock = threading.Lock()

    def announce(remote_url: str | None = None) -> None:
        with lock:
            if state["printed"]:
                return
            print(format_dashboard_line(remote_url or local_url), flush=True)
            state["printed"] = True

    return announce


def schedule_startup_dashboard_fallback(announce) -> threading.Timer:
    """Announce localhost after ``STARTUP_ANNOUNCE_WAIT_SECONDS`` if still needed.

    Returns immediately so the localhost server can bind without waiting for
    Tailscale. A remote callback that wins the one-shot race suppresses this
    fallback. A later mapping must not print a second Dashboard line.
    """
    timer = threading.Timer(STARTUP_ANNOUNCE_WAIT_SECONDS, announce)
    timer.daemon = True
    timer.name = "dashboard-startup-announce"
    timer.start()
    return timer


def lookup_in_payload(payload: dict, local_port: int) -> str | None:
    """Return the HTTPS Serve URL for *local_port* from Serve status JSON."""
    tcp = payload.get("TCP")
    web = payload.get("Web")
    if not isinstance(tcp, dict) or not isinstance(web, dict):
        return None

    for endpoint, server in sorted(web.items()):
        if not isinstance(endpoint, str) or not isinstance(server, dict):
            continue
        try:
            endpoint_port = urlsplit(f"//{endpoint}").port or 443
        except ValueError:
            continue
        listener = tcp.get(str(endpoint_port))
        if not isinstance(listener, dict) or listener.get("HTTPS") is not True:
            continue
        handlers = server.get("Handlers")
        if not isinstance(handlers, dict):
            continue
        for path, handler in sorted(handlers.items()):
            if not isinstance(path, str) or not isinstance(handler, dict):
                continue
            proxy = handler.get("Proxy")
            if not isinstance(proxy, str):
                continue
            if not _proxy_matches_local_port(proxy, local_port):
                continue
            route = path if path.startswith("/") else f"/{path}"
            return f"https://{endpoint}{route}"
    return None


def publish_dashboard(host: str, port: int) -> str | None:
    """Ensure an HTTPS Serve proxy for one loopback dashboard port.

    Prefers a same-port HTTPS listener when free; otherwise selects the next
    free listener without changing unrelated Serve routes. Failures never raise:
    missing CLI, auth, startup, lock exhaustion, or Serve errors return None so
    the localhost dashboard can still start.
    """
    try:
        return _publish_dashboard(host, port)
    except Exception:
        return None


def schedule_publish_dashboard(host: str, port: int, *, on_resolved) -> threading.Thread:
    """Publish in a daemon thread so localhost bind never waits on Tailscale.

    *on_resolved* is called with the Serve URL or None. Exceptions from
    publication or the callback are swallowed.
    """
    def _run() -> None:
        try:
            remote_url = publish_dashboard(host, port)
        except Exception:
            remote_url = None
        try:
            on_resolved(remote_url)
        except Exception:
            pass

    thread = threading.Thread(
        target=_run,
        name="dashboard-tailscale-publish",
        daemon=True,
    )
    thread.start()
    return thread


def serve_lock_path() -> Path:
    """Return the user-scoped flock that serializes machine-wide Serve updates.

    Repo and Fleet dashboard startups share this lock regardless of
    ORCHESTRA_FLEET_REPOS. Tests may override the path with
    KO_DASHBOARD_SERVE_LOCK_PATH.
    """
    override = os.environ.get("KO_DASHBOARD_SERVE_LOCK_PATH")
    if override:
        return Path(override).expanduser().resolve()
    return DEFAULT_LOCK_PATH.expanduser()


def _publish_dashboard(host: str, port: int) -> str | None:
    if host not in LOOPBACK_HOSTS or not _valid_port(port):
        return None
    if shutil.which("tailscale") is None:
        return None

    for _ in range(LOCK_ATTEMPTS):
        with _exclusive_serve_lock() as locked:
            if not locked:
                continue
            return _publish_locked(port)
    return None


def _publish_locked(port: int) -> str | None:
    if not _ensure_tailscale_running():
        return None
    payload = _serve_status_payload()
    if payload is None:
        return None
    existing = lookup_in_payload(payload, port)
    if existing:
        return existing
    https_port = _choose_https_listener_port(payload, port)
    if https_port is None:
        return None
    if not _create_https_mapping(https_port, local_port=port):
        return None
    refreshed = _serve_status_payload()
    if refreshed:
        created = lookup_in_payload(refreshed, port)
        if created:
            return created
    return _fallback_https_url(https_port)


def _ensure_tailscale_running() -> bool:
    state = _backend_state()
    if state == "Running":
        return True
    if state in AUTH_BLOCKED_STATES:
        return False
    timeout = f"{int(UP_TIMEOUT_SECONDS)}s"
    result = _run(["tailscale", "up", f"--timeout={timeout}"], timeout=UP_TIMEOUT_SECONDS + 2.0)
    if result is None or result.returncode != 0:
        retry_args = _suggested_up_args(result, timeout) if result is not None else None
        if retry_args is None:
            return False
        result = _run(retry_args, timeout=UP_TIMEOUT_SECONDS + 2.0)
        if result is None or result.returncode != 0:
            return False
    return _backend_state() == "Running"


def _suggested_up_args(
    result: subprocess.CompletedProcess[str],
    timeout: str,
) -> list[str] | None:
    """Return the CLI-suggested up command with Orchestra's bounded timeout.

    Tailscale requires every non-default preference to be repeated when an
    existing profile is brought up. The CLI prints a complete safe argv for
    that case. Parse it without a shell, retain those preferences, and reject
    suggestions that would reset the profile.
    """
    for raw_line in result.stderr.splitlines():
        line = raw_line.strip()
        if not line.startswith("tailscale up "):
            continue
        try:
            suggested = shlex.split(line)
        except ValueError:
            continue
        if suggested[:2] != ["tailscale", "up"]:
            continue

        preferences: list[str] = []
        skip_timeout_value = False
        for arg in suggested[2:]:
            if skip_timeout_value:
                skip_timeout_value = False
                continue
            if arg == "--timeout":
                skip_timeout_value = True
                continue
            if arg.startswith("--timeout="):
                continue
            if arg == "--reset":
                return None
            preferences.append(arg)
        if skip_timeout_value or not preferences:
            return None
        return ["tailscale", "up", f"--timeout={timeout}", *preferences]
    return None


def _backend_state() -> str | None:
    result = _run(["tailscale", "status", "--json"], timeout=COMMAND_TIMEOUT_SECONDS)
    payload = _load_json_object(result)
    if payload is None:
        return None
    state = payload.get("BackendState")
    return state if isinstance(state, str) else None


def _create_https_mapping(https_port: int, *, local_port: int) -> bool:
    result = _run(
        [
            "tailscale",
            "serve",
            "--bg",
            f"--https={https_port}",
            f"http://127.0.0.1:{local_port}",
        ],
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    return result is not None and result.returncode == 0


def _choose_https_listener_port(payload: dict, preferred: int) -> int | None:
    occupied = _occupied_listener_ports(payload)
    if preferred not in occupied:
        return preferred
    for candidate in range(preferred + 1, 65536):
        if candidate not in occupied:
            return candidate
    for candidate in range(1, preferred):
        if candidate not in occupied:
            return candidate
    return None


def _occupied_listener_ports(payload: dict) -> set[int]:
    occupied: set[int] = set()
    tcp = payload.get("TCP")
    if isinstance(tcp, dict):
        for key in tcp:
            try:
                occupied.add(int(key))
            except (TypeError, ValueError):
                continue
    web = payload.get("Web")
    if not isinstance(web, dict):
        return occupied
    for endpoint in web:
        if not isinstance(endpoint, str):
            continue
        try:
            endpoint_port = urlsplit(f"//{endpoint}").port or 443
        except ValueError:
            continue
        occupied.add(endpoint_port)
    return occupied


def _fallback_https_url(https_port: int) -> str | None:
    result = _run(["tailscale", "status", "--json"], timeout=COMMAND_TIMEOUT_SECONDS)
    payload = _load_json_object(result)
    if payload is None:
        return None
    self_status = payload.get("Self")
    if not isinstance(self_status, dict):
        return None
    dns_name = self_status.get("DNSName")
    if not isinstance(dns_name, str) or not dns_name.strip():
        return None
    host = dns_name.strip().rstrip(".")
    if not host:
        return None
    return f"https://{host}:{https_port}/"


def _serve_status_payload() -> dict | None:
    result = _run(["tailscale", "serve", "status", "--json"], timeout=COMMAND_TIMEOUT_SECONDS)
    if result is None or result.returncode != 0:
        return None
    text = result.stdout.strip()
    if not text or text == "null":
        return {}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        return None
    return payload


def _load_json_object(result: subprocess.CompletedProcess[str] | None) -> dict | None:
    if result is None or result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _loopback_http_port(local_url: str) -> int | None:
    try:
        local = urlsplit(local_url)
        port = local.port
    except (TypeError, ValueError):
        return None
    if local.scheme != "http" or local.hostname not in LOOPBACK_HOSTS or port is None:
        return None
    if not _valid_port(port):
        return None
    return port


def _proxy_matches_local_port(proxy: str, local_port: int) -> bool:
    try:
        target = urlsplit(proxy)
        target_port = target.port
    except ValueError:
        return False
    return (
        target.scheme == "http"
        and target.hostname in LOOPBACK_HOSTS
        and target_port == local_port
    )


def _valid_port(port: int) -> bool:
    return isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535


def _run(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


@contextmanager
def _exclusive_serve_lock():
    path = serve_lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+")
    except OSError:
        yield False
        return

    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    locked = False
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield locked
    finally:
        if locked:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()
