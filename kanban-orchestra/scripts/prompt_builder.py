"""Prompt and task-context assembly for Kanban Orchestra agents."""

import re
import subprocess
from pathlib import Path

import config
import repo_policy


DEFAULT_CODER = config.DEFAULT_CODER
DEFAULT_REVIEWER = config.DEFAULT_REVIEWER
MAX_PRIOR_COMMENTS = config.MAX_PRIOR_COMMENTS
MASTER_BRANCHES = {"master", "main"}
GITHUB_PR_URL_RE = re.compile(r"https://github\.com/[^\s/]+/[^\s/]+/pull/\d+")


def _prompts_dir():
    return Path(__file__).resolve().parent.parent / "prompts"


def _repo_root():
    """Return the git repo root, or a fallback string on failure."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return "(unknown — run: git rev-parse --show-toplevel)"


def _task_reviewer(task):
    """Return the code-review agent configured for a task."""
    return task.get("reviewer_agent") or DEFAULT_REVIEWER


def _build_reviewer_handoff(task, comments, skip_build_policy=False):
    """
    Build the reviewer handoff section injected into commit-review prompts.

    Provides repo root, task CLI path, the maker's recorded commit message,
    and the maker's recorded validation summary so reviewers start with
    complete context and do not need to rediscover the environment or rerun
    the maker's validation steps.

    skip_build_policy: when True, the repo has KANBAN_SKIP_BUILD_UNTIL_APPROVED enabled,
    so a deferred validation result is expected and normal.
    """
    repo_root = _repo_root()
    # Extract the most recent commit-message comment (the maker's summary)
    commit_msg_entries = [c for c in comments if c.get("kind") == "commit-message"]
    if commit_msg_entries:
        latest = commit_msg_entries[-1]["message"]
        maker_summary = f"**Maker's proposed commit message** (most recent):\n```\n{latest}\n```"
    else:
        maker_summary = "*(no commit-message comment recorded yet)*"

    # Extract the most recent validation comment for the current review round only.
    # Using the current round prevents stale round-N results from appearing in
    # a round-(N+1) review if the maker forgot to record fresh validation.
    current_round = task.get("review_round", 0)
    validation_entries = [
        c for c in comments
        if c.get("kind") == "validation" and c.get("review_round") == current_round
    ]
    if validation_entries:
        latest_validation = validation_entries[-1]["message"]
        validation_summary = (
            f"**Maker's validation summary** (most recent):\n```\n{latest_validation}\n```"
        )
    elif skip_build_policy:
        validation_summary = (
            "*(no full-build validation recorded — this repo has the standalone "
            "`KANBAN_SKIP_BUILD_UNTIL_APPROVED` marker in `AGENTS.md`, so the full build is intentionally deferred to "
            "`commit-make` finalization. The missing full-build result (e.g. test suite output) "
            "is expected here. The maker is still required to have recorded a deferred-validation "
            "comment explicitly stating that the full build was intentionally skipped — if that "
            "comment is present, the absence of a full-build result is correct and not a problem.)*"
        )
    else:
        validation_summary = "*(no validation comment recorded — maker may not have run the build)*"

    policy_note = (
        "\n> **Repo policy:** `KANBAN_SKIP_BUILD_UNTIL_APPROVED` — full-build validation is "
        "deferred to post-approval `commit-make` finalization. You are reviewing the diff without "
        "a full-build result. If the maker recorded a deferred-validation comment, that is "
        "expected and correct per repo policy.\n"
        if skip_build_policy else ""
    )

    return f"""## Reviewer Handoff

- **Repo root:** `{repo_root}`
- **Task CLI:** `task <subcommand>` (shorthand defined in shared context above; expands to `"$ORCHESTRA_DIR/bin/ko-task"`)
{policy_note}
{maker_summary}

{validation_summary}

**Your primary job** is to inspect `git diff --cached` and record an approval or rejection.
Do **not** rerun the maker's validation by default. Only run additional commands if the
diff or reported results give a specific reason to verify something — and prefer targeted
checks (e.g. `grep`, reading a single file) over full test reruns.
"""


def _latest_pr_metadata_comment(comments):
    """Return the most recent comment that appears to record GitHub PR metadata."""
    for comment in reversed(comments):
        message = comment.get("message") or ""
        if GITHUB_PR_URL_RE.search(message):
            return comment
    return None


def _build_pull_request_reviewer_handoff(task, comments):
    """Build the handoff section for pull-request-review prompts."""
    repo_root = _repo_root()
    latest = _latest_pr_metadata_comment(comments)
    if latest:
        metadata = f"**Maker's recorded PR metadata** (most recent):\n```\n{latest['message']}\n```"
    else:
        metadata = "*(no GitHub PR metadata comment with a PR URL recorded yet)*"

    return f"""## Pull Request Reviewer Handoff

- **Repo root:** `{repo_root}`
- **Task CLI:** `task <subcommand>` (shorthand defined in shared context above; expands to `"$ORCHESTRA_DIR/bin/ko-task"`)

{metadata}

**Your primary job** is to review the PR title and body quality, including whether
the branch summary accurately reflects the branch against `master`.
Do not perform implementation code review for this task kind.
"""


def _latest_other_evidence_comment(comments):
    """Return the most recent non-orchestrator comment for other-review evidence."""
    for comment in reversed(comments):
        if comment.get("kind") == "comment" and comment.get("author") != "orchestrator":
            return comment
    return None


def _build_other_reviewer_handoff(task, comments):
    """Build the handoff section for other-review prompts."""
    repo_root = _repo_root()
    latest = _latest_other_evidence_comment(comments)
    if latest:
        evidence = f"**Maker's recorded completion evidence** (most recent):\n```\n{latest['message']}\n```"
    else:
        evidence = "*(no maker completion evidence comment recorded yet)*"

    return f"""## Other Reviewer Handoff

- **Repo root:** `{repo_root}`
- **Task CLI:** `task <subcommand>` (shorthand defined in shared context above; expands to `"$ORCHESTRA_DIR/bin/ko-task"`)

{evidence}

**Your primary job** is to review whether the durable evidence is clear,
actionable, and appropriate for a commit-free and PR-free task.
"""


def _filter_comments_for_prompt(comments, verb, task=None):
    """
    Return a filtered, capped list of comments for the ## Prior Comments block.

    Filtering rules:
    - For reviewer verbs (commit-review, commit-review-supertask): exclude
      commit-message and validation kinds — both are already surfaced in the
      ## Reviewer Handoff section, so including them again would be redundant.
    - For commit-make: exclude commit-message and validation kinds. The build
      prompt must create fresh versions, and the finalization prompt explicitly
      reads the canonical commit-message from `task show-comments`, so inlining
      prior bodies only bloats small-task prompts.
    - For all verbs: cap at MAX_PRIOR_COMMENTS, keeping the most recent entries.

    Returns (filtered_comments, total_before_cap) so callers can render a
    truncation note when comments were dropped.
    """
    is_reviewer = verb in ("commit-review", "commit-review-supertask", "commit-plan-review")
    if is_reviewer or verb == "commit-make":
        filtered = [c for c in comments if c.get("kind") not in ("commit-message", "validation")]
    else:
        filtered = list(comments)

    # Always exclude plan-approval and plan-rejection from commit-review prompts
    # (they belong only to the planning phase and would be confusing noise in code review context)
    if verb in ("commit-review", "commit-review-supertask"):
        filtered = [c for c in filtered if c.get("kind") not in ("plan-approval", "plan-rejection")]

    total = len(filtered)
    if total > MAX_PRIOR_COMMENTS:
        filtered = filtered[-MAX_PRIOR_COMMENTS:]
    return filtered, total


def _prompt_path_for_verb(verb, task):
    """Return the concrete prompt file for a lifecycle verb."""
    prompt_name = verb
    if verb == "commit-make":
        if task.get("last_review_decision") == "approve":
            prompt_name = "commit-make-finalize"
        else:
            prompt_name = "commit-make-build"

    return _prompts_dir() / f"{prompt_name}.md"


def build_prompt(task, verb, agent_name, comments):
    """Assemble the full prompt from shared context + verb-specific prompt."""
    shared_path = _prompts_dir() / "shared-task-context.md"
    verb_path = _prompt_path_for_verb(verb, task)

    shared_text = shared_path.read_text() if shared_path.exists() else ""
    verb_text = verb_path.read_text() if verb_path.exists() else ""

    # Conditionally prepend Path C (stash recovery) for commit-make
    if verb == "commit-make" and task.get("stash_ref"):
        path_c_path = _prompts_dir() / "commit-make-stash-recovery.md"
        if path_c_path.exists():
            verb_text = path_c_path.read_text() + "\n\n" + verb_text

    # Filter and cap comments for injection
    filtered_comments, total_comments = _filter_comments_for_prompt(comments, verb, task)
    comments_text = ""
    if filtered_comments:
        lines = []
        if total_comments > MAX_PRIOR_COMMENTS:
            lines.append(f"*(showing {MAX_PRIOR_COMMENTS} most recent of {total_comments} comments)*")
        for c in filtered_comments:
            lines.append(f"- [{c['kind']}] (round {c['review_round']}, {c['author'] or 'unknown'}): {c['message']}")
        comments_text = "\n".join(lines)

    # Determine role and visibility
    is_coder = verb in ("commit-make", "commit-make-supertask", "commit-plan", "pull-request-make", "other-make")
    is_reviewer = verb in ("commit-review", "commit-review-supertask", "pull-request-review", "other-review")
    is_plan_reviewer = verb == "commit-plan-review"
    is_supertask_verb = verb in ("commit-make-supertask", "commit-review-supertask")
    is_pull_request_verb = verb in ("pull-request-make", "pull-request-review")
    is_other_verb = verb in ("other-make", "other-review")
    role = "coder" if is_coder else "reviewer"

    description = task["description"] or "(none)"

    # Build filtered task context. Task descriptions are authored as Markdown,
    # so keep the source block intact instead of flattening it into a list item.
    context_lines = [
        "## Task Context",
        f"- id: {task['id']}",
        f"- title: {task['title']}",
        "- description_markdown:",
        "  ```markdown",
        *[f"  {line}" for line in description.splitlines()],
        "  ```",
        f"- branch: {task['branch']}",
        f"- coder_agent: {task.get('coder_agent') or DEFAULT_CODER}",
        f"- reviewer_agent: {_task_reviewer(task)}",
        f"- review_round: {task['review_round']}",
    ]

    if not is_plan_reviewer:
        context_lines.append(f"- last_review_decision: {task['last_review_decision']}")

    if not is_plan_reviewer and not is_supertask_verb and not is_pull_request_verb and not is_other_verb:
        context_lines.append(f"- commit_hash: {task.get('commit_hash') or '(none)'}")

    if is_coder and not is_supertask_verb and not is_pull_request_verb and not is_other_verb:
        context_lines.append(f"- stash_ref: {task.get('stash_ref') or '(none)'}")

    # Show commit_plan only when relevant
    show_plan = False
    if is_plan_reviewer:
        show_plan = True
    elif verb == "commit-plan":
        show_plan = True
    elif verb == "commit-make" and task.get("last_review_decision") != "approve":
        show_plan = True

    if show_plan:
        context_lines.append(f"- commit_plan: {task.get('commit_plan') or '(none)'}")

    # Surface repo-level build policy so agents see it in task context
    try:
        skip_build_policy = repo_policy.read_skip_build_until_approved(_repo_root())
    except Exception:
        skip_build_policy = False
    if skip_build_policy:
        context_lines.append(
            "- skip_build_until_approved: yes "
            "(standalone KANBAN_SKIP_BUILD_UNTIL_APPROVED marker detected in repo AGENTS.md — "
            "see the commit-make build/finalization guidance below)"
        )

    try:
        allow_master_policy = repo_policy.read_allow_tasks_on_master(_repo_root())
    except Exception:
        allow_master_policy = False
    if task.get("branch") in MASTER_BRANCHES and allow_master_policy:
        context_lines.append(
            "- allow_tasks_on_master: yes "
            "(ALLOW_TASKS_ON_MASTER marker detected in repo AGENTS.md — "
            "this repo explicitly opts in to Kanban tasks on master/main)"
        )

    context_lines.extend([
        f"- assigned_agent: {agent_name}",
        f"- role: {role}",
        "",
        "## Prior Comments",
        comments_text or "(none)",
        "",
        "## CLI Commands Available"
    ])

    _t = 'task'
    if verb in ("pull-request-make", "other-make"):
        context_lines.extend([
            f"- {_t} show {task['id']}",
            f"- {_t} show-comments {task['id']}",
            f"- {_t} log {task['id']} \"<message>\"",
            f"- cat <<'EOF' | {_t} comment {task['id']} --message-stdin --comment",
            f"- {_t} list [--status <status>] [--next-step <step>] [--branch <branch>]",
        ])
    elif is_coder:
        context_lines.extend([
            f"- {_t} show {task['id']}",
            f"- {_t} show-comments {task['id']}",
            f"- {_t} log {task['id']} \"<message>\"",
            f"- {_t} set {task['id']} --stash-ref <stash-ref>",
            f"- {_t} set {task['id']} --commit-plan \"<plan text>\"",
            f"- cat <<'EOF' | {_t} comment {task['id']} --message-stdin --comment",
            f"- cat <<'EOF' | {_t} comment {task['id']} --message-stdin --validation",
            f"- cat <<'EOF' | {_t} comment {task['id']} --message-stdin --commit-message",
            f"- cat <<'EOF' | {_t} comment {task['id']} --message-stdin --done-without-commit",
            f"- {_t} get-commit-footer {task['id']}",
            f"- {_t} list [--status <status>] [--next-step <step>] [--branch <branch>]",
        ])
    elif is_reviewer:
        context_lines.extend([
            f"- {_t} show {task['id']}",
            f"- {_t} show-comments {task['id']}",
            f"- cat <<'EOF' | {_t} comment {task['id']} --message-stdin --approval --author {agent_name} --review-round {task['review_round']}",
            f"- cat <<'EOF' | {_t} comment {task['id']} --message-stdin --rejection --author {agent_name} --review-round {task['review_round']}",
        ])
    elif is_plan_reviewer:
        context_lines.extend([
            f"- {_t} show {task['id']}",
            f"- {_t} show-comments {task['id']}",
            f"- cat <<'EOF' | {_t} comment {task['id']} --message-stdin --plan-approval --author {agent_name}",
            f"- cat <<'EOF' | {_t} comment {task['id']} --message-stdin --plan-rejection --author {agent_name}",
        ])

    task_context = "\n".join(context_lines)

    # For reviewers, inject an explicit handoff section between task context and verb prompt
    if verb in ("pull-request-review",):
        reviewer_handoff = _build_pull_request_reviewer_handoff(task, comments)
        return f"{shared_text}\n\n{task_context}\n\n{reviewer_handoff}\n\n{verb_text}"

    if verb in ("other-review",):
        reviewer_handoff = _build_other_reviewer_handoff(task, comments)
        return f"{shared_text}\n\n{task_context}\n\n{reviewer_handoff}\n\n{verb_text}"

    if verb in ("commit-review", "commit-review-supertask"):
        reviewer_handoff = _build_reviewer_handoff(task, comments, skip_build_policy=skip_build_policy)
        return f"{shared_text}\n\n{task_context}\n\n{reviewer_handoff}\n\n{verb_text}"

    return f"{shared_text}\n\n{task_context}\n\n{verb_text}"
