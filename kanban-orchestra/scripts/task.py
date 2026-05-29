#!/usr/bin/env python3
"""
task.py — CLI for creating and managing Kanban Orchestra tasks.

Usage:
    task add "<title>" [--type <commit|pull_request|supertask|other>] [--description "<markdown>"] [--branch <branch>] [--coder-agent <agent>] [--reviewer-agent <agent>] [--allow-when-blocked]
    task set <id> [--title ".."] [--status ..] [--next-step ..] [--branch ..]
                  [--description "<markdown>"] [--stash-ref <ref>] [--allow-when-blocked <bool>] ...
    task list [--status ..] [--next-step ..] [--branch ..] [--page N]
    task show <id>
    task show-comments <id>
    task show-run-log <id>
    task log <id> "<message>"
    task comment <id> "<message>" [--comment|--approval|--rejection|--commit-message|--validation] [--author <name>] [--review-round N]
    task comment <id> --message-stdin [--comment|--approval|--rejection|--commit-message|--validation] [--author <name>] [--review-round N]
    task purge [--before <date>|--days <n>]
    task delete <id>
    task dump                       # dump DB to kanban-orchestra.sql
    task restore

Policy:
    Branches master/main are disabled for tasks by default. Use a feature
    branch, or add ALLOW_TASKS_ON_MASTER as a standalone line in AGENTS.md.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import db
import config
import repo_policy


AGENTS = config.AGENTS
VALID_SKIPS = {
    "commit-plan",
    "commit-plan-review",
    "commit-review",
    "commit-review-supertask",
    "pull-request-review",
    "other-review",
}
NORMAL_TASK_SKIPS = {"commit-plan", "commit-plan-review", "commit-review"}
SUPERTASK_SKIPS = {"commit-review-supertask"}
PULL_REQUEST_SKIPS = {"pull-request-review"}
OTHER_SKIPS = {"other-review"}
COMMIT_TYPES = {"commit", "task"}
TASK_TYPES = {"commit", "supertask", "pull_request", "other"}
TASK_TYPE_ALIASES = TASK_TYPES | {"task"}
COMMIT_STEPS = {"commit-make", "commit-review", "commit-plan", "commit-plan-review"}
SUPERTASK_STEPS = {"commit-make-supertask", "commit-review-supertask"}
PULL_REQUEST_STEPS = {"pull-request-make", "pull-request-review"}
OTHER_STEPS = {"other-make", "other-review"}
STEP_TYPES = {
    **{step: "commit" for step in COMMIT_STEPS},
    **{step: "supertask" for step in SUPERTASK_STEPS},
    **{step: "pull_request" for step in PULL_REQUEST_STEPS},
    **{step: "other" for step in OTHER_STEPS},
    "none": "any",
}
BRANCH_NAME_RE = re.compile(r'^[A-Za-z0-9._/][A-Za-z0-9._/ -]*$')
MASTER_BRANCHES = {"master", "main"}
MASTER_TASKS_DISABLED_ERROR = (
    "Error: tasks on master/main are disabled by default. "
    "Use a feature branch, or add ALLOW_TASKS_ON_MASTER as a standalone line "
    "in AGENTS.md to explicitly opt in."
)


def _json_out(obj):
    print(json.dumps(obj, indent=2, default=str))


def _parse_bool_arg(value):
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(
        "expected one of: true, false, yes, no, 1, 0, on, off"
    )


def _is_interactive():
    return sys.stdin.isatty() and os.environ.get("KANBAN_NONINTERACTIVE") != "1"


def _current_branch():
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _repo_root_for_policy():
    return Path(db.get_db_path()).resolve().parent


def _is_master_branch(branch):
    return branch in MASTER_BRANCHES


def _allow_tasks_on_master():
    return repo_policy.read_allow_tasks_on_master(_repo_root_for_policy())


def _validate_branch_name(branch):
    if not BRANCH_NAME_RE.match(branch):
        print(f"Error: invalid branch name '{branch}'.", file=sys.stderr)
        sys.exit(1)


def _reject_master_branch_without_marker(branch):
    if _is_master_branch(branch) and not _allow_tasks_on_master():
        print(MASTER_TASKS_DISABLED_ERROR, file=sys.stderr)
        sys.exit(1)


def _resolve_branch_for_ready(conn, task, branch_arg):
    """Ensure task has a branch before becoming ready. Returns branch or raises."""
    if branch_arg:
        return branch_arg
    if task.get("branch"):
        return task["branch"]
    if _is_interactive():
        current = _current_branch()
        if current and current not in ("master", "main"):
            answer = input(f"Use current branch '{current}'? [Y/n] ").strip().lower()
            if answer in ("", "y", "yes"):
                return current
        name = input("Enter branch name for this task: ").strip()
        if name:
            return name
        print("Error: a branch is required to set status to 'ready'.", file=sys.stderr)
        sys.exit(1)


def _normalize_task_type(kind):
    return "commit" if kind in COMMIT_TYPES else kind


def _resolve_add_task_type(args):
    kind_arg = args.kind
    type_arg = args.task_type
    if kind_arg and type_arg and _normalize_task_type(kind_arg) != _normalize_task_type(type_arg):
        print("Error: --kind and --type specify different task types.", file=sys.stderr)
        sys.exit(1)
    raw = type_arg or kind_arg or "commit"
    normalized = _normalize_task_type(raw)
    if normalized not in TASK_TYPES:
        print(
            f"Error: unknown task type '{raw}'. Valid: {', '.join(sorted(TASK_TYPES))}",
            file=sys.stderr,
        )
        sys.exit(1)
    return normalized


def _skip_set_for_type(task_type):
    if task_type == "commit":
        return NORMAL_TASK_SKIPS
    if task_type == "supertask":
        return SUPERTASK_SKIPS
    if task_type == "pull_request":
        return PULL_REQUEST_SKIPS
    if task_type == "other":
        return OTHER_SKIPS
    return set()


def _display_task_type(task_type):
    return task_type.replace("_", " ")


def _validate_skip_for_type(task_type, skip):
    if skip not in VALID_SKIPS:
        print(f"Error: unknown skip target '{skip}'. Valid: {', '.join(sorted(VALID_SKIPS))}", file=sys.stderr)
        sys.exit(1)
    allowed = _skip_set_for_type(task_type)
    if skip not in allowed:
        print(
            f"Error: skip target '{skip}' is not allowed for {_display_task_type(task_type)} tasks. "
            f"Valid: {', '.join(sorted(allowed)) if allowed else '(none)'}",
            file=sys.stderr,
        )
        sys.exit(1)


def _validate_next_step_for_type(task_type, next_step):
    step_type = STEP_TYPES.get(next_step)
    if step_type is None:
        print(f"Error: unknown next_step '{next_step}'.", file=sys.stderr)
        sys.exit(1)
    if step_type != "any" and step_type != task_type:
        print(
            f"Error: next_step '{next_step}' is not valid for {_display_task_type(task_type)} tasks.",
            file=sys.stderr,
        )
        sys.exit(1)
    else:
        print("Error: task has no branch and none was provided. "
              "Agents must specify --branch or operate on a task that already has one.",
              file=sys.stderr)
        sys.exit(1)


# ── Subcommands ────────────────────────────────────────────────────────

def cmd_add(args, conn):
    agent = args.coder_agent or config.DEFAULT_CODER
    if agent not in AGENTS:
        print(f"Error: coder-agent must be one of {AGENTS}", file=sys.stderr)
        sys.exit(1)
    reviewer_agent = args.reviewer_agent or config.DEFAULT_REVIEWER
    if reviewer_agent not in AGENTS:
        print(f"Error: reviewer-agent must be one of {AGENTS}", file=sys.stderr)
        sys.exit(1)

    kind = _resolve_add_task_type(args)
    parent_task_id = args.parent
    sequence_index = args.sequence_index
    branch = args.branch
    skips = list(args.skip or [])

    # Commit tasks skip planning by default; plan review is inferred from that
    # skip so users and defaults only need to store the root planning skip.
    if kind == "commit":
        if "commit-plan" not in skips:
            skips.insert(0, "commit-plan")

    # Validate skips
    for s in skips:
        _validate_skip_for_type(kind, s)

    if parent_task_id is not None:
        if kind != "commit":
            print(
                f"Error: child tasks must use type='commit'; got type='{kind}'",
                file=sys.stderr,
            )
            sys.exit(1)
        parent = db.get_task(conn, parent_task_id)
        if not parent:
            print(f"Error: parent task {parent_task_id} not found", file=sys.stderr)
            sys.exit(1)
        if parent.get("kind") != "supertask":
            print(
                f"Error: parent task {parent_task_id} is not a supertask",
                file=sys.stderr,
            )
            sys.exit(1)
        if not parent.get("branch"):
            print(
                f"Error: parent supertask {parent_task_id} has no branch",
                file=sys.stderr,
            )
            sys.exit(1)
        if branch is not None:
            print(
                "Error: --branch is not allowed for child tasks (branch is inherited from parent)",
                file=sys.stderr,
            )
            sys.exit(1)
        branch = parent["branch"]

    if branch is not None:
        _validate_branch_name(branch)
        _reject_master_branch_without_marker(branch)

    child_status = "ready" if parent_task_id is not None else None
    task_id = db.add_task(
        conn, args.title, description=args.description,
        branch=branch, coder_agent=agent, reviewer_agent=reviewer_agent,
        kind=kind, parent_task_id=parent_task_id, sequence_index=sequence_index,
        status=child_status, skips=skips,
        allow_when_blocked=args.allow_when_blocked,
    )

    if parent_task_id is not None:
        db.renumber_siblings(conn, parent_task_id)

    _json_out(db.get_task(conn, task_id))


def cmd_set(args, conn):
    task = db.get_task(conn, args.task_id)
    if not task:
        print(f"Error: task {args.task_id} not found", file=sys.stderr)
        sys.exit(1)

    fields = {}
    task_type = _normalize_task_type(task.get("kind", "commit"))
    if args.title is not None:
        fields["title"] = args.title
    if args.description is not None:
        fields["description"] = args.description
    if args.next_step is not None:
        _validate_next_step_for_type(task_type, args.next_step)
        fields["next_step"] = args.next_step
    if args.branch is not None:
        if task.get("parent_task_id") is not None:
            print(
                "Error: --branch cannot be set directly on child tasks",
                file=sys.stderr,
            )
            sys.exit(1)
        _validate_branch_name(args.branch)
        _reject_master_branch_without_marker(args.branch)
        fields["branch"] = args.branch
    if args.commit is not None:
        fields["commit_hash"] = args.commit
    if args.stash_ref is not None:
        fields["stash_ref"] = args.stash_ref or None
    if args.coder_agent is not None:
        if args.coder_agent not in AGENTS:
            print(f"Error: coder-agent must be one of {AGENTS}", file=sys.stderr)
            sys.exit(1)
        fields["coder_agent"] = args.coder_agent
    if args.reviewer_agent is not None:
        if args.reviewer_agent not in AGENTS:
            print(f"Error: reviewer-agent must be one of {AGENTS}", file=sys.stderr)
            sys.exit(1)
        fields["reviewer_agent"] = args.reviewer_agent
    if args.review_round is not None:
        fields["review_round"] = args.review_round
    if args.last_review_decision is not None:
        fields["last_review_decision"] = args.last_review_decision
    if args.sequence_index is not None:
        if task.get("parent_task_id") is None:
            print("Error: --sequence-index can only be set on child tasks", file=sys.stderr)
            sys.exit(1)
        fields["sequence_index"] = args.sequence_index
    if args.commit_plan is not None:
        fields["commit_plan"] = args.commit_plan or None
    if args.allow_when_blocked is not None:
        fields["allow_when_blocked"] = args.allow_when_blocked

    if args.add_skip:
        for s in args.add_skip:
            _validate_skip_for_type(task_type, s)
            db.add_task_skip(conn, args.task_id, s)

    if args.remove_skip:
        for s in args.remove_skip:
            _validate_skip_for_type(task_type, s)
            db.remove_task_skip(conn, args.task_id, s)

    # Handle branch resolution when setting status to ready
    if args.status is not None:
        if args.status == "ready":
            _validate_next_step_for_type(task_type, fields.get("next_step") or task.get("next_step"))
            if task_type != "other":
                branch = _resolve_branch_for_ready(conn, task, fields.get("branch"))
                _validate_branch_name(branch)
                _reject_master_branch_without_marker(branch)
                fields["branch"] = branch
        fields["status"] = args.status

    if not fields and not args.add_skip and not args.remove_skip:
        print("No fields to update.", file=sys.stderr)
        sys.exit(1)

    old_branch = task.get("branch")
    if fields:
        db.update_task(conn, args.task_id, **fields)

    # Propagate branch change to children that still carry the old branch
    if args.branch is not None and task.get("kind") == "supertask" and old_branch:
        children = db.get_child_tasks(conn, args.task_id)
        for child in children:
            if child.get("branch") == old_branch:
                db.update_task(conn, child["id"], branch=args.branch)

    # Reorder siblings when sequence_index changes on a child task
    if args.sequence_index is not None and task.get("parent_task_id") is not None:
        db.renumber_siblings(conn, task["parent_task_id"])

    # When a child is set to blocked, bubble up to parent supertask.
    if args.status == "blocked" and task.get("parent_task_id") is not None:
        parent_id = task["parent_task_id"]
        parent = db.get_task(conn, parent_id)
        if parent and parent["status"] != "blocked":
            db.update_task(conn, parent_id, status="blocked")

    # When a child is set back to ready, restore parent to pending_subtasks
    # if the parent is blocked and no other children are still blocked.
    if args.status == "ready" and task.get("parent_task_id") is not None:
        parent_id = task["parent_task_id"]
        parent = db.get_task(conn, parent_id)
        if parent and parent["status"] == "blocked":
            siblings = db.get_child_tasks(conn, parent_id)
            blocked_siblings = [
                s for s in siblings
                if s["status"] == "blocked" and s["id"] != args.task_id
            ]
            if not blocked_siblings:
                db.update_task(conn, parent_id, status="pending_subtasks")

    _json_out(db.get_task(conn, args.task_id))


def cmd_list(args, conn):
    tasks = db.list_tasks(
        conn, status=args.status, next_step=args.next_step,
        branch=args.branch, page=args.page,
    )
    _json_out(tasks)


def cmd_show(args, conn):
    task = db.get_task(conn, args.task_id)
    if not task:
        print(f"Error: task {args.task_id} not found", file=sys.stderr)
        sys.exit(1)
    _json_out(task)


def cmd_show_comments(args, conn):
    _json_out(db.get_comments(conn, args.task_id))


def cmd_show_run_log(args, conn):
    _json_out(db.get_run_log(conn, args.task_id))


def cmd_log(args, conn):
    db.add_run_log(conn, args.task_id, args.message)
    print("OK")


def cmd_comment(args, conn):
    message = _resolve_message_arg(args, "comment")
    kind = "comment"
    if args.approval:
        kind = "approval"
    elif args.rejection:
        kind = "rejection"
    elif args.commit_message:
        kind = "commit-message"
    elif args.validation:
        kind = "validation"
    elif args.plan_approval:
        kind = "plan-approval"
    elif args.plan_rejection:
        kind = "plan-rejection"
    elif args.done_without_commit:
        kind = "done-without-commit"
    elif args.deferred_build_changed:
        kind = "deferred-build-changed"

    # Approvals and rejections require --review-round and --author
    if kind in ("approval", "rejection"):
        if args.review_round is None:
            print("Error: --review-round is required for approvals and rejections.", file=sys.stderr)
            sys.exit(1)
        if not args.author:
            print("Error: --author is required for approvals and rejections.", file=sys.stderr)
            sys.exit(1)

    # Plan approvals and rejections require --author
    if kind in ("plan-approval", "plan-rejection"):
        if not args.author:
            print("Error: --author is required for plan approvals and rejections.", file=sys.stderr)
            sys.exit(1)

    try:
        db.add_comment(
            conn,
            args.task_id,
            message,
            kind=kind,
            author=args.author,
            review_round=args.review_round,
            expected_review_round=args.review_round if kind in ("approval", "rejection") else None,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    else:
        print("OK")


def cmd_follow_up(args, conn):
    task = db.get_task(conn, args.task_id)
    if not task:
        print(f"Error: task {args.task_id} not found", file=sys.stderr)
        sys.exit(1)
    if task["status"] != "running" or task["next_step"] != "commit-make":
        print(
            f"Error: task {args.task_id} is not currently in commit-make "
            f"(status={task['status']!r}, next_step={task['next_step']!r}); "
            "follow-up can only be declared during an active commit-make run",
            file=sys.stderr,
        )
        sys.exit(1)
    if task.get("follow_up_task_id") is not None:
        print(
            f"Error: task {args.task_id} already has a follow-up task "
            f"(id={task['follow_up_task_id']}); cannot declare a second follow-up",
            file=sys.stderr,
        )
        sys.exit(1)

    title = task["title"]
    m = re.search(r"(\d+)/x$", title)
    if m:
        n = int(m.group(1))
        base_title = title[:m.start()].rstrip()
    else:
        n = 1
        base_title = title
        new_current_title = f"{title} {n}/x"
        db.update_task(conn, args.task_id, title=new_current_title)

    follow_up_title = f"{base_title} {n + 1}/x"

    follow_up_id = db.add_task(
        conn,
        follow_up_title,
        description=args.description,
        branch=task["branch"],
        coder_agent=task["coder_agent"],
        reviewer_agent=task.get("reviewer_agent") or config.DEFAULT_REVIEWER,
        skips=["commit-plan"],
    )

    db.update_task(conn, args.task_id, follow_up_task_id=follow_up_id)

    _json_out(db.get_task(conn, follow_up_id))


def cmd_requeue(args, conn):
    try:
        task = db.reposition_task(
            conn,
            args.task_id,
            before_id=args.before,
            after_id=args.after,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    _json_out(task)


def cmd_purge(args, conn):
    db.purge_run_log(conn, before_date=args.before, days=args.days)
    print("OK")


def cmd_delete(args, conn):
    try:
        db.delete_task(conn, args.task_id)
        print("OK")
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_dump(args, conn):
    """Dump the database to kanban-orchestra.sql in the same directory as the DB."""
    db_path = db.get_db_path()
    sql_path = os.path.join(os.path.dirname(db_path) or ".", "kanban-orchestra.sql")
    with open(sql_path, "w") as f:
        for line in conn.iterdump():
            f.write(line + "\n")
    print(f"Dumped to {sql_path}")


def get_commit_footer(task_id, conn):
    """Return the canonical footer string for a task."""
    task = db.get_task(conn, task_id)
    if not task:
        return None
    agent = task.get("coder_agent") or config.DEFAULT_CODER
    coder_label = config.get_agent_display_label(agent)

    comments = db.get_comments(conn, task_id)
    approval_comments = [c for c in comments if c.get("kind") == "approval"]
    final_approval = approval_comments[-1] if approval_comments else None
    reviewer = final_approval.get("author") if final_approval else None
    reviewer_label = config.get_agent_display_label(reviewer) if reviewer else "pending"
    rejection_count = sum(1 for c in comments if c.get("kind") == "rejection")

    attribution = (
        f"coder: {coder_label}; "
        f"reviewer: {reviewer_label}; "
        f"review rejections: {rejection_count}"
    )
    return f"Task {task_id} ({attribution})"


def normalize_commit_message_footer(message, task_id, conn):
    """Ensure the commit message ends with the canonical footer.

    - Replaces a bare 'Task <id>' trailer with 'Task <id> (<attribution>)'.
    - Leaves the already-canonical 'Task <id> (...)' footer unchanged.
    - Replaces stale/non-canonical rich 'Task <id> (...)' footers.
    - Appends the canonical footer if none is present.
    """
    import re
    footer = get_commit_footer(task_id, conn)
    if footer is None:
        return message
    lines = message.rstrip("\n").split("\n")
    last = lines[-1].strip() if lines else ""
    canonical_pattern = re.compile(rf"^Task {task_id} \(.+\)$")
    bare_pattern = re.compile(rf"^Task {task_id}$")
    if last == footer:
        return message
    if canonical_pattern.match(last) or bare_pattern.match(last):
        lines[-1] = footer
        return "\n".join(lines)
    return message.rstrip("\n") + "\n" + footer


def cmd_get_commit_footer(args, conn):
    """Output the canonical commit footer for a task: Task <id> (<attribution>)."""
    footer = get_commit_footer(args.task_id, conn)
    if footer is None:
        print(f"Error: task {args.task_id} not found", file=sys.stderr)
        sys.exit(1)
    print(footer)


def cmd_restore(args):
    try:
        result = db.restore_dump()
    except (FileNotFoundError, ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Restored to {result['db_path']} from {result['sql_path']}")


def _resolve_message_arg(args, command_name):
    """Resolve either a positional message or stdin-fed message text."""
    if getattr(args, "message_stdin", False):
        if args.message not in (None, "-"):
            print(
                f"Error: {command_name} accepts either a positional message or --message-stdin, not both.",
                file=sys.stderr,
            )
            sys.exit(1)
        message = sys.stdin.read()
        if message.endswith("\r\n"):
            message = message[:-2]
        elif message.endswith("\n"):
            message = message[:-1]
        return message

    if args.message is None:
        print(
            f"Error: {command_name} requires a message argument unless --message-stdin is used.",
            file=sys.stderr,
        )
        sys.exit(1)
    return args.message


# ── Argument parsing ──────────────────────────────────────────────────

def build_parser():
    parser = argparse.ArgumentParser(
        prog="task",
        description="Kanban Orchestra task CLI",
        epilog=(
            "Policy: tasks on master/main are disabled by default. Use a feature "
            "branch, or add ALLOW_TASKS_ON_MASTER as a standalone line in AGENTS.md."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # add
    p_add = sub.add_parser("add")
    p_add.add_argument("title")
    p_add.add_argument(
        "--description",
        default=None,
        help="Task description as Markdown source.",
    )
    p_add.add_argument("--branch", default=None)
    p_add.add_argument("--coder-agent", default=None)
    p_add.add_argument("--reviewer-agent", default=None)
    p_add.add_argument("--type", dest="task_type", choices=sorted(TASK_TYPES), default=None)
    p_add.add_argument(
        "--kind",
        choices=sorted(TASK_TYPE_ALIASES),
        default=None,
        help="Legacy alias for --type; --kind task maps to --type commit.",
    )
    p_add.add_argument("--parent", type=int, default=None)
    p_add.add_argument("--sequence-index", type=int, default=None)
    p_add.add_argument("--skip", action="append", default=None)
    p_add.add_argument("--allow-when-blocked", action="store_true")

    # set
    p_set = sub.add_parser("set")
    p_set.add_argument("task_id", type=int)
    p_set.add_argument("--title", default=None)
    p_set.add_argument("--status", default=None)
    p_set.add_argument("--next-step", default=None)
    p_set.add_argument(
        "--description",
        default=None,
        help="Replace the task description with Markdown source.",
    )
    p_set.add_argument("--branch", default=None)
    p_set.add_argument("--commit", default=None)
    p_set.add_argument("--stash-ref", default=None)
    p_set.add_argument("--coder-agent", default=None)
    p_set.add_argument("--reviewer-agent", default=None)
    p_set.add_argument("--review-round", type=int, default=None)
    p_set.add_argument("--last-review-decision", default=None)
    p_set.add_argument("--sequence-index", type=int, default=None)
    p_set.add_argument("--commit-plan", default=None)
    p_set.add_argument("--allow-when-blocked", type=_parse_bool_arg, default=None)
    p_set.add_argument("--add-skip", action="append", default=None)
    p_set.add_argument("--remove-skip", action="append", default=None)

    # list
    p_list = sub.add_parser("list")
    p_list.add_argument("--status", default=None)
    p_list.add_argument("--next-step", default=None)
    p_list.add_argument("--branch", default=None)
    p_list.add_argument("--page", type=int, default=1)

    # show
    p_show = sub.add_parser("show")
    p_show.add_argument("task_id", type=int)

    # show-comments
    p_sc = sub.add_parser("show-comments")
    p_sc.add_argument("task_id", type=int)

    # show-run-log
    p_sr = sub.add_parser("show-run-log")
    p_sr.add_argument("task_id", type=int)

    # log
    p_log = sub.add_parser("log")
    p_log.add_argument("task_id", type=int)
    p_log.add_argument("message")

    # comment
    p_comment = sub.add_parser("comment")
    p_comment.add_argument("task_id", type=int)
    p_comment.add_argument("message", nargs="?", default=None)
    p_comment.add_argument("--message-stdin", action="store_true", default=False)
    p_comment.add_argument("--comment", action="store_true", default=False)
    p_comment.add_argument("--approval", action="store_true", default=False)
    p_comment.add_argument("--rejection", action="store_true", default=False)
    p_comment.add_argument("--commit-message", action="store_true", default=False)
    p_comment.add_argument("--validation", action="store_true", default=False)
    p_comment.add_argument("--plan-approval", action="store_true", default=False)
    p_comment.add_argument("--plan-rejection", action="store_true", default=False)
    p_comment.add_argument("--done-without-commit", action="store_true", default=False)
    p_comment.add_argument("--deferred-build-changed", action="store_true", default=False)
    p_comment.add_argument("--author", default=None)
    p_comment.add_argument("--review-round", type=int, default=None)

    # purge
    p_purge = sub.add_parser("purge")
    p_purge.add_argument("--before", default=None)
    p_purge.add_argument("--days", type=int, default=None)

    # delete
    p_del = sub.add_parser("delete")
    p_del.add_argument("task_id", type=int)

    # follow-up
    p_follow_up = sub.add_parser("follow-up")
    p_follow_up.add_argument("task_id", type=int)
    p_follow_up.add_argument(
        "--description",
        required=True,
        help="Follow-up task description as Markdown source.",
    )

    # requeue
    p_requeue = sub.add_parser("requeue")
    p_requeue.add_argument("task_id", type=int)
    requeue_pos = p_requeue.add_mutually_exclusive_group(required=True)
    requeue_pos.add_argument("--before", type=int, default=None, metavar="OTHER_ID")
    requeue_pos.add_argument("--after", type=int, default=None, metavar="OTHER_ID")

    # dump
    sub.add_parser("dump")

    # restore
    sub.add_parser("restore")

    # get-commit-footer
    p_gcf = sub.add_parser("get-commit-footer")
    p_gcf.add_argument("task_id", type=int)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "restore":
        cmd_restore(args)
        return

    conn = db.connect()
    try:
        dispatch = {
            "add": cmd_add,
            "set": cmd_set,
            "list": cmd_list,
            "show": cmd_show,
            "show-comments": cmd_show_comments,
            "show-run-log": cmd_show_run_log,
            "log": cmd_log,
            "comment": cmd_comment,
            "purge": cmd_purge,
            "delete": cmd_delete,
            "dump": cmd_dump,
            "requeue": cmd_requeue,
            "follow-up": cmd_follow_up,
            "get-commit-footer": cmd_get_commit_footer,
        }
        dispatch[args.command](args, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
