import os
import sys
from pathlib import Path

_shared = Path(__file__).resolve().parent.parent.parent / "shared_scripts"
if str(_shared) not in sys.path:
    sys.path.insert(0, str(_shared))
from agent_registry import (  # type: ignore  # noqa: E402
    AGENTS as AGENTS,
    AGENT_ALIASES as AGENT_ALIASES,
    AGENT_CMD as AGENT_CMD,
    AGENT_DISPLAY_LABELS as AGENT_DISPLAY_LABELS,
    AGENT_PROVIDERS as AGENT_PROVIDERS,
    has_review_agent_command as has_review_agent_command,
    is_valid_agent_spec,
    resolve_agent_attribution as resolve_agent_attribution,
    resolve_agent_command,
    resolve_agent_label,
    resolve_review_agent_command as resolve_review_agent_command,
)


def _agent_default(env_key: str, fallback: str) -> str:
    """Return the value of env_key if set to a known agent, else fallback."""
    val = os.environ.get(env_key, "").strip()
    if val and is_valid_agent_spec(val):
        return val
    return fallback


def get_agent_display_label(agent: str) -> str:
    """Return the friendly UI display label for an agent/model.

    Prefer the shared human-readable label map. If no label is configured,
    infer a label from the configured --model value, then fall back to the key.
    Commit attribution intentionally uses get_agent_attribution() instead.
    """
    label = resolve_agent_label(agent)
    if label is not None:
        return label

    cmd = resolve_agent_command(agent) or []
    for i, part in enumerate(cmd):
        if part == "--model" and i + 1 < len(cmd):
            return cmd[i + 1]
    return agent


def is_valid_agent(agent: str) -> bool:
    return is_valid_agent_spec(agent)


def get_agent_display_name(agent: str) -> str:
    """Backward-compatible alias for get_agent_display_label()."""
    return get_agent_display_label(agent)


def get_agent_attribution(agent: str, *, review: bool = False) -> str:
    """Return command-backed commit attribution for an agent spec."""
    return resolve_agent_attribution(agent, review=review)


DEFAULT_SUPER_PLANNER = _agent_default("ORCHESTRA_DEFAULT_SUPER_PLANNER", "opus")
DEFAULT_SUPER_REVIEWER = _agent_default("ORCHESTRA_DEFAULT_SUPER_REVIEWER", "codex")

DEFAULT_PLANNER = _agent_default("ORCHESTRA_DEFAULT_PLANNER", "sonnet")
DEFAULT_PLAN_REVIEWER = _agent_default("ORCHESTRA_DEFAULT_PLAN_REVIEWER", "codex")

DEFAULT_CODER = _agent_default("ORCHESTRA_DEFAULT_CODER", "sonnet")
DEFAULT_REVIEWER = _agent_default("ORCHESTRA_DEFAULT_REVIEWER", "codex")

DEFAULT_UNBLOCKER = _agent_default("ORCHESTRA_DEFAULT_UNBLOCKER", "sonnet")

MAX_REVIEW_ROUNDS = 5
MAX_PRIOR_COMMENTS = 10
POLL_INTERVAL = 5
HEARTBEAT_INTERVAL = 10
STOP_AFTER_TASK_FILE = "KANBAN_ORCHESTRATOR_STOP_AFTER_TASK"
DASHBOARD_START_REQUEST_FILE = "dashboard-start-request"
DASHBOARD_PORT_BASE = 8427
