#!/usr/bin/env python3
"""Behavioral tests for automatic post-commit pushes."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import automatic_push


COMMIT = "a" * 40
REMOTE_SHA = "b" * 40
BRANCH = "develop"
FORBIDDEN_PUSH_ARGS = {
    "--force",
    "-f",
    "--force-with-lease",
    "--all",
    "--mirror",
    "--tags",
    "--follow-tags",
    "-u",
    "--set-upstream",
}


class DestinationState:
    def __init__(self):
        self.branch = BRANCH
        self.commit = COMMIT
        self.head_sha = COMMIT
        self.remote_sha = REMOTE_SHA
        self.head_ref = f"refs/heads/{BRANCH}"
        self.symbolic_code = 0
        self.branch_missing = False
        self.upstream_remote = "origin"
        self.upstream_merge = f"refs/heads/{BRANCH}"
        self.configs = {
            f"branch.{BRANCH}.remote": ["origin"],
            f"branch.{BRANCH}.merge": [f"refs/heads/{BRANCH}"],
            f"branch.{BRANCH}.pushRemote": [],
            "remote.pushDefault": [],
            "remote.origin.url": ["https://example.com/repo.git"],
            "remote.origin.pushurl": [],
            "remote.origin.push": [],
            "push.default": ["simple"],
        }
        self.mirror = None
        self.ls_remote_code = 0
        self.ls_remote_stdout = None
        self.ls_remote_stderr = ""
        self.have_remote_object = True
        self.fetch_satisfied = False
        self.fetch_code = 0
        self.fetch_stderr = ""
        self.fetch_provides_object = True
        self.ancestor_code = 0
        self.push_code = 0
        self.push_stdout = None
        self.push_stderr = ""
        self.timeout_on = None
        self.calls = []
        self.push_url_override = None
        self.get_url_code = 0
        self.get_url_stderr = ""

    def run(self, repo, args, timeout):
        self.calls.append(list(args))
        if self.timeout_on and args[0] == self.timeout_on:
            raise subprocess.TimeoutExpired(args, timeout)
        if args[:2] == ["symbolic-ref", "--quiet"]:
            stdout = f"{self.head_ref}\n" if self.symbolic_code == 0 else ""
            return automatic_push.GitOutput(self.symbolic_code, stdout)
        if args[0] == "for-each-ref":
            if self.branch_missing:
                return automatic_push.GitOutput(0, "")
            if "remotename" in args[1]:
                return automatic_push.GitOutput(0, f"{self.upstream_remote}\n")
            if "remoteref" in args[1]:
                return automatic_push.GitOutput(0, f"{self.upstream_merge}\n")
        if args[:2] == ["rev-parse", "--verify"]:
            return automatic_push.GitOutput(0, f"{self.head_sha}\n")
        if args[:4] == ["remote", "get-url", "--push", "--all"]:
            if self.get_url_code != 0:
                return automatic_push.GitOutput(self.get_url_code, "", self.get_url_stderr)
            if self.push_url_override is not None:
                urls = self.push_url_override
            else:
                remote = self.upstream_remote or "origin"
                urls = (
                    self.configs.get(f"remote.{remote}.pushurl")
                    or self.configs.get(f"remote.{remote}.url")
                    or []
                )
            if not urls:
                return automatic_push.GitOutput(2, "", "No such remote")
            return automatic_push.GitOutput(0, "".join(f"{url}\n" for url in urls))
        if args[:2] == ["config", "--get-all"]:
            values = self.configs.get(args[2], [])
            if not values:
                return automatic_push.GitOutput(1, "")
            return automatic_push.GitOutput(0, "".join(f"{value}\n" for value in values))
        if args[:3] == ["config", "--get", "--bool"]:
            if self.mirror is None:
                return automatic_push.GitOutput(1, "")
            return automatic_push.GitOutput(0, "true\n" if self.mirror else "false\n")
        if args[0] == "ls-remote":
            stdout = self.ls_remote_stdout
            if stdout is None:
                stdout = f"{self.remote_sha}\trefs/heads/{self.branch}\n"
            return automatic_push.GitOutput(self.ls_remote_code, stdout, self.ls_remote_stderr)
        if args[0] == "cat-file":
            exists = self.have_remote_object or self.fetch_satisfied
            return automatic_push.GitOutput(0 if exists else 1)
        if args[0] == "fetch":
            if self.fetch_code == 0 and self.fetch_provides_object:
                self.fetch_satisfied = True
            return automatic_push.GitOutput(self.fetch_code, "", self.fetch_stderr)
        if args[0] == "merge-base":
            return automatic_push.GitOutput(self.ancestor_code)
        if args[0] == "push":
            stdout = self.push_stdout
            if stdout is None:
                refspec = f"refs/heads/{self.branch}:refs/heads/{self.branch}"
                stdout = f" \t{refspec}\tok\n"
            return automatic_push.GitOutput(self.push_code, stdout, self.push_stderr)
        return automatic_push.GitOutput(1, "", f"unexpected {' '.join(args)}")


class TestAutomaticPushDecisions(unittest.TestCase):
    def setUp(self):
        self._suppress = os.environ.pop(automatic_push.SUPPRESS_ENV, None)
        self.repo = Path(self.id().split(".")[-1])

    def tearDown(self):
        if self._suppress is None:
            os.environ.pop(automatic_push.SUPPRESS_ENV, None)
        else:
            os.environ[automatic_push.SUPPRESS_ENV] = self._suppress

    def _attempt(self, state, repo=None):
        root = repo or Path("/tmp/automatic-push-test")
        return automatic_push.attempt_automatic_push(
            root,
            state.branch,
            state.commit,
            run_git=state.run,
        )

    def _commands(self, state, name):
        return [call for call in state.calls if call[0] == name]

    def test_eligible_push_uses_one_existing_branch_refspec(self):
        state = DestinationState()
        state.configs["remote.origin.pushurl"] = ["ssh://example.com/repo.git"]
        result = self._attempt(state)

        self.assertEqual(result.status, "pushed")
        self.assertEqual(result.remote, "origin")
        self.assertEqual(result.remote_branch, BRANCH)
        self.assertEqual(
            self._commands(state, "push"),
            [[
                "push",
                "--no-follow-tags",
                "--recurse-submodules=no",
                "--porcelain",
                "origin",
                f"refs/heads/{BRANCH}:refs/heads/{BRANCH}",
            ]],
        )
        self.assertEqual(
            self._commands(state, "ls-remote"),
            [["ls-remote", "--heads", "ssh://example.com/repo.git", f"refs/heads/{BRANCH}"]],
        )
        self.assertEqual(
            self._commands(state, "remote"),
            [["remote", "get-url", "--push", "--all", "origin"]],
        )
        for call in self._commands(state, "push"):
            self.assertFalse(set(call) & FORBIDDEN_PUSH_ARGS)
            self.assertFalse(any("example.com" in arg or "ssh://" in arg for arg in call))
            self.assertNotIn("ssh://example.com/repo.git", call)

    def test_exact_configured_refspec_still_pushes(self):
        state = DestinationState()
        state.configs["remote.origin.push"] = [f"refs/heads/{BRANCH}:refs/heads/{BRANCH}"]
        self.assertEqual(self._attempt(state).status, "pushed")

    def test_missing_remote_object_fetches_that_ref_before_push(self):
        state = DestinationState()
        state.have_remote_object = False
        result = self._attempt(state)

        self.assertEqual(result.status, "pushed")
        self.assertEqual(
            self._commands(state, "fetch"),
            [[
                "fetch",
                "--no-tags",
                "--recurse-submodules=no",
                "https://example.com/repo.git",
                f"refs/heads/{BRANCH}",
            ]],
        )

    def test_opt_out_skips_without_git(self):
        repo = Path(self._tmp())
        (repo / "AGENTS.md").write_text("NO_AUTOMATIC_GIT_PUSH\n", encoding="utf-8")
        state = DestinationState()
        result = self._attempt(state, repo)
        self.assertEqual(result.status, "skipped")
        self.assertIn("opts out", result.reason)
        self.assertEqual(state.calls, [])

    def test_prose_marker_does_not_opt_out(self):
        repo = Path(self._tmp())
        (repo / "AGENTS.md").write_text(
            "Document NO_AUTOMATIC_GIT_PUSH as a standalone line to opt out.\n",
            encoding="utf-8",
        )
        state = DestinationState()
        self.assertEqual(self._attempt(state, repo).status, "pushed")

    def test_suppressed_environment_does_not_inspect_git(self):
        os.environ[automatic_push.SUPPRESS_ENV] = "1"
        state = DestinationState()
        result = self._attempt(state)
        self.assertEqual(result.status, "suppressed")
        self.assertEqual(state.calls, [])

    def test_detached_checkout_skips(self):
        state = DestinationState()
        state.symbolic_code = 1
        result = self._attempt(state)
        self.assertEqual(result.status, "skipped")
        self.assertIn("detached", result.reason)
        self.assertEqual(self._commands(state, "push"), [])

    def test_missing_upstream_skips(self):
        state = DestinationState()
        state.upstream_remote = ""
        state.upstream_merge = ""
        result = self._attempt(state)
        self.assertEqual(result.status, "skipped")
        self.assertIn("no upstream", result.reason)
        self.assertEqual(self._commands(state, "push"), [])

    def test_local_only_upstream_skips(self):
        state = DestinationState()
        state.upstream_remote = "."
        result = self._attempt(state)
        self.assertEqual(result.status, "skipped")
        self.assertIn("local-only", result.reason)

    def test_multiple_fetch_urls_skip(self):
        state = DestinationState()
        state.configs["remote.origin.url"] = [
            "https://example.com/a.git",
            "https://example.com/b.git",
        ]
        result = self._attempt(state)
        self.assertEqual(result.status, "skipped")
        self.assertIn("fetch URL", result.reason)
        self.assertEqual(self._commands(state, "push"), [])

    def test_resolved_push_url_is_queried_before_push(self):
        state = DestinationState()
        state.push_url_override = ["ssh://push.example/repo.git"]
        result = self._attempt(state)

        self.assertEqual(result.status, "pushed")
        self.assertEqual(
            self._commands(state, "ls-remote"),
            [["ls-remote", "--heads", "ssh://push.example/repo.git", f"refs/heads/{BRANCH}"]],
        )
        self.assertNotIn("origin", self._commands(state, "ls-remote")[0])
        push = self._commands(state, "push")
        self.assertEqual(push[0][4], "origin")
        self.assertNotIn("ssh://push.example/repo.git", push[0])

    def test_unresolved_push_destination_does_not_push(self):
        state = DestinationState()
        state.get_url_code = 2
        state.get_url_stderr = "fatal: No such remote 'origin'"
        result = self._attempt(state)
        self.assertEqual(result.status, "failed")
        self.assertEqual(self._commands(state, "ls-remote"), [])
        self.assertEqual(self._commands(state, "push"), [])

    def test_multiple_resolved_push_urls_skip(self):
        state = DestinationState()
        state.push_url_override = [
            "ssh://push.example/a.git",
            "ssh://push.example/b.git",
        ]
        result = self._attempt(state)
        self.assertEqual(result.status, "skipped")
        self.assertIn("ambiguous", result.reason)
        self.assertEqual(self._commands(state, "push"), [])
        self.assertEqual(self._commands(state, "ls-remote"), [])

    def test_multiple_push_urls_skip(self):
        state = DestinationState()
        state.configs["remote.origin.pushurl"] = [
            "ssh://example.com/a.git",
            "ssh://example.com/b.git",
        ]
        result = self._attempt(state)
        self.assertEqual(result.status, "skipped")
        self.assertIn("push URL", result.reason)

    def test_unexpected_push_remote_skips(self):
        state = DestinationState()
        state.configs[f"branch.{BRANCH}.pushRemote"] = ["fork"]
        result = self._attempt(state)
        self.assertEqual(result.status, "skipped")
        self.assertIn("does not match the upstream", result.reason)
        self.assertEqual(self._commands(state, "push"), [])

    def test_unexpected_push_default_remote_skips(self):
        state = DestinationState()
        state.configs["remote.pushDefault"] = ["fork"]
        result = self._attempt(state)
        self.assertEqual(result.status, "skipped")
        self.assertIn("does not match the upstream", result.reason)

    def test_ambiguous_upstream_config_skips(self):
        state = DestinationState()
        state.configs[f"branch.{BRANCH}.remote"] = ["origin", "fork"]
        result = self._attempt(state)
        self.assertEqual(result.status, "skipped")
        self.assertIn("ambiguous", result.reason)

    def test_different_upstream_branch_name_skips(self):
        state = DestinationState()
        state.upstream_merge = "refs/heads/main"
        state.configs[f"branch.{BRANCH}.merge"] = ["refs/heads/main"]
        result = self._attempt(state)
        self.assertEqual(result.status, "skipped")
        self.assertIn("does not match the local branch", result.reason)
        self.assertEqual(self._commands(state, "push"), [])

    def test_broadening_refspec_and_mirror_and_matching_skip(self):
        cases = [
            ("remote.origin.push", ["refs/heads/*:refs/heads/*"], "refspec"),
            ("remote.origin.push", [f"+refs/heads/{BRANCH}:refs/heads/{BRANCH}"], "refspec"),
            ("remote.origin.push", [
                f"refs/heads/{BRANCH}:refs/heads/{BRANCH}",
                "refs/heads/other:refs/heads/other",
            ], "refspec"),
            ("push.default", ["matching"], "broaden"),
        ]
        for key, values, expected in cases:
            with self.subTest(key=key, values=values):
                state = DestinationState()
                state.configs[key] = values
                result = self._attempt(state)
                self.assertEqual(result.status, "skipped")
                self.assertIn(expected, result.reason)
                self.assertEqual(self._commands(state, "push"), [])

        state = DestinationState()
        state.mirror = True
        result = self._attempt(state)
        self.assertEqual(result.status, "skipped")
        self.assertIn("broaden", result.reason)

    def test_absent_remote_branch_skips(self):
        state = DestinationState()
        state.ls_remote_stdout = ""
        result = self._attempt(state)
        self.assertEqual(result.status, "skipped")
        self.assertIn("absent", result.reason)
        self.assertEqual(self._commands(state, "push"), [])

    def test_divergent_history_skips(self):
        state = DestinationState()
        state.ancestor_code = 1
        result = self._attempt(state)
        self.assertEqual(result.status, "skipped")
        self.assertIn("fast-forward", result.reason)
        self.assertEqual(self._commands(state, "push"), [])

    def test_unverifiable_history_skips(self):
        state = DestinationState()
        state.have_remote_object = False
        state.fetch_provides_object = False
        result = self._attempt(state)
        self.assertEqual(result.status, "skipped")
        self.assertIn("could not be verified", result.reason)
        self.assertEqual(self._commands(state, "push"), [])

    def test_authentication_failure_does_not_push_and_redacts_secrets(self):
        state = DestinationState()
        token = "ghp_" + ("b" * 36)
        state.ls_remote_code = 128
        state.ls_remote_stderr = (
            "fatal: Authentication failed for "
            f"'https://user:secret-token@example.com/private.git'\n{token}\n"
        )
        result = self._attempt(state)
        self.assertEqual(result.status, "failed")
        self.assertIn("authentication", result.reason.lower())
        self.assertNotIn("secret-token", result.reason)
        self.assertNotIn(token, result.reason)
        self.assertNotIn("example.com", result.reason)
        self.assertIn("[redacted]", result.reason)
        self.assertEqual(self._commands(state, "push"), [])

    def test_server_rejection_fails_and_keeps_a_sanitized_reason(self):
        state = DestinationState()
        state.push_code = 1
        state.push_stdout = f"!\trefs/heads/{BRANCH}:refs/heads/{BRANCH}\t[rejected]\n"
        state.push_stderr = "failed to push some refs to https://user:secret-token@example.com/repo.git"
        result = self._attempt(state)
        self.assertEqual(result.status, "failed")
        self.assertIn("rejected", result.reason)
        self.assertNotIn("secret-token", result.reason)
        self.assertNotIn("example.com", result.reason)

    def test_timeout_fails_without_raising(self):
        state = DestinationState()
        state.timeout_on = "push"
        result = self._attempt(state)
        self.assertEqual(result.status, "failed")
        self.assertIn("timed out", result.reason)

    def test_new_remote_branch_porcelain_is_failure(self):
        state = DestinationState()
        state.push_stdout = f"*\trefs/heads/{BRANCH}:refs/heads/{BRANCH}\t[new branch]\n"
        result = self._attempt(state)
        self.assertEqual(result.status, "failed")
        self.assertIn("created a remote branch", result.reason)

    def test_noninteractive_env_preserves_configured_transport(self):
        env = automatic_push.noninteractive_git_env({
            "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
            "GIT_TERMINAL_PROMPT": "1",
            "PATH": "/usr/bin",
        })
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GCM_INTERACTIVE"], "never")
        self.assertEqual(env["GIT_SSH_COMMAND"], "ssh -o BatchMode=yes")
        self.assertNotIn("GIT_ASKPASS", env)

    def _tmp(self):
        import tempfile
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        return directory


class TestAutomaticPushLocalRemote(unittest.TestCase):
    def setUp(self):
        self._suppress = os.environ.pop(automatic_push.SUPPRESS_ENV, None)
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self._saved_env = {}
        empty = self.tmp / "gitconfig"
        empty.write_text("", encoding="utf-8")
        for key in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"):
            self._saved_env[key] = os.environ.get(key)
        os.environ["GIT_CONFIG_GLOBAL"] = str(empty)
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        os.environ.pop("GIT_CONFIG_SYSTEM", None)

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if self._suppress is None:
            os.environ.pop(automatic_push.SUPPRESS_ENV, None)
        else:
            os.environ[automatic_push.SUPPRESS_ENV] = self._suppress

    def _git(self, repo, *args, check=True):
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if check and result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)
        return result

    def _init_work(self, path):
        self._git(self.tmp, "init", "-q", "-b", BRANCH, str(path))
        hooks = self.tmp / "empty-hooks"
        hooks.mkdir(exist_ok=True)
        self._git(path, "config", "user.email", "test@example.com")
        self._git(path, "config", "user.name", "Test")
        self._git(path, "config", "commit.gpgsign", "false")
        self._git(path, "config", "core.hooksPath", str(hooks))

    def _pair(self):
        bare = self.tmp / "remote.git"
        work = self.tmp / "work"
        self._git(self.tmp, "init", "-q", "--bare", "-b", BRANCH, str(bare))
        self._init_work(work)
        (work / "README").write_text("one\n", encoding="utf-8")
        self._git(work, "add", "README")
        self._git(work, "commit", "-q", "-m", "init")
        self._git(work, "remote", "add", "origin", str(bare))
        return bare, work

    def _sha(self, repo, ref="HEAD"):
        return self._git(repo, "rev-parse", ref).stdout.strip()

    def test_default_push_updates_existing_remote_branch(self):
        bare, work = self._pair()
        self._git(work, "push", "-q", "-u", "origin", BRANCH)
        (work / "README").write_text("two\n", encoding="utf-8")
        self._git(work, "commit", "-q", "-am", "second")
        commit = self._sha(work)

        result = automatic_push.attempt_automatic_push(work, BRANCH, commit)

        self.assertEqual(result.status, "pushed", result.reason)
        self.assertEqual(result.remote, "origin")
        self.assertEqual(result.remote_branch, BRANCH)
        self.assertEqual(self._sha(bare, BRANCH), commit)

    def test_opt_out_leaves_the_new_commit_local(self):
        bare, work = self._pair()
        self._git(work, "push", "-q", "-u", "origin", BRANCH)
        published = self._sha(bare, BRANCH)
        (work / "AGENTS.md").write_text("NO_AUTOMATIC_GIT_PUSH\n", encoding="utf-8")
        (work / "README").write_text("two\n", encoding="utf-8")
        self._git(work, "add", "README", "AGENTS.md")
        self._git(work, "commit", "-q", "-m", "second")

        result = automatic_push.attempt_automatic_push(work, BRANCH, self._sha(work))

        self.assertEqual(result.status, "skipped")
        self.assertIn("opts out", result.reason)
        self.assertEqual(self._sha(bare, BRANCH), published)

    def test_absent_remote_branch_is_not_created(self):
        bare, work = self._pair()
        self._git(work, "config", f"branch.{BRANCH}.remote", "origin")
        self._git(work, "config", f"branch.{BRANCH}.merge", f"refs/heads/{BRANCH}")
        result = automatic_push.attempt_automatic_push(work, BRANCH, self._sha(work))
        self.assertEqual(result.status, "skipped")
        self.assertIn("absent", result.reason)
        self.assertNotEqual(self._git(bare, "rev-parse", "--verify", f"refs/heads/{BRANCH}", check=False).returncode, 0)

    def test_divergent_remote_is_not_pushed(self):
        bare, work = self._pair()
        self._git(work, "push", "-q", "-u", "origin", BRANCH)
        other = self.tmp / "other"
        self._git(self.tmp, "clone", "-q", str(bare), str(other))
        hooks = self.tmp / "empty-hooks"
        self._git(other, "config", "user.email", "test@example.com")
        self._git(other, "config", "user.name", "Test")
        self._git(other, "config", "commit.gpgsign", "false")
        self._git(other, "config", "core.hooksPath", str(hooks))
        (other / "README").write_text("remote\n", encoding="utf-8")
        self._git(other, "commit", "-q", "-am", "remote")
        self._git(other, "push", "-q", "origin", BRANCH)
        remote_tip = self._sha(bare, BRANCH)
        (work / "README").write_text("local\n", encoding="utf-8")
        self._git(work, "commit", "-q", "-am", "local")

        result = automatic_push.attempt_automatic_push(work, BRANCH, self._sha(work))

        self.assertEqual(result.status, "skipped", result.reason)
        self.assertIn("fast-forward", result.reason)
        self.assertEqual(self._sha(bare, BRANCH), remote_tip)

    def _bare(self, name):
        path = self.tmp / name
        self._git(self.tmp, "init", "-q", "--bare", "-b", BRANCH, str(path))
        return path

    def _track_origin(self, work):
        self._git(work, "config", f"branch.{BRANCH}.remote", "origin")
        self._git(work, "config", f"branch.{BRANCH}.merge", f"refs/heads/{BRANCH}")

    def _ref_exists(self, repo, ref=None):
        ref = ref or f"refs/heads/{BRANCH}"
        return self._git(repo, "rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0

    def _seed_bare(self, bare, text, message):
        seed = self.tmp / f"seed-{bare.name}-{message}"
        self._init_work(seed)
        (seed / "README").write_text(text, encoding="utf-8")
        self._git(seed, "add", "README")
        self._git(seed, "commit", "-q", "-m", message)
        self._git(seed, "remote", "add", "origin", str(bare))
        self._git(seed, "push", "-q", "origin", BRANCH)
        return self._sha(bare, BRANCH)

    def _rewrite_push_to(self, work, fetch, push):
        self._git(work, "config", f"url.{push}.pushInsteadOf", str(fetch))
        self._git(work, "config", f"url.{fetch}.insteadOf", str(push))

    def test_pushurl_absent_branch_is_not_created(self):
        fetch, work = self._pair()
        self._git(work, "push", "-q", "-u", "origin", BRANCH)
        published = self._sha(fetch, BRANCH)
        push = self._bare("push.git")
        self._git(work, "config", "remote.origin.pushurl", str(push))
        (work / "README").write_text("two\n", encoding="utf-8")
        self._git(work, "commit", "-q", "-am", "second")

        result = automatic_push.attempt_automatic_push(work, BRANCH, self._sha(work))

        self.assertEqual(result.status, "skipped", result.reason)
        self.assertIn("absent", result.reason)
        self.assertFalse(self._ref_exists(push))
        self.assertEqual(self._sha(fetch, BRANCH), published)

    def test_pushurl_divergent_history_is_not_pushed(self):
        fetch, work = self._pair()
        self._git(work, "push", "-q", "-u", "origin", BRANCH)
        published = self._sha(fetch, BRANCH)
        push = self._bare("push.git")
        push_tip = self._seed_bare(push, "other\n", "other")
        self._git(work, "config", "remote.origin.pushurl", str(push))
        (work / "README").write_text("local\n", encoding="utf-8")
        self._git(work, "commit", "-q", "-am", "local")

        result = automatic_push.attempt_automatic_push(work, BRANCH, self._sha(work))

        self.assertEqual(result.status, "skipped", result.reason)
        self.assertIn("fast-forward", result.reason)
        self.assertEqual(self._sha(push, BRANCH), push_tip)
        self.assertEqual(self._sha(fetch, BRANCH), published)

    def test_pushurl_existing_branch_is_fast_forwarded(self):
        fetch, work = self._pair()
        push = self._bare("push.git")
        self._git(work, "push", "-q", str(push), f"HEAD:refs/heads/{BRANCH}")
        fetch_tip = self._seed_bare(fetch, "fetchside\n", "fetchside")
        self._track_origin(work)
        self._git(work, "config", "remote.origin.pushurl", str(push))
        (work / "README").write_text("local\n", encoding="utf-8")
        self._git(work, "commit", "-q", "-am", "local")

        result = automatic_push.attempt_automatic_push(work, BRANCH, self._sha(work))

        self.assertEqual(result.status, "pushed", result.reason)
        self.assertEqual(self._sha(push, BRANCH), self._sha(work))
        self.assertEqual(self._sha(fetch, BRANCH), fetch_tip)

    def test_push_rewrite_absent_target_is_not_created(self):
        fetch, work = self._pair()
        self._git(work, "push", "-q", "-u", "origin", BRANCH)
        published = self._sha(fetch, BRANCH)
        push = self._bare("push.git")
        self._rewrite_push_to(work, fetch, push)
        (work / "README").write_text("two\n", encoding="utf-8")
        self._git(work, "commit", "-q", "-am", "second")

        result = automatic_push.attempt_automatic_push(work, BRANCH, self._sha(work))

        self.assertEqual(result.status, "skipped", result.reason)
        self.assertIn("absent", result.reason)
        self.assertFalse(self._ref_exists(push))
        self.assertEqual(self._sha(fetch, BRANCH), published)

    def test_push_rewrite_divergent_target_is_not_pushed(self):
        fetch, work = self._pair()
        self._git(work, "push", "-q", "-u", "origin", BRANCH)
        published = self._sha(fetch, BRANCH)
        push = self._bare("push.git")
        push_tip = self._seed_bare(push, "other\n", "other")
        self._rewrite_push_to(work, fetch, push)
        (work / "README").write_text("local\n", encoding="utf-8")
        self._git(work, "commit", "-q", "-am", "local")

        result = automatic_push.attempt_automatic_push(work, BRANCH, self._sha(work))

        self.assertEqual(result.status, "skipped", result.reason)
        self.assertIn("fast-forward", result.reason)
        self.assertEqual(self._sha(push, BRANCH), push_tip)
        self.assertEqual(self._sha(fetch, BRANCH), published)

    def test_push_rewrite_existing_branch_is_fast_forwarded(self):
        fetch, work = self._pair()
        push = self._bare("push.git")
        self._git(work, "push", "-q", str(push), f"HEAD:refs/heads/{BRANCH}")
        fetch_tip = self._seed_bare(fetch, "fetchside\n", "fetchside")
        self._track_origin(work)
        self._rewrite_push_to(work, fetch, push)
        (work / "README").write_text("local\n", encoding="utf-8")
        self._git(work, "commit", "-q", "-am", "local")

        result = automatic_push.attempt_automatic_push(work, BRANCH, self._sha(work))

        self.assertEqual(result.status, "pushed", result.reason)
        self.assertEqual(self._sha(push, BRANCH), self._sha(work))
        self.assertEqual(self._sha(fetch, BRANCH), fetch_tip)


if __name__ == "__main__":
    unittest.main()
