#!/usr/bin/env python3
"""repo_policy.py — Parse repo-local policy markers from AGENTS.md."""

from pathlib import Path


def _read_standalone_marker(repo_root, marker: str) -> bool:
    """Return True when AGENTS.md contains marker as its own stripped line."""
    agents_md = Path(repo_root) / "AGENTS.md"
    if not agents_md.exists():
        return False
    try:
        text = agents_md.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        if line.strip() == marker:
            return True
    return False


def read_skip_build_until_approved(repo_root) -> bool:
    """Return True when AGENTS.md contains the deferred coder-build marker.

    The marker must appear as a standalone line (after stripping whitespace):
        CODER_SKIP_BUILD_UNTIL_APPROVED_BY_KANBAN_REVIEWER
    Freeform prose mentioning the marker name does not count.
    """
    return _read_standalone_marker(repo_root, "CODER_SKIP_BUILD_UNTIL_APPROVED_BY_KANBAN_REVIEWER")


def read_allow_tasks_on_master(repo_root) -> bool:
    """Return True if AGENTS.md in repo_root contains the ALLOW_TASKS_ON_MASTER marker.

    The marker must appear as a standalone line (after stripping whitespace):
        ALLOW_TASKS_ON_MASTER
    Freeform prose mentioning the marker name does not count.
    """
    return _read_standalone_marker(repo_root, "ALLOW_TASKS_ON_MASTER")
