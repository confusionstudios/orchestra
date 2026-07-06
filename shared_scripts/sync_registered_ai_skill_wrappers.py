#!/usr/bin/env python3
"""Sync shared AI skill wrappers into every registered opted-in repo."""

from __future__ import annotations

import argparse
from pathlib import Path

from sync_ai_skill_wrappers import (
    _default_orchestra_dir,
    _default_registry_path,
    sync_registered_repos,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run fix mode and normal AI skill wrapper sync for every repo "
            "registered with ko-sync-skills --register."
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
    args = parser.parse_args()
    if not args.orchestra_dir:
        parser.error("set ORCHESTRA_DIR or pass --orchestra-dir")
    return args


def main() -> int:
    args = parse_args()
    registry_path = Path(args.registry).expanduser() if args.registry else _default_registry_path()
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
