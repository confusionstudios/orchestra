#!/usr/bin/env python3
"""Regenerate the README dashboard screenshots from synthetic demo data.

Renders the real dashboard and Fleet Dashboard code against a throwaway repo
and database so the images always match the shipped UI. Every repo name, path,
branch, and task in the output is invented; nothing from a real fleet is read.

Requires the checkout virtualenv, which is where FastAPI and PyYAML live, and a
Playwright headless shell (`playwright install chromium`).

Usage:
    .venv/bin/python docs/make_screenshots.py [--out DOCS_DIR]
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Playwright's headless shell, not the desktop Chrome: it has no profile, no
# keychain access, and no first-run state, so it cannot disturb a real browser.
# Playwright caches under ~/Library/Caches on macOS and ~/.cache on Linux.
PLAYWRIGHT_CACHES = ("Library/Caches/ms-playwright", ".cache/ms-playwright")
CHROME_CANDIDATES = sorted(
    (
        found
        for cache in PLAYWRIGHT_CACHES
        for found in Path.home().glob(
            f"{cache}/chromium_headless_shell-*/"
            "chrome-headless-shell-*/chrome-headless-shell"
        )
    ),
    reverse=True,
)

FLEET_ACCENT = "violet"
REPO_ACCENT = "cyan"


def _utc(offset_minutes: float = 0) -> str:
    stamp = datetime.now(timezone.utc) - timedelta(minutes=offset_minutes)
    return stamp.strftime("%Y-%m-%d %H:%M:%S")


def _backdate(conn, task_id: int, **columns) -> None:
    """Set timestamp columns directly so ages read like a repo in real use."""
    sets = ", ".join(f"{name} = ?" for name in columns)
    conn.execute(
        f"UPDATE tasks SET {sets} WHERE id = ?", [*columns.values(), task_id]
    )
    conn.commit()


def force_accent(html: str, accent: str) -> str:
    """Pin the accent palette; the page normally reads it from a cookie."""
    needle = 'let requested = "green";'
    if needle not in html:
        raise SystemExit("accent bootstrap shape changed; update force_accent()")
    return html.replace(needle, f'let requested = "{accent}";')


def shoot(
    html_path: Path,
    png_path: Path,
    width: int,
    height: int,
    *,
    real_home: str,
    profile_dir: Path,
) -> None:
    """Screenshot one page with the headless shell in a scratch profile.

    The browser gets the real HOME back: this process fakes HOME so the page
    renders abbreviated paths, and a browser that inherited it would hunt for a
    profile and keychain that do not exist.
    """
    if not CHROME_CANDIDATES:
        raise SystemExit(
            "no Playwright chrome-headless-shell found; "
            "install one with `playwright install chromium`"
        )
    subprocess.run(
        [
            str(CHROME_CANDIDATES[0]),
            "--headless",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--password-store=basic",
            "--use-mock-keychain",
            f"--user-data-dir={profile_dir}",
            "--force-device-scale-factor=2",
            f"--screenshot={png_path}",
            f"--window-size={width},{height}",
            "--virtual-time-budget=4000",
            html_path.as_uri(),
        ],
        check=True,
        capture_output=True,
        timeout=90,
        env={**os.environ, "HOME": real_home},
    )


def seed_demo_db(conn, db):
    """Create the invented task set shown in the README screenshots."""
    goal = (
        "## Goal\n\n"
        "Show the Fleet Dashboard in the public README with realistic, "
        "privacy-safe data.\n\n"
        "## Acceptance criteria\n\n"
        "- Capture the current card grid and dashboard actions.\n"
        "- Keep every repo name and path synthetic.\n"
        "- Verify the dark-safe accent picker.\n"
    )
    t1 = db.add_task(
        conn,
        "Add Fleet Dashboard screenshots",
        description=goal,
        branch="feature/dashboard-preview",
        coder_agent="cursor:grok-4.6-high",
        reviewer_agent="codex",
    )
    db.update_task(
        conn, t1, status="running", next_step="commit-review", review_round=2
    )
    db.add_task_skip(conn, t1, "commit-plan")
    db.add_comment(
        conn,
        t1,
        "Implementation complete. Browser checks cover the repo and Fleet layouts.",
        kind="comment",
        author="cursor:grok-4.6-high",
        review_round=1,
    )
    db.add_comment(
        conn,
        t1,
        "Approved: the screenshots use synthetic data and match the shipped dashboard.",
        kind="approval",
        author="codex",
        review_round=1,
    )
    db.add_run_log(
        conn,
        t1,
        "Picked up task and rendered synthetic dashboard state",
        author="orchestrator",
    )
    db.add_run_log(
        conn, t1, "Screenshot validation complete", verb="done", author="orchestrator"
    )

    t2 = db.add_task(
        conn,
        "Document remote dashboard startup",
        branch="feature/dashboard-preview",
        coder_agent="cursor:grok-4.6-high",
        reviewer_agent="codex",
    )
    db.update_task(conn, t2, status="ready", next_step="commit-plan")

    t3 = db.add_task(
        conn,
        "Improve status card accessibility",
        branch="feature/dashboard-preview",
        coder_agent="codex",
        reviewer_agent="codex",
    )
    db.update_task(conn, t3, status="ready", next_step="commit-plan")

    t4 = db.add_task(
        conn,
        "Export a fleet health snapshot",
        branch="develop",
        coder_agent="cursor:grok-4.6-high",
        reviewer_agent="codex",
    )
    db.update_task(conn, t4, status="none")  # status 'none' is the Icebox
    _backdate(conn, t4, updated_at=_utc(6 * 60))

    t5 = db.add_task(
        conn,
        "Choose deployment notification policy",
        branch="feature/notifications",
        coder_agent="cursor:grok-4.6-high",
        reviewer_agent="codex",
    )
    db.update_task(conn, t5, status="blocked", block_reason="needs_decision")
    _backdate(conn, t5, updated_at=_utc(6 * 60))

    t6 = db.add_task(
        conn,
        "Add dark-safe accent picker",
        branch="feature/dashboard-colors",
        coder_agent="codex",
        reviewer_agent="codex",
    )
    db.update_task(conn, t6, status="done", commit_hash="a1b2c3d4", review_round=1)
    # done_at must land in its own call; update_task() stamps it CURRENT_TIMESTAMP
    # whenever status is part of the same update.
    _backdate(conn, t6, done_at=_utc(24), updated_at=_utc(24))

    t7 = db.add_task(
        conn,
        "Show review rounds in Recently Done",
        branch="feature/dashboard-metadata",
        coder_agent="cursor:grok-4.6-high",
        reviewer_agent="codex",
    )
    db.update_task(conn, t7, status="done", commit_hash="e5f6a7b8", review_round=2)
    _backdate(conn, t7, done_at=_utc(48), updated_at=_utc(48))

    db.upsert_runtime(
        conn,
        status="running",
        pid=4242,
        started_at=_utc(47),
        last_heartbeat_at=_utc(0.05),
        current_task_id=t1,
        current_step="commit-review",
        current_branch="feature/dashboard-preview",
        review_round=2,
        active_agents=1,
        status_message="Review round 2 in progress",
    )
    return t1


def build_fleet_cards(fleet_dashboard):
    """Return the invented fleet used in the README's Fleet Dashboard image."""
    Card = fleet_dashboard.FleetCard
    Task = fleet_dashboard.FleetCurrentTask

    def card(name, branch, status, task, ready, done, icebox, url, failure=None):
        return Card(
            name=name,
            path=f"~/src/{name}",
            branch=branch,
            status=status,
            invalid=False,
            current_task=task,
            ready_count=ready,
            recently_done_count=done,
            icebox_count=icebox,
            dashboard_url=url,
            tailscale_url=None,
            last_start_failure=failure,
        )

    return [
        card(
            "orchestra",
            "feature/fleet-dashboard",
            "running",
            Task(182, "Add Fleet Dashboard screenshots"),
            2, 8, 4,
            "http://127.0.0.1:8420",
        ),
        card("mobile-app", "develop", "idle", None, 1, 5, 7, "http://127.0.0.1:8421"),
        card(
            "api-service",
            "feature/auth-refresh",
            "blocked",
            Task(64, "Refresh authentication flow"),
            0, 3, 2,
            "http://127.0.0.1:8422",
        ),
        card(
            "docs-site", "main", "stopped", None, 3, 4, 1, None,
            failure="Worktree dirty",
        ),
        card(
            "desktop-client",
            "release/2.4",
            "starting",
            Task(27, "Prepare desktop release"),
            1, 6, 0,
            "http://127.0.0.1:8424",
        ),
        card(
            "automation", "develop", "error", None, 0, 2, 5, None,
            failure="Startup failed",
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None, help="docs directory for the PNGs")
    args = parser.parse_args()

    out_dir = Path(args.out).resolve() if args.out else Path(__file__).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)

    real_home = os.path.expanduser("~")
    work = Path(tempfile.mkdtemp(prefix="orchestra-screenshots-"))
    profile_dir = work / "chrome-profile"
    home = work / "home"
    repo = home / "src" / "orchestra"
    repo.mkdir(parents=True)

    # A real git repo so db.get_repo_root() resolves, under a fake home so the
    # nav path renders as ~/src/orchestra instead of a real location.
    env = {**os.environ, "HOME": str(home)}
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "demo@example.com"],
        ["git", "config", "user.name", "Demo"],
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
    ):
        subprocess.run(cmd, cwd=repo, env=env, check=True, capture_output=True)

    os.environ["HOME"] = str(home)
    os.chdir(repo)

    db_path = repo / "kanban-orchestra.db"
    os.environ["KANBAN_DB"] = str(db_path)

    sys.path.insert(0, str(ORCHESTRA_SCRIPTS))
    try:
        import db  # noqa: E402
        import dashboard  # noqa: E402
        import fleet_dashboard  # noqa: E402
        from fastapi.testclient import TestClient  # noqa: E402
    except ModuleNotFoundError as exc:
        raise SystemExit(
            f"missing dependency {exc.name!r}; run this with the checkout "
            "virtualenv: .venv/bin/python docs/make_screenshots.py"
        ) from exc

    conn = db.connect(str(db_path))
    task_id = seed_demo_db(conn, db)

    client = TestClient(dashboard.app)
    pages = [
        ("dashboard-overview", client.get("/").text, REPO_ACCENT, 1280, 2090),
        (
            "dashboard-task",
            client.get(f"/task/{task_id}").text,
            REPO_ACCENT,
            1280,
            1508,
        ),
        (
            "fleet-dashboard",
            fleet_dashboard.render_page(
                build_fleet_cards(fleet_dashboard),
                config_display="~/.config/orchestra/fleet.repos",
                local_access=True,
            ),
            FLEET_ACCENT,
            1440,
            790,
        ),
    ]

    for name, html, accent, width, height in pages:
        html_path = work / f"{name}.html"
        html_path.write_text(force_accent(html, accent), encoding="utf-8")
        png_path = out_dir / f"{name}.png"
        shoot(
            html_path,
            png_path,
            width,
            height,
            real_home=real_home,
            profile_dir=profile_dir,
        )
        print(f"wrote {png_path}")

    conn.close()
    shutil.rmtree(work, ignore_errors=True)
    return 0


ORCHESTRA_SCRIPTS = (
    Path(__file__).resolve().parent.parent / "kanban-orchestra" / "scripts"
)


if __name__ == "__main__":
    raise SystemExit(main())
