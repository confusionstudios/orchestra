"""
Load the canonical Orchestra agent registry.

Agent keys, command templates, and display labels live in
agent_registry.yaml. Keep this module small so every runtime can share the
same source of truth.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import yaml


REGISTRY_PATH = Path(__file__).with_name("agent_registry.yaml")
_PROVIDER_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/+-]+$")


def _load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"agent registry must be a mapping: {path}")
    return raw


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


def _validate_provider(name: Any, entry: Any) -> tuple[str, list[str], str]:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("agent provider has missing name")
    if not _PROVIDER_RE.fullmatch(name):
        raise ValueError(f"agent provider has invalid name: {name}")
    if not isinstance(entry, dict):
        raise ValueError(f"agent provider entries must be mappings: {name}")

    label = entry.get("label")
    command = entry.get("command")
    if not isinstance(label, str) or not label.strip():
        raise ValueError(f"agent provider has missing label: {name}")
    if "{model}" not in label:
        raise ValueError(f"agent provider label must contain {{model}}: {name}")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        raise ValueError(f"agent provider has invalid command: {name}")

    prompt_count = sum(part.count("{prompt}") for part in command)
    if prompt_count != 1:
        raise ValueError(f"agent provider command must contain exactly one {{prompt}} placeholder: {name}")
    model_count = sum(part.count("{model}") for part in command)
    if model_count < 1:
        raise ValueError(f"agent provider command must contain a {{model}} placeholder: {name}")

    return name, command, label


def load_agent_registry(path: Path = REGISTRY_PATH) -> tuple[list[str], dict[str, list[str]], dict[str, str]]:
    raw = _load_registry(path)
    agents_raw = raw.get("agents")
    if not isinstance(agents_raw, list):
        raise ValueError(f"agent registry must contain an agents list: {path}")

    agents: list[str] = []
    commands: dict[str, list[str]] = {}
    labels: dict[str, str] = {}
    seen: set[str] = set()

    for entry in agents_raw:
        key, command, label = _validate_agent(entry, seen)
        agents.append(key)
        commands[key] = command
        labels[key] = label

    return agents, commands, labels


def load_agent_providers(path: Path = REGISTRY_PATH) -> tuple[list[str], dict[str, list[str]], dict[str, str]]:
    raw = _load_registry(path)
    providers_raw = raw.get("providers", {})
    if providers_raw is None:
        providers_raw = {}
    if not isinstance(providers_raw, dict):
        raise ValueError(f"agent registry providers must be a mapping: {path}")

    providers: list[str] = []
    commands: dict[str, list[str]] = {}
    labels: dict[str, str] = {}
    for name, entry in providers_raw.items():
        provider, command, label = _validate_provider(name, entry)
        providers.append(provider)
        commands[provider] = command
        labels[provider] = label
    return providers, commands, labels


def _split_provider_model(spec: str) -> tuple[str, str] | None:
    if ":" not in spec:
        return None
    provider, model = spec.split(":", 1)
    if not provider or not model:
        return None
    if not _PROVIDER_RE.fullmatch(provider):
        return None
    if not _MODEL_RE.fullmatch(model):
        return None
    if "{prompt}" in model or "{model}" in model:
        return None
    return provider, model


def resolve_agent_command(spec: str) -> list[str] | None:
    """Return a command template for an alias or provider:model agent spec."""
    if spec in AGENT_CMD:
        return list(AGENT_CMD[spec])

    parsed = _split_provider_model(spec)
    if parsed is None:
        return None
    provider, model = parsed
    command = PROVIDER_CMD.get(provider)
    if command is None:
        return None
    return [part.replace("{model}", model) for part in command]


def resolve_agent_label(spec: str) -> str | None:
    """Return a display label for an alias or provider:model agent spec."""
    if spec in AGENT_DISPLAY_LABELS:
        return AGENT_DISPLAY_LABELS[spec]

    parsed = _split_provider_model(spec)
    if parsed is None:
        return None
    provider, model = parsed
    label = PROVIDER_DISPLAY_LABELS.get(provider)
    if label is None:
        return None
    return label.replace("{model}", model)


def is_valid_agent_spec(spec: str) -> bool:
    return resolve_agent_command(spec) is not None


AGENTS, AGENT_CMD, AGENT_DISPLAY_LABELS = load_agent_registry()
AGENT_PROVIDERS, PROVIDER_CMD, PROVIDER_DISPLAY_LABELS = load_agent_providers()
