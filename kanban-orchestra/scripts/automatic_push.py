#!/usr/bin/env python3
"""Best-effort automatic push after a local commit.

The push is fail-closed. It runs only when the checkout is on the expected
branch and that branch already has one same-name remote upstream. Before
writing, the single effective push URL is resolved the way ``git push``
resolves it, including one pushInsteadOf or insteadOf rewrite, and the branch
must already exist at that exact destination. The push command itself still
uses the configured remote name so the repository's transport and credentials
stay in effect. Nothing in this module embeds a host, credential, or private
access route.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import repo_policy

SUPPRESS_ENV = "ORCHESTRA_SUPPRESS_AUTOMATIC_PUSH"
LOCAL_GIT_TIMEOUT_SECONDS = 15
NETWORK_GIT_TIMEOUT_SECONDS = 60

_HASH_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_ALLOWED_PUSH_DEFAULTS = {"simple", "upstream", "current", "nothing"}
_URL_RE = re.compile(r"\b(?:https?|ssh|git|file|ftp)://\S+|\bgit@[\w.\-]+:\S+", re.IGNORECASE)
_HOME_PATH_RE = re.compile(r"(?:/Users|/home)/[^\s:]+")
_TOKEN_RE = re.compile(
    r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b|\bgithub_pat_[A-Za-z0-9_]+\b",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+\S+")


class AutomaticPushSuppressed(Exception):
    """Raised when a test interlock blocks the real git push."""


class _Stop(Exception):
    def __init__(self, attempt: "PushAttempt"):
        self.attempt = attempt


@dataclass(frozen=True)
class GitOutput:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class PushAttempt:
    status: str
    reason: str = ""
    remote: str = ""
    remote_branch: str = ""

    @staticmethod
    def pushed(remote: str, remote_branch: str) -> "PushAttempt":
        return PushAttempt("pushed", "", remote, remote_branch)

    @staticmethod
    def skipped(reason: str) -> "PushAttempt":
        return PushAttempt("skipped", sanitize_reason(reason))

    @staticmethod
    def failed(reason: str) -> "PushAttempt":
        return PushAttempt("failed", sanitize_reason(reason))

    @staticmethod
    def suppressed() -> "PushAttempt":
        return PushAttempt("suppressed")


def sanitize_reason(text: str) -> str:
    """Return a short single-line reason without credentials or URLs."""
    cleaned = _URL_RE.sub("[url]", str(text or ""))
    cleaned = _HOME_PATH_RE.sub("[path]", cleaned)
    cleaned = _TOKEN_RE.sub("[redacted]", cleaned)
    cleaned = _BEARER_RE.sub("Bearer [redacted]", cleaned)
    cleaned = "".join(ch for ch in cleaned if ch >= " " or ch in "\t")
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > 240:
        cleaned = cleaned[:237] + "..."
    return cleaned


def noninteractive_git_env(base: dict | None = None) -> dict:
    """Return an environment that refuses prompts and keeps configured transport."""
    env = dict(os.environ if base is None else base)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    return env


def attempt_automatic_push(repo_root, expected_branch, commit_hash, run_git=None) -> PushAttempt:
    """Push commit_hash to its existing upstream, or explain why not.

    ``run_git`` is ``(repo, args, timeout) -> GitOutput`` for tests. Production
    uses a noninteractive git subprocess. A suppressed environment returns
    without inspecting or contacting a remote.
    """
    if _suppressed():
        return PushAttempt.suppressed()
    try:
        return _attempt(Path(repo_root), expected_branch or "", commit_hash or "", run_git or default_run_git)
    except AutomaticPushSuppressed:
        return PushAttempt.suppressed()
    except _Stop as stop:
        return stop.attempt
    except subprocess.TimeoutExpired:
        return PushAttempt.failed("git command timed out")
    except Exception as exc:
        return PushAttempt.failed(f"unexpected error: {exc}")


def default_run_git(repo_root: Path, args: list[str], timeout: float) -> GitOutput:
    if args[:1] == ["push"] and _suppressed():
        raise AutomaticPushSuppressed()
    try:
        if _push_endpoint_read(args) is not None:
            return _run_push_endpoint_read(repo_root, args, timeout)
        return _run_git(repo_root, args, timeout, noninteractive_git_env())
    except subprocess.TimeoutExpired:
        raise
    except OSError as exc:
        raise _Stop(PushAttempt.failed(f"could not run git: {exc}")) from exc


_TRANSPORT_KEY_PREFIXES = ("credential.", "http.", "ssh.", "protocol.")
_TRANSPORT_KEYS = {"core.sshcommand", "core.gitproxy"}


def _run_git(repo: Path, args: list[str], timeout: float, env: dict) -> GitOutput:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return GitOutput(completed.returncode, completed.stdout or "", completed.stderr or "")


def _push_endpoint_read(args: list[str]) -> tuple[str, str, str] | None:
    """Return (kind, endpoint, ref) when args read a resolved push URL."""
    if args[:2] == ["ls-remote", "--heads"] and len(args) == 4:
        return ("ls-remote", args[2], args[3])
    if args[:3] == ["fetch", "--no-tags", "--recurse-submodules=no"] and len(args) == 5:
        return ("fetch", args[3], args[4])
    return None


def _run_push_endpoint_read(repo: Path, args: list[str], timeout: float) -> GitOutput:
    """Read a push URL without letting a second fetch rewrite change the target.

    ``git remote get-url --push`` already applied the one rewrite ``git push``
    uses. ``ls-remote`` and ``fetch`` apply fetch insteadOf rules again, which
    can point at a different repository. When that would happen, read the
    resolved URL with those rules removed.
    """
    parsed = _push_endpoint_read(args)
    if parsed is None:
        return GitOutput(1, "", "push destination could not be verified")
    kind, endpoint, ref = parsed
    env = noninteractive_git_env()
    shown = _run_git(repo, ["ls-remote", "--get-url", endpoint], LOCAL_GIT_TIMEOUT_SECONDS, env)
    if shown.returncode == 0 and shown.stdout.strip() == endpoint:
        return _run_git(repo, args, timeout, env)
    return _run_endpoint_without_fetch_rewrite(repo, kind, endpoint, ref, timeout, env)


def _run_endpoint_without_fetch_rewrite(
    repo: Path,
    kind: str,
    endpoint: str,
    ref: str,
    timeout: float,
    base_env: dict,
) -> GitOutput:
    tmp = Path(tempfile.mkdtemp(prefix="ko-push-endpoint-"))
    try:
        config_file = tmp / "gitconfig"
        config_file.write_text("", encoding="utf-8")
        if not _write_transport_config(repo, config_file, base_env):
            return GitOutput(1, "", "push destination could not be verified")
        env = _probe_env(base_env, config_file)
        probe = tmp / "probe"
        init = _run_git(tmp, ["init", "-q", str(probe)], LOCAL_GIT_TIMEOUT_SECONDS, env)
        if init.returncode != 0:
            return GitOutput(1, "", "push destination could not be verified")
        objects = _objects_dir(repo, base_env)
        if objects is None:
            return GitOutput(1, "", "push destination could not be verified")
        alternates = probe / ".git" / "objects" / "info" / "alternates"
        alternates.parent.mkdir(parents=True, exist_ok=True)
        alternates.write_text(str(objects) + "\n", encoding="utf-8")
        configured = _run_git(
            probe,
            ["config", "remote.push-endpoint.url", endpoint],
            LOCAL_GIT_TIMEOUT_SECONDS,
            env,
        )
        if configured.returncode != 0:
            return GitOutput(1, "", "push destination could not be verified")
        if kind == "ls-remote":
            return _run_git(
                probe,
                ["ls-remote", "--quiet", "--heads", "push-endpoint", ref],
                timeout,
                env,
            )
        fetched = _run_git(
            probe,
            [
                "fetch",
                "--no-tags",
                "--no-prune",
                "--recurse-submodules=no",
                "push-endpoint",
                f"{ref}:{ref}",
            ],
            timeout,
            env,
        )
        if fetched.returncode != 0:
            return fetched
        if not _import_missing_commits(probe, repo, ref, env, timeout):
            return GitOutput(1, "", "push destination could not be verified")
        return fetched
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _write_transport_config(repo: Path, dest: Path, env: dict) -> bool:
    """Copy credential and transport settings without URL rewrite rules."""
    try:
        listed = subprocess.run(
            ["git", "config", "--list", "--null"],
            cwd=str(repo),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=LOCAL_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if listed.returncode != 0:
        return False
    for key, value in _parse_config_null(listed.stdout):
        if not _keep_transport_key(key):
            continue
        try:
            wrote = subprocess.run(
                ["git", "config", "--file", str(dest), "--add", key, value],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=LOCAL_GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if wrote.returncode != 0:
            return False
    return True


def _parse_config_null(blob: bytes):
    if blob.startswith(b"\n"):
        blob = blob[1:]
    for entry in blob.split(b"\0"):
        if not entry:
            continue
        if entry.startswith(b"\n"):
            entry = entry[1:]
        key, sep, value = entry.partition(b"\n")
        if not sep:
            continue
        yield (
            key.decode("utf-8", "surrogateescape"),
            value.decode("utf-8", "surrogateescape"),
        )


def _keep_transport_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _TRANSPORT_KEYS:
        return True
    return lowered.startswith(_TRANSPORT_KEY_PREFIXES)


def _probe_env(base: dict, config_file: Path) -> dict:
    env = noninteractive_git_env(base)
    env["GIT_CONFIG_GLOBAL"] = str(config_file)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env.pop("GIT_CONFIG_SYSTEM", None)
    env.pop("GIT_CONFIG_COUNT", None)
    env.pop("GIT_CONFIG_PARAMETERS", None)
    for key in list(env):
        if key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"):
            env.pop(key, None)
    return env


def _objects_dir(repo: Path, env: dict) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-path", "objects"],
            cwd=str(repo),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=LOCAL_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    path = Path((result.stdout or "").strip())
    if not path.is_absolute():
        path = (repo / path).resolve()
    return path


def _import_missing_commits(probe: Path, repo: Path, ref: str, env: dict, timeout: float) -> bool:
    listed = _run_git(probe, ["rev-list", ref], timeout, env)
    if listed.returncode != 0:
        return False
    missing = []
    for sha in listed.stdout.split():
        if not _valid_commit(sha):
            return False
        if not _commit_exists(repo, sha, env):
            missing.append(sha)
    if not missing:
        return True
    try:
        packed = subprocess.run(
            ["git", "pack-objects", "--stdout"],
            cwd=str(probe),
            env=env,
            input=("\n".join(missing) + "\n").encode(),
            capture_output=True,
            timeout=timeout,
        )
        if packed.returncode != 0:
            return False
        unpacked = subprocess.run(
            ["git", "unpack-objects", "-q"],
            cwd=str(repo),
            env=env,
            input=packed.stdout,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return unpacked.returncode == 0


def _commit_exists(repo: Path, sha: str, env: dict) -> bool:
    try:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=str(repo),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=LOCAL_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _resolve_push_endpoint(run_git, repo: Path, remote: str) -> str:
    result = _git(
        run_git,
        repo,
        ["remote", "get-url", "--push", "--all", remote],
        LOCAL_GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        _stop_failed(_transport_reason(result))
    urls = []
    for line in result.stdout.splitlines():
        if not line or any(ch.isspace() for ch in line) or line.startswith("-"):
            _stop_skipped("push destination is ambiguous")
        urls.append(line)
    if len(urls) != 1:
        _stop_skipped("push destination is ambiguous")
    return urls[0]


def _suppressed() -> bool:
    return os.environ.get(SUPPRESS_ENV) == "1"


def _attempt(repo: Path, expected_branch: str, commit_hash: str, run_git) -> PushAttempt:
    if repo_policy.read_no_automatic_git_push(repo):
        return PushAttempt.skipped("repository opts out of automatic push")
    if not _valid_commit(commit_hash):
        return PushAttempt.skipped("recorded commit hash is unusable")
    if not _valid_branch(expected_branch):
        return PushAttempt.skipped("expected branch is missing or unsafe")

    head = _git(run_git, repo, ["symbolic-ref", "--quiet", "HEAD"], LOCAL_GIT_TIMEOUT_SECONDS)
    if head.returncode != 0:
        return PushAttempt.skipped("checkout is detached")
    if head.stdout.strip() != f"refs/heads/{expected_branch}":
        return PushAttempt.skipped("checkout is not the expected branch")

    remote, remote_branch = _upstream(run_git, repo, expected_branch)
    if remote_branch != expected_branch:
        return PushAttempt.skipped("upstream branch does not match the local branch")
    _reject_redirects(run_git, repo, expected_branch, remote, remote_branch)

    local_sha = _git(run_git, repo, ["rev-parse", "--verify", "HEAD"], LOCAL_GIT_TIMEOUT_SECONDS)
    if local_sha.returncode != 0 or local_sha.stdout.strip() != commit_hash:
        return PushAttempt.skipped("HEAD does not match the recorded commit")

    remote_sha = _remote_tip(run_git, repo, remote, remote_branch)
    _require_ancestor(run_git, repo, remote_sha, commit_hash)
    _push(run_git, repo, remote, expected_branch, remote_branch)
    return PushAttempt.pushed(remote, remote_branch)


def _upstream(run_git, repo: Path, branch: str) -> tuple[str, str]:
    remote = _for_each_ref(run_git, repo, "%(upstream:remotename)", branch)
    merge = _for_each_ref(run_git, repo, "%(upstream:remoteref)", branch)
    if remote is None or merge is None:
        _stop_skipped("expected branch does not exist")
    if not remote:
        _stop_skipped("branch has no upstream")
    if remote == ".":
        _stop_skipped("upstream is local-only")
    if not _valid_remote(remote):
        _stop_skipped("upstream remote is unexpected")
    if not merge.startswith("refs/heads/"):
        _stop_skipped("upstream does not name a single remote branch")
    remote_branch = merge[len("refs/heads/"):]
    if not _valid_branch(remote_branch):
        _stop_skipped("upstream does not name a single remote branch")

    configured_remote = _config_values(run_git, repo, f"branch.{branch}.remote")
    configured_merge = _config_values(run_git, repo, f"branch.{branch}.merge")
    if configured_remote != [remote] or configured_merge != [merge]:
        _stop_skipped("upstream mapping is ambiguous")
    return remote, remote_branch


def _reject_redirects(run_git, repo: Path, branch: str, remote: str, remote_branch: str) -> None:
    push_remote = _config_values(run_git, repo, f"branch.{branch}.pushRemote")
    push_default_remote = _config_values(run_git, repo, "remote.pushDefault")
    if len(push_remote) > 1 or len(push_default_remote) > 1:
        _stop_skipped("push destination is ambiguous")
    effective = push_remote[0] if push_remote else (push_default_remote[0] if push_default_remote else remote)
    if effective != remote:
        _stop_skipped("push destination does not match the upstream remote")

    urls = _config_values(run_git, repo, f"remote.{remote}.url")
    pushurls = _config_values(run_git, repo, f"remote.{remote}.pushurl")
    if len(urls) != 1 or not urls[0].strip() or any(ch.isspace() for ch in urls[0]):
        _stop_skipped("fetch URL is missing or ambiguous")
    if len(pushurls) > 1 or any(not item.strip() or any(ch.isspace() for ch in item) for item in pushurls):
        _stop_skipped("push URL is ambiguous")

    refspecs = _config_values(run_git, repo, f"remote.{remote}.push")
    if len(refspecs) > 1 or (refspecs and not _exact_refspec(refspecs[0], branch, remote_branch)):
        _stop_skipped("configured push refspec is unexpected")

    defaults = _config_values(run_git, repo, "push.default")
    if len(defaults) > 1 or (defaults and defaults[0].lower() not in _ALLOWED_PUSH_DEFAULTS):
        _stop_skipped("push configuration could broaden the push")

    mirror = _git(
        run_git,
        repo,
        ["config", "--get", "--bool", f"remote.{remote}.mirror"],
        LOCAL_GIT_TIMEOUT_SECONDS,
    )
    if mirror.returncode == 0 and mirror.stdout.strip().lower() == "true":
        _stop_skipped("push configuration could broaden the push")
    if mirror.returncode not in (0, 1):
        _stop_failed(_transport_reason(mirror))


def _remote_tip(run_git, repo: Path, remote: str, remote_branch: str) -> str:
    # git ls-remote <remote> reads the fetch URL. The push may update a
    # different repository, so the tip has to come from the resolved push URL.
    endpoint = _resolve_push_endpoint(run_git, repo, remote)
    expected_ref = f"refs/heads/{remote_branch}"
    listed = _git(
        run_git,
        repo,
        ["ls-remote", "--heads", endpoint, expected_ref],
        NETWORK_GIT_TIMEOUT_SECONDS,
    )
    if listed.returncode != 0:
        _stop_failed(_transport_reason(listed))
    matches = []
    for line in listed.stdout.splitlines():
        if not line.strip():
            continue
        sha, separator, ref = line.partition("\t")
        if separator != "\t" or ref != expected_ref or not _valid_commit(sha):
            _stop_skipped("remote branch listing is ambiguous")
        matches.append(sha)
    if not matches:
        _stop_skipped("remote branch is absent")
    if len(matches) != 1:
        _stop_skipped("remote branch listing is ambiguous")
    sha = matches[0]
    if not _object_exists(run_git, repo, sha):
        fetched = _git(
            run_git,
            repo,
            ["fetch", "--no-tags", "--recurse-submodules=no", endpoint, expected_ref],
            NETWORK_GIT_TIMEOUT_SECONDS,
        )
        if fetched.returncode != 0:
            _stop_failed(_transport_reason(fetched))
        if not _object_exists(run_git, repo, sha):
            _stop_skipped("remote history could not be verified")
    return sha


def _require_ancestor(run_git, repo: Path, remote_sha: str, commit_hash: str) -> None:
    check = _git(
        run_git,
        repo,
        ["merge-base", "--is-ancestor", remote_sha, commit_hash],
        LOCAL_GIT_TIMEOUT_SECONDS,
    )
    if check.returncode == 0:
        return
    if check.returncode == 1:
        _stop_skipped("histories are not a fast-forward")
    _stop_skipped("remote history could not be verified")


def _push(run_git, repo: Path, remote: str, local_branch: str, remote_branch: str) -> None:
    refspec = f"refs/heads/{local_branch}:refs/heads/{remote_branch}"
    result = _git(
        run_git,
        repo,
        [
            "push",
            "--no-follow-tags",
            "--recurse-submodules=no",
            "--porcelain",
            remote,
            refspec,
        ],
        NETWORK_GIT_TIMEOUT_SECONDS,
    )
    problem = _porcelain_problem(result.stdout)
    if result.returncode != 0 or problem:
        detail = problem or "push was rejected"
        extra = sanitize_reason(result.stderr)
        if extra and extra not in detail:
            detail = f"{detail}: {extra}"
        _stop_failed(detail)


def _porcelain_problem(stdout: str) -> str | None:
    flags = []
    for line in stdout.splitlines():
        if "\t" not in line or not line:
            continue
        flags.append(line[0])
    if not flags:
        return None
    if "*" in flags:
        return "push created a remote branch"
    if "+" in flags:
        return "push applied a forced update"
    if "-" in flags:
        return "push deleted a remote ref"
    if "!" in flags:
        return "push was rejected"
    if any(flag not in {" ", "="} for flag in flags):
        return "push reported an unexpected result"
    return None


def _object_exists(run_git, repo: Path, sha: str) -> bool:
    probed = _git(
        run_git,
        repo,
        ["cat-file", "-e", f"{sha}^{{commit}}"],
        LOCAL_GIT_TIMEOUT_SECONDS,
    )
    return probed.returncode == 0


def _for_each_ref(run_git, repo: Path, fmt: str, branch: str) -> str | None:
    result = _git(
        run_git,
        repo,
        ["for-each-ref", f"--format={fmt}", f"refs/heads/{branch}"],
        LOCAL_GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        _stop_failed(_transport_reason(result))
    if result.stdout == "":
        return None
    return result.stdout.strip()


def _config_values(run_git, repo: Path, key: str) -> list[str]:
    result = _git(run_git, repo, ["config", "--get-all", key], LOCAL_GIT_TIMEOUT_SECONDS)
    if result.returncode == 1:
        return []
    if result.returncode != 0:
        _stop_failed(_transport_reason(result))
    values = [line.strip() for line in result.stdout.splitlines()]
    if any(value == "" for value in values):
        _stop_skipped("git configuration is ambiguous")
    return values


def _git(run_git, repo: Path, args: list[str], timeout: float) -> GitOutput:
    try:
        result = run_git(repo, args, timeout)
    except subprocess.TimeoutExpired as exc:
        raise _Stop(PushAttempt.failed("git command timed out")) from exc
    except OSError as exc:
        raise _Stop(PushAttempt.failed(f"could not run git: {exc}")) from exc
    if not isinstance(result, GitOutput):
        raise _Stop(PushAttempt.failed("git runner returned an unexpected result"))
    return result


def _transport_reason(result: GitOutput) -> str:
    text = sanitize_reason(f"{result.stderr}\n{result.stdout}")
    lowered = text.lower()
    markers = (
        "auth",
        "permission denied",
        "could not resolve",
        "network",
        "connection refused",
        "could not read from remote",
        "terminal prompts disabled",
        "timed out",
        "operation not permitted",
    )
    if any(marker in lowered for marker in markers):
        prefix = "authentication, network, or server error"
        return f"{prefix}: {text}" if text else prefix
    if text:
        return f"git command failed: {text}"
    return f"git command failed with exit {result.returncode}"


def _exact_refspec(spec: str, local_branch: str, remote_branch: str) -> bool:
    if spec.startswith("+") or "*" in spec or any(ch.isspace() for ch in spec):
        return False
    source, separator, dest = spec.partition(":")
    if separator != ":":
        return local_branch == remote_branch and _names_branch(spec, local_branch)
    if ":" in dest:
        return False
    return _names_branch(source, local_branch) and _names_branch(dest, remote_branch)


def _names_branch(ref: str, branch: str) -> bool:
    return ref == branch or ref == f"refs/heads/{branch}"


def _valid_commit(value: str) -> bool:
    return bool(_HASH_RE.fullmatch(value))


def _valid_branch(name: str) -> bool:
    if not name or name.startswith(("-", "/")) or name.endswith(("/", ".", ".lock")):
        return False
    if "//" in name or ".." in name or "@{" in name:
        return False
    return re.fullmatch(r"[A-Za-z0-9._+\-/]+", name) is not None


def _valid_remote(name: str) -> bool:
    if not name or name.startswith("-") or name in {".", ".."}:
        return False
    if any(ch.isspace() for ch in name) or "://" in name or "@" in name:
        return False
    return re.fullmatch(r"[A-Za-z0-9._+\-/]+", name) is not None


def _stop_skipped(reason: str) -> None:
    raise _Stop(PushAttempt.skipped(reason))


def _stop_failed(reason: str) -> None:
    raise _Stop(PushAttempt.failed(reason))
