#!/usr/bin/env python3
"""Focused tests for the durable smart-unblock watcher."""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
import smart_unblock


@contextmanager
def capture_popen():
    """Patch subprocess.Popen so tests can inspect the real spawned processes."""
    real_popen = subprocess.Popen
    procs = []

    def factory(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        procs.append(proc)
        return proc

    with patch.object(subprocess, "Popen", side_effect=factory) as popen:
        popen.procs = procs
        yield popen


def wait_until(predicate, timeout=10.0, interval=0.05):
    """Poll `predicate` until it is true or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class SmartUnblockTestCase(unittest.TestCase):
    def setUp(self):
        self.repo_root = Path(tempfile.mkdtemp(prefix="ko-unblock-"))
        self.db_path = str(self.repo_root / "kanban-orchestra.db")
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.repo_root, ignore_errors=True)

    def _blocked_task(self, title="Blocked task", **fields):
        task_id = db.add_task(self.conn, title, branch="feat-x")
        db.update_task(
            self.conn,
            task_id,
            status="blocked",
            block_reason="review_cap",
            resume_next_step="commit-make",
            **fields,
        )
        return task_id


class TestProcessLifecycle(SmartUnblockTestCase):
    def test_status_reports_not_running_before_start(self):
        status = smart_unblock.watcher_status(self.db_path)
        self.assertFalse(status["running"])
        self.assertIsNone(status["pid"])

    def test_run_watcher_holds_lock_then_releases_it(self):
        seen = {}

        def fake_poll(conn, db_path=None, *, agent=None, stop_event=None):
            seen["running_during_cycle"] = smart_unblock.watcher_status(db_path)["running"]
            return []

        with patch.object(smart_unblock, "poll_once", side_effect=fake_poll):
            cycles = smart_unblock.run_watcher(
                self.db_path, agent="sonnet", interval=0, max_cycles=1
            )

        self.assertEqual(cycles, 1)
        self.assertTrue(seen["running_during_cycle"])
        self.assertFalse(smart_unblock.watcher_status(self.db_path)["running"])

    def test_stop_watcher_terminates_running_process(self):
        started = smart_unblock.start_watcher(self.db_path, agent="sonnet", interval=3600)
        self.addCleanup(smart_unblock.stop_watcher, self.db_path)
        self.assertTrue(started["started"], started)
        pid = started["pid"]
        self.assertIsNotNone(pid)

        stopped = smart_unblock.stop_watcher(self.db_path)
        self.assertTrue(stopped["stopped"], stopped)
        self.assertFalse(smart_unblock.watcher_status(self.db_path)["running"])

    def test_stop_kills_an_in_flight_consultation_and_its_delayed_actions(self):
        """A real detached watcher inside a long-running consultation: `stop`
        must remove the watcher *and* the agent, so nothing acts on the task
        afterwards."""
        task_id = self._blocked_task()
        scripts_dir = str(Path(__file__).resolve().parent)
        task_cli = Path(scripts_dir) / "task.py"
        started = self.repo_root / "agent-started"
        late = self.repo_root / "agent-late-action"
        agent_script = (
            f'touch "{started}"\n'
            "sleep 2\n"
            f'printf %s "late unblock action" | "{sys.executable}" "{task_cli}" '
            f"comment {task_id} --message-stdin --comment --author smart-unblock\n"
            f'touch "{late}"\n'
            "sleep 30\n"
        )
        agent_cmd = ["/bin/sh", "-c", agent_script, "fake-agent", "{prompt}"]
        runner = self.repo_root / "run_watcher.py"
        runner.write_text(
            textwrap.dedent(
                f"""
                import sys
                sys.path.insert(0, {scripts_dir!r})
                import smart_unblock
                smart_unblock.config.resolve_agent_command = lambda agent: {agent_cmd!r}
                sys.exit(smart_unblock.main(
                    ["--db", {self.db_path!r}, "run", "--agent", "sonnet", "--interval", "3600"]
                ))
                """
            ).strip(),
            encoding="utf-8",
        )

        env = dict(os.environ, KANBAN_DB=self.db_path)
        watcher = subprocess.Popen(
            [sys.executable, str(runner)],
            cwd=str(self.repo_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
        self.addCleanup(smart_unblock.stop_watcher, self.db_path)

        self.assertTrue(
            wait_until(lambda: started.exists()), "fake agent never started"
        )
        self.assertTrue(
            wait_until(lambda: smart_unblock.watcher_status(self.db_path)["consultation_pgid"]),
            "watcher never published its consultation process group",
        )
        pgid = smart_unblock.watcher_status(self.db_path)["consultation_pgid"]

        stopped = smart_unblock.stop_watcher(self.db_path)

        self.assertTrue(stopped["stopped"], stopped)
        self.assertFalse(smart_unblock.watcher_status(self.db_path)["running"])
        self.assertFalse(
            smart_unblock._group_is_alive(pgid), "consultation survived the stop"
        )
        watcher.wait(timeout=10)

        # Well past the point where the agent would have acted on the task.
        time.sleep(2.5)
        self.assertFalse(late.exists(), "stopped agent still performed a delayed action")
        messages = [c["message"] for c in db.get_comments(self.conn, task_id)]
        self.assertNotIn("late unblock action", messages)

    def test_watcher_survives_a_failing_cycle(self):
        calls = []

        def flaky_poll(conn, db_path=None, *, agent=None, stop_event=None):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return []

        with patch.object(smart_unblock, "poll_once", side_effect=flaky_poll):
            cycles = smart_unblock.run_watcher(
                self.db_path, agent="sonnet", interval=0, max_cycles=2
            )

        self.assertEqual(cycles, 2)
        self.assertEqual(len(calls), 2)

    def test_stop_event_ends_the_loop(self):
        stop_event = threading.Event()

        def poll_and_stop(conn, db_path=None, *, agent=None, stop_event=None):
            stop_event.set()
            return []

        with patch.object(smart_unblock, "poll_once", side_effect=poll_and_stop):
            cycles = smart_unblock.run_watcher(
                self.db_path, agent="sonnet", interval=60, stop_event=stop_event
            )

        self.assertEqual(cycles, 1)


class TestDuplicateSuppression(SmartUnblockTestCase):
    def test_second_watcher_cannot_take_the_lock(self):
        handle = smart_unblock.acquire_watcher_lock(self.db_path, agent="sonnet", interval=60)
        try:
            with self.assertRaises(smart_unblock.WatcherAlreadyRunning):
                smart_unblock.acquire_watcher_lock(self.db_path, agent="sonnet", interval=60)
        finally:
            smart_unblock.release_watcher_lock(handle)

    def test_run_watcher_refuses_while_another_holds_the_lock(self):
        handle = smart_unblock.acquire_watcher_lock(self.db_path, agent="sonnet", interval=60)
        try:
            with patch.object(smart_unblock, "poll_once", return_value=[]) as poll:
                with self.assertRaises(smart_unblock.WatcherAlreadyRunning):
                    smart_unblock.run_watcher(self.db_path, agent="sonnet", interval=0, max_cycles=1)
            poll.assert_not_called()
        finally:
            smart_unblock.release_watcher_lock(handle)

    def test_start_watcher_is_a_no_op_when_already_running(self):
        handle = smart_unblock.acquire_watcher_lock(self.db_path, agent="sonnet", interval=60)
        try:
            with patch.object(subprocess, "Popen") as popen:
                result = smart_unblock.start_watcher(self.db_path, agent="sonnet")
            popen.assert_not_called()
            self.assertFalse(result["started"])
            self.assertEqual(result["reason"], "already running")
        finally:
            smart_unblock.release_watcher_lock(handle)


class TestRepeatedBlockSuppression(SmartUnblockTestCase):
    def test_unchanged_block_is_only_sent_once(self):
        self._blocked_task()

        with patch.object(
            smart_unblock, "invoke_unblock_agent", return_value={"agent": "sonnet", "returncode": 0}
        ) as invoke:
            first = smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")
            second = smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")

        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(first[0]["action"], "consulted")
        self.assertEqual(second[0]["action"], "skipped-unchanged")

    def test_changed_evidence_is_reconsidered(self):
        task_id = self._blocked_task()

        with patch.object(
            smart_unblock, "invoke_unblock_agent", return_value={"agent": "sonnet", "returncode": 0}
        ) as invoke:
            smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")
            db.add_comment(self.conn, task_id, "Operator: use the stashed work", author="operator")
            results = smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")

        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(results[0]["action"], "consulted")

    def test_watcher_own_comment_does_not_retrigger_consultation(self):
        task_id = self._blocked_task()

        def commenting_invoke(agent, prompt, tid, db_path=None, **kwargs):
            db.add_comment(
                self.conn,
                tid,
                "Needs a human decision: unattributed worktree changes.",
                author=smart_unblock.WATCHER_AUTHOR,
            )
            return {"agent": agent, "returncode": 0}

        with patch.object(smart_unblock, "invoke_unblock_agent", side_effect=commenting_invoke) as invoke:
            smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")
            results = smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")

        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(results[0]["action"], "skipped-unchanged")
        notes = [
            c for c in db.get_comments(self.conn, task_id)
            if c["author"] == smart_unblock.WATCHER_AUTHOR
        ]
        self.assertEqual(len(notes), 1)

    def test_real_consultation_output_does_not_retrigger_consultation(self):
        """A full cycle — transcript written, comment left via the task CLI — is
        not treated as new evidence by the next poll."""
        task_id = self._blocked_task()
        task_cli = Path(__file__).resolve().parent / "task.py"
        script = (
            'echo "watcher consultation output"\n'
            'printf %s "recovery is unsafe: unattributed worktree changes" '
            f'| "{sys.executable}" "{task_cli}" comment {task_id} '
            "--message-stdin --comment --author smart-unblock\n"
        )
        agent_cmd = ["/bin/sh", "-c", script, "smart-unblock-agent", "{prompt}"]

        with patch.dict(os.environ, {"KANBAN_DB": self.db_path}):
            with patch.object(
                smart_unblock.config, "resolve_agent_command", return_value=agent_cmd
            ):
                first = smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")
                second = smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")

        self.assertEqual(first[0]["action"], "consulted")
        self.assertEqual(first[0]["returncode"], 0)
        self.assertEqual(second[0]["action"], "skipped-unchanged")

        transcripts = sorted(
            p.name for p in (db.get_artifacts_root(self.db_path) / f"task-{task_id}").iterdir()
        )
        self.assertEqual(len(transcripts), 1)
        self.assertIn("smart-unblock", transcripts[0])
        notes = [
            c for c in db.get_comments(self.conn, task_id)
            if c["author"] == smart_unblock.WATCHER_AUTHOR
        ]
        self.assertEqual(len(notes), 1)

    def test_state_is_pruned_when_a_task_is_no_longer_blocked(self):
        task_id = self._blocked_task()

        with patch.object(
            smart_unblock, "invoke_unblock_agent", return_value={"agent": "sonnet", "returncode": 0}
        ):
            smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")
            self.assertIn(str(task_id), smart_unblock.read_state(self.db_path))
            db.update_task(self.conn, task_id, status="ready", block_reason=None, resume_next_step=None)
            smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")

        self.assertNotIn(str(task_id), smart_unblock.read_state(self.db_path))

    def test_ready_tasks_are_never_consulted(self):
        db.add_task(self.conn, "Ready task", branch="feat-x")

        with patch.object(smart_unblock, "invoke_unblock_agent") as invoke:
            results = smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")

        invoke.assert_not_called()
        self.assertEqual(results, [])


class TestEvidenceAndInvocation(SmartUnblockTestCase):
    def test_evidence_includes_block_metadata_and_worktree_facts(self):
        task_id = self._blocked_task()
        db.add_comment(self.conn, task_id, "Reviewer rejected round 5", author="codex")
        db.add_run_log(self.conn, task_id, "blocked at review cap", verb="commit-review")

        evidence = smart_unblock.collect_block_evidence(self.conn, task_id, self.db_path)

        self.assertEqual(evidence["task"]["block_reason"], "review_cap")
        self.assertEqual(evidence["task"]["resume_next_step"], "commit-make")
        self.assertIn("Reviewer rejected round 5", str(evidence["comments"]))
        self.assertIn("blocked at review cap", str(evidence["run_log"]))
        self.assertIn("stash_list", evidence)
        self.assertIn("worktree_status", evidence)
        self.assertNotIn("updated_at", evidence["task"])

    def test_evidence_includes_latest_transcript_tail(self):
        task_id = self._blocked_task()
        transcript = db.get_artifacts_root(self.db_path) / f"task-{task_id}"
        transcript.mkdir(parents=True, exist_ok=True)
        (transcript / "20260101-000000-commit-make-opus.log").write_text(
            "agent output line\n", encoding="utf-8"
        )

        evidence = smart_unblock.collect_block_evidence(self.conn, task_id, self.db_path)

        self.assertIn("agent output line", evidence["transcript"]["tail"])

    def test_watcher_transcripts_are_kept_out_of_the_task_transcript(self):
        task_id = self._blocked_task()
        transcript = db.get_artifacts_root(self.db_path) / f"task-{task_id}"
        transcript.mkdir(parents=True, exist_ok=True)
        (transcript / "20260101-000000-commit-make-opus.log").write_text(
            "coder output line\n", encoding="utf-8"
        )
        (transcript / "20260102-000000-smart-unblock-sonnet.log").write_text(
            "watcher output line\n", encoding="utf-8"
        )

        evidence = smart_unblock.collect_block_evidence(self.conn, task_id, self.db_path)

        self.assertIn("coder output line", evidence["transcript"]["tail"])
        self.assertNotIn("watcher output line", evidence["transcript"]["tail"])
        self.assertIn("watcher output line", evidence["prior_unblock_transcript"]["tail"])

    def test_fingerprint_ignores_watcher_notes_only(self):
        task_id = self._blocked_task()
        evidence = smart_unblock.collect_block_evidence(self.conn, task_id, self.db_path)
        baseline = smart_unblock.evidence_fingerprint(evidence)

        with_note = dict(evidence, prior_unblock_notes=[{"message": "explained"}])
        self.assertEqual(smart_unblock.evidence_fingerprint(with_note), baseline)

        with_transcript = dict(
            evidence,
            prior_unblock_transcript={"path": "/t/20260101-000000-smart-unblock-sonnet.log", "tail": "x"},
        )
        self.assertEqual(smart_unblock.evidence_fingerprint(with_transcript), baseline)

        changed = dict(evidence, worktree_status=" M file.py")
        self.assertNotEqual(smart_unblock.evidence_fingerprint(changed), baseline)

    def test_prompt_carries_task_evidence_and_leaves_the_decision_to_the_llm(self):
        task_id = self._blocked_task()
        evidence = smart_unblock.collect_block_evidence(self.conn, task_id, self.db_path)

        prompt = smart_unblock.build_unblock_prompt(evidence)

        self.assertIn(f"Task {task_id} is blocked", prompt)
        self.assertIn("review_cap", prompt)
        self.assertIn(f"continue {task_id}", prompt)
        self.assertIn("Decide why it is blocked", prompt)

    def test_invoke_runs_the_configured_agent_command_with_the_prompt(self):
        task_id = self._blocked_task()
        agent_cmd = ["/bin/sh", "-c", 'echo "done: $1"', "fake-agent", "{prompt}"]

        with patch.object(smart_unblock.config, "resolve_agent_command", return_value=agent_cmd):
            with capture_popen() as popen:
                outcome = smart_unblock.invoke_unblock_agent(
                    "sonnet", "PROMPT-BODY", task_id, self.db_path
                )

        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[0], "/bin/sh")
        self.assertIn("PROMPT-BODY", cmd)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(outcome["returncode"], 0)
        self.assertTrue(Path(outcome["transcript_path"]).is_file())
        self.assertIn("done: PROMPT-BODY", Path(outcome["transcript_path"]).read_text(encoding="utf-8"))

    def test_invoke_reports_unknown_agent_without_running_anything(self):
        task_id = self._blocked_task()

        with patch.object(smart_unblock.config, "resolve_agent_command", return_value=None):
            with patch.object(subprocess, "Popen") as popen:
                outcome = smart_unblock.invoke_unblock_agent("nope", "PROMPT", task_id, self.db_path)

        popen.assert_not_called()
        self.assertIn("unknown agent", outcome["error"])

    def test_invoke_reports_agent_timeout_and_kills_the_agent(self):
        task_id = self._blocked_task()
        agent_cmd = ["/bin/sh", "-c", "sleep 30", "fake-agent", "{prompt}"]

        with patch.object(smart_unblock.config, "resolve_agent_command", return_value=agent_cmd):
            with capture_popen() as popen:
                outcome = smart_unblock.invoke_unblock_agent(
                    "sonnet", "PROMPT", task_id, self.db_path, timeout=1
                )

        self.assertIn("timed out", outcome["error"])
        self.assertFalse(outcome.get("interrupted"))
        self.assertFalse(smart_unblock._group_is_alive(popen.procs[0].pid))

    def test_invoke_stops_the_agent_when_the_watcher_is_stopping(self):
        task_id = self._blocked_task()
        marker = self.repo_root / "late-action"
        script = f'sleep 1; touch "{marker}"; sleep 30'
        agent_cmd = ["/bin/sh", "-c", script, "fake-agent", "{prompt}"]
        stop_event = threading.Event()
        threading.Timer(0.3, stop_event.set).start()

        with patch.object(smart_unblock.config, "resolve_agent_command", return_value=agent_cmd):
            with capture_popen() as popen:
                outcome = smart_unblock.invoke_unblock_agent(
                    "sonnet", "PROMPT", task_id, self.db_path, stop_event=stop_event
                )

        self.assertTrue(outcome["interrupted"])
        self.assertIn("stopped with the watcher", outcome["error"])
        self.assertFalse(smart_unblock._group_is_alive(popen.procs[0].pid))
        time.sleep(1.5)
        self.assertFalse(marker.exists(), "killed agent still performed a delayed action")

    def test_poll_does_not_record_a_fingerprint_for_an_interrupted_consultation(self):
        task_id = self._blocked_task()
        stop_event = threading.Event()

        def interrupted_invoke(agent, prompt, tid, db_path=None, **kwargs):
            stop_event.set()
            return {"agent": agent, "returncode": None, "error": "stopped", "interrupted": True}

        with patch.object(smart_unblock, "invoke_unblock_agent", side_effect=interrupted_invoke):
            results = smart_unblock.poll_once(
                self.conn, self.db_path, agent="sonnet", stop_event=stop_event
            )

        self.assertEqual(results[0]["action"], "interrupted")
        self.assertNotIn(str(task_id), smart_unblock.read_state(self.db_path))

    def test_consultation_is_recorded_in_the_run_log(self):
        task_id = self._blocked_task()

        with patch.object(
            smart_unblock,
            "invoke_unblock_agent",
            return_value={"agent": "sonnet", "returncode": 0, "transcript_path": "/tmp/t.log"},
        ):
            smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")

        messages = [r["message"] for r in db.get_run_log(self.conn, task_id)]
        self.assertTrue(any("smart-unblock consulted sonnet" in m for m in messages))


class TestCli(SmartUnblockTestCase):
    def test_status_command_prints_json(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = smart_unblock.main(["--db", self.db_path, "status"])

        self.assertEqual(code, 0)
        self.assertIn('"running": false', buffer.getvalue())

    def test_run_command_reports_an_existing_watcher(self):
        handle = smart_unblock.acquire_watcher_lock(self.db_path, agent="sonnet", interval=60)
        buffer = io.StringIO()
        try:
            with redirect_stderr(buffer):
                code = smart_unblock.main(["--db", self.db_path, "run", "--once"])
        finally:
            smart_unblock.release_watcher_lock(handle)

        self.assertEqual(code, 1)
        self.assertIn("already holds", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
