"""Pause safety exercises use disposable repositories and harmless agents only."""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import active_agent_processes as processes
import agent_runner
import db
import queue_control as control


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


@pytest.fixture
def queue(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "feature")
    git(root, "config", "user.email", "pause@example.com")
    git(root, "config", "user.name", "Pause Test")
    (root / "tracked").write_text("baseline\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "Baseline")
    conn = db.connect(str(tmp_path / "state.db"), repo_root=root)
    yield conn, root
    conn.close()


def claim(conn, **kwargs):
    tid = db.add_task(conn, "Interrupted", branch="feature", status="ready", **kwargs)
    evidence = control.capture_evidence(conn, tid)
    assert db.claim_ready_task(conn, tid, attempt_evidence=evidence)
    return tid


def pause(conn):
    control.request_pause(conn)
    return control.reconcile(conn)


def test_offline_idle_pause_persists_and_is_queue_scoped(queue, tmp_path):
    conn, root = queue
    other_root = tmp_path / "other"
    other_root.mkdir()
    git(other_root, "init", "-q")
    other = db.connect(db.get_connection_db_path(conn), repo_root=other_root)
    try:
        tid = db.add_task(conn, "Ready", status="ready", branch="feature")
        other_tid = db.add_task(other, "Other", status="ready", branch="feature")
        assert control.control(conn, "pause")["paused"]
        assert not db.claim_ready_task(conn, tid)
        assert db.claim_ready_task(other, other_tid)
        reopened = db.connect(db.get_connection_db_path(conn), queue_id=db.connection_queue_id(conn))
        try:
            assert db.queue_control(reopened)["paused"]
            assert db.find_ready_task(reopened) is None
            assert control.control(reopened, "resume")["paused"] is False
            assert db.claim_ready_task(reopened, tid)
        finally:
            reopened.close()
    finally:
        other.close()


def test_staged_unstaged_untracked_archive_and_fresh_decisions(queue):
    conn, root = queue
    tid = claim(conn, skips=["commit-plan"])
    db.add_comment(conn, tid, "Old approval", kind="approval")
    db.add_comment(conn, tid, "Old validation", kind="validation")
    (root / "tracked").write_text("staged\n")
    git(root, "add", "tracked")
    (root / "tracked").write_text("unstaged\n")
    (root / "new").write_text("untracked\n")
    head = git(root, "rev-parse", "HEAD")
    state = pause(conn)
    assert state["journal"].get("error") is None
    assert state["journal"]["stage"] == "complete"
    ref = state["journal"]["archive_ref"]
    assert git(root, "rev-parse", ref) == state["journal"]["archive_oid"]
    assert not git(root, "status", "--porcelain")
    assert git(root, "rev-parse", "HEAD") == head
    task = db.get_task(conn, tid)
    assert (task["status"], task["next_step"], task["stash_ref"]) == ("none", "commit-make", None)
    assert len(db.get_comments(conn, tid)) == 3
    assert not any(c["kind"] in {"approval", "validation"} for c in db.get_comments(conn, tid, current_attempt=True))
    assert control.request_pause(conn) == state
    control.resume(conn)
    assert db.get_task(conn, tid)["status"] == "none"
    git(root, "stash", "apply", "--index", ref)
    assert git(root, "show", ":tracked") == "staged"
    assert (root / "tracked").read_text() == "unstaged\n"
    assert (root / "new").read_text() == "untracked\n"


@pytest.mark.parametrize("kind,skips,start", [
    ("commit", [], "commit-plan"), ("commit", ["commit-plan"], "commit-make"),
    ("supertask", [], "commit-make-supertask"), ("other", [], "other-make"),
    ("pull_request", [], "pull-request-make"),
])
def test_start_phase_and_configuration_preserved(queue, kind, skips, start):
    conn, _ = queue
    tid = claim(conn, kind=kind, skips=skips, description="keep", coder_agent="codex", reviewer_agent="opus")
    state = pause(conn)
    assert state["journal"]["stage"] == "complete"
    task = db.get_task(conn, tid)
    assert task["next_step"] == start
    assert task["description"] == "keep"
    assert task["coder_agent"] == "codex"
    assert task["reviewer_agent"] == "opus"
    assert task["skips"] == skips
    assert "archive_oid" not in state["journal"]


@pytest.mark.parametrize("owned", [False, True])
def test_dirty_baseline_and_legacy_attempt_preserve_unrelated_work(queue, owned):
    conn, root = queue
    (root / "unrelated").write_text("human work")
    tid = db.add_task(conn, "Legacy", branch="feature", status="ready")
    db.claim_ready_task(conn, tid, attempt_evidence=control.capture_evidence(conn, tid) if owned else None)
    state = pause(conn)
    assert "ownership evidence" in state["journal"]["error"]
    assert (root / "unrelated").read_text() == "human work"
    assert db.get_task(conn, tid)["status"] == "running"
    assert not git(root, "stash", "list")
    with pytest.raises(ValueError, match="needs attention"):
        control.resume(conn)


def test_pinned_phase_selected_and_frozen(queue):
    conn, _ = queue
    tid = claim(conn)
    db.update_task(conn, tid, status="ready", next_step="commit-review")
    state = control.request_pause(conn)
    assert state["journal"]["task_id"] == tid
    with pytest.raises(db.QueuePaused):
        db.update_task(conn, tid, status="done")
    assert control.reconcile(conn)["journal"]["stage"] == "complete"


def test_commit_race_and_terminal_transition_preserved(queue):
    conn, root = queue
    tid = claim(conn)
    (root / "tracked").write_text("landed\n")
    git(root, "commit", "-qam", "Agent commit")
    commit = git(root, "rev-parse", "HEAD")
    db.update_task(conn, tid, commit_hash=commit)
    state = pause(conn)
    assert state["journal"]["stage"] == "complete"
    assert db.get_task(conn, tid)["commit_hash"] == commit
    assert git(root, "rev-parse", "HEAD") == commit
    control.resume(conn)
    db.update_task(conn, tid, status="ready")
    db.claim_ready_task(conn, tid, attempt_evidence=control.capture_evidence(conn, tid))
    db.update_task(conn, tid, status="done")
    state = pause(conn)
    assert "Terminal" in state["journal"]["outcome"]
    assert db.get_task(conn, tid)["status"] == "done"


@pytest.mark.parametrize("boundary", ["processes-stopped", "archive-prepared", "archived"])
def test_restart_at_each_journal_boundary(queue, boundary):
    conn, root = queue
    tid = claim(conn)
    (root / "tracked").write_text("work\n")
    control.request_pause(conn)
    save = control._save
    crashed = False

    def crash(connection, journal):
        nonlocal crashed
        save(connection, journal)
        if journal["stage"] == boundary and not crashed:
            crashed = True
            raise KeyboardInterrupt("simulated worker exit")

    with patch.object(control, "_save", side_effect=crash), pytest.raises(KeyboardInterrupt):
        control.reconcile(conn)
    reopened = db.connect(db.get_connection_db_path(conn), queue_id=db.connection_queue_id(conn))
    try:
        state = control.reconcile(reopened)
        assert state["journal"]["stage"] == "complete", state
        assert db.get_task(reopened, tid)["status"] == "none"
        assert len(git(root, "stash", "list").splitlines()) == 1
    finally:
        reopened.close()


def test_crash_after_git_stash_before_db_ack_discovers_marker(queue):
    conn, root = queue
    claim(conn)
    (root / "new").write_text("work")
    control.request_pause(conn)
    original = control._git

    def crash(path, *args):
        result = original(path, *args)
        if args[:2] == ("stash", "push"):
            raise KeyboardInterrupt("after Git mutation")
        return result

    with patch.object(control, "_git", side_effect=crash), pytest.raises(KeyboardInterrupt):
        control.reconcile(conn)
    state = control.reconcile(conn)
    assert state["journal"]["stage"] == "complete"
    assert len(git(root, "stash", "list").splitlines()) == 1
    assert git(root, "show", state["journal"]["archive_ref"] + "^3:new") == "work"


def test_no_change_never_links_preexisting_stash(queue):
    conn, root = queue
    (root / "unrelated").write_text("earlier")
    git(root, "stash", "push", "-u", "-m", "earlier")
    old = git(root, "rev-parse", "stash@{0}")
    claim(conn)
    state = pause(conn)
    assert state["journal"]["stage"] == "complete"
    assert "archive_oid" not in state["journal"]
    assert git(root, "rev-parse", "stash@{0}") == old


def test_git_failure_preserves_files_and_retry(queue):
    conn, root = queue
    tid = claim(conn)
    (root / "tracked").write_text("work\n")
    control.request_pause(conn)
    original = control._git

    def fail(path, *args):
        if args[:2] == ("stash", "push"):
            raise PermissionError("injected stash permission failure")
        return original(path, *args)

    with patch.object(control, "_git", side_effect=fail):
        state = control.reconcile(conn)
    assert "permission" in state["journal"]["error"]
    assert (root / "tracked").read_text() == "work\n"
    assert db.get_task(conn, tid)["status"] == "running"
    assert control.reconcile(conn)["journal"]["stage"] == "complete"


def test_unacknowledged_launch_and_identity_change_fail_closed(queue):
    conn, root = queue
    tid = claim(conn)
    with pytest.raises(RuntimeError):
        with control.launch_gate(conn):
            raise RuntimeError("crash before process registration")
    state = pause(conn)
    assert "launch" in state["journal"]["error"]
    assert db.get_task(conn, tid)["status"] == "running"
    assert not git(root, "stash", "list")


def test_real_silent_agent_tree_exits_before_git_mutation(queue, tmp_path):
    conn, root = queue
    tid = claim(conn)
    qid, path = db.connection_queue_id(conn), db.get_connection_db_path(conn)
    ready = tmp_path / "agent-ready"
    child_pid = tmp_path / "child-pid"
    code = ("import pathlib,subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)']); "
            f"pathlib.Path({str(child_pid)!r}).write_text(str(p.pid)); "
            "pathlib.Path('tracked').write_text('staged\\n'); "
            "subprocess.run(['git','add','tracked'],check=True); "
            "pathlib.Path('tracked').write_text('unstaged\\n'); "
            "pathlib.Path('new').write_text('untracked'); "
            f"pathlib.Path({str(ready)!r}).touch(); time.sleep(60)")
    errors = []

    def run():
        worker = db.connect(path, queue_id=qid)
        try:
            agent_runner.run_agent("sonnet", "", tid, worker, "commit-make")
        except db.QueuePaused:
            pass
        except BaseException as exc:
            errors.append(exc)
        finally:
            worker.close()

    with patch.object(agent_runner, "_repo_root_for_subprocess", return_value=str(root)), \
         patch.object(agent_runner, "_resolve_command_template", return_value=[sys.executable, "-c", code]):
        worker = threading.Thread(target=run)
        worker.start()
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        control.request_pause(conn)
        worker.join(timeout=15)
        assert not worker.is_alive()
    assert not errors
    assert processes.process_identity(int(child_pid.read_text())) is None or not processes.group_members(int(child_pid.read_text()))
    original = control._git

    def checked_git(path, *args):
        assert not processes._read_active_agents(db.get_connection_db_path(conn), queue_id=qid)
        return original(path, *args)

    with patch.object(control, "_git", side_effect=checked_git):
        state = control.reconcile(conn)
    assert state["journal"]["stage"] == "complete", state
    git(root, "stash", "apply", "--index", state["journal"]["archive_ref"])
    assert git(root, "show", ":tracked") == "staged"
    assert (root / "tracked").read_text() == "unstaged\n"


def test_foreign_process_metadata_is_preserved(queue):
    conn, _ = queue
    tid = claim(conn)
    path, qid = db.get_connection_db_path(conn), db.connection_queue_id(conn)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        processes.register_active_agent(task_id=tid, verb="test", agent_name="test", pid=proc.pid,
                                        db_path=path, queue_id=qid)
        metadata = processes._path(path, queue_id=qid)
        payload = json.loads(metadata.read_text())
        payload["agents"][0]["process_identity"] = "stale identity"
        metadata.write_text(json.dumps(payload))
        state = pause(conn)
        assert "reused" in state["journal"]["error"]
        assert proc.poll() is None
        assert metadata.exists()
        assert db.get_task(conn, tid)["status"] == "running"
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def test_pause_claim_serialization(queue):
    conn, _ = queue
    tid = db.add_task(conn, "Racing", status="ready", branch="feature")
    path, qid = db.get_connection_db_path(conn), db.connection_queue_id(conn)
    barrier = threading.Barrier(2)
    outcomes = []

    def contender():
        other = db.connect(path, queue_id=qid)
        try:
            barrier.wait()
            outcomes.append(db.claim_ready_task(other, tid))
        finally:
            other.close()

    thread = threading.Thread(target=contender)
    thread.start()
    barrier.wait()
    state = control.request_pause(conn)
    thread.join()
    # Exactly one serialized outcome: pause selected the winner, or claim failed.
    assert bool(outcomes[0]) == (state["journal"]["task_id"] == tid)
    assert db.claim_ready_task(conn, tid) is None


def test_existing_candidate_is_pinned_and_not_auto_restored(queue):
    conn, root = queue
    tid = claim(conn)
    (root / "tracked").write_text("candidate\n")
    git(root, "add", "tracked")
    git(root, "stash", "push", "-m", "existing candidate")
    oid = git(root, "rev-parse", "stash@{0}")
    db.update_task(conn, tid, stash_ref="stash@{0}")
    state = pause(conn)
    assert state["journal"]["archive_oid"] == oid
    assert git(root, "rev-parse", state["journal"]["archive_ref"]) == oid
    assert db.get_task(conn, tid)["stash_ref"] is None


def test_manual_resolution_requires_clean_checkout_and_records_attestation(queue):
    conn, root = queue
    tid = db.add_task(conn, "Legacy", branch="feature", status="ready")
    db.claim_ready_task(conn, tid)
    (root / "unrelated").write_text("save manually")
    assert pause(conn)["journal"]["error"]
    with pytest.raises(ValueError, match="checkout must be clean"):
        control.control(conn, "resume", resolution="Verified all processes exited")
    git(root, "stash", "push", "-u", "-m", "manually saved")
    state = control.control(conn, "resume", resolution="Saved files in manual stash; verified all processes exited")
    assert not state["paused"]
    assert db.get_task(conn, tid)["status"] == "none"
    assert "Operator attested" in db.get_comments(conn, tid)[-1]["message"]


def test_repo_http_controls_protect_origin_and_identity_and_show_offline_pause(queue, monkeypatch):
    from fastapi.testclient import TestClient
    import dashboard
    conn, _ = queue
    path, qid = db.get_connection_db_path(conn), db.connection_queue_id(conn)
    monkeypatch.setattr(dashboard, "_open_conn", lambda: db.connect(path, queue_id=qid))
    with TestClient(dashboard.app) as client:
        assert client.post(f"/queue/{qid}/pause", headers={"origin": "https://foreign.example"}).status_code == 403
        assert client.post("/queue/foreign/pause", headers={"origin": "http://testserver"}).status_code == 404
        assert client.post(f"/queue/{qid}/pause", headers={"origin": "http://testserver"}, follow_redirects=False).status_code == 303
        html = dashboard.render_health_card(None, conn)
        assert "Paused" in html and "Resume" in html and "no runtime row" in html
        assert client.post(f"/queue/{qid}/resume", headers={"origin": "http://testserver"}, follow_redirects=False).status_code == 303


def test_update_and_fleet_card_show_pause_independent_of_liveness(queue):
    import dashboard
    import fleet_dashboard
    import get_kanban_update
    conn, _ = queue
    pause(conn)
    update = get_kanban_update.build_update(conn)
    assert "QUEUE: Paused" in update
    assert "no runtime row" in update
    card = fleet_dashboard.FleetCard(name="Test", path="test", branch="feature", status="paused", invalid=False,
        current_task=None, ready_count=1, recently_done_count=0, icebox_count=1,
        dashboard_url=None, tailscale_url=None, last_start_failure=None,
        queue_id=db.connection_queue_id(conn), pause_detail="Paused — complete", worker_status="stopped")
    html = fleet_dashboard.render_card(card)
    assert "Resume" in html and "Worker: stopped" in html and "Paused" in html
    assert dashboard.STATUS_BADGE_CLASS["paused"]


def test_standalone_unblock_cancels_and_does_not_apply_decision(queue, tmp_path):
    import smart_unblock
    conn, root = queue
    tid = db.add_task(conn, "Blocked", branch="feature")
    db.update_task(conn, tid, status="blocked")
    path, qid = db.get_connection_db_path(conn), db.connection_queue_id(conn)
    ready = tmp_path / "consult-ready"
    command = [sys.executable, "-c", f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); time.sleep(60)"]
    results = []

    def consult():
        results.append(smart_unblock.invoke_unblock_agent("sonnet", "", tid, path, queue_id=qid))

    with patch.object(smart_unblock.config, "resolve_agent_command", return_value=command):
        thread = threading.Thread(target=consult)
        thread.start()
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        control.request_pause(conn)
        thread.join(timeout=12)
        assert not thread.is_alive()
    assert results[0].get("error")
    assert smart_unblock.poll_once(conn, path, agent="sonnet") == []
    assert db.get_task(conn, tid)["status"] == "blocked"
    assert control.reconcile(conn)["journal"]["stage"] == "idle"


def test_live_worker_restart_paused_and_resume_without_restart(queue, tmp_path, monkeypatch):
    import orchestrator
    conn, root = queue
    path, qid = db.get_connection_db_path(conn), db.connection_queue_id(conn)
    interrupted = claim(conn, skips=["commit-plan"])
    (root / "tracked").write_text("interrupted work\n")
    control.request_pause(conn)
    waiting = db.add_task(conn, "Next ticket", branch="feature", status="ready", skips=["commit-plan"])
    picked = []
    dashboard_polls = []
    errors = []
    monkeypatch.setattr(orchestrator, "POLL_INTERVAL", 0.02)
    monkeypatch.setattr(orchestrator, "HEARTBEAT_INTERVAL", 0.02)
    monkeypatch.setattr(orchestrator, "check_dashboard_process", lambda *a: dashboard_polls.append(1))
    monkeypatch.setattr(orchestrator, "handle_dashboard_start_request", lambda *a: None)
    monkeypatch.setattr(orchestrator, "start_smart_unblock", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "stop_smart_unblock", lambda: None)

    def advance(task, worker):
        picked.append(task["id"])
        db.update_task(worker, task["id"], status="done", next_step="none")
        (root / orchestrator.STOP_AFTER_TASK_FILE).touch()
        return True

    monkeypatch.setattr(orchestrator, "advance", advance)

    def run():
        worker = db.connect(path, queue_id=qid)
        try:
            orchestrator.main_loop(worker, db_path=path)
        except BaseException as exc:
            errors.append(exc)
        finally:
            orchestrator.stop_heartbeat()
            worker.close()

    thread = threading.Thread(target=run)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while db.queue_control(conn)["journal"]["stage"] != "complete" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert db.queue_control(conn)["journal"]["stage"] == "complete"
        heartbeat = db.get_runtime(conn)["last_heartbeat_at"]
        time.sleep(0.08)
        assert thread.is_alive() and heartbeat and dashboard_polls
        assert picked == []
        assert db.get_task(conn, interrupted)["status"] == "none"
        control.resume(conn)
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert picked == [waiting]
        assert db.get_task(conn, interrupted)["status"] == "none"
        assert not errors
    finally:
        if thread.is_alive():
            (root / orchestrator.STOP_AFTER_TASK_FILE).touch()
            thread.join(timeout=10)


def test_descendants_after_leader_exit_still_terminated(queue, tmp_path):
    conn, _ = queue
    tid = claim(conn)
    path, qid = db.get_connection_db_path(conn), db.connection_queue_id(conn)
    ready, release = tmp_path / "ready-tree", tmp_path / "release-tree"
    code = ("import subprocess,sys,time; from pathlib import Path; "
            "subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)']); "
            f"Path({str(ready)!r}).touch();\n"
            f"while not Path({str(release)!r}).exists(): time.sleep(.01)\n")
    proc = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    try:
        processes.register_active_agent(task_id=tid, verb="test", agent_name="test", pid=proc.pid, db_path=path, queue_id=qid)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        release.touch()
        proc.wait(timeout=5)
        processes.clear_active_agent(pid=proc.pid, db_path=path, queue_id=qid)
        assert processes._read_active_agents(path, queue_id=qid)
        assert pause(conn)["journal"]["stage"] == "complete"
        assert not processes.group_members(proc.pid)
    finally:
        try:
            if processes.group_members(proc.pid):
                os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


@pytest.mark.parametrize("payload", ["broken-json", '{"agents": {}}', '{"agents": [{"pid": 123}]}'])
def test_malformed_process_metadata_fails_closed(queue, payload):
    conn, root = queue
    tid = claim(conn)
    path = processes._path(db.get_connection_db_path(conn), queue_id=db.connection_queue_id(conn))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload)
    (root / "tracked").write_text("work\n")
    state = pause(conn)
    assert state["journal"]["error"]
    assert (root / "tracked").read_text() == "work\n"
    assert db.get_task(conn, tid)["status"] == "running"
    assert path.exists()


def test_fleet_http_exact_queue_and_origin(queue, monkeypatch):
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    import fleet_dashboard
    conn, root = queue
    qid, path = db.connection_queue_id(conn), db.get_connection_db_path(conn)
    repo = SimpleNamespace(root=root, error=None)
    monkeypatch.setattr(fleet_dashboard, "_configured_repo", lambda label: repo if label == "test" else None)
    monkeypatch.setattr(fleet_dashboard.fleet, "_registered_queue", lambda root: {"id": qid})
    original_connect = fleet_dashboard.db.connect
    monkeypatch.setattr(fleet_dashboard.db, "connect", lambda *a, **kw: original_connect(path, **kw))
    monkeypatch.setattr(fleet_dashboard, "collect_card", lambda repo: fleet_dashboard.FleetCard(
        name="test", path="test", branch="feature", status="paused", invalid=False, current_task=None,
        ready_count=0, recently_done_count=0, icebox_count=0, dashboard_url=None, tailscale_url=None,
        last_start_failure=None, queue_id=qid))
    with TestClient(fleet_dashboard.app) as client:
        assert client.post(f"/repos/test/pause?queue_id={qid}", headers={"origin": "https://foreign.example"}).status_code == 403
        assert client.post("/repos/test/pause?queue_id=foreign", headers={"origin": "http://testserver"}).status_code == 409
        response = client.post(f"/repos/test/pause?queue_id={qid}", headers={"origin": "http://testserver"})
        assert response.status_code == 200
        assert db.queue_control(conn)["paused"]


def test_registration_failure_closes_gate_and_preserves_checkout(queue):
    conn, root = queue
    tid = claim(conn)
    (root / "tracked").write_text("owned work\n")
    proc = None
    try:
        with patch.object(processes, "_write_active_agents", side_effect=PermissionError("metadata denied")):
            with pytest.raises(db.QueuePaused), control.launch_gate(conn):
                proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
                processes.register_active_agent(task_id=tid, verb="test", agent_name="test", pid=proc.pid,
                    db_path=db.get_connection_db_path(conn), queue_id=db.connection_queue_id(conn))
        assert db.queue_control(conn)["paused"]
        assert "registration" in db.queue_control(conn)["journal"]["error"]
        state = control.reconcile(conn)
        assert "launch" in state["journal"]["error"]
        assert db.get_task(conn, tid)["status"] == "running"
        assert (root / "tracked").read_text() == "owned work\n"
        assert not processes.group_members(proc.pid)
    finally:
        if proc:
            try:
                if processes.group_members(proc.pid):
                    os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()


def test_termination_denied_preserves_process_metadata_files_and_ticket(queue):
    conn, root = queue
    tid = claim(conn)
    path, qid = db.get_connection_db_path(conn), db.connection_queue_id(conn)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        processes.register_active_agent(task_id=tid, verb="test", agent_name="test", pid=proc.pid, db_path=path, queue_id=qid)
        (root / "tracked").write_text("work\n")
        with patch.object(processes.os, "killpg", side_effect=PermissionError("termination denied")):
            state = pause(conn)
        assert "termination denied" in state["journal"]["error"]
        assert proc.poll() is None
        assert processes._path(path, queue_id=qid).exists()
        assert (root / "tracked").read_text() == "work\n"
        assert db.get_task(conn, tid)["status"] == "running"
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def test_reset_transaction_failure_retries_without_losing_archive(queue):
    conn, root = queue
    tid = claim(conn)
    db.add_comment(conn, tid, "old decision", kind="approval")
    (root / "tracked").write_text("work\n")
    conn.execute("""CREATE TRIGGER injected_reset_failure BEFORE UPDATE OF status ON tasks
        WHEN NEW.status = 'none' BEGIN SELECT RAISE(ABORT, 'injected reset failure'); END""")
    conn.commit()
    state = pause(conn)
    assert state["journal"]["stage"] == "archived"
    assert "injected reset" in state["journal"]["error"]
    assert db.get_task(conn, tid)["status"] == "running"
    assert len(db.get_comments(conn, tid, current_attempt=True)) == 1
    assert git(root, "rev-parse", state["journal"]["archive_ref"]) == state["journal"]["archive_oid"]
    conn.execute("DROP TRIGGER injected_reset_failure")
    conn.commit()
    assert control.reconcile(conn)["journal"]["stage"] == "complete"
    assert len(db.get_comments(conn, tid, current_attempt=True)) == 1
    assert db.get_comments(conn, tid)[0]["archived_attempt"]


def test_new_files_after_archiving_are_preserved_without_reset(queue):
    conn, root = queue
    tid = claim(conn)
    (root / "tracked").write_text("owned work\n")
    control.request_pause(conn)
    save = control._save

    def stop_after_archive(connection, journal):
        save(connection, journal)
        if journal["stage"] == "archived":
            raise KeyboardInterrupt("worker stopped after archiving")

    with patch.object(control, "_save", side_effect=stop_after_archive), pytest.raises(KeyboardInterrupt):
        control.reconcile(conn)
    (root / "human-work").write_text("unrelated new work")
    state = control.reconcile(conn)
    assert "changed after archive" in state["journal"]["error"]
    assert (root / "human-work").read_text() == "unrelated new work"
    assert db.get_task(conn, tid)["status"] == "running"
    assert len(git(root, "stash", "list").splitlines()) == 1


def test_public_worker_startup_accepts_pause_without_dispatch(queue, monkeypatch):
    import orchestrator
    conn, _ = queue
    path, qid = db.get_connection_db_path(conn), db.connection_queue_id(conn)
    pause(conn)
    seen = []
    monkeypatch.setattr(orchestrator.db, "launch_connection", lambda: db.connect(path, queue_id=qid))
    monkeypatch.setattr(orchestrator, "main_loop", lambda worker, **kw: seen.append(db.queue_control(worker)["paused"]))
    monkeypatch.setattr(orchestrator.os, "setpgrp", lambda: None)
    monkeypatch.setattr(orchestrator.signal, "signal", lambda *a: None)
    monkeypatch.setattr(orchestrator, "stop_dashboard", lambda: None)
    monkeypatch.setattr(orchestrator, "stop_smart_unblock", lambda: None)
    assert orchestrator.main(["--no-dashboard"]) in (None, 0)
    assert seen == [True]
    assert db.queue_control(conn)["paused"]
