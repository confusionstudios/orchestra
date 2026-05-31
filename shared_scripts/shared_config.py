"""Compatibility exports for the shared Orchestra agent registry."""

# New agent definitions belong in agent_registry.yaml.
from agent_registry import (  # noqa: F401
    AGENTS,
    AGENT_CMD,
    AGENT_DISPLAY_LABELS,
    AGENT_PROVIDERS,
    PROVIDER_CMD,
    PROVIDER_DISPLAY_LABELS,
    is_valid_agent_spec,
    resolve_agent_command,
    resolve_agent_label,
)
