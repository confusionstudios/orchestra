#!/usr/bin/env python3
"""Smoke-test replacement agent CLIs through the Orchestra agent registry."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
import time

from agent_registry import resolve_agent_command, resolve_agent_label


@dataclass(frozen=True)
class SmokeAgent:
    name: str
    spec: str
    enabled: bool = True


# Edit this matrix when testing a different replacement-agent set.
AGENT_MATRIX = (
    SmokeAgent("antigravity", "antigravity"),
    SmokeAgent("cursor-composer-2.5", "cursor-composer-2.5"),
    SmokeAgent("kilo-opus-4.8", "kilo:kilo/anthropic/claude-opus-4.8"),
)


def default_output_dir() -> Path:
    return Path.cwd() / ".kanban-orchestra" / "agent-smoke"


def prompt_for(label: str, output_path: Path) -> str:
    return f"""You are being smoke-tested as an Orchestra replacement agent.

Please do all of the following:
1. Create or overwrite this exact UTF-8 text file:
   {output_path}
2. Put a concise report in the file with:
   - agent label requested by caller: {label}
   - model name and version you believe you are using
   - effort/reasoning level if you can determine it
   - CLI/app identity if visible
   - current working directory
   - one sentence confirming whether you successfully wrote this file
3. Also reply to the caller with the same model name/version and effort level.

Do not edit any other files.
"""


def selected_agents(extra_agents: list[str] | None, skipped: set[str]) -> list[SmokeAgent]:
    agents = [agent for agent in AGENT_MATRIX if agent.enabled]
    if extra_agents:
        for raw in extra_agents:
            name, sep, spec = raw.partition("=")
            if not sep or not name or not spec:
                raise SystemExit(f"invalid --agent value, expected NAME=SPEC: {raw}")
            agents.append(SmokeAgent(name, spec))
    return [
        agent
        for agent in agents
        if agent.name not in skipped and agent.spec not in skipped
    ]


def command_summary(cmd: list[str], prompt: str) -> str:
    redacted = [
        f"<prompt: {len(prompt)} chars>" if part == prompt else part
        for part in cmd
    ]
    return " ".join(redacted[:4]) + (" ..." if len(redacted) > 4 else "")


def run_one(agent: SmokeAgent, output_dir: Path, timeout: int) -> int:
    cmd_template = resolve_agent_command(agent.spec)
    if cmd_template is None:
        print(f"{agent.name}: invalid agent spec: {agent.spec}", file=sys.stderr)
        return 2

    label = resolve_agent_label(agent.spec) or agent.spec
    report_path = output_dir / f"agent-smoke-{agent.name}.txt"
    stdout_path = output_dir / f"agent-smoke-{agent.name}.stdout.txt"
    stderr_path = output_dir / f"agent-smoke-{agent.name}.stderr.txt"
    prompt = prompt_for(label, report_path)
    cmd = [part.replace("{prompt}", prompt) for part in cmd_template]

    print(f"\n== {agent.name} ==", flush=True)
    print(f"spec: {agent.spec}", flush=True)
    print(f"label: {label}", flush=True)
    print(f"report: {report_path}", flush=True)
    print(f"command: {command_summary(cmd, prompt)}", flush=True)

    started = time.monotonic()
    try:
        with (
            stdout_path.open("w", encoding="utf-8") as stdout_file,
            stderr_path.open("w", encoding="utf-8") as stderr_file,
        ):
            proc = subprocess.run(
                cmd,
                cwd=str(output_dir),
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                timeout=timeout,
                close_fds=True,
            )
    except FileNotFoundError as exc:
        stderr_path.write_text(str(exc) + "\n", encoding="utf-8")
        print(f"exit: 127 ({exc})")
        return 127
    except subprocess.TimeoutExpired:
        with stderr_path.open("a", encoding="utf-8") as stderr_file:
            stderr_file.write(f"\nTimed out after {timeout}s\n")
        print(f"exit: timeout after {timeout}s")
        return 124

    elapsed = time.monotonic() - started
    stdout = stdout_path.read_text(encoding="utf-8") if stdout_path.exists() else ""
    stderr = stderr_path.read_text(encoding="utf-8") if stderr_path.exists() else ""

    wrote_report = report_path.exists() and report_path.stat().st_size > 0
    print(f"exit: {proc.returncode} ({elapsed:.1f}s)")
    print(f"wrote report: {'yes' if wrote_report else 'no'}")
    if stdout.strip():
        print("caller response:")
        print(stdout.strip()[:2000])
    if stderr.strip():
        print(f"stderr saved: {stderr_path}")

    return 0 if proc.returncode == 0 and wrote_report else (proc.returncode or 1)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke-test replacement agents through Orchestra registry specs."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_output_dir(),
        help="Directory where agents write report/stdout/stderr files. Default: .kanban-orchestra/agent-smoke",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Per-agent timeout in seconds. Default: 600",
    )
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="NAME_OR_SPEC",
        help="Skip a matrix entry by name or registry spec. May be repeated.",
    )
    parser.add_argument(
        "--agent",
        action="append",
        metavar="NAME=SPEC",
        help="Add an agent spec for this run. May be repeated.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List the enabled default matrix and exit.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    agents = selected_agents(args.agent, set(args.skip))

    if args.list:
        for agent in agents:
            label = resolve_agent_label(agent.spec) or agent.spec
            print(f"{agent.name}: {agent.spec} ({label})")
        return 0

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"registry: {Path(__file__).with_name('agent_registry.yaml')}")
    print(f"output_dir: {output_dir}")
    if not agents:
        print("No agents selected.")
        return 0

    failures = 0
    for agent in agents:
        if run_one(agent, output_dir, args.timeout):
            failures += 1

    print(f"\ncomplete: {len(agents) - failures}/{len(agents)} succeeded")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
