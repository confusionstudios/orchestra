#!/usr/bin/env python3
"""Focused tests for native smart-unblock recovery."""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
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


class SmartUnblockTestCase(unittest.TestCase):
    def setUp(self):
        self.repo_root = Path(tempfile.mkdtemp(prefix="ko-smart-unblock-"))
        self.db_path = str(self.repo_root / "kanban-orchestra.db")
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.repo_root, ignore_errors=True)

    def _blocked_task(self, title="Blocked task", **fields):
        task_id = db.add_task(self.conn, title, branch="feat-x")
        payload = {
            "status": "blocked",
            "block_reason": "review_cap",
            "resume_next_step": "commit-make",
        }
        payload.update(fields)
        db.update_task(self.conn, task_id, **payload)
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


class TestRepeatedBlockSuppression(SmartUnblockTestCase):
    def _identifying_invoke(self, agent, prompt, tid, db_path=None, **kwargs):
        """Fake `invoke_unblock_agent` that leaves a conforming BLOCKED decision."""
        db.add_comment(
            self.conn,
            tid,
            f"smart-unblock ({agent}): BLOCKED needs a human decision: unattributed worktree changes.",
            author=smart_unblock.WATCHER_AUTHOR,
        )
        return {"agent": agent, "returncode": 0}

    def test_unchanged_block_is_only_sent_once(self):
        self._blocked_task()

        with patch.object(
            smart_unblock, "invoke_unblock_agent", side_effect=self._identifying_invoke
        ) as invoke:
            first = smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")
            second = smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")

        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(first[0]["action"], "explained")
        self.assertEqual(second[0]["action"], "skipped-unchanged")


class TestHumanContinueAndRepeatSuppression(TestRepeatedBlockSuppression):
    def test_parse_human_continue_and_resume(self):
        self.assertEqual(
            smart_unblock.parse_human_continue_decision("CONTINUE reviewer is healthy"),
            {"add_review_rounds": None, "reason": "reviewer is healthy"},
        )
        self.assertEqual(
            smart_unblock.parse_human_continue_decision("RESUME +2 grant more rounds"),
            {"add_review_rounds": 2, "reason": "grant more rounds"},
        )
        self.assertIsNone(smart_unblock.parse_human_continue_decision("please unblock"))

    def test_human_continue_resumes_reviewer_unavailable_without_llm(self):
        task_id = self._blocked_task(
            block_reason=db.BLOCK_REASON_REVIEWER_UNAVAILABLE,
            resume_next_step="commit-review",
        )
        db.add_comment(
            self.conn, task_id, "Blocked: reviewer unavailable", author="orchestrator",
        )
        db.add_comment(
            self.conn, task_id, "CONTINUE reviewer is healthy again", author="operator",
        )

        with patch.object(smart_unblock, "invoke_unblock_agent") as invoke:
            results = smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")

        invoke.assert_not_called()
        self.assertEqual(results[0]["action"], "recovered-human-continue")
        task = db.get_task(self.conn, task_id)
        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["next_step"], "commit-review")
        self.assertIsNone(task["block_reason"])
        self.assertIsNone(task["resume_next_step"])

    def test_human_continue_is_observed_after_unchanged_skip(self):
        task_id = self._blocked_task(
            block_reason=db.BLOCK_REASON_REVIEWER_UNAVAILABLE,
            resume_next_step="commit-review",
        )
        db.add_comment(
            self.conn, task_id, "Blocked: reviewer unavailable", author="orchestrator",
        )

        def identifying_invoke(agent, prompt, tid, db_path=None, **kwargs):
            db.add_comment(
                self.conn,
                tid,
                f"smart-unblock ({agent}): BLOCKED needs a human decision: reviewer down.",
                author=smart_unblock.WATCHER_AUTHOR,
            )
            return {"agent": agent, "returncode": 0}

        with patch.object(smart_unblock, "invoke_unblock_agent", side_effect=identifying_invoke):
            first = smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")
            second = smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")

        self.assertEqual(first[0]["action"], "explained")
        self.assertEqual(second[0]["action"], "skipped-unchanged")

        db.add_comment(
            self.conn, task_id, "CONTINUE retry review now", author="operator",
        )
        with patch.object(smart_unblock, "invoke_unblock_agent") as invoke:
            third = smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")

        invoke.assert_not_called()
        self.assertEqual(third[0]["action"], "recovered-human-continue")
        self.assertEqual(db.get_task(self.conn, task_id)["status"], "ready")
        self.assertEqual(db.get_task(self.conn, task_id)["next_step"], "commit-review")

    def test_changed_evidence_is_reconsidered(self):
        task_id = self._blocked_task()

        with patch.object(
            smart_unblock, "invoke_unblock_agent", side_effect=self._identifying_invoke
        ) as invoke:
            smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")
            db.add_comment(self.conn, task_id, "Operator: use the stashed work", author="operator")
            results = smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")

        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(results[0]["action"], "explained")

    def test_watcher_own_comment_does_not_retrigger_consultation(self):
        task_id = self._blocked_task()

        def commenting_invoke(agent, prompt, tid, db_path=None, **kwargs):
            db.add_comment(
                self.conn,
                tid,
                f"smart-unblock ({agent}): BLOCKED needs a human decision: unattributed worktree changes.",
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
            'printf %s "smart-unblock (sonnet): BLOCKED recovery is unsafe: unattributed worktree changes" '
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

        self.assertEqual(first[0]["action"], "explained")
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
            smart_unblock, "invoke_unblock_agent", side_effect=self._identifying_invoke
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
        self.assertIn(f"comment {task_id} --message-stdin", prompt)
        self.assertIn("RESUME", prompt)
        self.assertIn("BLOCKED", prompt)
        self.assertIn("do not touch task status yourself", prompt)
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


class TestParseDecision(unittest.TestCase):
    def test_resume_without_rounds(self):
        decision = smart_unblock.parse_decision(
            "smart-unblock (sonnet): RESUME the block is stale", "sonnet"
        )
        self.assertEqual(decision, {"action": "resume", "add_review_rounds": None, "reason": "the block is stale"})

    def test_resume_with_rounds(self):
        decision = smart_unblock.parse_decision(
            "smart-unblock (sonnet): RESUME +2 grant more review rounds", "sonnet"
        )
        self.assertEqual(
            decision, {"action": "resume", "add_review_rounds": 2, "reason": "grant more review rounds"}
        )

    def test_blocked(self):
        decision = smart_unblock.parse_decision(
            "smart-unblock (sonnet): BLOCKED needs a human", "sonnet"
        )
        self.assertEqual(decision, {"action": "blocked", "reason": "needs a human"})

    def test_wrong_agent_is_not_recognised(self):
        self.assertIsNone(
            smart_unblock.parse_decision("smart-unblock (codex): RESUME safe", "sonnet")
        )

    def test_missing_verdict_is_not_recognised(self):
        self.assertIsNone(
            smart_unblock.parse_decision("smart-unblock (sonnet): looks fine to me", "sonnet")
        )

    def test_missing_identity_is_not_recognised(self):
        self.assertIsNone(smart_unblock.parse_decision("RESUME safe", "sonnet"))


class TestResumeAndRetryProtocol(SmartUnblockTestCase):
    """The watcher -- never the agent -- performs the resume, strictly after
    reading back an already-durable, verified decision comment. These cover
    the recovery path itself and the retry behaviour for the ways a
    consultation can fail to produce a trustworthy decision."""

    def _resume_invoke(self, message):
        def invoke(agent, prompt, tid, db_path=None, **kwargs):
            db.add_comment(self.conn, tid, message.format(agent=agent), author=smart_unblock.WATCHER_AUTHOR)
            return {"agent": agent, "returncode": 0}
        return invoke

    def test_verified_resume_is_applied_by_the_watcher_itself(self):
        task_id = self._blocked_task()
        with patch.object(
            smart_unblock,
            "invoke_unblock_agent",
            side_effect=self._resume_invoke("smart-unblock ({agent}): RESUME +1 stale review cap"),
        ):
            results = smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")

        self.assertEqual(results[0]["action"], "recovered")
        task = db.get_task(self.conn, task_id)
        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["max_review_rounds"], config.MAX_REVIEW_ROUNDS + 1)
        # The decision comment (already durable before the watcher acted) is
        # necessarily older than the "Operator continued..." comment the
        # watcher's own continuation adds.
        decision = next(c for c in db.get_comments(self.conn, task_id) if c["author"] == smart_unblock.WATCHER_AUTHOR)
        operator = next(c for c in db.get_comments(self.conn, task_id) if c["author"] == "operator")
        self.assertLess(decision["id"], operator["id"])

    def test_resume_that_cannot_be_applied_is_not_fingerprinted(self):
        """RESUME without a rounds grant on a review-cap block is invalid --
        the watcher must not silently trust or fingerprint it."""
        task_id = self._blocked_task()  # review_cap block, needs +N
        with patch.object(
            smart_unblock,
            "invoke_unblock_agent",
            side_effect=self._resume_invoke("smart-unblock ({agent}): RESUME looks stale"),
        ):
            results = smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")

        self.assertEqual(results[0]["action"], "resume-failed")
        task = db.get_task(self.conn, task_id)
        self.assertEqual(task["status"], "blocked")
        self.assertNotIn(str(task_id), smart_unblock.read_state(self.db_path))

    def test_nonzero_exit_is_not_trusted_even_with_a_decision_comment(self):
        """A consultation that exits nonzero is treated as failed, even if it
        left what looks like a valid decision comment on its way out."""
        task_id = self._blocked_task()

        def invoke(agent, prompt, tid, db_path=None, **kwargs):
            db.add_comment(
                self.conn, tid, f"smart-unblock ({agent}): RESUME +1 looks safe", author=smart_unblock.WATCHER_AUTHOR
            )
            return {"agent": agent, "returncode": 1}

        with patch.object(smart_unblock, "invoke_unblock_agent", side_effect=invoke):
            results = smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")

        self.assertEqual(results[0]["action"], "consult-failed")
        task = db.get_task(self.conn, task_id)
        self.assertEqual(task["status"], "blocked")
        self.assertNotIn(str(task_id), smart_unblock.read_state(self.db_path))

    def test_nonzero_exit_is_retried_on_the_next_cycle(self):
        task_id = self._blocked_task()
        calls = []

        def flaky_invoke(agent, prompt, tid, db_path=None, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                return {"agent": agent, "returncode": 1}
            db.add_comment(
                self.conn, tid, f"smart-unblock ({agent}): RESUME +1 safe now", author=smart_unblock.WATCHER_AUTHOR
            )
            return {"agent": agent, "returncode": 0}

        with patch.object(smart_unblock, "invoke_unblock_agent", side_effect=flaky_invoke):
            first = smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")
            second = smart_unblock.poll_once(self.conn, self.db_path, agent="sonnet")

        self.assertEqual(first[0]["action"], "consult-failed")
        self.assertEqual(second[0]["action"], "recovered")
        self.assertEqual(len(calls), 2)
        self.assertEqual(db.get_task(self.conn, task_id)["status"], "ready")


class TestStandaloneWatcherRemoved(unittest.TestCase):
    def test_ko_unblock_wrapper_is_gone(self):
        repo_root = Path(__file__).resolve().parents[2]
        self.assertFalse((repo_root / "bin" / "ko-unblock").exists())

    def test_user_facing_docs_do_not_describe_a_standalone_watcher(self):
        repo_root = Path(__file__).resolve().parents[2]
        for relative in (
            "README.md",
            "AI-skills/narrate.md",
            "AI-skills/kanban.md",
            "tasks/kanban-orchestra-spec.md",
        ):
            text = (repo_root / relative).read_text(encoding="utf-8")
            self.assertNotIn("ko-unblock", text, relative)


if __name__ == "__main__":
    unittest.main()
