#!/usr/bin/env python3
"""
Install thin Orchestra AI skill wrappers into user-level interactive-client
skill directories.

Intended invocation:

  "$ORCHESTRA_DIR/bin/ko-install-global-skills"

Wrappers are generated from `$ORCHESTRA_DIR/AI-skills/*.md` (excluding
`AI-readme.md`) and written under:

  ~/.claude/skills/orch-kb-<skill>/SKILL.md
  ~/.claude/skills/orch-adhoc-<skill>/SKILL.md
  ~/.codex/skills/orch-kb-<skill>/SKILL.md
  ~/.codex/skills/orch-adhoc-<skill>/SKILL.md
  ~/.kilocode/skills/orch-kb-<skill>/SKILL.md
  ~/.kilocode/skills/orch-adhoc-<skill>/SKILL.md
  ~/.gemini/antigravity-cli/skills/orch-kb-<skill>/SKILL.md
  ~/.gemini/antigravity-cli/skills/orch-adhoc-<skill>/SKILL.md

Each wrapper points at the canonical skill through `$ORCHESTRA_DIR` rather than
copying skill text. The installer only overwrites wrappers it can identify as
previously generated Orchestra wrappers. It removes generated wrappers whose
canonical source no longer exists. Unknown or hand-edited skills are left
untouched. Pass `--check` to verify installed wrappers without writing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path


# Explicit target map: agent id -> skills root relative to $HOME.
# Paths are not assumed to be `.<agent>/skills`.
SKILL_TARGETS: dict[str, str] = {
    "claude": ".claude/skills",
    "codex": ".codex/skills",
    "kilo": ".kilocode/skills",
    "antigravity": ".gemini/antigravity-cli/skills",
}
AGENTS = tuple(SKILL_TARGETS)
KANBAN_SKILLS = frozenset(
    {
        "get-kanban-update",
        "kanban",
        "narrate",
        "plan-to-tasks",
        "review-recent-kanban-tasks",
    }
)
KANBAN_WRAPPER_PREFIX = "orch-kb-"
ADHOC_WRAPPER_PREFIX = "orch-adhoc-"
CURRENT_WRAPPER_PREFIXES = (KANBAN_WRAPPER_PREFIX, ADHOC_WRAPPER_PREFIX)

_FRONT_MATTER_RE = re.compile(
    r"\A---\nname: (?P<name>[^\n]+)\ndescription: (?P<description>[^\n]+)\n---\n\n(?P<body>.*)\Z",
    re.DOTALL,
)


def _default_orchestra_dir() -> str | None:
    return os.environ.get("ORCHESTRA_DIR")


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
    for prefix in CURRENT_WRAPPER_PREFIXES:
        if wrapper_name.startswith(prefix):
            return wrapper_name[len(prefix) :]
    return wrapper_name


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


def _is_current_generated_body(body: str, skill_name: str) -> bool:
    pattern = (
        r"\AFollow the shared skill:\n\n"
        rf"- Location: \$ORCHESTRA_DIR/AI-skills/{re.escape(skill_name)}\.md\n"
        rf"- Least Seen at: .*/AI-skills/{re.escape(skill_name)}\.md\Z"
    )
    return re.fullmatch(pattern, body) is not None


def is_generated_wrapper(content: str, skill_name: str) -> bool:
    match = _FRONT_MATTER_RE.match(content)
    if not match or _unwrap_wrapper_skill_name(match.group("name")) != skill_name:
        return False
    return _is_current_generated_body(match.group("body").strip(), skill_name)


def _generated_wrapper_skill_name(content: str) -> str | None:
    """Return the skill name when ``content`` is a managed Orchestra wrapper."""
    match = _FRONT_MATTER_RE.match(content)
    if not match:
        return None
    skill_name = _unwrap_wrapper_skill_name(match.group("name"))
    if skill_name == match.group("name"):
        return None
    return skill_name if _is_current_generated_body(match.group("body").strip(), skill_name) else None


def _agent_skills_root(home: Path, agent: str) -> Path:
    return home / SKILL_TARGETS[agent]


def _relative_wrapper_path(agent: str, wrapper_name: str) -> str:
    return f"{SKILL_TARGETS[agent]}/{wrapper_name}/SKILL.md"


def install_global_skills(
    orchestra_dir: Path,
    home: Path | None = None,
    *,
    check: bool = False,
) -> dict[str, list[str]]:
    """Install or verify user-level Orchestra skill wrappers.

    Returns a summary with keys: created, updated, removed, unchanged, skipped,
    missing, stale. In normal mode, ``removed`` lists obsolete generated wrappers.
    In check mode, ``created`` is unused; ``missing`` lists absent wrappers and
    ``updated`` lists wrappers that would be rewritten; ``stale`` lists wrappers
    that would be removed.
    """
    home = (home or Path.home()).expanduser().resolve()
    orchestra_dir = orchestra_dir.resolve()
    summary: dict[str, list[str]] = {
        "created": [],
        "updated": [],
        "removed": [],
        "unchanged": [],
        "skipped": [],
        "missing": [],
        "stale": [],
    }
    expected_paths: set[str] = set()

    for canonical_path in _canonical_skill_files(orchestra_dir):
        skill_name = canonical_path.stem
        wrapper_name = _wrapper_skill_name(skill_name)
        description = _skill_description(canonical_path)
        wrapper_text = render_wrapper(skill_name, description, canonical_path)

        for agent in AGENTS:
            wrapper_path = _agent_skills_root(home, agent) / wrapper_name / "SKILL.md"
            relative_path = _relative_wrapper_path(agent, wrapper_name)
            expected_paths.add(relative_path)

            if not wrapper_path.exists():
                if check:
                    summary["missing"].append(relative_path)
                    continue
                wrapper_path.parent.mkdir(parents=True, exist_ok=True)
                wrapper_path.write_text(wrapper_text, encoding="utf-8")
                summary["created"].append(relative_path)
                continue

            current_text = wrapper_path.read_text(encoding="utf-8")
            if current_text == wrapper_text:
                summary["unchanged"].append(relative_path)
                continue

            if is_generated_wrapper(current_text, skill_name):
                if check:
                    summary["updated"].append(relative_path)
                    continue
                wrapper_path.write_text(wrapper_text, encoding="utf-8")
                summary["updated"].append(relative_path)
                continue

            summary["skipped"].append(relative_path)

    for agent in AGENTS:
        root = _agent_skills_root(home, agent)
        if not root.exists():
            continue
        for wrapper_path in root.glob("orch-*/SKILL.md"):
            relative_path = _relative_wrapper_path(agent, wrapper_path.parent.name)
            if relative_path in expected_paths:
                continue
            skill_name = _generated_wrapper_skill_name(wrapper_path.read_text(encoding="utf-8"))
            if not skill_name:
                continue
            if check:
                summary["stale"].append(relative_path)
                continue
            wrapper_path.unlink()
            if not any(wrapper_path.parent.iterdir()):
                wrapper_path.parent.rmdir()
            summary["removed"].append(relative_path)

    return summary


def _print_summary(summary: dict[str, list[str]], *, check: bool) -> None:
    if check:
        print(
            "Global AI skill wrappers check:"
            f" missing={len(summary['missing'])}"
            f" would_update={len(summary['updated'])}"
            f" would_remove={len(summary['stale'])}"
            f" unchanged={len(summary['unchanged'])}"
            f" skipped={len(summary['skipped'])}"
        )
        ordered = (
            ("missing", "Missing"),
            ("updated", "Would update"),
            ("stale", "Would remove"),
            ("unchanged", "All good"),
            ("skipped", "Skipped"),
        )
    else:
        print(
            "Global AI skill wrappers installed:"
            f" created={len(summary['created'])}"
            f" updated={len(summary['updated'])}"
            f" removed={len(summary['removed'])}"
            f" unchanged={len(summary['unchanged'])}"
            f" skipped={len(summary['skipped'])}"
        )
        ordered = (
            ("skipped", "Skipped"),
            ("created", "Added"),
            ("updated", "Updated"),
            ("removed", "Removed"),
            ("unchanged", "All good"),
        )

    for key, label in ordered:
        print(f"{label} ({len(summary[key])}):")
        for relative_path in summary[key]:
            print(f"  - {relative_path}")
            if key == "skipped":
                print(f"Warning: skipped unrecognized skill wrapper: {relative_path}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Install thin Orchestra AI skill wrappers into user-level Claude, "
            "Codex, Kilo, and Antigravity CLI skill directories. Normally run "
            "this as `ko-install-global-skills`."
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
        "--home",
        default=None,
        help=(
            "Home directory whose Claude, Codex, Kilo, and Antigravity CLI "
            "skill trees receive wrappers. Defaults to the current user home."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify installed wrappers match expected content without writing.",
    )
    args = parser.parse_args(argv)
    if not args.orchestra_dir:
        parser.error("set ORCHESTRA_DIR or pass --orchestra-dir")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    home = Path(args.home).expanduser() if args.home else None
    summary = install_global_skills(
        Path(args.orchestra_dir),
        home=home,
        check=args.check,
    )
    _print_summary(summary, check=args.check)
    if args.check and (
        summary["missing"] or summary["updated"] or summary["stale"] or summary["skipped"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
