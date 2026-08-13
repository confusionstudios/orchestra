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


def _validate_command(command: Any, *, subject: str, require_model: bool = False) -> list[str]:
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        raise ValueError(f"{subject} has invalid command")

    prompt_count = sum(part.count("{prompt}") for part in command)
    if prompt_count != 1:
        raise ValueError(f"{subject} command must contain exactly one {{prompt}} placeholder")
    model_count = sum(part.count("{model}") for part in command)
    if require_model and model_count < 1:
        raise ValueError(f"{subject} command must contain a {{model}} placeholder")
    if not require_model and model_count:
        raise ValueError(f"{subject} command must not contain a {{model}} placeholder")

    return command


def _validate_optional_review_command(command: Any, *, subject: str, require_model: bool = False) -> list[str] | None:
    if command is None:
        return None
    return _validate_command(command, subject=subject, require_model=require_model)


def _validate_agent(entry: Any, seen: set[str]) -> tuple[str, list[str], str, list[str] | None]:
    if not isinstance(entry, dict):
        raise ValueError("agent registry entries must be mappings")

    key = entry.get("key")
    label = entry.get("label")
    command = entry.get("command")
    review_command = entry.get("review_command")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("agent registry entry has missing key")
    if key in seen:
        raise ValueError(f"duplicate agent key in registry: {key}")
    if not isinstance(label, str) or not label.strip():
        raise ValueError(f"agent registry entry has missing label: {key}")
    command = _validate_command(command, subject=f"agent registry entry {key}")
    review_command = _validate_optional_review_command(
        review_command,
        subject=f"agent registry entry {key} review_command",
    )

    seen.add(key)
    return key, command, label, review_command


def _validate_provider(name: Any, entry: Any) -> tuple[str, list[str], str, list[str] | None]:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("agent provider has missing name")
    if not _PROVIDER_RE.fullmatch(name):
        raise ValueError(f"agent provider has invalid name: {name}")
    if not isinstance(entry, dict):
        raise ValueError(f"agent provider entries must be mappings: {name}")

    label = entry.get("label")
    command = entry.get("command")
    review_command = entry.get("review_command")
    if not isinstance(label, str) or not label.strip():
        raise ValueError(f"agent provider has missing label: {name}")
    if "{model}" not in label:
        raise ValueError(f"agent provider label must contain {{model}}: {name}")
    command = _validate_command(command, subject=f"agent provider {name}", require_model=True)
    review_command = _validate_optional_review_command(
        review_command,
        subject=f"agent provider {name} review_command",
        require_model=True,
    )

    return name, command, label, review_command


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
        key, command, label, _review_command = _validate_agent(entry, seen)
        agents.append(key)
        commands[key] = command
        labels[key] = label

    return agents, commands, labels


def load_agent_review_commands(path: Path = REGISTRY_PATH) -> dict[str, list[str]]:
    raw = _load_registry(path)
    agents_raw = raw.get("agents")
    if not isinstance(agents_raw, list):
        raise ValueError(f"agent registry must contain an agents list: {path}")

    commands: dict[str, list[str]] = {}
    seen: set[str] = set()
    for entry in agents_raw:
        key, _command, _label, review_command = _validate_agent(entry, seen)
        if review_command is not None:
            commands[key] = review_command
    return commands


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
        provider, command, label, _review_command = _validate_provider(name, entry)
        providers.append(provider)
        commands[provider] = command
        labels[provider] = label
    return providers, commands, labels


def load_provider_review_commands(path: Path = REGISTRY_PATH) -> dict[str, list[str]]:
    raw = _load_registry(path)
    providers_raw = raw.get("providers", {})
    if providers_raw is None:
        providers_raw = {}
    if not isinstance(providers_raw, dict):
        raise ValueError(f"agent registry providers must be a mapping: {path}")

    commands: dict[str, list[str]] = {}
    for name, entry in providers_raw.items():
        provider, _command, _label, review_command = _validate_provider(name, entry)
        if review_command is not None:
            commands[provider] = review_command
    return commands


def _agent_keys(raw: dict[str, Any], *, path: Path) -> set[str]:
    agents_raw = raw.get("agents")
    if not isinstance(agents_raw, list):
        raise ValueError(f"agent registry must contain an agents list: {path}")
    seen: set[str] = set()
    for entry in agents_raw:
        _validate_agent(entry, seen)
    return seen


def _provider_names(raw: dict[str, Any], *, path: Path) -> set[str]:
    providers_raw = raw.get("providers", {})
    if providers_raw is None:
        providers_raw = {}
    if not isinstance(providers_raw, dict):
        raise ValueError(f"agent registry providers must be a mapping: {path}")
    names: set[str] = set()
    for name, entry in providers_raw.items():
        provider, _command, _label, _review_command = _validate_provider(name, entry)
        names.add(provider)
    return names


def _validate_alias_graph(
    aliases: dict[str, str],
    *,
    agent_keys: set[str],
    provider_names: set[str],
) -> None:
    for name in aliases:
        path = [name]
        current = aliases[name]
        while current in aliases:
            if current in path:
                raise ValueError(f"alias cycle: {' -> '.join(path + [current])}")
            path.append(current)
            current = aliases[current]
        if current in agent_keys:
            continue
        parsed = _split_provider_model(current)
        if parsed is not None and parsed[0] in provider_names:
            continue
        raise ValueError(f"alias {name} target not found: {current}")


def load_agent_aliases(path: Path = REGISTRY_PATH) -> dict[str, str]:
    """Load semantic aliases that target existing agent specs without duplicating commands."""
    raw = _load_registry(path)
    agent_keys = _agent_keys(raw, path=path)
    provider_names = _provider_names(raw, path=path)

    aliases_raw = raw.get("aliases", {})
    if aliases_raw is None:
        aliases_raw = {}
    if not isinstance(aliases_raw, dict):
        raise ValueError(f"agent registry aliases must be a mapping: {path}")

    aliases: dict[str, str] = {}
    for name, target in aliases_raw.items():
        if not isinstance(name, str) or not name.strip() or not _PROVIDER_RE.fullmatch(name):
            raise ValueError(f"alias has invalid name: {name}")
        if name in agent_keys:
            raise ValueError(f"duplicate agent name in registry: {name}")
        if not isinstance(target, str) or not target.strip():
            raise ValueError(f"alias {name} has invalid target")
        aliases[name] = target

    _validate_alias_graph(aliases, agent_keys=agent_keys, provider_names=provider_names)
    return aliases


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


def _deref_alias(spec: str) -> str:
    """Follow registry alias chains to a command-backed agent spec."""
    path: list[str] = []
    current = spec
    while current in AGENT_ALIASES:
        if current in path:
            raise ValueError(f"alias cycle: {' -> '.join(path + [current])}")
        path.append(current)
        current = AGENT_ALIASES[current]
    return current


def resolve_agent_command(spec: str) -> list[str] | None:
    """Return a command template for an alias or provider:model agent spec."""
    spec = _deref_alias(spec)
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


def has_review_agent_command(spec: str) -> bool:
    """Return True when an alias/provider:model spec has an explicit review command."""
    spec = _deref_alias(spec)
    if spec in AGENT_REVIEW_CMD:
        return True

    parsed = _split_provider_model(spec)
    if parsed is None:
        return False
    provider, _model = parsed
    return provider in PROVIDER_REVIEW_CMD


def resolve_review_agent_command(spec: str) -> list[str] | None:
    """Return the review command for an agent spec, falling back to the normal command."""
    spec = _deref_alias(spec)
    if spec in AGENT_REVIEW_CMD:
        return list(AGENT_REVIEW_CMD[spec])
    if spec in AGENT_CMD:
        return list(AGENT_CMD[spec])

    parsed = _split_provider_model(spec)
    if parsed is None:
        return None
    provider, model = parsed
    command = PROVIDER_REVIEW_CMD.get(provider) or PROVIDER_CMD.get(provider)
    if command is None:
        return None
    return [part.replace("{model}", model) for part in command]


def resolve_agent_label(spec: str) -> str | None:
    """Return a display label for an alias or provider:model agent spec."""
    spec = _deref_alias(spec)
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


def _command_option(command: list[str], *options: str) -> str | None:
    """Return an explicitly supplied command option value, if present."""
    for index, part in enumerate(command):
        if part in options and index + 1 < len(command):
            return command[index + 1]
        for option in options:
            prefix = f"{option}="
            if part.startswith(prefix):
                return part[len(prefix):]
    return None


def _command_reasoning_effort(command: list[str]) -> str | None:
    """Return an explicit Codex reasoning-effort config value, if present."""
    config_options = {"-c", "--config"}
    for index, part in enumerate(command):
        value = command[index + 1] if part in config_options and index + 1 < len(command) else None
        if value is None and part.startswith("--config="):
            value = part.removeprefix("--config=")
        if value and value.startswith("model_reasoning_effort="):
            return value.split("=", 1)[1].strip('"\'')
    return None


def resolve_agent_attribution(spec: str, *, review: bool = False) -> str:
    """Return the exact agent spec plus facts explicit in its resolved command.

    Display labels are intentionally excluded: they are UI copy and can become
    stale. Only an explicit --model option and model_reasoning_effort config are
    included as additional facts.
    """
    command = resolve_review_agent_command(spec) if review else resolve_agent_command(spec)
    if not command:
        return "unattributed"

    model = _command_option(command, "-m", "--model")
    effort = _command_reasoning_effort(command)

    facts = []
    if model and _split_provider_model(spec) is None:
        facts.append(f"model: {model}")
    if effort:
        facts.append(f"reasoning effort: {effort}")
    return f"{spec} ({'; '.join(facts)})" if facts else spec


def is_valid_agent_spec(spec: str) -> bool:
    return resolve_agent_command(spec) is not None


AGENTS, AGENT_CMD, AGENT_DISPLAY_LABELS = load_agent_registry()
AGENT_PROVIDERS, PROVIDER_CMD, PROVIDER_DISPLAY_LABELS = load_agent_providers()
AGENT_REVIEW_CMD = load_agent_review_commands()
PROVIDER_REVIEW_CMD = load_provider_review_commands()
AGENT_ALIASES = load_agent_aliases()
