#!/usr/bin/env python3
"""
kanban.py — bootstrap the current working repo for Kanban Orchestra.

Run from a work repo after exporting `ORCHESTRA_DIR` to the Orchestra checkout root.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db


def main():
    try:
        workspace = db.get_workspace()
        orchestra_dir = db.get_orchestra_dir()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        conn = db.connect(repo_root=workspace["repo_root"])
    except db.MigrationRequired as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    created = bool(getattr(conn, "orchestra_queue_created", False))
    queue_id = db.connection_queue_id(conn)
    conn.close()

    _ensure_gitignore(Path(workspace["repo_root"]))

    print(f"Repo root: {workspace['repo_root']}")
    print(f"Orchestra: {orchestra_dir}")
    print(f"Database: {workspace['db_path']}")
    print(f"Queue: {queue_id}")
    print(f"SQL dump: {workspace['sql_path']}")
    print("Status: created kanban queue" if created else "Status: using existing kanban queue")
    print("Ready for kanban task work.")


GITIGNORE_ENTRIES = [
    "kanban-orchestra.db",
    "kanban-orchestra.db-journal",
    ".kanban-orchestra/",
    "kanban-orchestra.db-shm",
    "kanban-orchestra.db-wal",
    "kanban-orchestra.lock",
]


def _ensure_gitignore(repo_root: Path) -> None:
    gitignore = repo_root / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    missing = [e for e in GITIGNORE_ENTRIES if e not in existing]
    if not missing:
        return
    separator = "\n" if existing and not existing.endswith("\n") else ""
    addition = separator + "\n# Kanban Orchestra\n" + "\n".join(missing) + "\n"
    with gitignore.open("a") as f:
        f.write(addition)


if __name__ == "__main__":
    main()
