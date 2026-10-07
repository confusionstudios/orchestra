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
import agent_registry


def configure_agents(repo_root):
    return agent_registry.configure(Path(repo_root))


def agent_snapshot(kind="commit", *, coder=None, reviewer=None):
    registry = agent_registry.effective()
    primary = "super_planner" if kind == "supertask" else "coder"
    review_role = "super_reviewer" if kind == "supertask" else "reviewer"
    roles = {}
    for role in (primary, review_role, "planner", "plan_reviewer", "unblocker"):
        explicit = coder if role == primary else reviewer if role == review_role else None
        roles[role] = role_snapshot(role, explicit)
    return {"version": 1, "roles": roles, "fingerprint": registry.fingerprint}


def role_snapshot(role, explicit=None):
    registry = agent_registry.effective()
    choice = registry.role(role, explicit)
    spec, patch = choice["agent"], choice["patch"]
    run = registry.command(spec, patch=patch)
    review = registry.command(spec, review=True, patch=patch)
    return {
        "spec": spec, "source": choice["source"],
        "run": run, "review": review,
        "label": registry.label(spec),
        "attribution": agent_registry.attribution_from_command(spec, run),
        "review_attribution": agent_registry.attribution_from_command(spec, review),
    }


def check_worker_config(conn):
    worker = conn.execute("SELECT fingerprint FROM agent_worker_config WHERE singleton=1").fetchone()
    runtime = conn.execute("SELECT pid, status FROM orchestrator_runtime WHERE singleton=1").fetchone()
    if not worker or not runtime or runtime["status"] in ("stopped", "error", "hard-break"):
        return
    try:
        os.kill(runtime["pid"], 0)
    except (OSError, TypeError):
        return
    if worker["fingerprint"] != agent_registry.effective().fingerprint:
        raise ValueError("shared or repo-local agent configuration changed while the worker is running; restart the worker before admitting or editing agents")


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
# Bounded retries for reviewer transport, tool-host, and no-decision failures.
# These do not consume content-review rounds. After this many consecutive
# failures the task blocks as reviewer_unavailable and resumes at review.
REVIEWER_INFRA_ATTEMPTS = 5
REVIEWER_INFRA_BACKOFF_SECONDS = (2, 4, 8, 16)
MAX_PRIOR_COMMENTS = 10
POLL_INTERVAL = 5
HEARTBEAT_INTERVAL = 10
STOP_AFTER_TASK_FILE = "KANBAN_ORCHESTRATOR_STOP_AFTER_TASK"
DASHBOARD_START_REQUEST_FILE = "dashboard-start-request"
DASHBOARD_PORT_BASE = 8427
