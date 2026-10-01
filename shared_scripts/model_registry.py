"""Explicit, bounded CLI model discovery and per-work-repository last-good cache."""
from __future__ import annotations

import datetime as dt
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import subprocess
import tempfile
import time

PROVIDERS = ("codex", "claude", "cursor", "kilo", "antigravity")
EXECUTABLES = {"codex": "codex", "claude": "claude", "cursor": "agent", "kilo": "kilo", "antigravity": "agy"}
CACHE = Path(".kanban-orchestra/model-cache.json")
TIMEOUT = 12
FRESH_DAYS = 7


def scope(provider: str) -> str:
    # Hash context, including credentials, without recording their values.
    names = {"codex": ("CODEX_HOME", "OPENAI_API_KEY"), "claude": ("CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY"),
             "cursor": ("CURSOR_CONFIG_DIR", "CURSOR_API_KEY"), "kilo": ("KILO_API_KEY",),
             "antigravity": ("GOOGLE_API_KEY",)}[provider]
    context = [str(os.getuid()), str(Path.home().resolve())]
    context.extend(f"{name}={os.environ.get(name, '')}" for name in names)
    auth_files = {
        "codex": [Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"],
        "claude": [Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / ".credentials.json"],
        "cursor": [Path.home() / "Library/Keychains/login.keychain-db"],
        "kilo": [Path.home() / ".config/kilo/auth.json"],
        "antigravity": [Path.home() / ".config/agy/auth.json"],
    }
    for path in auth_files[provider]:
        try:
            info = path.stat()
            context.append(f"{path}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}")
        except OSError:
            context.append(f"{path}:missing")
    return hashlib.sha256("\0".join(context).encode()).hexdigest()[:24]


def _check_model(model):
    if not isinstance(model, dict) or not isinstance(model.get("id"), str) or not model["id"].strip():
        raise ValueError("model missing an ID")
    if any(c in model["id"] for c in "\x00\r\n"):
        raise ValueError("invalid model ID")
    if not isinstance(model.get("label"), str) or not model["label"].strip():
        raise ValueError("model missing a label")
    if not isinstance(model.get("capabilities", {}), dict):
        raise ValueError("invalid model capabilities")


def _validated(models):
    if not isinstance(models, list) or not models:
        raise ValueError("empty model listing")
    seen = set()
    for model in models:
        _check_model(model)
        if model["id"] in seen:
            raise ValueError("duplicate model ID")
        seen.add(model["id"])
    return models


def read(root: Path):
    path = Path(root) / CACHE
    if not path.exists():
        return {"version": 1, "providers": {}}, "cache missing; run ko-task models refresh"
    try:
        data = json.loads(path.read_text())
        if (not isinstance(data, dict) or data.get("version") != 1
                or not isinstance(data.get("providers"), dict)
                or not isinstance(data.get("diagnostics", {}), dict)):
            raise ValueError("invalid cache version or structure")
        for name, entry in data["providers"].items():
            if name not in PROVIDERS or not isinstance(entry, dict) or not isinstance(entry.get("scope"), str):
                raise ValueError("invalid provider entry")
            if "models" in entry:
                _validated(entry["models"])
        return data, None
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"version": 1, "providers": {}}, f"cache corrupt ({exc}); run ko-task models refresh"


def view(root: Path, provider: str):
    data, diagnostic = read(root)
    entry = data["providers"].get(provider, {})
    if entry and entry.get("scope") != scope(provider):
        diagnostic = data.get("diagnostics", {}).get(provider, {})
        reason = diagnostic.get("reason") if diagnostic.get("scope") == scope(provider) else None
        return {"state": "unavailable", "reason": reason or "authentication/configuration scope changed; refresh",
                "models": [], "last_success": None}
    if not entry:
        return {"state": "unavailable", "reason": diagnostic or "never refreshed; run ko-task models refresh", "models": [], "last_success": None}
    if not shutil.which(EXECUTABLES[provider]):
        reason = f"{EXECUTABLES[provider]} executable missing"
        if entry.get("models"):
            reason += "; cached list retained"
        return {"state": "unavailable", "reason": reason,
                "models": entry.get("models", []), "last_success": entry.get("last_success"),
                "last_attempt": entry.get("last_attempt")}
    state, reason = entry.get("state", "stale"), entry.get("reason")
    if state == "fresh":
        try:
            age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(entry["last_success"])
            if age > dt.timedelta(days=FRESH_DAYS):
                state, reason = "stale", f"last refresh older than {FRESH_DAYS} days; refresh"
        except (KeyError, ValueError, TypeError):
            state, reason = "stale", "invalid refresh timestamp; refresh"
    return {"state": state, "reason": reason, "models": entry.get("models", []),
            "last_success": entry.get("last_success"), "last_attempt": entry.get("last_attempt")}


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _store(root, provider, models, reason):
    root = Path(root)
    path = root / CACHE
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    with lock.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        data, diagnostic = read(root)
        if diagnostic and path.exists():
            # Preserve a corrupt file for diagnosis; only a successful refresh can replace it.
            if models is None:
                return
        old = data["providers"].get(provider, {})
        current_scope = scope(provider)
        if old and old.get("scope") != current_scope and models is None:
            data.setdefault("diagnostics", {})[provider] = {"scope": current_scope,
                                                              "reason": reason, "last_attempt": _now()}
            # Keep the previous account's last-good data until a successful refresh.
        else:
            if old.get("scope") != current_scope:
                old = {}
            entry = {**old, "scope": current_scope, "last_attempt": _now()}
            if models is not None:
                entry.update(models=_validated(models), state="fresh", reason=None, last_success=entry["last_attempt"])
                data.get("diagnostics", {}).pop(provider, None)
            else:
                entry.update(state="stale" if old.get("models") else "unavailable", reason=reason)
            data["providers"][provider] = entry
        fd, temporary = tempfile.mkstemp(prefix=".model-cache-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(data, output, indent=2, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _stop(proc):
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=1)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
    for pipe in (proc.stdin, proc.stdout):
        if pipe:
            pipe.close()


def _lines(command, messages=(), timeout=TIMEOUT):
    """Read protocol lines with one deadline; terminate and reap every child."""
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryFile(mode="w+t") as errors:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=errors, start_new_session=True)
        try:
            for message in messages:
                proc.stdin.write((json.dumps(message) + "\n").encode())
                proc.stdin.flush()
            proc.stdin.close()
            buffer = b""
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("model discovery timed out")
                ready, _, _ = select.select([proc.stdout], [], [], remaining)
                if not ready:
                    raise TimeoutError("model discovery timed out")
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    if buffer.strip():
                        yield buffer.decode(errors="replace")
                    errors.seek(0)
                    message = errors.read().strip()
                    try:
                        status = proc.wait(timeout=max(.01, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired as exc:
                        raise TimeoutError("model discovery timed out") from exc
                    if status:
                        raise RuntimeError(message[:300] or f"command exited {proc.returncode}")
                    return
                buffer += chunk
                if len(buffer) > 8_000_000:
                    raise ValueError("model listing too large")
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.strip():
                        yield line.decode(errors="replace")
        finally:
            _stop(proc)


def _model(item, *, id_key="id"):
    if not isinstance(item, dict):
        raise ValueError("malformed model entry")
    identifier = item.get(id_key)
    label = item.get("displayName") or item.get("name") or item.get("label") or identifier
    capabilities = {k: v for k, v in item.items() if k not in {id_key, "displayName", "name", "label"}}
    return {"id": identifier, "label": label, "capabilities": capabilities}


def _codex(timeout):
    deadline = time.monotonic() + timeout
    # A single app-server process handles all pages. Send each request only after its predecessor.
    with tempfile.TemporaryFile(mode="w+t") as errors:
        proc = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=errors, start_new_session=True)
        buffer = b""
        def request(message):
            nonlocal buffer
            proc.stdin.write((json.dumps(message) + "\n").encode()); proc.stdin.flush()
            while True:
                if b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    if not raw.strip():
                        continue
                    event = json.loads(raw)
                    if event.get("id") == message["id"]:
                        if "error" in event:
                            raise RuntimeError(str(event["error"])[:300])
                        return event.get("result")
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([proc.stdout], [], [], remaining)[0]:
                    raise TimeoutError("Codex model discovery timed out")
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    errors.seek(0)
                    raise RuntimeError(errors.read().strip()[:300] or "incomplete Codex response")
                buffer += chunk
                if len(buffer) > 8_000_000:
                    raise ValueError("Codex response too large")
        try:
            request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "kanban-orchestra", "version": "1"}, "capabilities": {}}})
            proc.stdin.write(b'{"jsonrpc":"2.0","method":"initialized","params":{}}\n'); proc.stdin.flush()
            models, cursor, seen = [], None, set()
            for page in range(100):
                result = request({"jsonrpc": "2.0", "id": page + 2, "method": "model/list", "params": {"cursor": cursor} if cursor else {}})
                if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                    raise ValueError("malformed Codex model page")
                models.extend(_model(item) for item in result["data"])
                cursor = result.get("nextCursor")
                if cursor is None:
                    return _validated(models)
                if not isinstance(cursor, str) or not cursor or cursor in seen:
                    raise ValueError("invalid Codex pagination cursor")
                seen.add(cursor)
            raise ValueError("incomplete Codex pagination")
        finally:
            _stop(proc)


def _claude(timeout):
    message = {"type": "control_request", "request_id": "models", "request": {"subtype": "initialize"}}
    command = ["claude", "--print", "--verbose", "--input-format", "stream-json", "--output-format", "stream-json"]
    with closing(_lines(command, [message], timeout)) as lines:
        for line in lines:
            event = json.loads(line)
            envelope = event.get("response", {})
            if event.get("type") == "control_response" and isinstance(envelope, dict) and envelope.get("request_id") == "models":
                response = envelope.get("response", {})
                if not isinstance(response, dict) or not isinstance(response.get("models"), list):
                    raise ValueError("malformed Claude initialization")
                return _validated([_model(item, id_key="value") for item in response["models"]])
    raise ValueError("incomplete Claude initialization")


def _listing(provider, timeout):
    command = {"cursor": ["agent", "--list-models"], "kilo": ["kilo", "models"],
               "antigravity": ["agy", "models"]}[provider]
    lines = list(_lines(command, timeout=timeout))
    body = "\n".join(lines).strip()
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        parsed = parsed.get("models")
    if isinstance(parsed, list):
        return _validated([_model(item) if isinstance(item, dict) else {"id": item, "label": item, "capabilities": {}} for item in parsed])
    models = []
    for line in lines:
        stripped = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
        if not stripped or stripped.lower().startswith(("available models", "model id", "provider")):
            continue
        if re.match(r"(?i)^(error|unknown|unsupported|unauthorized|please|login|log in|authentication|failed|no models?|none|not found|unable|cannot|could not)\b", stripped):
            raise RuntimeError(stripped[:300])
        # Labels need a column delimiter; ordinary diagnostic prose is not a model row.
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._/@:-]*)(?:\s{2,}|\s+-\s+)(.*)$", stripped)
        if match:
            models.append({"id": match[1], "label": match[2].strip() or match[1], "capabilities": {}})
        elif re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:-]*", stripped):
            models.append({"id": stripped, "label": stripped, "capabilities": {}})
        else:
            raise ValueError(f"malformed {provider} listing")
    return _validated(models)


def refresh(root: Path, provider: str, timeout=TIMEOUT):
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    root = Path(root)
    (root / CACHE).parent.mkdir(parents=True, exist_ok=True)
    lock = root / CACHE.parent / f"model-cache-{provider}.lock"
    with lock.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            if not shutil.which(EXECUTABLES[provider]):
                raise FileNotFoundError(f"{EXECUTABLES[provider]} executable missing")
            models = _codex(timeout) if provider == "codex" else _claude(timeout) if provider == "claude" else _listing(provider, timeout)
            _store(root, provider, models, None)
        except (FileNotFoundError, TimeoutError, ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
            reason = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))
            _store(root, provider, None, " ".join(reason.split())[:300])
    return view(root, provider)
