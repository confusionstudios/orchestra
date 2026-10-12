#!/usr/bin/env python3
"""Deterministic Git and Kanban checks for create-codex-worktree."""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
from pathlib import Path


KANBAN_FILES = (
    "kanban-orchestra.db",
    "kanban-orchestra.db-journal",
    "kanban-orchestra.db-shm",
    "kanban-orchestra.db-wal",
    "kanban-orchestra.sql",
    "kanban-orchestra.lock",
)
KANBAN_DIRS = (".kanban-orchestra",)
FORBIDDEN_TOP = {".git", ".kanban-orchestra"}
FORBIDDEN_NAMES = {
    "kanban-orchestra.sql",
    "kanban-orchestra.lock",
    "agents.local.yaml",
}
ROOT_AUTH = {"auth.json", "credentials.json", "credentials.yml", ".netrc"}
RUNTIME_FILES = ("shared_scripts/agents.local.yaml",)
LONG_LIVED_BRANCHES = ("develop", "main", "master")


class PrepError(Exception):
    """Abort a create-codex-worktree helper step."""


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"git {' '.join(args)} failed"
        raise PrepError(detail)
    return result


def _resolve_root(root: Path | None) -> Path:
    start = (root or Path.cwd()).resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise PrepError(result.stderr.strip() or f"not a git repository: {start}")
    return Path(result.stdout.strip()).resolve()


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except OSError:
        return False


def _is_nonsymlink_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _assert_safe_pattern(pattern: str) -> None:
    relative = pattern.lstrip("/")
    if not relative or ".." in Path(relative).parts:
        raise PrepError(f"unsafe .worktreeinclude pattern: {pattern}")


def _ref_exists(root: Path, branch: str) -> bool:
    result = _git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    return result.returncode == 0


def _current_branch(root: Path) -> str:
    result = _git(root, "branch", "--show-current")
    return result.stdout.strip()


def preflight(root: Path | None = None) -> Path:
    current_root = _resolve_root(root)
    status = _git(current_root, "status", "--short")
    if status.stdout.strip():
        raise PrepError(
            "invoking worktree is dirty; make it clean before creating a Codex "
            "worktree. Do not stash, commit, or discard changes for the user."
        )
    manifest = current_root / ".worktreeinclude"
    if not _is_regular_file(manifest):
        raise PrepError(
            ".worktreeinclude is missing or is not a regular file; create and "
            "review it before continuing. Do not create it for the user."
        )
    return current_root


def require_inputs(
    mode: str | None,
    branch: str | None,
    parking: str | None = None,
    base: str | None = None,
) -> None:
    missing: list[str] = []
    if mode not in {"existing", "new"}:
        missing.append("mode (`existing` or `new`)")
    if not branch:
        missing.append("target branch")
    if mode == "existing" and not parking:
        missing.append("parking branch")
    if mode == "new" and not base:
        missing.append("base branch")
    if missing:
        raise PrepError(
            "missing required value(s): "
            + ", ".join(missing)
            + ". Ask the user."
        )


def _attached_branch(root: Path) -> str | None:
    result = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _porcelain_path(line: str) -> Path:
    raw = line[len("worktree "):]
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        raw = raw[1:-1]
    return Path(raw)


def _branch_holder(root: Path, branch: str) -> Path | None:
    result = _git(root, "worktree", "list", "--porcelain")
    current: Path | None = None
    holder: Path | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current = _porcelain_path(line)
        elif line == f"branch refs/heads/{branch}":
            holder = current
    return holder.resolve() if holder is not None else None


def _assert_branch_available(root: Path, branch: str, *allowed: Path) -> None:
    holder = _branch_holder(root, branch)
    if holder is None:
        return
    if holder in {path.resolve() for path in allowed}:
        return
    raise PrepError(f"target branch {branch} is checked out in {holder}")


def resolve_creation(
    root: Path | None,
    mode: str | None,
    branch: str | None,
    parking: str | None = None,
    base: str | None = None,
) -> tuple[str, str, str | None, str | None]:
    """Fill mode, base, and parking when the checkout identifies one value."""
    current_root = _resolve_root(root)
    if not branch or not branch.strip():
        raise PrepError("missing required value(s): target branch. Ask the user; do not invent a branch.")
    branch = branch.strip()
    if mode not in {None, "existing", "new"}:
        raise PrepError("missing required value(s): mode (`existing` or `new`). Ask the user.")
    if mode is None:
        mode = "existing" if _ref_exists(current_root, branch) else "new"
    if mode == "existing":
        if not parking:
            current = _attached_branch(current_root)
            if current and current != branch:
                parking = current
            else:
                candidates = [
                    name for name in LONG_LIVED_BRANCHES
                    if name != branch and _ref_exists(current_root, name)
                ]
                if len(candidates) != 1:
                    listed = ", ".join(candidates) if candidates else "none"
                    raise PrepError(f"parking branch is ambiguous for {branch} ({listed}). Ask the user.")
                parking = candidates[0]
        return mode, branch, parking, None
    if not base:
        current = _attached_branch(current_root)
        if not current:
            raise PrepError("base branch is ambiguous because the checkout is detached. Ask the user.")
        if current == branch:
            raise PrepError(f"base branch is ambiguous for new branch {branch}. Ask the user.")
        base = current
    return mode, branch, None, base


def prepare_existing(root: Path, branch: str, parking: str) -> Path:
    current_root = preflight(root)
    require_inputs("existing", branch, parking=parking)
    if branch == parking:
        raise PrepError("parking branch must differ from the target branch")
    if not _ref_exists(current_root, branch):
        raise PrepError(f"existing branch does not exist: {branch}")
    if not _ref_exists(current_root, parking):
        raise PrepError(f"parking branch does not exist: {parking}")
    _assert_branch_available(current_root, branch, current_root)
    if _current_branch(current_root) == branch:
        _git(current_root, "switch", parking)
    return current_root


def prepare_new(root: Path, branch: str, base: str) -> Path:
    current_root = preflight(root)
    require_inputs("new", branch, base=base)
    format_check = _git(current_root, "check-ref-format", "--branch", branch, check=False)
    if format_check.returncode != 0:
        raise PrepError(f"invalid new branch name: {branch}")
    if not _ref_exists(current_root, base):
        raise PrepError(f"base branch does not exist: {base}")
    if _ref_exists(current_root, branch):
        raise PrepError(f"new branch already exists: {branch}")
    _git(current_root, "switch", base)
    return current_root


def attach_existing(root: Path, branch: str) -> str:
    worktree = _resolve_root(root)
    _assert_branch_available(worktree, branch, worktree)
    _git(worktree, "switch", branch)
    attached = _git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    if attached != branch:
        raise PrepError(f"worktree did not attach to {branch}")
    return attached


def attach_new(root: Path, branch: str) -> str:
    worktree = _resolve_root(root)
    _git(worktree, "switch", "-c", branch)
    attached = _git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    if attached != branch:
        raise PrepError(f"worktree did not create {branch}")
    return attached


def _manifest_patterns(manifest: Path) -> list[str]:
    patterns: list[str] = []
    for raw in manifest.read_text(encoding="utf-8").splitlines():
        pattern = raw.strip()
        if not pattern or pattern.startswith("#"):
            continue
        patterns.append(pattern)
    return patterns


def _is_forbidden(relative: Path) -> bool:
    parts = relative.parts
    if not parts or parts[0] in FORBIDDEN_TOP or ".git" in parts or ".kanban-orchestra" in parts:
        return True
    name = parts[-1]
    if name.startswith("kanban-orchestra.db") or name in FORBIDDEN_NAMES:
        return True
    return len(parts) == 1 and name in ROOT_AUTH


def _ls_untracked_ignored(root: Path, *exclude_args: str) -> set[str]:
    result = _git(
        root,
        "ls-files",
        "-z",
        "--others",
        "--ignored",
        *exclude_args,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "git ls-files failed"
        raise PrepError(detail)
    files: set[str] = set()
    for raw in result.stdout.split("\0"):
        if not raw:
            continue
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts:
            continue
        files.add(relative.as_posix())
    return files


def _ignored_manifest_files(source: Path) -> list[Path]:
    root = source.resolve()
    manifest = root / ".worktreeinclude"
    for pattern in _manifest_patterns(manifest):
        _assert_safe_pattern(pattern)
    selected = _ls_untracked_ignored(root, f"--exclude-from={manifest}")
    ignored = _ls_untracked_ignored(root, "--exclude-standard")
    files: list[Path] = []
    rejected: list[str] = []
    for key in sorted(selected & ignored):
        relative = Path(key)
        if _is_forbidden(relative):
            rejected.append(relative.as_posix())
            continue
        if _is_nonsymlink_regular_file(root / relative):
            files.append(relative)
    if rejected:
        raise PrepError(
            "refusing Kanban, auth, or runtime state selected by .worktreeinclude: "
            + ", ".join(rejected)
        )
    return files


def verify_copy(source: Path, dest: Path) -> list[str]:
    source = source.resolve()
    dest = dest.resolve()
    manifest = source / ".worktreeinclude"
    if not _is_regular_file(manifest):
        raise PrepError(
            ".worktreeinclude is missing or is not a regular file in the source checkout"
        )
    missing: list[str] = []
    mismatched: list[str] = []
    copied: list[str] = []
    for relative in _ignored_manifest_files(source):
        src_file = source / relative
        dest_file = dest / relative
        copied.append(relative.as_posix())
        if not dest_file.is_file():
            missing.append(relative.as_posix())
            continue
        if dest_file.read_bytes() != src_file.read_bytes():
            mismatched.append(relative.as_posix())
    if missing or mismatched:
        problems = []
        if missing:
            problems.append("missing: " + ", ".join(missing))
        if mismatched:
            problems.append("content mismatch: " + ", ".join(mismatched))
        raise PrepError("worktreeinclude copy verification failed; " + "; ".join(problems))
    return copied


def verify_kanban_absent(root: Path | None = None) -> Path:
    worktree = _resolve_root(root)
    present: list[str] = []
    for name in (*KANBAN_FILES, *ROOT_AUTH, *RUNTIME_FILES):
        if (worktree / name).exists():
            present.append(name)
    for name in KANBAN_DIRS:
        if (worktree / name).exists():
            present.append(name + "/")
    if present:
        raise PrepError(
            "Kanban database/runtime state is present in the new worktree: "
            + ", ".join(present)
            + ". Stop; do not run ko-kanban."
        )
    return worktree


def bootstrap_kanban(root: Path | None = None) -> Path:
    worktree = verify_kanban_absent(root)
    orchestra_dir = os.environ.get("ORCHESTRA_DIR", "").strip()
    if not orchestra_dir:
        raise PrepError("ORCHESTRA_DIR is not set")
    ko_kanban = Path(orchestra_dir) / "bin" / "ko-kanban"
    if not os.access(ko_kanban, os.X_OK):
        raise PrepError(f"ko-kanban is not executable: {ko_kanban}")
    env = os.environ.copy()
    env.pop("KANBAN_DB", None)
    result = subprocess.run(
        [str(ko_kanban)],
        cwd=worktree,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise PrepError((result.stderr or result.stdout).strip() or "ko-kanban failed")
    if "Status: created kanban database" not in result.stdout:
        raise PrepError(
            "ko-kanban did not create a pristine database:\n" + result.stdout.strip()
        )
    db_line = next(
        (line for line in result.stdout.splitlines() if line.startswith("Database: ")),
        "",
    )
    db_path = Path(db_line.split(" ", 1)[1]).resolve() if db_line else worktree / "kanban-orchestra.db"
    if db_path != (worktree / "kanban-orchestra.db").resolve():
        raise PrepError(f"Kanban database is not in the new worktree: {db_path}")
    sys.stdout.write(result.stdout)
    if result.stdout and not result.stdout.endswith("\n"):
        sys.stdout.write("\n")
    return db_path


def fleet_commands(worktree: Path, orchestra_dir: Path) -> list[list[str]]:
    """Return the Fleet start and status commands for one worktree path."""
    binary = str(Path(orchestra_dir) / "bin" / "ko-fleet")
    root = str(worktree)
    return [[binary, "start", root], [binary, "status", root]]


def dashboard_url_from_status(text: str) -> str:
    marker = "Dashboard: "
    urls = [line.split(marker, 1)[1].strip() for line in text.splitlines() if marker in line]
    if len(urls) != 1:
        raise PrepError("fleet status did not report exactly one dashboard for the selected worktree")
    url = urls[0]
    if not url.startswith(("http://", "https://")):
        raise PrepError(f"worktree dashboard is not running: {url}")
    return url


def start_instance(root: Path | None = None) -> str:
    """Start and report only this worktree through Fleet's path selector."""
    worktree = _resolve_root(root)
    orchestra_dir = os.environ.get("ORCHESTRA_DIR", "").strip()
    if not orchestra_dir:
        raise PrepError("ORCHESTRA_DIR is not set")
    ko_fleet = Path(orchestra_dir) / "bin" / "ko-fleet"
    if not os.access(ko_fleet, os.X_OK):
        raise PrepError(f"ko-fleet is not executable: {ko_fleet}")
    status_out = ""
    for command in fleet_commands(worktree, Path(orchestra_dir)):
        result = subprocess.run(command, cwd=worktree, capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or "ko-fleet failed"
            raise PrepError(detail)
        if command[1] == "status":
            status_out = result.stdout
    url = dashboard_url_from_status(status_out)
    print(f"dashboard: {url}")
    if status_out:
        sys.stdout.write(status_out if status_out.endswith("\n") else status_out + "\n")
    return url


def _add_root_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, default=None)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    _add_root_arg(sub.add_parser("preflight", help="Require a clean checkout and .worktreeinclude"))

    prepare = sub.add_parser("prepare", help="Park or switch the invoking checkout")
    _add_root_arg(prepare)
    prepare.add_argument("--mode", choices=("existing", "new"), default=None)
    prepare.add_argument("--branch", required=True)
    prepare.add_argument("--parking", default=None)
    prepare.add_argument("--base", default=None)

    attach = sub.add_parser("attach", help="Attach or create the target branch in the worktree")
    _add_root_arg(attach)
    attach.add_argument("--mode", choices=("existing", "new"), required=True)
    attach.add_argument("--branch", required=True)

    copy = sub.add_parser("verify-copy", help="Verify .worktreeinclude files were copied")
    copy.add_argument("--source", type=Path, required=True)
    copy.add_argument("--dest", type=Path, required=True)

    absent = sub.add_parser("verify-kanban-absent", help="Require no copied Kanban state")
    _add_root_arg(absent)

    bootstrap = sub.add_parser("bootstrap-kanban", help="Create a pristine Kanban database")
    _add_root_arg(bootstrap)

    start = sub.add_parser("start-instance", help="Start and report only this worktree's Fleet instance")
    _add_root_arg(start)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "preflight":
            root = preflight(args.root)
            print(root)
        elif args.command == "prepare":
            root = _resolve_root(args.root)
            mode, branch, parking, base = resolve_creation(
                root, args.mode, args.branch, parking=args.parking, base=args.base,
            )
            if mode == "existing":
                prepared = prepare_existing(root, branch, parking or "")
            else:
                prepared = prepare_new(root, branch, base or "")
            print(f"mode: {mode}")
            print(f"branch: {branch}")
            print(f"parking: {parking or ''}")
            print(f"base: {base or ''}")
            print(prepared)
        elif args.command == "attach":
            if args.mode == "existing":
                print(attach_existing(args.root, args.branch))
            else:
                print(attach_new(args.root, args.branch))
        elif args.command == "verify-copy":
            copied = verify_copy(args.source, args.dest)
            print("copied:" if copied else "copied: none")
            for relative in copied:
                print(relative)
        elif args.command == "verify-kanban-absent":
            print(verify_kanban_absent(args.root))
        elif args.command == "bootstrap-kanban":
            bootstrap_kanban(args.root)
        elif args.command == "start-instance":
            start_instance(args.root)
        else:
            raise PrepError(f"unknown command: {args.command}")
    except PrepError as exc:
        message = str(exc).strip() or "create-codex-worktree helper failed"
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
