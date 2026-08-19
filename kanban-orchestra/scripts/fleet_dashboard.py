#!/usr/bin/env python3
"""Collect Fleet Dashboard card state and serve the fleet-scoped HTML page."""

from __future__ import annotations

import atexit
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import dashboard
import dashboard_tailscale
import db
import fleet

app = FastAPI(title="Kanban Orchestra Fleet Dashboard")
_FAVICON = Path(__file__).resolve().parent.parent / "favicon.ico"


@dataclass(frozen=True)
class FleetCurrentTask:
    id: int
    title: str


@dataclass(frozen=True)
class FleetCard:
    name: str
    path: str
    branch: str | None
    status: str
    invalid: bool
    current_task: FleetCurrentTask | None
    ready_count: int
    recently_done_count: int
    icebox_count: int
    dashboard_url: str | None
    tailscale_url: str | None
    last_start_failure: str | None


def collect_cards() -> list[FleetCard]:
    """Return one card-data record per configured fleet repo."""
    return [collect_card(repo) for repo in fleet.load_repos()]


def collect_card(repo: fleet.FleetRepo) -> FleetCard:
    """Return card-data for one configured fleet repo from live state."""
    process_status, _, _, _ = fleet.repo_process_state(repo)
    runtime = fleet.repo_runtime_status(repo)
    conn = _open_repo_db(repo)
    try:
        status = _product_status(process_status, conn)
        current_task = None
        if not repo.error and status != "stopped":
            current_task = _current_task(runtime, conn)
        ready_count, recently_done_count, icebox_count = _queue_counts(conn)
    finally:
        if conn is not None:
            conn.close()

    dashboard_url = _local_dashboard_url(repo)
    tailscale_url = None
    if dashboard_url is not None:
        tailscale_url = fleet.tailscale_dashboard_url(dashboard_url)

    last_start_failure = None
    if status == "stopped" and fleet.dirty_lines(repo):
        last_start_failure = "Worktree dirty"

    return FleetCard(
        name=repo.label,
        path=fleet.display_path(repo.root or repo.path),
        branch=_repo_branch(repo),
        status=status,
        invalid=bool(repo.error),
        current_task=current_task,
        ready_count=ready_count,
        recently_done_count=recently_done_count,
        icebox_count=icebox_count,
        dashboard_url=dashboard_url,
        tailscale_url=tailscale_url,
        last_start_failure=last_start_failure,
    )


def _open_repo_db(repo: fleet.FleetRepo) -> sqlite3.Connection | None:
    """Open a repo Kanban DB read-only without creating or migrating it."""
    if repo.root is None:
        return None
    db_path = repo.root / "kanban-orchestra.db"
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _product_status(process_status: str, conn: sqlite3.Connection | None) -> str:
    if process_status == "invalid":
        return "error"
    if process_status == "stopped":
        return "stopped"

    activity = "busy"
    if process_status.startswith("running/"):
        activity = process_status.split("/", 1)[1]
    if activity == "idle":
        if _blocked_by_gate(conn):
            return "blocked"
        return "idle"
    if activity == "starting":
        return "starting"
    if activity in {"error", "hard-break"}:
        return "error"
    return "running"


def _blocked_by_gate(conn: sqlite3.Connection | None) -> bool:
    if conn is None:
        return False
    try:
        return bool(db.list_ready_tasks_blocked_by_blocked_gate(conn))
    except sqlite3.Error:
        return False


def _current_task(
    runtime: dict | None,
    conn: sqlite3.Connection | None,
) -> FleetCurrentTask | None:
    if not runtime or conn is None:
        return None
    task_id = runtime.get("current_task_id")
    if not task_id:
        return None
    try:
        row = conn.execute(
            "SELECT id, title FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return FleetCurrentTask(id=int(row["id"]), title=str(row["title"]))


def _queue_counts(conn: sqlite3.Connection | None) -> tuple[int, int, int]:
    if conn is None:
        return 0, 0, 0
    try:
        ready = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'ready'"
        ).fetchone()[0]
        icebox = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'none'"
        ).fetchone()[0]
        done_rows = conn.execute(
            "SELECT done_at FROM tasks WHERE status = 'done'"
        ).fetchall()
    except sqlite3.Error:
        return 0, 0, 0
    recently_done = sum(1 for row in done_rows if _is_recently_done(row["done_at"]))
    return int(ready), recently_done, int(icebox)


def _parse_utc_datetime(dt_str: str | None) -> datetime | None:
    """Parse a timestamp and normalize it to UTC, matching dashboard recency rules."""
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_recently_done(done_at: str | None) -> bool:
    dt = _parse_utc_datetime(done_at)
    if dt is None:
        return False
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 0:
        secs = 0
    return secs < int(dashboard.DONE_RECENCY_CUTOFF.total_seconds())


def _local_dashboard_url(repo: fleet.FleetRepo) -> str | None:
    if repo.error:
        return None
    url = fleet.dashboard_status_url(repo)
    if not url or url == "-":
        return None
    return url


def _repo_branch(repo: fleet.FleetRepo) -> str | None:
    if repo.root is None:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(repo.root), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch or None


FLEET_CSS = """
.nav-current {
  color: var(--ink);
  font-size: 0.84rem;
}

.repo-list {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
}

.repo-record {
  display: flex;
  min-width: 0;
  aspect-ratio: 1;
  margin: 0;
  flex-direction: column;
  overflow: hidden;
}

.repo-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--border);
}

.status-controls {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

.start-button {
  display: inline-grid;
  width: 22px;
  height: 22px;
  padding: 0;
  place-items: center;
  background: var(--accent-soft);
  border: 1px solid var(--accent-dim);
  border-radius: 2px;
  color: var(--accent);
  cursor: pointer;
}

.start-button::before {
  width: 0;
  height: 0;
  margin-left: 2px;
  border-top: 4px solid transparent;
  border-bottom: 4px solid transparent;
  border-left: 7px solid currentColor;
  content: "";
}

.start-button:hover {
  border-color: var(--accent);
  color: #ffffff;
}

.start-button:disabled {
  cursor: wait;
  opacity: 0.55;
}

.start-button.is-starting::before {
  width: auto;
  height: auto;
  margin: 0;
  border: 0;
  content: "...";
  font-family: inherit;
  font-size: 0.68rem;
}

.repo-name {
  display: block;
  overflow: hidden;
  color: var(--accent);
  font-size: 1.05rem;
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.repo-path,
.branch {
  margin-top: 3px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.repo-details {
  padding: 14px 0;
}

.detail-label {
  display: block;
  margin-bottom: 2px;
  color: var(--muted);
  font-size: 0.72rem;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}

.task-detail {
  margin-top: 2px;
}

.queue-boxes {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: 14px;
}

.queue-box {
  min-width: 0;
  padding: 9px 8px;
  background: #080808;
  border: 1px solid var(--border);
  border-radius: 2px;
}

.queue-count {
  display: block;
  margin-bottom: 2px;
  color: var(--ink);
  font-size: 1.18rem;
  line-height: 1.2;
}

.queue-label {
  display: block;
  overflow: hidden;
  color: var(--muted);
  font-size: 0.67rem;
  letter-spacing: 0.03em;
  line-height: 1.25;
  text-overflow: ellipsis;
  text-transform: uppercase;
}

.queue-box-ready .queue-count {
  color: var(--green);
}

.dashboard-links {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin-top: auto;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}

.dashboard-links a {
  display: flex;
  min-height: 30px;
  align-items: center;
  justify-content: center;
  padding: 5px 7px;
  background: var(--accent);
  border: 1px solid var(--accent);
  border-radius: 2px;
  color: var(--bg);
  font-size: 0.76rem;
  line-height: 1.2;
  text-align: center;
}

.dashboard-links .via-tailscale {
  grid-column: 3;
}

.dashboard-links a:hover {
  background: var(--accent-hover);
  color: var(--bg);
  text-decoration: none;
}

.dashboard-links,
.unavailable,
.start-result:last-child {
  margin-top: auto;
}

.unavailable,
.start-result {
  padding-top: 12px;
  border-top: 1px solid var(--border);
}

.unavailable {
  color: var(--muted);
}

.start-reason {
  color: var(--orange);
}

@media (max-width: 960px) {
  .repo-list {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 720px) {
  .repo-list {
    gap: 12px;
  }
}

@media (max-width: 560px) {
  .repo-list {
    grid-template-columns: 1fr;
  }
}
"""


FLEET_JS = r"""
(() => {
  const list = document.querySelector(".repo-list");
  if (!list) return;

  function showFailure(card, message) {
    let result = card.querySelector(".start-result");
    if (!result) {
      result = document.createElement("div");
      result.className = "start-result";
      result.setAttribute("aria-live", "polite");
      result.innerHTML =
        '<span class="detail-label">Last start failed</span>' +
        '<span class="start-reason"></span>';
      const unavailable = card.querySelector(".unavailable");
      if (unavailable) unavailable.replaceWith(result);
      else card.appendChild(result);
    }
    const reason = result.querySelector(".start-reason");
    if (reason) reason.textContent = message;
  }

  list.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-start-repo]");
    if (!button || button.disabled) return;
    const label = button.dataset.startRepo;
    const card = button.closest(".repo-record");
    if (!label || !card) return;

    button.disabled = true;
    button.classList.add("is-starting");
    try {
      const response = await fetch("/repos/" + encodeURIComponent(label) + "/start", {
        method: "POST",
        headers: { Accept: "application/json" },
      });
      const payload = await response.json();
      if (payload && payload.html) {
        card.outerHTML = payload.html;
        return;
      }
      button.classList.remove("is-starting");
      button.disabled = false;
      showFailure(card, (payload && payload.error) || "Start failed");
    } catch (err) {
      button.classList.remove("is-starting");
      button.disabled = false;
      showFailure(card, "Start failed");
    }
  });
})();
"""


def _esc(value) -> str:
    return dashboard._esc(value)


def _config_display() -> str:
    return fleet.display_path(fleet.config_path())


def _hostname_is_loopback(hostname: str | None) -> bool:
    if not hostname:
        return False
    return hostname.strip("[]").lower() in dashboard_tailscale.LOOPBACK_HOSTS


def _host_header_is_loopback(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlsplit("//" + value.split(",", 1)[0].strip())
    return _hostname_is_loopback(parsed.hostname)


def _origin_url_is_loopback(value: str) -> bool | None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return _hostname_is_loopback(parsed.hostname)


def _is_local_access(request: Request) -> bool:
    """Return True when the Fleet Dashboard is viewed from loopback.

    The process binds to loopback, so client IP cannot distinguish a local
    browser from Tailscale Serve. Origin, Referer, Host, and Serve's
    X-Forwarded-Host / X-Forwarded-Proto headers can. Non-loopback or HTTPS
    forwarded views never offer localhost dashboard links.
    """
    forwarded_host = request.headers.get("x-forwarded-host")
    if forwarded_host and not _host_header_is_loopback(forwarded_host):
        return False
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if forwarded_proto:
        proto = forwarded_proto.split(",", 1)[0].strip().lower()
        if proto == "https":
            return False
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if value:
            loopback = _origin_url_is_loopback(value)
            if loopback is not None:
                return loopback
    host = request.headers.get("host")
    if host:
        return _host_header_is_loopback(host)
    return False


def _repo_action_url(card: FleetCard, *, local_access: bool) -> str | None:
    if local_access:
        return card.dashboard_url
    return card.tailscale_url


def _new_tab_link(href: str, label: str, *, class_name: str | None = None) -> str:
    cls = f' class="{class_name}"' if class_name else ""
    return (
        f'<a{cls} href="{_esc(href)}" target="_blank" '
        f'rel="noopener noreferrer">{label}</a>'
    )


def _current_task_html(card: FleetCard, *, local_access: bool) -> str:
    if card.current_task is None:
        return '<span class="muted">None</span>'
    label = f"#{card.current_task.id} {_esc(card.current_task.title)}"
    href = _repo_action_url(card, local_access=local_access)
    if href:
        task_href = f"{href.rstrip('/')}/task/{card.current_task.id}"
        return _new_tab_link(task_href, label)
    return f"<span>{label}</span>"


def _status_html(card: FleetCard) -> str:
    badge_cls = dashboard.STATUS_BADGE_CLASS.get(card.status, "badge-none")
    badge = f'<span class="badge {badge_cls}">{_esc(card.status)}</span>'
    if card.invalid or card.status != "stopped":
        return badge
    return (
        '<div class="status-controls">'
        f"{badge}"
        f'<button class="start-button" type="button" '
        f'data-start-repo="{_esc(card.name)}" '
        f'aria-label="Start {_esc(card.name)}"></button>'
        "</div>"
    )


def _name_html(card: FleetCard, *, local_access: bool) -> str:
    name = _esc(card.name)
    href = _repo_action_url(card, local_access=local_access)
    if href:
        return _new_tab_link(href, name, class_name="repo-name")
    return f'<span class="repo-name">{name}</span>'


def _branch_html(card: FleetCard) -> str:
    branch = _esc(card.branch) if card.branch else "-"
    return f'<div class="branch muted"><code>{branch}</code></div>'


def _footer_html(card: FleetCard, *, local_access: bool) -> str:
    parts: list[str] = []
    if card.last_start_failure:
        parts.append(
            '<div class="start-result" aria-live="polite">'
            '<span class="detail-label">Last start failed</span>'
            f'<span class="start-reason">{_esc(card.last_start_failure)}</span>'
            "</div>"
        )
    links: list[str] = []
    if local_access and card.dashboard_url:
        links.append(_new_tab_link(card.dashboard_url, "Dashboard"))
    if card.tailscale_url:
        links.append(
            _new_tab_link(card.tailscale_url, "Via Tailscale", class_name="via-tailscale")
        )
    if links:
        parts.append(f'<div class="dashboard-links">{"".join(links)}</div>')
    elif not card.dashboard_url and not card.last_start_failure:
        parts.append('<div class="unavailable">Dashboard unavailable</div>')
    return "".join(parts)


def render_card(card: FleetCard, *, local_access: bool = True) -> str:
    """Return HTML for one fleet repository card."""
    return (
        f'<article class="card repo-record" data-repo-label="{_esc(card.name)}">'
        '<div class="repo-top">'
        f"<div>{_name_html(card, local_access=local_access)}"
        f'<div class="repo-path muted">{_esc(card.path)}</div>'
        f"{_branch_html(card)}</div>"
        f"{_status_html(card)}"
        "</div>"
        '<div class="repo-details">'
        "<div>"
        '<span class="detail-label">Current task</span>'
        f"{_current_task_html(card, local_access=local_access)}"
        "</div>"
        "</div>"
        '<div class="queue-boxes">'
        '<div class="queue-box queue-box-ready">'
        f'<span class="queue-count">{card.ready_count}</span>'
        '<span class="queue-label">Ready</span>'
        "</div>"
        '<div class="queue-box">'
        f'<span class="queue-count">{card.recently_done_count}</span>'
        '<span class="queue-label">Recently Done</span>'
        "</div>"
        '<div class="queue-box">'
        f'<span class="queue-count">{card.icebox_count}</span>'
        '<span class="queue-label">Icebox</span>'
        "</div>"
        "</div>"
        f"{_footer_html(card, local_access=local_access)}"
        "</article>"
    )


def render_page(
    cards: list[FleetCard],
    *,
    config_display: str | None = None,
    local_access: bool = True,
) -> str:
    """Return the Fleet Dashboard HTML document for *cards*."""
    if config_display is None:
        config_display = _config_display()
    cards_html = "\n".join(
        render_card(card, local_access=local_access) for card in cards
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Fleet Dashboard</title>
  <link rel="icon" type="image/x-icon" href="/favicon.ico">
  <script>{dashboard.accent_bootstrap_script()}</script>
  <style>{dashboard.COMMON_CSS}
{FLEET_CSS}</style>
</head>
<body>
  <nav aria-label="Orchestra navigation">
    <a class="nav-title" href="/">Kanban Orchestra</a>
    <span class="nav-current">Fleet</span>
    {dashboard.accent_picker_html()}
    <span class="nav-repo-path" title="{_esc(config_display)}">{_esc(config_display)}</span>
  </nav>
  <main>
    <h1>Fleet Dashboard</h1>
    <section class="repo-list" aria-label="Fleet repositories">
      {cards_html}
    </section>
  </main>
  <script>{dashboard.accent_picker_script()}</script>
  <script>{FLEET_JS}</script>
</body>
</html>
"""


@app.get("/favicon.ico")
def favicon():
    return FileResponse(_FAVICON, media_type="image/x-icon")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return HTMLResponse(
        render_page(collect_cards(), local_access=_is_local_access(request))
    )


def _configured_repo(label: str) -> fleet.FleetRepo | None:
    for repo in fleet.load_repos():
        if repo.label == label:
            return repo
    return None


def _card_payload(card: FleetCard) -> dict:
    return asdict(card)


def _start_payload(
    *,
    ok: bool,
    error: str | None = None,
    card: FleetCard | None = None,
    local_access: bool = True,
) -> dict:
    payload = {"ok": ok}
    if error:
        payload["error"] = error
    if card is not None:
        payload["card"] = _card_payload(card)
        payload["html"] = render_card(card, local_access=local_access)
    return payload


def _refreshed_card(repo: fleet.FleetRepo, error: str | None = None) -> FleetCard:
    card = collect_card(repo)
    if error and card.last_start_failure != error:
        return replace(card, last_start_failure=error)
    return card


@app.post("/repos/{label}/start")
def start_repo(label: str, request: Request):
    """Start one configured fleet repo and return refreshed collector card state."""
    local_access = _is_local_access(request)
    repo = _configured_repo(label)
    if repo is None:
        return JSONResponse(
            _start_payload(ok=False, error="Unknown repo", local_access=local_access),
            status_code=404,
        )
    if not repo.managed:
        return JSONResponse(
            _start_payload(ok=False, error="Unmanaged repo", local_access=local_access),
            status_code=400,
        )
    if repo.error:
        return JSONResponse(
            _start_payload(ok=False, error="Invalid config", local_access=local_access),
            status_code=400,
        )

    error = fleet.try_start_repo(repo)
    card = _refreshed_card(repo, error)
    if error:
        return JSONResponse(
            _start_payload(ok=False, error=error, card=card, local_access=local_access),
            status_code=409,
        )
    return JSONResponse(_start_payload(ok=True, card=card, local_access=local_access))


def _write_dashboard_metadata(host: str, port: int, remote_url: str | None = None) -> None:
    """Write fleet-scoped dashboard metadata beside fleet.repos."""
    path = fleet.fleet_dashboard_metadata_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "role": "fleet-dashboard",
        "pid": os.getpid(),
        "host": host,
        "port": port,
        "url": f"http://{host}:{port}",
        "remote_url": remote_url,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)
    atexit.register(_remove_dashboard_metadata)


def _remove_dashboard_metadata() -> None:
    """Remove fleet-scoped dashboard metadata when the process exits."""
    path = fleet.fleet_dashboard_metadata_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _run_dashboard(host: str, preferred_port: int, *, _uvicorn=None) -> None:
    """Find a free port and start the Fleet Dashboard uvicorn server."""
    if _uvicorn is None:
        try:
            import uvicorn as _uvicorn  # type: ignore[no-redef]
        except ModuleNotFoundError:
            print(
                "uvicorn is not installed. Run: python3 -m pip install uvicorn",
                file=sys.stderr,
            )
            raise SystemExit(1)

    port = dashboard._find_free_port(host, preferred_port)
    _write_dashboard_metadata(host, port, remote_url=None)

    def _record_remote(remote_url: str | None) -> None:
        if remote_url:
            _write_dashboard_metadata(host, port, remote_url=remote_url)

    dashboard_tailscale.schedule_publish_dashboard(
        host,
        port,
        on_resolved=_record_remote,
    )
    if port != preferred_port:
        print(
            f"Port {preferred_port} is in use; fleet dashboard starting on port {port}.",
            flush=True,
        )
    try:
        _uvicorn.run(
            "fleet_dashboard:app",
            host=host,
            port=port,
            reload=False,
            log_level="info",
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    _run_dashboard(
        host="127.0.0.1",
        preferred_port=int(os.environ.get("KO_FLEET_DASH_PORT", str(fleet.FLEET_DASHBOARD_PORT))),
    )
