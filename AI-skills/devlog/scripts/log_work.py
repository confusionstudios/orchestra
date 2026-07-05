#!/usr/bin/env python3
"""Append a work-log entry to an Obsidian developer journal."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


DEVLOG_ENV = "ORCH_DEVLOG_DIR"


def default_journal_dir() -> Path:
    raw = os.environ.get(DEVLOG_ENV, "").strip()
    if not raw:
        raise ValueError(f"{DEVLOG_ENV} is not set")
    return Path(raw).expanduser()


def sunday_for(day: date) -> date:
    return day - timedelta(days=(day.weekday() + 1) % 7)


def parse_day(raw: str | None) -> date:
    if raw is None:
        return datetime.now().date()
    return date.fromisoformat(raw)


def normalize_entry(raw: str) -> str:
    lines = [line.rstrip() for line in raw.strip().splitlines()]
    return "\n".join(line for line in lines if line.strip())


def format_project(raw: str) -> str:
    project = raw.strip()
    if project.startswith("**") and project.endswith("**"):
        return project
    return f"**{project.strip('`*')}**"


def format_single_project_bullet(project: str, entry: str) -> list[str]:
    lines = entry.splitlines()
    return [f"- {project}: {lines[0]}", *[f"  {line}" for line in lines[1:]]]


def format_nested_entry(entry: str) -> list[str]:
    lines = entry.splitlines()
    return [f"  - {lines[0]}", *[f"    {line}" for line in lines[1:]]]


def find_day_bounds(lines: list[str], heading: str) -> tuple[int, int]:
    heading_index = next(index for index, line in enumerate(lines) if line == heading)
    end = len(lines)
    for index in range(heading_index + 1, len(lines)):
        if lines[index].startswith("# "):
            end = index - 1 if index > 0 and lines[index - 1].strip() == "" else index
            break
    return heading_index, end


def project_line_kind(line: str, project: str) -> str | None:
    if line == f"- {project}":
        return "group"
    if line.startswith(f"- {project}: "):
        return "single"
    return None


def find_project_block(lines: list[str], start: int, end: int, project: str) -> tuple[int, int, str] | None:
    for index in range(start + 1, end):
        kind = project_line_kind(lines[index], project)
        if kind is None:
            continue

        block_end = index + 1
        while block_end < end and (
            lines[block_end].startswith("  ") or lines[block_end].strip() == ""
        ):
            block_end += 1
        return index, block_end, kind
    return None


def convert_single_to_group(lines: list[str], block_start: int, block_end: int, project: str) -> None:
    first_line = lines[block_start]
    first_entry = first_line.removeprefix(f"- {project}: ")
    continuation = lines[block_start + 1 : block_end]
    grouped = [f"- {project}", f"  - {first_entry}"]
    grouped.extend(
        f"    {line[2:]}" if line.startswith("  ") else f"    {line}"
        for line in continuation
    )
    lines[block_start:block_end] = grouped


def append_under_heading(text: str, heading: str, project: str, entry: str) -> str:
    if heading not in text:
        if text and not text.endswith("\n"):
            text += "\n"
        if text.strip():
            text += "\n"
        text += f"{heading}\n"

    lines = text.splitlines()
    heading_index, day_end = find_day_bounds(lines, heading)
    existing_project = find_project_block(lines, heading_index, day_end, project)
    if existing_project is not None:
        block_start, block_end, kind = existing_project
        if kind == "single":
            convert_single_to_group(lines, block_start, block_end, project)
            _, day_end = find_day_bounds(lines, heading)
            block_start, block_end, _ = find_project_block(lines, heading_index, day_end, project)
        lines[block_end:block_end] = format_nested_entry(entry)
        return "\n".join(lines).rstrip() + "\n"

    insert_at = day_end
    bullet_lines = format_single_project_bullet(project, entry)
    if insert_at < len(lines) and lines[insert_at].startswith("# "):
        bullet_lines.append("")
    lines[insert_at:insert_at] = bullet_lines
    return "\n".join(lines).rstrip() + "\n"


def append_entry(journal_dir: Path, day: date, project: str, entry: str) -> Path:
    week_start = sunday_for(day)
    note_path = journal_dir / f"{week_start.isoformat()} - Week.md"
    heading = f"# {day.isoformat()} - {day.strftime('%A')}"
    formatted_project = format_project(project)

    if not journal_dir.is_dir():
        raise FileNotFoundError(f"journal directory does not exist: {journal_dir}")

    text = note_path.read_text(encoding="utf-8") if note_path.exists() else ""
    note_path.write_text(
        append_under_heading(text, heading, formatted_project, entry),
        encoding="utf-8",
    )
    return note_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Append a work-log entry to the configured Obsidian developer journal."
    )
    parser.add_argument("entry", nargs="*", help="Entry text. Ignored when --stdin is used.")
    parser.add_argument(
        "--project",
        required=True,
        help="Project label to place first in the bullet, for example `orchestra`.",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read entry text from standard input.",
    )
    parser.add_argument(
        "--journal-dir",
        type=Path,
        help=f"Journal directory. Defaults to ${DEVLOG_ENV}.",
    )
    parser.add_argument(
        "--date",
        help="Entry date as YYYY-MM-DD. Defaults to today.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    raw_entry = sys.stdin.read() if args.stdin else " ".join(args.entry)
    entry = normalize_entry(raw_entry)
    if not entry:
        print("error: entry text is required", file=sys.stderr)
        return 2

    try:
        journal_dir = args.journal_dir.expanduser() if args.journal_dir else default_journal_dir()
        note_path = append_entry(
            journal_dir=journal_dir,
            day=parse_day(args.date),
            project=args.project,
            entry=entry,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(note_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
