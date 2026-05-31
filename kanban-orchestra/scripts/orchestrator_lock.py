#!/usr/bin/env python3
"""Helpers for the repo-scoped Kanban orchestrator singleton lock."""

from pathlib import Path

import fcntl

import db


def read_singleton_lock_metadata(
    db_path: str | None = None,
    *,
    lock_path: str | Path | None = None,
) -> dict[str, str]:
    """Read best-effort identity metadata from the repo singleton lock file."""
    path = Path(lock_path).resolve() if lock_path is not None else db.get_lock_path(db_path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return {}

    metadata = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key:
            metadata[key] = value
    return metadata


def singleton_lock_available(db_path: str | None = None) -> bool:
    """Return whether the repo-scoped orchestrator singleton lock is free."""
    lock_path = db.get_lock_path(db_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()
