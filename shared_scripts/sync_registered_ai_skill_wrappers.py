#!/usr/bin/env python3
"""Sync shared AI skill wrappers into every registered opted-in repo."""

from __future__ import annotations

import argparse
from pathlib import Path

from sync_ai_skill_wrappers import (
    _default_orchestra_dir,
    _default_registry_path,
    _git_repo_root,
    _read_marker_data,
    _read_registered_repo_paths,
    _skill_sync_marker_path,
    sync_registered_repos,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "List every repo registered with ko-sync-skills --register. "
            "Pass --apply to run fix mode and normal AI skill wrapper sync."
        )
    )
    parser.add_argument(
        "--orchestra-dir",
        default=_default_orchestra_dir(),
        help=(
            "Repo containing the canonical AI-skills directory. Defaults to "
            "`$ORCHESTRA_DIR`."
        ),
    )
    parser.add_argument(
        "--registry",
        default=None,
        help=(
            "Private repo registry path. Defaults to $ORCHESTRA_SKILL_SYNC_REPOS "
            "or ~/.config/orchestra/skill-sync.repos."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually fix and sync registered repos. Without this, only list what would be synced.",
    )
    args = parser.parse_args()
    if not args.orchestra_dir:
        parser.error("set ORCHESTRA_DIR or pass --orchestra-dir")
    return args


def registered_repo_rows(registry_path: Path) -> list[dict[str, str]]:
    rows = []
    for path in _read_registered_repo_paths(registry_path):
        row = {
            "status": "ok",
            "path": str(path),
            "project": "-",
        }
        if not path.exists():
            row["status"] = "missing"
            rows.append(row)
            continue

        repo_root = _git_repo_root(path)
        if repo_root is None:
            row["status"] = "not-git"
            rows.append(row)
            continue

        row["path"] = str(repo_root)
        marker = _skill_sync_marker_path(repo_root)
        if not marker.is_file():
            row["status"] = "unmarked"
            rows.append(row)
            continue

        raw_project = _read_marker_data(marker).get("devlog_project")
        if isinstance(raw_project, str) and raw_project.strip():
            row["project"] = raw_project.strip()
        rows.append(row)
    return rows


def print_registered_repo_rows(rows: list[dict[str, str]]) -> None:
    print("Registered AI skill repos (dry run; pass --apply to sync):")
    if not rows:
        print("  none")
        return
    for row in rows:
        print(f"  {row['status']:<8} {row['path']}  {row['project']}")


def main() -> int:
    args = parse_args()
    registry_path = Path(args.registry).expanduser() if args.registry else _default_registry_path()
    if not args.apply:
        print_registered_repo_rows(registered_repo_rows(registry_path))
        return 0

    summary = sync_registered_repos(Path(args.orchestra_dir), registry_path)
    print(
        "Registered AI skill repos synchronized:"
        f" synced={len(summary['synced'])}"
        f" skipped={len(summary['skipped'])}"
        f" failed={len(summary['failed'])}"
    )
    for key, label in (
        ("synced", "Synced"),
        ("skipped", "Skipped"),
        ("failed", "Failed"),
    ):
        print(f"{label} ({len(summary[key])}):")
        for item in summary[key]:
            print(f"  - {item}")
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
