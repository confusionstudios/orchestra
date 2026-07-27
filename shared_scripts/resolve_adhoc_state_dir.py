#!/usr/bin/env python3
"""Resolve the shared per-user, per-Git-worktree ad-hoc handoff state directory.

Intended invocation:

  "$ORCHESTRA_DIR/bin/ko-adhoc-state-dir"
  "$ORCHESTRA_DIR/bin/ko-adhoc-state-dir" --ensure

Resolution order:

1. `$ORCH_ADHOC_STATE_DIR` when set (exact directory; caller owns isolation).
2. Otherwise `$XDG_STATE_HOME/orchestra/adhoc/<repo-key>/<worktree-key>/`,
   falling back to `~/.local/state/orchestra/adhoc/<repo-key>/<worktree-key>/`
   when `XDG_STATE_HOME` is unset.

`<repo-key>` is the SHA-256 hex digest of this repository's absolute
`git-common-dir` (shared by linked worktrees). `<worktree-key>` is the
SHA-256 hex digest of this worktree's absolute `git-dir` (distinct per
linked worktree). Every skill in one worktree resolves the same directory;
separate worktrees and distinct repositories do not share state.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path


STATE_ENV = "ORCH_ADHOC_STATE_DIR"
XDG_STATE_ENV = "XDG_STATE_HOME"


def _absolute_git_path(flag: str, cwd: Path | None = None) -> Path:
    """Return an absolute path from `git rev-parse` for the given path flag."""
    start = (cwd or Path.cwd()).resolve()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", flag],
            cwd=start,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        try:
            result = subprocess.run(
                ["git", "rev-parse", flag],
                cwd=start,
                capture_output=True,
                text=True,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ValueError(
                f"not inside a Git repository (cwd={start})"
            ) from exc
    except OSError as exc:
        raise ValueError(f"failed to resolve {flag} (cwd={start})") from exc

    raw = result.stdout.strip()
    if not raw:
        raise ValueError(f"{flag} was empty (cwd={start})")
    path = Path(raw)
    if not path.is_absolute():
        path = (start / path).resolve()
    else:
        path = path.resolve()
    return path


def git_common_dir(cwd: Path | None = None) -> Path:
    """Return the absolute git-common-dir for the repository containing cwd."""
    return _absolute_git_path("--git-common-dir", cwd)


def git_dir(cwd: Path | None = None) -> Path:
    """Return the absolute git-dir for the worktree containing cwd."""
    return _absolute_git_path("--git-dir", cwd)


def repo_key(cwd: Path | None = None) -> str:
    """Stable filesystem-safe key for the Git repository containing cwd."""
    common = git_common_dir(cwd)
    return hashlib.sha256(str(common).encode("utf-8")).hexdigest()


def worktree_key(cwd: Path | None = None) -> str:
    """Stable filesystem-safe key for the Git worktree containing cwd."""
    directory = git_dir(cwd)
    return hashlib.sha256(str(directory).encode("utf-8")).hexdigest()


def default_state_base() -> Path:
    """Return the XDG-style user state base for Orchestra ad-hoc data."""
    xdg = os.environ.get(XDG_STATE_ENV, "").strip()
    if xdg:
        return Path(xdg).expanduser()
    return Path.home() / ".local" / "state"


def resolve_adhoc_state_dir(cwd: Path | None = None) -> Path:
    """Resolve the ad-hoc handoff state directory for the current worktree."""
    override = os.environ.get(STATE_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()

    return (
        default_state_base()
        / "orchestra"
        / "adhoc"
        / repo_key(cwd)
        / worktree_key(cwd)
    ).resolve()


def ensure_adhoc_state_dir(cwd: Path | None = None) -> Path:
    """Resolve the state directory and create it if missing."""
    path = resolve_adhoc_state_dir(cwd)
    path.mkdir(parents=True, exist_ok=True)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the Orchestra ad-hoc handoff state directory."
    )
    parser.add_argument(
        "--ensure",
        action="store_true",
        help="create the directory if it does not exist",
    )
    parser.add_argument(
        "--cwd",
        type=Path,
        default=None,
        help="resolve relative to this working directory (default: process cwd)",
    )
    args = parser.parse_args(argv)

    try:
        path = (
            ensure_adhoc_state_dir(args.cwd)
            if args.ensure
            else resolve_adhoc_state_dir(args.cwd)
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
