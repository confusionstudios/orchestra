"""Durable queue pause and worker-owned, restartable interruption cleanup.

Controls only close the gate. The worker reconciles after its lifecycle unwinds;
no HTTP/CLI thread ever stashes underneath a running lifecycle.
"""
import json
import fcntl
import os
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import active_agent_processes
import db


COMPLETE = {"complete", "idle"}


def control(conn, action, *, resolution=None):
    """Shared CLI/HTTP service; offline cleanup uses the worker singleton lock."""
    if action == "resume":
        if resolution:
            resolve_manually(conn, resolution)
        return resume(conn)
    if action != "pause":
        raise ValueError("Unknown queue control")
    request_pause(conn)
    path = db.get_lock_path(db.get_connection_db_path(conn), queue_id=db.connection_queue_id(conn))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return db.queue_control(conn)
        try:
            return reconcile(conn)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def resolve_manually(conn, note):
    """Explicit operator attestation after manual recovery; require offline worker."""
    state = db.queue_control(conn)
    if not state["paused"] or not state["journal"].get("error"):
        raise ValueError("Manual resolution requires a paused queue with a reported error")
    path = db.get_lock_path(db.get_connection_db_path(conn), queue_id=db.connection_queue_id(conn))
    with path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Stop this queue's worker before manually resolving uncertain cleanup") from None
        try:
            journal = state["journal"]
            tid = journal.get("task_id")
            active_agent_processes.stop_verified_agents(db.get_connection_db_path(conn), queue_id=db.connection_queue_id(conn))
            import smart_unblock
            if smart_unblock.active_consultation_task_id(db.get_connection_db_path(conn), queue_id=db.connection_queue_id(conn)) is not None:
                raise ValueError("Smart-unblock is still consulting")
            evidence = capture_evidence(conn, tid)
            if not evidence.get("clean"):
                raise ValueError("Manually preserve/recover unfinished files first; checkout must be clean")
            if tid is not None:
                conn.execute("INSERT OR REPLACE INTO queue_attempts VALUES (?, ?, ?)",
                             (db.connection_queue_id(conn), tid, json.dumps(evidence)))
                journal["evidence"] = evidence
                # Retain any verified archive and its link across manual recovery.
                if journal.get("stage") != "archived":
                    journal["stage"] = "processes-stopped"
            journal["manual_resolution"] = note
            conn.execute("DELETE FROM queue_launch_intents WHERE queue_id=?", (db.connection_queue_id(conn),))
            _save(conn, journal)
            result = reconcile(conn)
            if result["journal"].get("stage") not in COMPLETE:
                raise ValueError(describe(result))
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _save(conn, journal):
    conn.execute("UPDATE queue_controls SET journal=? WHERE queue_id=?",
                 (json.dumps(journal), db.connection_queue_id(conn)))
    conn.commit()


def request_pause(conn):
    """Serialize the durable pause with claims and process launches."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        state = db.queue_control(conn)
        if state["paused"]:
            conn.rollback()
            return state
        attempt = conn.execute("SELECT task_id, evidence FROM queue_attempts WHERE queue_id=?",
                               (db.connection_queue_id(conn),)).fetchone()
        running = conn.execute("SELECT id FROM tasks WHERE queue_id=? AND status='running'",
                               (db.connection_queue_id(conn),)).fetchone()
        journal = {"stage": "requested", "id": uuid.uuid4().hex,
                   "task_id": attempt[0] if attempt else (running[0] if running else None),
                   "evidence": json.loads(attempt[1]) if attempt else None}
        selected = db.get_task(conn, journal["task_id"]) if journal["task_id"] is not None else None
        journal["phase"] = selected["next_step"] if selected else None
        conn.execute("INSERT INTO queue_controls VALUES (?, 1, ?) ON CONFLICT(queue_id) "
                     "DO UPDATE SET paused=1, journal=excluded.journal",
                     (db.connection_queue_id(conn), json.dumps(journal)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return db.queue_control(conn)


def resume(conn):
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        state = db.queue_control(conn)
        if state["paused"] and state["journal"].get("stage") not in COMPLETE:
            raise ValueError("Pause cleanup needs attention or is still in progress: "
                             + describe(state) + ". Preserve files and repair the reported issue, then retry Pause.")
        conn.execute("UPDATE queue_controls SET paused=0 WHERE queue_id=?", (db.connection_queue_id(conn),))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return db.queue_control(conn)


def describe(state):
    if not state["paused"]:
        return "Active"
    journal = state["journal"]
    return "Paused — " + journal.get("stage", "requested") + (
        ": " + journal["error"] if journal.get("error") else "")


def _git(root, *args):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def capture_evidence(conn, task_id):
    """A clean pre-attempt baseline is required; legacy/dirty attempts fail closed."""
    root = db.execution_root(conn).resolve()
    try:
        return {"token": uuid.uuid4().hex, "root": str(root),
                "queue_id": db.connection_queue_id(conn),
                "repository_id": db.connection_repository_id(conn),
                "head": _git(root, "rev-parse", "HEAD"),
                "branch": _git(root, "branch", "--show-current"),
                "clean": not _git(root, "status", "--porcelain", "--untracked-files=all"),
                "task_id": task_id}
    except (OSError, subprocess.CalledProcessError):
        return {"task_id": task_id, "clean": False}


def finish_attempt(conn, task_id):
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    if not db.queue_control(conn)["paused"]:
        conn.execute("DELETE FROM queue_attempts WHERE queue_id=? AND task_id=?",
                     (db.connection_queue_id(conn), task_id))
    conn.commit()


@contextmanager
def mutation_gate(conn):
    """Finish ordinary stash/restore acknowledgment before Pause can select it."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        db.check_not_paused(conn)
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


@contextmanager
def launch_gate(conn):
    """Pause cannot slip between the launch check and process registration."""
    if conn is None:
        yield
        return
    # A durable launch intent makes a crash between fork and registration fail
    # closed. Registration is an atomic file write before this intent is cleared.
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    db.check_not_paused(conn)
    row = conn.execute("SELECT evidence FROM queue_attempts WHERE queue_id=?",
                       (db.connection_queue_id(conn),)).fetchone()
    evidence = json.loads(row[0]) if row else None
    launch_id = uuid.uuid4().hex
    conn.execute("INSERT INTO queue_launch_intents VALUES (?, ?)", (launch_id, db.connection_queue_id(conn)))
    if evidence:
        evidence["launch_pending"] = True
        conn.execute("UPDATE queue_attempts SET evidence=? WHERE queue_id=?",
                     (json.dumps(evidence), db.connection_queue_id(conn)))
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        db.check_not_paused(conn)
        yield
        conn.execute("DELETE FROM queue_launch_intents WHERE id=?", (launch_id,))
        if evidence:
            evidence["launch_pending"] = False
            conn.execute("UPDATE queue_attempts SET evidence=? WHERE queue_id=?",
                         (json.dumps(evidence), db.connection_queue_id(conn)))
        conn.commit()
    except active_agent_processes.AgentRegistrationError as exc:
        conn.rollback()
        request_pause(conn)
        journal = db.queue_control(conn)["journal"]
        journal["error"] = str(exc)
        _save(conn, journal)
        raise db.QueuePaused(str(exc)) from exc
    except FileNotFoundError:
        # Popen could not create a child. Registration failures are wrapped
        # above, so this path has no process whose identity could be lost.
        conn.execute("DELETE FROM queue_launch_intents WHERE id=?", (launch_id,))
        if evidence:
            evidence["launch_pending"] = False
            conn.execute("UPDATE queue_attempts SET evidence=? WHERE queue_id=?",
                         (json.dumps(evidence), db.connection_queue_id(conn)))
        conn.commit()
        raise
    except BaseException:
        conn.rollback()
        raise


@contextmanager
def watch_process(conn, proc):
    """Cancel even silent subprocesses while their owning thread drains stdout."""
    if conn is None:
        yield
        return
    stopped = threading.Event()
    path = db.get_connection_db_path(conn)
    qid = db.connection_queue_id(conn)

    def watch():
        control = db.connect(path, queue_id=qid)
        try:
            while not stopped.wait(0.1):
                if db.queue_control(control)["paused"]:
                    try:
                        active_agent_processes.stop_verified_agents(path, queue_id=qid)
                    except Exception as exc:
                        # Serialize read/modify/write with reconciliation so a
                        # late monitor error cannot rewind a completed journal.
                        control.execute("BEGIN IMMEDIATE")
                        journal = db.queue_control(control)["journal"]
                        if journal.get("stage") not in COMPLETE:
                            journal["error"] = f"Process termination uncertain: {exc}"
                        _save(control, journal)
                    return
        finally:
            control.close()

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=10)
    db.check_not_paused(conn)


def _archive(conn, journal, root):
    """Discover a pre-journaled stash after a crash; pin it before resetting."""
    marker = "ko-pause-" + journal["id"]
    ref = "refs/orchestra/paused/" + journal["id"]
    journal["archive_ref"] = ref
    journal["marker"] = marker
    journal["stage"] = "archive-prepared"
    _save(conn, journal)  # must precede any Git mutation
    entries = _git(root, "stash", "list", "--format=%H %gs").splitlines()
    matches = [line.split()[0] for line in entries if marker in line]
    try:
        pinned = _git(root, "rev-parse", "--verify", ref)
    except subprocess.CalledProcessError:
        pinned = None
    oid = pinned or (matches[0] if len(matches) == 1 else None)
    if len(matches) > 1:
        raise ValueError("Multiple pause archives match; inspect " + marker)
    if not oid and _git(root, "status", "--porcelain", "--untracked-files=all"):
        _git(root, "stash", "push", "--include-untracked", "-m", marker)
        entries = _git(root, "stash", "list", "--format=%H %gs").splitlines()
        matches = [line.split()[0] for line in entries if marker in line]
        if len(matches) != 1:
            raise ValueError("Cannot verify a newly created pause stash: " + marker)
        oid = matches[0]
    if oid:
        _git(root, "cat-file", "-e", oid + "^{commit}")
        _git(root, "update-ref", ref, oid)
        journal["archive_oid"] = oid
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Worktree is still dirty after archiving; preserve files and inspect " + ref)
    journal["stage"] = "archived"
    _save(conn, journal)


def reconcile(conn):
    """Called only by the worker, outside advance(), before recovery/dispatch."""
    state = db.queue_control(conn)
    if not state["paused"]:
        return state
    journal = state["journal"]
    if journal.get("stage") in COMPLETE:
        return state
    try:
        path, qid = db.get_connection_db_path(conn), db.connection_queue_id(conn)
        active_agent_processes.stop_verified_agents(path, queue_id=qid)
        if conn.execute("SELECT 1 FROM queue_launch_intents WHERE queue_id=?", (qid,)).fetchone():
            raise ValueError("Unacknowledged agent launch; verify process exit and resolve manually")
        # Also wait for the standalone watcher to leave validation/rollback.
        import smart_unblock
        if smart_unblock.active_consultation_task_id(path, queue_id=qid) is not None:
            raise ValueError("Waiting for smart-unblock consultation to quiesce")
        task_id = journal.get("task_id")
        if task_id is None:
            journal.update(stage="idle", error=None)
            _save(conn, journal)
            return db.queue_control(conn)
        task = db.get_task(conn, task_id)
        if task is None:
            raise ValueError("Interrupted ticket is missing")
        if task["status"] == "done":
            journal.update(stage="complete", error=None, outcome="Terminal transition won the pause race; preserved")
            _save(conn, journal)
            finish_attempt_after_pause(conn, task_id)
            return db.queue_control(conn)
        evidence = journal.get("evidence")
        if not evidence or not evidence.get("clean"):
            raise ValueError("No clean pre-attempt ownership evidence; preserve worktree and restore/resolve manually")
        if evidence.get("launch_pending"):
            raise ValueError("Agent launch was not acknowledged; verify all agent processes exited before manual recovery")
        root = db.execution_root(conn).resolve()
        if (evidence.get("root") != str(root) or evidence.get("queue_id") != qid
                or evidence.get("repository_id") != db.connection_repository_id(conn)
                or not db._checkout_owns_queue(root, qid, db.connection_repository_id(conn))):
            raise ValueError("Checkout ownership changed; preserve files and inspect queue identity")
        attempt = conn.execute("SELECT task_id, evidence FROM queue_attempts WHERE queue_id=?", (qid,)).fetchone()
        if not attempt or attempt[0] != task_id or json.loads(attempt[1]).get("token") != evidence["token"]:
            raise ValueError("Active attempt identity changed; preserve files")
        branch = _git(root, "branch", "--show-current")
        if branch not in {evidence["branch"], task.get("branch")}:
            raise ValueError("Branch changed outside the attempt; preserve files")
        journal["head_after_exit"] = _git(root, "rev-parse", "HEAD")
        if journal.get("stage") not in {"archive-prepared", "archived"}:
            journal["stage"] = "processes-stopped"
            _save(conn, journal)
        if journal["stage"] != "archived":
            if task.get("stash_ref"):
                if _git(root, "status", "--porcelain", "--untracked-files=all"):
                    raise ValueError("Both saved candidate and worktree changes exist; preserve both and resolve manually")
                oid = _git(root, "rev-parse", task["stash_ref"] + "^{commit}")
                ref = "refs/orchestra/paused/" + journal["id"]
                _git(root, "update-ref", ref, oid)
                journal.update(archive_ref=ref, archive_oid=oid, stage="archived")
                _save(conn, journal)
            else:
                _archive(conn, journal, root)
        if _git(root, "status", "--porcelain", "--untracked-files=all"):
            raise ValueError("Files changed after archive verification; preserve them and resolve manually")
        # Reset and archive link are one transaction. Never reset Git HEAD.
        conn.execute("BEGIN IMMEDIATE")
        kind = db.task_type(task)
        start = {"supertask": "commit-make-supertask", "pull_request": "pull-request-make",
                 "other": "other-make"}.get(kind)
        if start is None:
            start = "commit-make" if db.should_skip_step(conn, task_id, "commit-plan") else "commit-plan"
        cutoff = conn.execute("SELECT COALESCE(MAX(id),0) FROM comments WHERE task_id=?", (task_id,)).fetchone()[0]
        conn.execute("INSERT OR REPLACE INTO task_attempt_cutoffs VALUES (?, ?)", (task_id, cutoff))
        conn.execute("""UPDATE tasks SET status='none', next_step=?, review_round=0,
            last_review_decision='none', commit_plan=NULL, stash_ref=NULL, block_reason=NULL,
            resume_next_step=NULL, ready_at=NULL, last_ready_at=NULL, first_started_at=NULL,
            done_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=? AND queue_id=?""", (start, task_id, qid))
        archive = journal.get("archive_ref") if journal.get("archive_oid") else None
        message = ("Emergency pause: interrupted attempt reset to none. "
                   + (f"WIP archived at {archive} ({journal['archive_oid']}). Explicit recovery: git stash apply --index {archive}. "
                      if archive else "No unfinished Git changes to archive. ")
                   + f"HEAD preserved at {journal['head_after_exit']}. Old decisions are retained for audit only.")
        if journal.get("manual_resolution"):
            message += " Operator attested manual preservation and process exit: " + journal["manual_resolution"]
        conn.execute("INSERT INTO comments (task_id, message, kind, author) VALUES (?, ?, 'comment', 'orchestrator')",
                     (task_id, message))
        conn.execute("DELETE FROM queue_attempts WHERE queue_id=?", (qid,))
        journal.update(stage="complete", error=None)
        conn.execute("UPDATE queue_controls SET journal=? WHERE queue_id=?", (json.dumps(journal), qid))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        journal["error"] = str(exc)
        _save(conn, journal)
    return db.queue_control(conn)


def finish_attempt_after_pause(conn, task_id):
    conn.execute("DELETE FROM queue_attempts WHERE queue_id=? AND task_id=?",
                 (db.connection_queue_id(conn), task_id))
    conn.commit()
