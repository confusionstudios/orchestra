#!/usr/bin/env python3
"""
Create or refresh thin AI skill wrappers for Claude and the Open Agent
Standard path supported by Codex, Antigravity, Kilo, and other compatible agents.

Intended invocation:

  "$ORCHESTRA_DIR/bin/ko-sync-skills" <target-repo>

This script is meant to be run from the Orchestra checkout referenced by
`$ORCHESTRA_DIR`, so the canonical skills come from
`$ORCHESTRA_DIR/AI-skills/*.md` unless `--orchestra-dir` is overridden.

Wrappers are generated from the canonical skill docs in AI-skills/*.md and
written into a target repo under category-specific names:

  .claude/skills/orch-kb-<skill>/SKILL.md
  .agents/skills/orch-kb-<skill>/SKILL.md
  .claude/skills/orch-adhoc-<skill>/SKILL.md
  .agents/skills/orch-adhoc-<skill>/SKILL.md

The script only overwrites wrappers it can confidently identify as previously
generated wrappers. Unknown or hand-edited files are left untouched. Pass
`--fix` for explicit cleanup of generated wrappers left behind by older sync
layouts.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path


AGENTS = ("claude", "agents")
SKILL_SYNC_MARKER = ".orchestra-skill-sync"
SKILL_SYNC_REGISTRY_ENV = "ORCHESTRA_SKILL_SYNC_REPOS"
SKILL_SYNC_MARKER_GITIGNORE_ENTRY = f"!{SKILL_SYNC_MARKER}"
KANBAN_SKILLS = frozenset(
    {
        "get-kanban-update",
        "kanban",
        "plan-to-tasks",
        "review-recent-kanban-tasks",
    }
)
KANBAN_WRAPPER_PREFIX = "orch-kb-"
ADHOC_WRAPPER_PREFIX = "orch-adhoc-"
LEGACY_WRAPPER_PREFIX = "ko-"
CURRENT_WRAPPER_PREFIXES = (KANBAN_WRAPPER_PREFIX, ADHOC_WRAPPER_PREFIX)
GENERATED_WRAPPER_PREFIXES = (*CURRENT_WRAPPER_PREFIXES, LEGACY_WRAPPER_PREFIX)
GENERATED_WRAPPER_GITIGNORE_ENTRIES = tuple(
    f".{agent}/skills/{prefix}*/"
    for agent in AGENTS
    for prefix in CURRENT_WRAPPER_PREFIXES
)

MARKER_TEXT = (
    "# Orchestra shared AI skill sync opt-in.\n"
    "# This file is safe to commit; machine-local repo paths live in\n"
    "# ~/.config/orchestra/skill-sync.repos or $ORCHESTRA_SKILL_SYNC_REPOS.\n"
    "skills = true\n"
)

_FRONT_MATTER_RE = re.compile(
    r"\A---\nname: (?P<name>[^\n]+)\ndescription: (?P<description>[^\n]+)\n---\n\n(?P<body>.*)\Z",
    re.DOTALL,
)


def _default_orchestra_dir() -> str | None:
    return os.environ.get("ORCHESTRA_DIR")


def _default_registry_path() -> Path:
    raw = os.environ.get(SKILL_SYNC_REGISTRY_ENV)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".config" / "orchestra" / "skill-sync.repos"


def _canonical_skill_files(orchestra_dir: Path) -> list[Path]:
    skills_dir = orchestra_dir / "AI-skills"
    return sorted(
        path for path in skills_dir.glob("*.md") if path.is_file() and path.name != "AI-readme.md"
    )


def _skill_description(canonical_path: Path) -> str:
    for raw_line in canonical_path.read_text(encoding="utf-8").splitlines():
        line = " ".join(raw_line.strip().split())
        if line:
            return line
    raise ValueError(f"skill file is empty: {canonical_path}")


def _wrapper_prefix(skill_name: str) -> str:
    return KANBAN_WRAPPER_PREFIX if skill_name in KANBAN_SKILLS else ADHOC_WRAPPER_PREFIX


def _wrapper_skill_name(skill_name: str) -> str:
    return f"{_wrapper_prefix(skill_name)}{skill_name}"


def _unwrap_wrapper_skill_name(wrapper_name: str) -> str:
    for prefix in GENERATED_WRAPPER_PREFIXES:
        if wrapper_name.startswith(prefix):
            return wrapper_name[len(prefix) :]
    return wrapper_name


def _is_expected_current_wrapper_name(wrapper_name: str, skill_name: str) -> bool:
    return wrapper_name == _wrapper_skill_name(skill_name)


def _has_generated_wrapper_prefix(wrapper_name: str) -> bool:
    return any(wrapper_name.startswith(prefix) for prefix in GENERATED_WRAPPER_PREFIXES)


def render_wrapper(skill_name: str, description: str, canonical_path: Path) -> str:
    wrapper_name = _wrapper_skill_name(skill_name)
    return (
        f"---\n"
        f"name: {wrapper_name}\n"
        f"description: {json.dumps(description)}\n"
        f"---\n\n"
        f"Follow the shared skill:\n\n"
        f"- Location: $ORCHESTRA_DIR/AI-skills/{skill_name}.md\n"
        f"- Least Seen at: {canonical_path.resolve()}\n"
    )


def _legacy_codex_title(skill_name: str) -> str:
    return " ".join(part.capitalize() for part in skill_name.split("-"))


def _legacy_generated_bodies(skill_name: str, canonical_path: Path) -> set[str]:
    relative_path = f"AI-skills/{skill_name}.md"
    absolute_path = str(canonical_path.resolve())
    orchestra_repo = str(canonical_path.resolve().parents[1])
    codex_title = _legacy_codex_title(skill_name)
    return {
        f"@{relative_path}",
        f"@{absolute_path}",
        f"[//]: # (ORCHESTRA_REPO: {orchestra_repo})\n@{absolute_path}",
        f"Read and follow the instructions in `{relative_path}`.",
        f"Read and follow the instructions in `{absolute_path}`.",
        (
            f"# {codex_title}\n\n"
            f"Canonical instructions: `{relative_path}`\n\n"
            f"Load that file and follow it exactly. If this skill conflicts with the canonical file, "
            f"the canonical file wins."
        ),
        (
            f"# {codex_title}\n\n"
            f"Canonical instructions: `{absolute_path}`\n\n"
            f"Load that file and follow it exactly. If this skill conflicts with the canonical file, "
            f"the canonical file wins."
        ),
    }


def _is_current_generated_body(body: str, skill_name: str) -> bool:
    pattern = (
        r"\AFollow the shared skill:\n\n"
        rf"- Location: \$ORCHESTRA_DIR/AI-skills/{re.escape(skill_name)}\.md\n"
        rf"- Least Seen at: .*/AI-skills/{re.escape(skill_name)}\.md\Z"
    )
    return re.fullmatch(pattern, body) is not None


def _is_generated_body(body: str, skill_name: str, canonical_path: Path | None) -> bool:
    body = body.strip()
    if _is_current_generated_body(body, skill_name):
        return True
    if canonical_path is not None and body in _legacy_generated_bodies(skill_name, canonical_path):
        return True

    relative_path = f"AI-skills/{skill_name}.md"
    codex_title = _legacy_codex_title(skill_name)
    relative_legacy_bodies = {
        f"@{relative_path}",
        f"Read and follow the instructions in `{relative_path}`.",
        (
            f"# {codex_title}\n\n"
            f"Canonical instructions: `{relative_path}`\n\n"
            f"Load that file and follow it exactly. If this skill conflicts with the canonical file, "
            f"the canonical file wins."
        ),
    }
    if body in relative_legacy_bodies:
        return True

    absolute_reference_patterns = [
        rf"\A@.*/AI-skills/{re.escape(skill_name)}\.md\Z",
        rf"\A\[//\]: # \(ORCHESTRA_REPO: .+\)\n@.*/AI-skills/{re.escape(skill_name)}\.md\Z",
        rf"\ARead and follow the instructions in `.*/AI-skills/{re.escape(skill_name)}\.md`\.\Z",
        (
            rf"\A# {re.escape(codex_title)}\n\n"
            rf"Canonical instructions: `.*/AI-skills/{re.escape(skill_name)}\.md`\n\n"
            rf"Load that file and follow it exactly\. If this skill conflicts with the canonical file, "
            rf"the canonical file wins\.\Z"
        ),
    ]
    return any(re.fullmatch(pattern, body) is not None for pattern in absolute_reference_patterns)


def is_generated_wrapper(content: str, skill_name: str, canonical_path: Path) -> bool:
    return _is_generated_wrapper(content, skill_name, canonical_path)


def _is_generated_wrapper(
    content: str, skill_name: str, canonical_path: Path | None = None
) -> bool:
    match = _FRONT_MATTER_RE.match(content)
    if not match or _unwrap_wrapper_skill_name(match.group("name")) != skill_name:
        return False
    return _is_generated_body(match.group("body"), skill_name, canonical_path)


def _remove_wrapper_dir(wrapper_path: Path) -> None:
    shutil.rmtree(wrapper_path.parent)
    for parent in (wrapper_path.parent.parent, wrapper_path.parent.parent.parent):
        try:
            parent.rmdir()
        except OSError:
            break


def _ensure_generated_wrapper_gitignore(target: Path) -> list[str]:
    gitignore = target / ".gitignore"
    original = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    lines = original.splitlines()
    missing = [entry for entry in GENERATED_WRAPPER_GITIGNORE_ENTRIES if entry not in lines]
    if not missing:
        return []

    existing_entry_indices = [
        index
        for index, line in enumerate(lines)
        if line in GENERATED_WRAPPER_GITIGNORE_ENTRIES
    ]
    if existing_entry_indices:
        insert_at = max(existing_entry_indices) + 1
    else:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("# Generated Orchestra skill wrappers. Canonical source lives in AI-skills/.")
        insert_at = len(lines)

    for entry in missing:
        lines.insert(insert_at, entry)
        insert_at += 1

    gitignore.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return missing


def _ensure_skill_sync_marker_gitignore(repo_root: Path) -> bool:
    marker = _skill_sync_marker_path(repo_root)
    result = subprocess.run(
        ["git", "-C", str(repo_root), "check-ignore", "-q", "--", str(marker.relative_to(repo_root))],
        check=False,
    )
    if result.returncode != 0:
        return False

    gitignore = repo_root / ".gitignore"
    original = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    lines = original.splitlines()
    if SKILL_SYNC_MARKER_GITIGNORE_ENTRY in lines:
        return False
    if lines and lines[-1] != "":
        lines.append("")
    lines.append("# Orchestra shared AI skill sync opt-in marker.")
    lines.append(SKILL_SYNC_MARKER_GITIGNORE_ENTRY)
    gitignore.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def _git_repo_root(path: Path) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def _require_git_repo_root(path: Path) -> Path:
    root = _git_repo_root(path)
    if root is None:
        raise ValueError(f"not inside a git repo: {path}")
    return root


def _skill_sync_marker_path(repo_root: Path) -> Path:
    return repo_root / SKILL_SYNC_MARKER


def _read_marker_data(marker: Path) -> dict[str, object]:
    if not marker.is_file():
        return {}
    try:
        return tomllib.loads(marker.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return {}


def _marker_text(project_name: str | None = None) -> str:
    text = MARKER_TEXT
    if project_name:
        text += f"devlog_project = {json.dumps(project_name)}\n"
    return text


def _read_registered_repo_paths(registry_path: Path) -> list[Path]:
    if not registry_path.exists():
        return []
    paths: list[Path] = []
    seen: set[Path] = set()
    for raw_line in registry_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        path = Path(os.path.expandvars(line)).expanduser()
        try:
            key = path.resolve()
        except OSError:
            key = path.absolute()
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths


def _write_registered_repo_paths(registry_path: Path, repo_roots: list[Path]) -> None:
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    seen: set[Path] = set()
    lines = [
        "# One git repo root per line. Repos must also contain .orchestra-skill-sync.\n"
    ]
    for root in repo_roots:
        resolved = root.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        lines.append(f"{resolved}\n")
    registry_path.write_text("".join(lines), encoding="utf-8")


def register_repo_for_skill_sync(
    repo: Path,
    registry_path: Path,
    project_name: str | None = None,
) -> dict[str, str | bool]:
    repo_root = _require_git_repo_root(repo)
    marker = _skill_sync_marker_path(repo_root)
    marker_created = not marker.exists()
    if project_name is None:
        raw_project = _read_marker_data(marker).get("devlog_project")
        if isinstance(raw_project, str) and raw_project.strip():
            project_name = raw_project.strip()
    marker.write_text(_marker_text(project_name), encoding="utf-8")
    marker_gitignore_added = _ensure_skill_sync_marker_gitignore(repo_root)

    registered: list[Path] = []
    for path in _read_registered_repo_paths(registry_path):
        if not path.exists():
            continue
        root = _git_repo_root(path)
        if root is not None:
            registered.append(root)
    already_registered = repo_root in registered
    if not already_registered:
        registered.append(repo_root)
        _write_registered_repo_paths(registry_path, registered)
    elif not registry_path.exists():
        _write_registered_repo_paths(registry_path, registered)

    return {
        "repo": str(repo_root),
        "marker": str(marker),
        "registry": str(registry_path.expanduser()),
        "marker_created": marker_created,
        "marker_gitignore_added": marker_gitignore_added,
        "registry_added": not already_registered,
    }


def unregister_repo_for_skill_sync(repo: Path, registry_path: Path) -> dict[str, str | bool]:
    repo_root = _git_repo_root(repo)
    remove_path = repo_root if repo_root is not None else repo.expanduser().resolve()
    existing = _read_registered_repo_paths(registry_path)
    kept: list[Path] = []
    removed = False
    for path in existing:
        try:
            key = path.resolve()
        except OSError:
            key = path.absolute()
        if key == remove_path:
            removed = True
            continue
        kept.append(path)
    _write_registered_repo_paths(registry_path, kept)

    marker_removed = False
    marker = _skill_sync_marker_path(remove_path)
    if marker.exists() and marker.is_file():
        marker.unlink()
        marker_removed = True

    return {
        "repo": str(remove_path),
        "marker": str(marker),
        "registry": str(registry_path.expanduser()),
        "registry_removed": removed,
        "marker_removed": marker_removed,
    }


def _git_relative_path(path: Path, repo_root: Path) -> str | None:
    try:
        return path.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return None


def _target_relative_path(path: Path, target: Path) -> str:
    try:
        return path.resolve().relative_to(target).as_posix()
    except ValueError:
        return str(path)


def _remove_git_index_paths(target: Path, paths: list[Path]) -> list[str]:
    if not paths:
        return []

    repo_root = _git_repo_root(target)
    if repo_root is None:
        return []

    git_paths = sorted(
        {
            git_path
            for path in paths
            if (git_path := _git_relative_path(path, repo_root)) is not None
        }
    )
    if not git_paths:
        return []

    tracked: list[str] = []
    for git_path in git_paths:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z", "--", git_path],
            capture_output=True,
            check=True,
        )
        tracked.extend(
            path.decode("utf-8")
            for path in result.stdout.split(b"\0")
            if path
        )

    if not tracked:
        return []

    subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "rm",
            "-r",
            "-f",
            "--cached",
            "--ignore-unmatch",
            "--",
            *git_paths,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )

    return sorted(
        {
            _target_relative_path(repo_root / tracked_path, target)
            for tracked_path in tracked
        }
    )


def fix_skill_wrappers(target: Path, orchestra_dir: Path) -> dict[str, list[str]]:
    target = target.resolve()
    orchestra_dir = orchestra_dir.resolve()
    summary = {
        "removed": [],
        "ignored": [],
        "skipped": [],
        "gitignore_added": [],
        "git_index_removed": [],
    }
    canonical_paths = {
        canonical_path.stem: canonical_path
        for canonical_path in _canonical_skill_files(orchestra_dir)
    }
    git_index_candidates: list[Path] = []

    summary["gitignore_added"] = _ensure_generated_wrapper_gitignore(target)

    for agent in AGENTS:
        skills_root = target / f".{agent}" / "skills"
        if not skills_root.is_dir():
            continue

        for wrapper_dir in sorted(path for path in skills_root.iterdir() if path.is_dir()):
            wrapper_path = wrapper_dir / "SKILL.md"
            if not wrapper_path.is_file():
                continue

            relative_path = str(wrapper_path.relative_to(target))
            wrapper_name = wrapper_dir.name
            if wrapper_name.startswith(LEGACY_WRAPPER_PREFIX):
                git_index_candidates.append(wrapper_dir)
                _remove_wrapper_dir(wrapper_path)
                summary["removed"].append(relative_path)
                continue

            skill_name = _unwrap_wrapper_skill_name(wrapper_name)
            canonical_path = canonical_paths.get(skill_name)
            current_text = wrapper_path.read_text(encoding="utf-8")
            is_generated = _is_generated_wrapper(current_text, skill_name, canonical_path)

            if _has_generated_wrapper_prefix(wrapper_name) and agent in AGENTS:
                if is_generated and _is_expected_current_wrapper_name(wrapper_name, skill_name):
                    git_index_candidates.append(wrapper_dir)
                    summary["ignored"].append(relative_path)
                elif is_generated:
                    git_index_candidates.append(wrapper_dir)
                    _remove_wrapper_dir(wrapper_path)
                    summary["removed"].append(relative_path)
                else:
                    summary["skipped"].append(relative_path)
                continue

            if is_generated:
                git_index_candidates.append(wrapper_dir)
                _remove_wrapper_dir(wrapper_path)
                summary["removed"].append(relative_path)
                continue

            summary["skipped"].append(relative_path)

    summary["git_index_removed"] = _remove_git_index_paths(target, git_index_candidates)
    return summary


def sync_skill_wrappers(target: Path, orchestra_dir: Path) -> dict[str, list[str]]:
    target = target.resolve()
    orchestra_dir = orchestra_dir.resolve()
    summary = {"created": [], "updated": [], "removed": [], "unchanged": [], "skipped": []}

    for canonical_path in _canonical_skill_files(orchestra_dir):
        skill_name = canonical_path.stem
        wrapper_name = _wrapper_skill_name(skill_name)
        description = _skill_description(canonical_path)
        wrapper_text = render_wrapper(skill_name, description, canonical_path)

        for agent in AGENTS:
            wrapper_path = target / f".{agent}" / "skills" / wrapper_name / "SKILL.md"
            wrapper_path.parent.mkdir(parents=True, exist_ok=True)
            relative_path = str(wrapper_path.relative_to(target))

            if not wrapper_path.exists():
                wrapper_path.write_text(wrapper_text, encoding="utf-8")
                summary["created"].append(relative_path)
                continue

            current_text = wrapper_path.read_text(encoding="utf-8")
            if current_text == wrapper_text:
                summary["unchanged"].append(relative_path)
                continue

            if is_generated_wrapper(current_text, skill_name, canonical_path):
                wrapper_path.write_text(wrapper_text, encoding="utf-8")
                summary["updated"].append(relative_path)
                continue

            summary["skipped"].append(relative_path)

    return summary


def sync_registered_repos(orchestra_dir: Path, registry_path: Path) -> dict[str, list[str]]:
    orchestra_dir = orchestra_dir.resolve()
    summary = {"synced": [], "skipped": [], "failed": []}
    for path in _read_registered_repo_paths(registry_path):
        try:
            if not path.exists():
                summary["skipped"].append(f"{path}: path does not exist")
                continue
            repo_root = _require_git_repo_root(path)
            if not _skill_sync_marker_path(repo_root).is_file():
                summary["skipped"].append(f"{repo_root}: missing {SKILL_SYNC_MARKER}")
                continue
            fix_skill_wrappers(repo_root, orchestra_dir)
            sync_skill_wrappers(repo_root, orchestra_dir)
            summary["synced"].append(str(repo_root))
        except Exception as exc:  # noqa: BLE001 - report and continue syncing other repos
            summary["failed"].append(f"{path}: {exc}")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create or refresh thin AI skill wrappers for Claude and the "
            "Open Agent Standard path supported by Codex, Antigravity, Kilo, and other compatible agents. "
            "Normally run this as "
            '`ko-sync-skills`.'
        )
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Target repo to update. Defaults to the current working directory.",
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
        "--fix",
        action="store_true",
        help=(
            "Clean generated wrapper directories left by older sync layouts. "
            "This is explicit so repo-owned or ambiguous skills are not removed "
            "during normal sync."
        ),
    )
    parser.add_argument(
        "--registry",
        default=None,
        help=(
            "Private repo registry path for --register and --unregister. "
            "Defaults to $ORCHESTRA_SKILL_SYNC_REPOS or "
            "~/.config/orchestra/skill-sync.repos."
        ),
    )
    parser.add_argument(
        "--register",
        nargs="?",
        const=".",
        metavar="REPO",
        help="Opt a git repo into registered shared skill sync.",
    )
    parser.add_argument(
        "--project-name",
        help=(
            "Human project name to store as devlog_project while registering, "
            "for example 'MIDI Designer'."
        ),
    )
    parser.add_argument(
        "--unregister",
        nargs="?",
        const=".",
        metavar="REPO",
        help="Remove a git repo from registered shared skill sync.",
    )
    args = parser.parse_args()
    if not args.orchestra_dir:
        parser.error("set ORCHESTRA_DIR or pass --orchestra-dir")
    mode_count = sum(bool(value) for value in (args.register, args.unregister))
    if mode_count > 1:
        parser.error("--register and --unregister are mutually exclusive")
    if mode_count and args.fix:
        parser.error("--fix cannot be combined with --register or --unregister")
    if args.project_name and not args.register:
        parser.error("--project-name can only be used with --register")
    return args


def main() -> int:
    args = parse_args()
    registry_path = Path(args.registry).expanduser() if args.registry else _default_registry_path()

    if args.register:
        result = register_repo_for_skill_sync(
            Path(args.register),
            registry_path,
            project_name=args.project_name,
        )
        print(f"Registered for skill sync: {result['repo']}")
        print(f"Marker: {result['marker']}")
        print(f"Registry: {result['registry']}")
        return 0

    if args.unregister:
        result = unregister_repo_for_skill_sync(Path(args.unregister), registry_path)
        print(f"Unregistered from skill sync: {result['repo']}")
        print(f"Marker removed: {result['marker_removed']}")
        print(f"Registry removed: {result['registry_removed']}")
        print(f"Registry: {result['registry']}")
        return 0

    if args.fix:
        summary = fix_skill_wrappers(Path(args.target), Path(args.orchestra_dir))
        print(
            "AI skill wrappers fixed:"
            f" removed={len(summary['removed'])}"
            f" ignored={len(summary['ignored'])}"
            f" gitignore_added={len(summary['gitignore_added'])}"
            f" git_index_removed={len(summary['git_index_removed'])}"
            f" skipped={len(summary['skipped'])}"
        )
        for key, label in (
            ("gitignore_added", "Gitignore added"),
            ("git_index_removed", "Git index removed"),
            ("removed", "Removed"),
            ("ignored", "Ignored"),
            ("skipped", "Skipped"),
        ):
            print(f"{label} ({len(summary[key])}):")
            for relative_path in summary[key]:
                print(f"  - {relative_path}")
                if key == "skipped":
                    print(f"Warning: skipped ambiguous wrapper: {relative_path}")
        return 0

    summary = sync_skill_wrappers(Path(args.target), Path(args.orchestra_dir))

    ordered_labels = [
        ("skipped", "Skipped"),
        ("created", "Added"),
        ("updated", "Updated"),
        ("removed", "Removed"),
        ("unchanged", "All good"),
    ]
    print(
        "AI skill wrappers synchronized:"
        f" created={len(summary['created'])}"
        f" updated={len(summary['updated'])}"
        f" removed={len(summary['removed'])}"
        f" unchanged={len(summary['unchanged'])}"
        f" skipped={len(summary['skipped'])}"
    )
    for key, label in ordered_labels:
        print(f"{label} ({len(summary[key])}):")
        for relative_path in summary[key]:
            print(f"  - {relative_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
