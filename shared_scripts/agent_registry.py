"""
Load the canonical Orchestra agent registry.

Agent keys, command templates, and display labels live in
agent_registry.yaml. Keep this module small so every runtime can share the
same source of truth.
"""

from __future__ import annotations

from pathlib import Path
import hashlib
import os
import re
import subprocess
from typing import Any

import yaml


REGISTRY_PATH = Path(__file__).with_name("agent_registry.yaml")
_PROVIDER_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _valid_model(model: Any) -> bool:
    return (isinstance(model, str) and bool(model.strip()) and "\x00" not in model
            and "\n" not in model and "\r" not in model
            and "{prompt}" not in model and "{model}" not in model)


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


def _reject_codex_review_uncommitted_with_prompt(command: list[str], *, subject: str) -> None:
    """Current Codex CLI treats `review --uncommitted` and `[PROMPT]` as exclusive."""
    if not command or command[0] != "codex":
        return
    if "review" not in command:
        return
    has_uncommitted = "--uncommitted" in command
    has_prompt = any("{prompt}" in part for part in command)
    if has_uncommitted and has_prompt:
        raise ValueError(
            f"{subject} cannot combine --uncommitted with {{prompt}}: "
            "current Codex CLI treats those arguments as mutually exclusive"
        )


def _validate_optional_review_command(command: Any, *, subject: str, require_model: bool = False) -> list[str] | None:
    if command is None:
        return None
    command = _validate_command(command, subject=subject, require_model=require_model)
    _reject_codex_review_uncommitted_with_prompt(command, subject=subject)
    return command


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
    if not _valid_model(model):
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
    if _ACTIVE is not None and _ACTIVE.fingerprint != "missing":
        return _ACTIVE.command(spec)
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
    if _ACTIVE is not None and _ACTIVE.fingerprint != "missing":
        target = _ACTIVE._target(spec)
        parsed = _split_provider_model(target)
        return target in _ACTIVE.reviews or bool(parsed and parsed[0] in PROVIDER_REVIEW_CMD)
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
    if _ACTIVE is not None and _ACTIVE.fingerprint != "missing":
        return _ACTIVE.command(spec, review=True)
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
    if _ACTIVE is not None and _ACTIVE.fingerprint != "missing":
        return _ACTIVE.label(spec)
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
    return attribution_from_command(spec, command)


def attribution_from_command(spec: str, command: list[str] | None) -> str:
    """Describe a persisted command without consulting current aliases."""
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


ROLE_FALLBACKS = {
    "coder": "sonnet", "reviewer": "codex", "planner": "sonnet",
    "plan_reviewer": "codex", "super_planner": "opus",
    "super_reviewer": "codex", "unblocker": "sonnet",
}
LOCAL_CONFIG_NAME = Path(".kanban-orchestra/agents.yaml")
MODEL_CACHE_NAME = Path(".kanban-orchestra/model-cache.json")
_PROVIDER_EXECUTABLES = {
    "codex": "codex", "claude": "claude", "cursor": "remote-control-cursor",
    "kilo": "kilo", "antigravity": "agy",
}


def _provider_for(command: list[str]) -> str:
    for provider, executable in _PROVIDER_EXECUTABLES.items():
        if command and command[0] == executable:
            return provider
    raise ValueError(f"unknown provider command: {command[:1]}")


def _replace_model(command: list[str], model: str) -> list[str]:
    result = list(command)
    for index, part in enumerate(result[:-1]):
        if part in ("--model", "-m"):
            result[index + 1] = model
            return result
    if result and result[0] == "agy":
        return [result[0], "--model", model, *result[1:]]
    raise ValueError("provider template has no --model argument")


def _option_insertion_point(command: list[str]) -> int:
    # These subcommands immediately precede the prompt in their templates.
    # A model or option value may also literally be "review" or "run".
    if command[0] == "codex" and command[-2:] == ["review", "{prompt}"]:
        return len(command) - 2
    if command[0] == "kilo" and command[-2:] == ["run", "{prompt}"]:
        return len(command) - 2
    return command.index("{prompt}")


def _patch_reasoning(command: list[str], provider: str, value: str | None) -> list[str]:
    if provider != "codex":
        raise ValueError(f"reasoning is unsupported for provider {provider}; omit reasoning")
    result = list(command)
    for index, part in enumerate(result[:-1]):
        if part in ("-c", "--config") and result[index + 1].startswith("model_reasoning_effort="):
            del result[index:index + 2]
            break
    if value is not None:
        insertion = _option_insertion_point(result)
        result[insertion:insertion] = ["-c", f'model_reasoning_effort="{value}"']
    return result


def _patch_options(command: list[str], options: dict[str, Any]) -> list[str]:
    result = list(command)
    for option, value in options.items():
        if not isinstance(option, str) or not re.fullmatch(r"--?[A-Za-z][A-Za-z0-9-]*", option):
            raise ValueError(f"invalid option name {option!r}; use a CLI flag such as --trust")
        if option in ("--model", "-m", "-c", "--config"):
            raise ValueError(f"{option} is configured through model or reasoning, not options")
        if value is not None and type(value) is not bool and (not isinstance(value, str) or not value or "{prompt}" in value):
            raise ValueError(f"invalid value for option {option}; use a nonempty string, true, false, or null")
        indexes = [i for i, part in enumerate(result) if part == option]
        for index in reversed(indexes):
            del result[index]
            if option in ("--output-format", "--mode", "--timeout") and index < len(result) and result[index] != "{prompt}":
                del result[index]
        if value is not None and value is not False:
            insertion = _option_insertion_point(result)
            result[insertion:insertion] = [option] + ([] if value is True else [value])
    return result


class EffectiveRegistry:
    """Validated, immutable-for-one-process view of one work repository."""

    def __init__(self, repo_root: Path):
        self.path = Path(repo_root).resolve() / LOCAL_CONFIG_NAME
        self.agents = dict(AGENT_CMD)
        self.reviews = dict(AGENT_REVIEW_CMD)
        self.labels = dict(AGENT_DISPLAY_LABELS)
        self.aliases = dict(AGENT_ALIASES)
        self.defaults: dict[str, dict[str, Any]] = {}
        self.sources: dict[str, str] = {name: "product" for name in self.agents}
        self.field_sources: dict[str, dict[str, str]] = {
            name: {field: "product" for field in ("provider", "model", "reasoning", "options", "label")}
            for name in self.agents
        }
        self.alias_sources: dict[str, str] = {name: "product" for name in self.aliases}
        if not self.path.exists():
            self.fingerprint = "missing"
            return
        contents = self.path.read_bytes()
        self.fingerprint = hashlib.sha256(contents).hexdigest()
        try:
            raw = yaml.safe_load(contents)
            self._load(raw)
        except (ValueError, yaml.YAMLError) as exc:
            raise ValueError(f"{self.path}: {exc}") from exc

    def _load(self, raw: Any) -> None:
        if (not isinstance(raw, dict) or set(raw) - {"version", "agents", "aliases", "defaults"}
                or type(raw.get("version")) is not int or raw["version"] != 1):
            raise ValueError("expected version: 1 and only agents, aliases, defaults mappings")
        for section in ("agents", "aliases", "defaults"):
            if not isinstance(raw.get(section, {}), dict):
                raise ValueError(f"{section}: expected a mapping")
        for name, patch in raw.get("agents", {}).items():
            if not isinstance(name, str) or not _PROVIDER_RE.fullmatch(name):
                raise ValueError(f"agents.{name}: invalid name")
            self._validate_patch(patch, f"agents.{name}", agent=True)
            if name in self.aliases and name not in self.agents:
                del self.aliases[name]
            baseline = self.agents.get(name)
            baseline_provider = _provider_for(baseline) if baseline else None
            provider = patch.get("provider", baseline_provider)
            if provider not in PROVIDER_CMD:
                raise ValueError(f"agents.{name}.provider: choose {', '.join(PROVIDER_CMD)}")
            if baseline is None and "model" not in patch:
                raise ValueError(f"agents.{name}.model: required for a new agent")
            if provider != baseline_provider and "model" not in patch:
                raise ValueError(f"agents.{name}.model: required when changing provider")
            if provider != baseline_provider:
                command = [part.replace("{model}", patch["model"]) for part in PROVIDER_CMD[provider]]
                review_base = PROVIDER_REVIEW_CMD.get(provider)
                review = [part.replace("{model}", patch["model"]) for part in review_base] if review_base else None
            else:
                command = list(baseline)
                review = list(self.reviews[name]) if name in self.reviews else None
                if "model" in patch:
                    command = _replace_model(command, patch["model"])
                    if review:
                        review = _replace_model(review, patch["model"])
            if "reasoning" in patch:
                command = _patch_reasoning(command, provider, patch["reasoning"])
                if review:
                    review = _patch_reasoning(review, provider, patch["reasoning"])
            if "options" in patch:
                command = _patch_options(command, patch["options"])
                if review:
                    review = _patch_options(review, patch["options"])
            self.agents[name] = command
            if review:
                self.reviews[name] = review
            else:
                self.reviews.pop(name, None)
            self.labels[name] = patch.get("label", self.labels.get(name) if provider == baseline_provider else PROVIDER_DISPLAY_LABELS[provider].replace("{model}", patch["model"]))
            self.sources[name] = str(self.path)
            self.field_sources[name] = dict(self.field_sources.get(name, {}))
            for field in patch:
                self.field_sources[name][field] = str(self.path)
        self.aliases.update(raw.get("aliases", {}))
        for name in raw.get("aliases", {}):
            if not isinstance(name, str) or not _PROVIDER_RE.fullmatch(name):
                raise ValueError(f"aliases.{name}: invalid name")
            if name in self.agents and name not in raw.get("agents", {}):
                self.agents.pop(name)
                self.reviews.pop(name, None)
                self.labels.pop(name, None)
            self.alias_sources[name] = str(self.path)
        for name, target in self.aliases.items():
            if name in self.agents:
                raise ValueError(f"aliases.{name}: collides with an agent")
            if not isinstance(target, str) or not target:
                raise ValueError(f"aliases.{name}: expected agent or provider:model")
        _validate_alias_graph(self.aliases, agent_keys=set(self.agents), provider_names=set(PROVIDER_CMD))
        for role, patch in raw.get("defaults", {}).items():
            if role not in ROLE_FALLBACKS:
                raise ValueError(f"defaults.{role}: unknown role")
            self._validate_patch(patch, f"defaults.{role}")
            if "agent" in patch and self.command(patch["agent"]) is None:
                raise ValueError(f"defaults.{role}.agent: unknown agent {patch['agent']!r}")
            spec = patch.get("agent", ROLE_FALLBACKS[role])
            try:
                self.command(spec, patch=patch)
                self.command(spec, review=True, patch=patch)
            except ValueError as exc:
                raise ValueError(f"defaults.{role}: {exc}") from exc
            self.defaults[role] = patch

    @staticmethod
    def _validate_patch(patch: Any, field: str, *, agent: bool = False) -> None:
        allowed = ({"provider", "model", "reasoning", "options", "label"} if agent
                   else {"agent", "model", "reasoning", "options"})
        if not isinstance(patch, dict) or set(patch) - allowed:
            raise ValueError(f"{field}: expected mapping with only {', '.join(sorted(allowed))}")
        for key in ("model", "agent", "provider", "label"):
            if key in patch and (not isinstance(patch[key], str) or not patch[key]
                                 or (key == "model" and not _valid_model(patch[key]))):
                raise ValueError(f"{field}.{key}: expected nonempty literal string")
        if "reasoning" in patch and patch["reasoning"] is not None and (not isinstance(patch["reasoning"], str) or not re.fullmatch(r"[A-Za-z0-9_-]+", patch["reasoning"])):
            raise ValueError(f"{field}.reasoning: expected a level or null")
        if "options" in patch:
            if not isinstance(patch["options"], dict):
                raise ValueError(f"{field}.options: expected mapping")
            _patch_options(["{prompt}"], patch["options"])

    def _target(self, spec: str) -> str:
        path: list[str] = []
        while spec in self.aliases:
            if spec in path:
                raise ValueError(f"alias cycle: {' -> '.join(path + [spec])}")
            path.append(spec)
            spec = self.aliases[spec]
        return spec

    def command(self, spec: str, *, review: bool = False, patch: dict[str, Any] | None = None) -> list[str] | None:
        target = self._target(spec)
        command = (self.reviews.get(target) if review else None) or self.agents.get(target)
        if command is None:
            parsed = _split_provider_model(target)
            if parsed is None or parsed[0] not in PROVIDER_CMD:
                return None
            provider, model = parsed
            template = (PROVIDER_REVIEW_CMD.get(provider) if review else None) or PROVIDER_CMD[provider]
            command = [part.replace("{model}", model) for part in template]
        command = list(command)
        if patch:
            provider = _provider_for(command)
            if "model" in patch:
                command = _replace_model(command, patch["model"])
            if "reasoning" in patch:
                command = _patch_reasoning(command, provider, patch["reasoning"])
            if "options" in patch:
                command = _patch_options(command, patch["options"])
        return command

    def label(self, spec: str) -> str | None:
        target = self._target(spec)
        if target in self.labels:
            return self.labels[target]
        parsed = _split_provider_model(target)
        return PROVIDER_DISPLAY_LABELS[parsed[0]].replace("{model}", parsed[1]) if parsed and parsed[0] in PROVIDER_DISPLAY_LABELS else None

    def role(self, name: str, explicit: str | None = None) -> dict[str, Any]:
        import os
        if explicit is not None:
            spec, patch, source = explicit, {}, "task"
        elif f"ORCHESTRA_DEFAULT_{name.upper()}" in os.environ:
            spec, patch, source = os.environ[f"ORCHESTRA_DEFAULT_{name.upper()}"], {}, "environment"
        else:
            patch = self.defaults.get(name, {})
            spec = patch.get("agent", ROLE_FALLBACKS[name])
            source = str(self.path) if patch else "product"
        if self.command(spec) is None:
            raise ValueError(f"{source}: {name} agent {spec!r} is invalid; choose an agent or provider:model")
        return {"agent": spec, "patch": {key: value for key, value in patch.items() if key != "agent"}, "source": source}


_ACTIVE: EffectiveRegistry | None = None


def configure(repo_root: Path) -> EffectiveRegistry:
    global _ACTIVE
    _ACTIVE = EffectiveRegistry(repo_root)
    return _ACTIVE


def effective() -> EffectiveRegistry:
    return _ACTIVE or EffectiveRegistry(work_repo_root())


def work_repo_root() -> Path:
    """Resolve the launched work repo, never the registry module's checkout."""
    if os.environ.get("KANBAN_DB"):
        return Path(os.environ["KANBAN_DB"]).expanduser().resolve().parent
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True, check=False)
    return Path(result.stdout.strip() or Path.cwd()).resolve()
