"""
Shared agent invocation config for all orchestration systems.
"""

# How to invoke each agent.
# {prompt} is replaced at runtime with the full prompt + task file content.
# These defaults are intentionally non-interactive so queued local automation
# can run to completion. Treat this file as local agent permission policy and
# adjust it before running the orchestrator on a worktree.
AGENT_CMD = {
    "haiku":   ["claude", "--model", "Haiku", "-p", "{prompt}", "--dangerously-skip-permissions", "--output-format", "text"],
    "sonnet":  ["claude", "--model", "Sonnet", "-p", "{prompt}", "--dangerously-skip-permissions", "--output-format", "text"],
    "opus":    ["claude", "--model", "claude-opus-4-6", "-p", "{prompt}", "--dangerously-skip-permissions", "--output-format", "text"],
    "claude":  ["claude", "--model", "Sonnet", "-p", "{prompt}", "--dangerously-skip-permissions", "--output-format", "text"],
    "codex":   ["codex", "exec", "--yolo", "{prompt}"],
    "gemini":  ["gemini", "-p", "{prompt}", "--yolo", "--output-format", "text"],
    "kilo":    ["kilo", "--model", "kilo/kilo-auto/free", "run", "{prompt}"],
}

# Human-readable agent/model labels used in generated attribution footers.
# Keep these in shared config so prompts do not need to ask agents to self-report.
AGENT_DISPLAY_LABELS = {
    "codex": "GPT-5.5 medium",
    "opus": "Claude Opus 4.6",
    "sonnet": "Claude Sonnet 4.5",
    "haiku": "Claude Haiku 4.5",
    "claude": "Claude Sonnet 4.5",
}
