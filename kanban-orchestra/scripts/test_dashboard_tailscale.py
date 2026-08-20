#!/usr/bin/env python3
"""Behavioral tests for shared dashboard Tailscale Serve publication."""

from __future__ import annotations

import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dashboard
import dashboard_tailscale
import fleet
import fleet_dashboard


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def _status(backend: str = "Running", dns_name: str = "node.example.ts.net.") -> dict:
    return {"BackendState": backend, "Self": {"DNSName": dns_name}}


def _serve_mapping(local_port: int = 8427, https_port: int | None = None, proxy_port: int | None = None) -> dict:
    https_port = local_port if https_port is None else https_port
    proxy_port = local_port if proxy_port is None else proxy_port
    return _add_serve_mapping({}, https_port=https_port, proxy_port=proxy_port)


def _add_serve_mapping(
    serve: dict,
    *,
    https_port: int,
    proxy_port: int,
) -> dict:
    tcp = dict(serve.get("TCP") or {})
    web = dict(serve.get("Web") or {})
    tcp[str(https_port)] = {"HTTPS": True}
    web[f"node.example.ts.net:{https_port}"] = {
        "Handlers": {"/": {"Proxy": f"http://127.0.0.1:{proxy_port}"}}
    }
    return {"TCP": tcp, "Web": web}


class FakeTailscale:
    """Scripted Tailscale CLI used by publication tests."""

    def __init__(
        self,
        *,
        backend: str | None = "Running",
        serve: dict | str | None = None,
        up_backend: str = "Running",
        required_up_flags: list[str] | None = None,
        fail: set[str] | None = None,
        timeout: set[str] | None = None,
    ):
        self.backend = backend
        self.serve = {} if serve is None else serve
        self.up_backend = up_backend
        self.required_up_flags = required_up_flags
        self.fail = fail or set()
        self.timeout = timeout or set()
        self.commands: list[list[str]] = []

    def __call__(self, args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str] | None:
        self.commands.append(list(args))
        name = self._name(args)
        if name in self.timeout:
            return None
        if name in self.fail:
            return _completed(returncode=1, stderr=f"{name} failed")
        if name == "status":
            if self.backend is None:
                return _completed(returncode=1, stderr="tailscaled not running")
            return _completed(json.dumps(_status(self.backend)))
        if name == "up":
            if self.required_up_flags is not None:
                expected = ["tailscale", "up", "--timeout=15s", *self.required_up_flags]
                if args != expected:
                    suggested = " ".join(expected)
                    return _completed(
                        returncode=1,
                        stderr=(
                            "Error: changing settings via 'tailscale up' requires mentioning all\n"
                            "non-default flags. Use the command below:\n\n"
                            f"\t{suggested}\n"
                        ),
                    )
            self.backend = self.up_backend
            return _completed()
        if name == "serve-status":
            if isinstance(self.serve, str):
                return _completed(self.serve)
            return _completed(json.dumps(self.serve))
        if name == "serve-create":
            https_port = self._https_port(args)
            proxy_port = self._proxy_port(args)
            self.serve = _add_serve_mapping(
                self.serve if isinstance(self.serve, dict) else {},
                https_port=https_port,
                proxy_port=proxy_port,
            )
            return _completed()
        return _completed(returncode=1, stderr=f"unexpected {args}")

    def _name(self, args: list[str]) -> str:
        if args[:2] == ["tailscale", "status"]:
            return "status"
        if args[:2] == ["tailscale", "up"]:
            return "up"
        if args[:3] == ["tailscale", "serve", "status"]:
            return "serve-status"
        if args[:2] == ["tailscale", "serve"] and "--bg" in args:
            return "serve-create"
        return "other"

    def _https_port(self, args: list[str]) -> int:
        for arg in args:
            if arg.startswith("--https="):
                return int(arg.split("=", 1)[1])
        raise AssertionError(f"missing --https in {args}")

    def _proxy_port(self, args: list[str]) -> int:
        target = args[-1] if args else ""
        try:
            port = int(str(target).rsplit(":", 1)[1])
        except (IndexError, TypeError, ValueError) as exc:
            raise AssertionError(f"missing proxy port in {args}") from exc
        return port


def _publish(fake: FakeTailscale, host: str = "127.0.0.1", port: int = 8427) -> str | None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with tempfile.TemporaryDirectory() as tmpdir, \
            patch.dict(os.environ, {"KO_DASHBOARD_SERVE_LOCK_PATH": str(Path(tmpdir) / "lock")}), \
            patch.object(dashboard_tailscale.shutil, "which", return_value="/usr/bin/tailscale"), \
            patch.object(dashboard_tailscale, "_run", side_effect=fake), \
            redirect_stdout(stdout), \
            redirect_stderr(stderr):
        remote = dashboard_tailscale.publish_dashboard(host, port)
    return remote, stdout.getvalue(), stderr.getvalue()


class TestLookupDashboardUrl(unittest.TestCase):
    def test_matches_existing_https_proxy(self):
        result = _completed(json.dumps(_serve_mapping()))
        with patch.object(dashboard_tailscale.shutil, "which", return_value="/usr/bin/tailscale"), \
             patch.object(dashboard_tailscale, "_run", return_value=result):
            remote = dashboard_tailscale.lookup_dashboard_url("http://127.0.0.1:8427")
        self.assertEqual(remote, "https://node.example.ts.net:8427/")

    def test_requires_matching_https_proxy(self):
        status = _serve_mapping(https_port=8427, proxy_port=9000)
        status["TCP"]["8428"] = {"HTTPS": False}
        status["Web"]["node.example.ts.net:8428"] = {
            "Handlers": {"/": {"Proxy": "http://127.0.0.1:8427"}}
        }
        result = _completed(json.dumps(status))
        with patch.object(dashboard_tailscale.shutil, "which", return_value="/usr/bin/tailscale"), \
             patch.object(dashboard_tailscale, "_run", return_value=result):
            remote = dashboard_tailscale.lookup_dashboard_url("http://127.0.0.1:8427")
        self.assertIsNone(remote)

    def test_absent_without_cli(self):
        with patch.object(dashboard_tailscale.shutil, "which", return_value=None), \
             patch.object(dashboard_tailscale, "_run") as run_mock:
            remote = dashboard_tailscale.lookup_dashboard_url("http://127.0.0.1:8427")
        self.assertIsNone(remote)
        run_mock.assert_not_called()

    def test_absent_without_serve_config(self):
        for stdout in ("null", "{}"):
            with self.subTest(stdout=stdout), \
                 patch.object(dashboard_tailscale.shutil, "which", return_value="/usr/bin/tailscale"), \
                 patch.object(dashboard_tailscale, "_run", return_value=_completed(stdout)):
                remote = dashboard_tailscale.lookup_dashboard_url("http://127.0.0.1:8427")
            self.assertIsNone(remote)


class TestPublishDashboard(unittest.TestCase):
    def test_cli_absence_is_silent(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(dashboard_tailscale.shutil, "which", return_value=None), \
             patch.object(dashboard_tailscale, "_run") as run_mock, \
             redirect_stdout(stdout), \
             redirect_stderr(stderr):
            remote = dashboard_tailscale.publish_dashboard("127.0.0.1", 8427)
        self.assertIsNone(remote)
        run_mock.assert_not_called()
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_stopped_to_up_creates_same_port_mapping(self):
        fake = FakeTailscale(backend="Stopped", serve={})
        remote, stdout, stderr = _publish(fake)
        self.assertEqual(remote, "https://node.example.ts.net:8427/")
        self.assertIn(["tailscale", "up", "--timeout=15s"], fake.commands)
        self.assertIn(
            ["tailscale", "serve", "--bg", "--https=8427", "http://127.0.0.1:8427"],
            fake.commands,
        )
        self.assertTrue(all(cmd[0] == "tailscale" for cmd in fake.commands))
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")

    def test_stopped_retries_up_with_cli_suggested_preferences(self):
        fake = FakeTailscale(backend="Stopped", serve={}, required_up_flags=["--accept-routes"])
        remote, stdout, stderr = _publish(fake)
        self.assertEqual(remote, "https://node.example.ts.net:8427/")
        self.assertEqual(
            [cmd for cmd in fake.commands if fake._name(cmd) == "up"],
            [
                ["tailscale", "up", "--timeout=15s"],
                ["tailscale", "up", "--timeout=15s", "--accept-routes"],
            ],
        )
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")

    def test_stopped_does_not_accept_cli_reset_suggestion(self):
        fake = FakeTailscale(backend="Stopped", serve={}, required_up_flags=["--reset"])
        remote, _, _ = _publish(fake)
        self.assertIsNone(remote)
        self.assertEqual(
            [cmd for cmd in fake.commands if fake._name(cmd) == "up"],
            [["tailscale", "up", "--timeout=15s"]],
        )

    def test_already_running_skips_up(self):
        fake = FakeTailscale(backend="Running", serve={})
        remote, _, _ = _publish(fake)
        self.assertEqual(remote, "https://node.example.ts.net:8427/")
        self.assertNotIn("up", [fake._name(cmd) for cmd in fake.commands])
        self.assertIn(
            ["tailscale", "serve", "--bg", "--https=8427", "http://127.0.0.1:8427"],
            fake.commands,
        )

    def test_exact_mapping_reuse_does_not_create(self):
        fake = FakeTailscale(serve=_serve_mapping(8427))
        remote, _, _ = _publish(fake)
        self.assertEqual(remote, "https://node.example.ts.net:8427/")
        self.assertNotIn("serve-create", [fake._name(cmd) for cmd in fake.commands])

    def test_new_mapping_creation_uses_argument_list(self):
        fake = FakeTailscale(serve={})
        captured = {}

        def _run(args, *, timeout):
            if args[:2] == ["tailscale", "serve"] and "--bg" in args:
                captured["args"] = list(args)
                captured["timeout"] = timeout
            return fake(args, timeout=timeout)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.dict(os.environ, {"KO_DASHBOARD_SERVE_LOCK_PATH": str(Path(tmpdir) / "lock")}), \
                patch.object(dashboard_tailscale.shutil, "which", return_value="/usr/bin/tailscale"), \
                patch.object(dashboard_tailscale, "_run", side_effect=_run), \
                redirect_stdout(stdout), \
                redirect_stderr(stderr):
            remote = dashboard_tailscale.publish_dashboard("127.0.0.1", 8427)

        self.assertEqual(remote, "https://node.example.ts.net:8427/")
        self.assertEqual(
            captured["args"],
            ["tailscale", "serve", "--bg", "--https=8427", "http://127.0.0.1:8427"],
        )
        self.assertEqual(captured["timeout"], dashboard_tailscale.COMMAND_TIMEOUT_SECONDS)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_conflicting_mapping_uses_alternate_https_listener(self):
        fake = FakeTailscale(serve=_serve_mapping(https_port=8427, proxy_port=9000))
        remote, stdout, stderr = _publish(fake)
        self.assertEqual(remote, "https://node.example.ts.net:8428/")
        self.assertIn(
            ["tailscale", "serve", "--bg", "--https=8428", "http://127.0.0.1:8427"],
            fake.commands,
        )
        self.assertEqual(
            fake.serve["Web"]["node.example.ts.net:8427"]["Handlers"]["/"]["Proxy"],
            "http://127.0.0.1:9000",
        )
        self.assertEqual(
            fake.serve["Web"]["node.example.ts.net:8428"]["Handlers"]["/"]["Proxy"],
            "http://127.0.0.1:8427",
        )
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")

    def test_conflicting_mapping_skips_occupied_alternate_listeners(self):
        serve = _add_serve_mapping(
            _serve_mapping(https_port=8427, proxy_port=9000),
            https_port=8428,
            proxy_port=9001,
        )
        fake = FakeTailscale(serve=serve)
        remote, _, _ = _publish(fake)
        self.assertEqual(remote, "https://node.example.ts.net:8429/")
        self.assertIn(
            ["tailscale", "serve", "--bg", "--https=8429", "http://127.0.0.1:8427"],
            fake.commands,
        )
        self.assertEqual(
            fake.serve["Web"]["node.example.ts.net:8427"]["Handlers"]["/"]["Proxy"],
            "http://127.0.0.1:9000",
        )

    def test_choose_https_listener_wraps_after_last_port(self):
        payload = {"TCP": {"65535": {"HTTPS": True}}, "Web": {}}
        self.assertEqual(
            dashboard_tailscale._choose_https_listener_port(payload, 65535),
            1,
        )

    def test_command_failure_returns_none_without_raising(self):
        fake = FakeTailscale(fail={"serve-status"})
        remote, stdout, stderr = _publish(fake)
        self.assertIsNone(remote)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")

    def test_authentication_failure_skips_up(self):
        fake = FakeTailscale(backend="NeedsLogin")
        remote, _, _ = _publish(fake)
        self.assertIsNone(remote)
        self.assertEqual(fake.commands, [["tailscale", "status", "--json"]])

    def test_up_failure_returns_none(self):
        fake = FakeTailscale(backend="Stopped", fail={"up"})
        remote, _, _ = _publish(fake)
        self.assertIsNone(remote)

    def test_serve_create_failure_returns_none(self):
        fake = FakeTailscale(serve={}, fail={"serve-create"})
        remote, _, _ = _publish(fake)
        self.assertIsNone(remote)

    def test_timeout_returns_none(self):
        fake = FakeTailscale(timeout={"status"})
        remote, _, _ = _publish(fake)
        self.assertIsNone(remote)

    def test_publish_exception_is_swallowed(self):
        with patch.object(dashboard_tailscale.shutil, "which", side_effect=RuntimeError("boom")):
            self.assertIsNone(dashboard_tailscale.publish_dashboard("127.0.0.1", 8427))

    def test_non_loopback_host_is_ignored(self):
        fake = FakeTailscale()
        remote, _, _ = _publish(fake, host="0.0.0.0")
        self.assertIsNone(remote)
        self.assertEqual(fake.commands, [])

    def test_orchestra_fleet_repos_does_not_split_lock_domain(self):
        expected = dashboard_tailscale.DEFAULT_LOCK_PATH.expanduser()
        kept = {
            key: value
            for key, value in os.environ.items()
            if key not in {"KO_DASHBOARD_SERVE_LOCK_PATH", "ORCHESTRA_FLEET_REPOS"}
        }
        with patch.dict(os.environ, kept, clear=True):
            without_fleet = dashboard_tailscale.serve_lock_path()
            os.environ["ORCHESTRA_FLEET_REPOS"] = "/tmp/custom-fleet-repos"
            with_fleet = dashboard_tailscale.serve_lock_path()
        self.assertEqual(without_fleet, expected)
        self.assertEqual(with_fleet, expected)

    def test_lock_serializes_concurrent_publish(self):
        fake = FakeTailscale(serve={})
        results: dict[int, str | None] = {}
        ready = threading.Barrier(3)

        def _publish_one(port: int) -> None:
            ready.wait()
            results[port] = dashboard_tailscale.publish_dashboard("127.0.0.1", port)

        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "lock"
            handle = lock_path.open("a+")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                with patch.dict(os.environ, {"KO_DASHBOARD_SERVE_LOCK_PATH": str(lock_path)}), \
                     patch.object(dashboard_tailscale, "LOCK_WAIT_SECONDS", 0.2), \
                     patch.object(dashboard_tailscale.shutil, "which", return_value="/usr/bin/tailscale"), \
                     patch.object(dashboard_tailscale, "_run", side_effect=fake):
                    threads = [
                        threading.Thread(target=_publish_one, args=(port,))
                        for port in (8427, 8428)
                    ]
                    for thread in threads:
                        thread.start()
                    ready.wait()
                    time.sleep(0.05)
                    self.assertEqual(fake.commands, [])
                    # Hold the lock past one wait so waiters must retry, then
                    # release so both exact loopback ports still publish.
                    time.sleep(0.3)
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    for thread in threads:
                        thread.join(timeout=5)
                        self.assertFalse(thread.is_alive())
            finally:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                handle.close()

        self.assertEqual(results.get(8427), "https://node.example.ts.net:8427/")
        self.assertEqual(results.get(8428), "https://node.example.ts.net:8428/")
        self.assertEqual(
            fake.serve["Web"]["node.example.ts.net:8427"]["Handlers"]["/"]["Proxy"],
            "http://127.0.0.1:8427",
        )
        self.assertEqual(
            fake.serve["Web"]["node.example.ts.net:8428"]["Handlers"]["/"]["Proxy"],
            "http://127.0.0.1:8428",
        )

    def test_subprocess_run_uses_argv_timeout_and_no_shell(self):
        with patch.object(dashboard_tailscale.subprocess, "run", return_value=_completed("{}")) as run_mock:
            result = dashboard_tailscale._run(["tailscale", "serve", "status", "--json"], timeout=8.0)
        self.assertIsNotNone(result)
        args, kwargs = run_mock.call_args
        self.assertEqual(args[0], ["tailscale", "serve", "status", "--json"])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["timeout"], 8.0)


def _join_publish_threads(timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    for thread in threading.enumerate():
        if thread.name != "dashboard-tailscale-publish":
            continue
        remaining = max(0.0, deadline - time.monotonic())
        thread.join(timeout=remaining)


class TestSchedulePublishDashboard(unittest.TestCase):
    def test_schedule_returns_before_publish_finishes(self):
        started = threading.Event()
        release = threading.Event()
        resolved: list[str | None] = []

        def slow_publish(host, port):
            started.set()
            self.assertTrue(release.wait(timeout=5))
            return "https://node.example.ts.net:8427/"

        with patch.object(dashboard_tailscale, "publish_dashboard", side_effect=slow_publish):
            thread = dashboard_tailscale.schedule_publish_dashboard(
                "127.0.0.1",
                8427,
                on_resolved=resolved.append,
            )
            self.assertTrue(started.wait(timeout=1))
            self.assertEqual(resolved, [])
            release.set()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(resolved, ["https://node.example.ts.net:8427/"])

    def test_schedule_swallows_callback_errors(self):
        def boom(_remote_url):
            raise RuntimeError("callback failed")

        with patch.object(
            dashboard_tailscale,
            "publish_dashboard",
            return_value="https://node.example.ts.net:8427/",
        ):
            thread = dashboard_tailscale.schedule_publish_dashboard(
                "127.0.0.1",
                8427,
                on_resolved=boom,
            )
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())


class TestDashboardStartupIntegration(unittest.TestCase):
    def test_repo_dashboard_publishes_selected_port_and_records_remote_url(self):
        uv = MagicMock()
        uv.run = MagicMock()
        identity = {
            "repo_root": "/tmp/repo",
            "repo_label": "repo",
            "db_path": "/tmp/repo/kanban-orchestra.db",
            "runtime_root": "/tmp/repo/.kanban-orchestra",
            "lock_path": "/tmp/repo/kanban-orchestra.lock",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            metadata_path = Path(tmpdir) / "dashboard.json"
            with patch.object(dashboard, "_find_free_port", return_value=8431), \
                 patch.object(
                     dashboard.dashboard_tailscale,
                     "publish_dashboard",
                     return_value="https://node.example.ts.net:8431/",
                 ) as publish_mock, \
                 patch.dict(os.environ, {"KO_DASHBOARD_METADATA_PATH": str(metadata_path)}), \
                 patch.object(dashboard.db, "get_instance_identity", return_value=identity):
                dashboard._run_dashboard("127.0.0.1", 8427, _uvicorn=uv)
                _join_publish_threads()

            publish_mock.assert_called_once_with("127.0.0.1", 8431)
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["url"], "http://127.0.0.1:8431")
            self.assertEqual(payload["remote_url"], "https://node.example.ts.net:8431/")
            self.assertEqual(uv.run.call_args.kwargs["port"], 8431)

    def test_repo_dashboard_starts_when_publish_raises(self):
        uv = MagicMock()
        uv.run = MagicMock()
        with patch.object(dashboard, "_find_free_port", return_value=8427), \
             patch.object(
                 dashboard.dashboard_tailscale,
                 "publish_dashboard",
                 side_effect=RuntimeError("serve exploded"),
             ), \
             patch.object(dashboard, "_write_dashboard_metadata") as write_mock:
            dashboard._run_dashboard("127.0.0.1", 8427, _uvicorn=uv)
            _join_publish_threads()
        uv.run.assert_called_once()
        write_mock.assert_called_once_with("127.0.0.1", 8427, remote_url=None)

    def test_fleet_dashboard_publishes_selected_port_and_records_remote_url(self):
        uv = MagicMock()
        uv.run = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            metadata_path = Path(tmpdir) / "fleet-dashboard.json"
            with patch.object(dashboard, "_find_free_port", return_value=8426), \
                 patch.object(
                     fleet_dashboard.dashboard_tailscale,
                     "publish_dashboard",
                     return_value="https://node.example.ts.net:8426/",
                 ) as publish_mock, \
                 patch.object(fleet, "fleet_dashboard_metadata_path", return_value=metadata_path):
                fleet_dashboard._run_dashboard("127.0.0.1", 8426, _uvicorn=uv)
                _join_publish_threads()

            publish_mock.assert_called_once_with("127.0.0.1", 8426)
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["role"], "fleet-dashboard")
            self.assertEqual(payload["remote_url"], "https://node.example.ts.net:8426/")
            self.assertEqual(uv.run.call_args.kwargs["port"], 8426)

    def test_fleet_dashboard_ready_while_publication_hangs(self):
        started = threading.Event()
        hang = threading.Event()
        ready: dict[str, dict | None] = {}

        def hanging_publish(host, port):
            started.set()
            hang.wait(timeout=5)
            return "https://node.example.ts.net:8426/"

        def fake_run(*args, **kwargs):
            try:
                self.assertTrue(started.wait(timeout=1))
                with patch.object(fleet, "dashboard_endpoint_ready", return_value=True), \
                     patch.object(fleet, "pid_alive", return_value=True):
                    ready["payload"] = fleet.wait_fleet_dashboard_ready(timeout=1.0)
            finally:
                hang.set()

        uv = MagicMock()
        uv.run = MagicMock(side_effect=fake_run)

        with tempfile.TemporaryDirectory() as tmpdir:
            metadata_path = Path(tmpdir) / "fleet-dashboard.json"
            with patch.object(dashboard, "_find_free_port", return_value=8426), \
                 patch.object(
                     fleet_dashboard.dashboard_tailscale,
                     "publish_dashboard",
                     side_effect=hanging_publish,
                 ), \
                 patch.object(fleet, "fleet_dashboard_metadata_path", return_value=metadata_path):
                fleet_dashboard._run_dashboard("127.0.0.1", 8426, _uvicorn=uv)
                _join_publish_threads()

        payload = ready.get("payload")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["url"], "http://127.0.0.1:8426")
        self.assertIsNone(payload.get("remote_url"))
        uv.run.assert_called_once()

    def test_fleet_wrapper_uses_shared_lookup(self):
        with patch.object(
            dashboard_tailscale,
            "lookup_dashboard_url",
            return_value="https://node.example.ts.net:8427/",
        ) as lookup_mock:
            remote = fleet.tailscale_dashboard_url("http://127.0.0.1:8427")
        self.assertEqual(remote, "https://node.example.ts.net:8427/")
        lookup_mock.assert_called_once_with("http://127.0.0.1:8427")


if __name__ == "__main__":
    unittest.main()
