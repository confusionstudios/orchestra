"""
Load the canonical Orchestra agent registry.

Agent keys, command templates, and display labels live in
agent_registry.yaml. Keep this module small so every runtime can share the
same source of truth.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


REGISTRY_PATH = Path(__file__).with_name("agent_registry.yaml")


def _load_registry(path: Path = REGISTRY_PATH) -> list[dict[str, Any]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"agent registry must be a mapping: {path}")
    agents = raw.get("agents")
    if not isinstance(agents, list):
        raise ValueError(f"agent registry must contain an agents list: {path}")
    return agents


def _validate_agent(entry: Any, seen: set[str]) -> tuple[str, list[str], str]:
    if not isinstance(entry, dict):
        raise ValueError("agent registry entries must be mappings")

    key = entry.get("key")
    label = entry.get("label")
    command = entry.get("command")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("agent registry entry has missing key")
    if key in seen:
        raise ValueError(f"duplicate agent key in registry: {key}")
    if not isinstance(label, str) or not label.strip():
        raise ValueError(f"agent registry entry has missing label: {key}")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        raise ValueError(f"agent registry entry has invalid command: {key}")

    prompt_count = sum(part.count("{prompt}") for part in command)
    if prompt_count != 1:
        raise ValueError(f"agent command must contain exactly one {{prompt}} placeholder: {key}")

    seen.add(key)
    return key, command, label


def load_agent_registry(path: Path = REGISTRY_PATH) -> tuple[list[str], dict[str, list[str]], dict[str, str]]:
    agents: list[str] = []
    commands: dict[str, list[str]] = {}
    labels: dict[str, str] = {}
    seen: set[str] = set()

    for entry in _load_registry(path):
        key, command, label = _validate_agent(entry, seen)
        agents.append(key)
        commands[key] = command
        labels[key] = label

    return agents, commands, labels


AGENTS, AGENT_CMD, AGENT_DISPLAY_LABELS = load_agent_registry()
