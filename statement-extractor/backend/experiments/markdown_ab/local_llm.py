"""Local-only language-model access for the PoC (Ollama on this machine) and the guards around it.

* ``OllamaClient`` implements the application's ``LLMClient`` protocol (a ``model`` attribute and ``complete``), so it
  is injected into the existing extraction path without changing that path.
* ``verify_endpoint`` accepts only an http endpoint whose host is a loopback address.
* ``verify_model`` reads the installed model's digest from Ollama and requires it to match the pin.
* ``verify_app_isolation`` confirms, after the application settings are loaded, that external calls are off, the Groq
  key is empty, a Groq client cannot be built and the database setting points at the unused sentinel path.
* ``SocketGuard`` refuses, inside this Python process, every socket connection to a non-loopback address.
* ``NetworkMonitor`` samples the operating system's TCP table while a run is in progress and flags any connection
  from this process or the Ollama server to a non-loopback address. Sampling can miss very short connections.

Nothing here logs prompts, responses or statement text. Raw answers are kept in memory for the run folder only.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from app.services.llm_client import LLMError, LLMResponse

DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
#: Context window requested from Ollama: system prompt + schema (~2.1k tokens) + one chunk (<= ~3.5k) + answer (<= 8k).
NUM_CTX = 16384
CALL_TIMEOUT_S = 3600
#: Groq's API passes the JSON schema (with its field descriptions) to the model. Ollama's ``format`` only constrains
#: decoding, so the client shows the same schema to the model in the system message. Same text for both pipelines.
SCHEMA_NOTE = ("\n\nAnswer with one JSON object and nothing else. It must follow this JSON Schema; "
               "the descriptions explain each field:\n")


class LocalOnlyError(RuntimeError):
    """The local-only boundary could not be established or was violated."""


def is_loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address.split("%", 1)[0].strip("[]")).is_loopback
    except ValueError:
        return False


# --------------------------------------------------------------------------
# Endpoint and model identity
# --------------------------------------------------------------------------


def verify_endpoint(endpoint: str) -> dict[str, Any]:
    parts = urllib.parse.urlsplit(endpoint)
    if parts.scheme != "http":
        raise LocalOnlyError(f"endpoint scheme must be http on loopback, got {parts.scheme!r}")
    host = parts.hostname or ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise LocalOnlyError(f"endpoint host {host!r} is not a loopback address")
    port = parts.port or 11434
    resolved = sorted({info[4][0] for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)})
    if not resolved or not all(is_loopback(a) for a in resolved):
        raise LocalOnlyError(f"endpoint {host} resolves to a non-loopback address")
    base = f"http://{'[' + host + ']' if ':' in host else host}:{port}"
    return {"endpoint": base, "host": host, "port": port, "resolved": resolved, "loopback_only": True}


def _http(endpoint: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(endpoint + path, data=data, method="POST" if body is not None else "GET",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")[:200]
        except Exception:  # noqa: BLE001 - the error body is optional
            pass
        if exc.code == 404:
            raise LLMError("llm_model_unavailable", f"Ollama does not have the model ({detail})", fatal=True) from None
        if exc.code == 400 and ("format" in detail or "schema" in detail or "grammar" in detail):
            raise LLMError("llm_schema_rejected", "Ollama rejected the output schema.") from None
        raise LLMError("llm_server_error", f"Ollama answered HTTP {exc.code}.") from None
    except (TimeoutError, socket.timeout):
        raise LLMError("llm_timeout", "The local model did not answer in time.") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise LLMError("llm_timeout", "The local model did not answer in time.") from None
        raise LLMError("llm_connection", "Could not connect to the local Ollama server.") from None
    except json.JSONDecodeError:
        raise LLMError("llm_server_error", "Ollama sent a response that is not JSON.") from None


def model_identity(endpoint: str, name: str) -> dict[str, Any]:
    tags = _http(endpoint, "/api/tags")
    entry = next((m for m in tags.get("models", []) if name in (m.get("name"), m.get("model"))), None)
    if entry is None:
        raise LocalOnlyError(f"model {name} is not installed in the local Ollama")
    show = _http(endpoint, "/api/show", {"model": name})
    info = show.get("model_info") or {}
    details = entry.get("details") or {}
    return {
        "name": name,
        "digest": entry.get("digest") or "",
        "size_bytes": entry.get("size"),
        "family": details.get("family"),
        "parameter_size": details.get("parameter_size"),
        "quantization": details.get("quantization_level"),
        "format": details.get("format"),
        "context_length": next((v for k, v in info.items() if k.endswith(".context_length")), None),
        "capabilities": show.get("capabilities"),
        "remote_host": entry.get("remote_host") or show.get("remote_host"),
        "modified_at": entry.get("modified_at"),
        "ollama_version": _http(endpoint, "/api/version").get("version"),
    }


def verify_model(endpoint: str, name: str, pin: str) -> dict[str, Any]:
    """The installed model must be stored locally and its digest must start with the pin (at least 12 hex digits)."""
    identity = model_identity(endpoint, name)
    if identity["remote_host"]:
        raise LocalOnlyError(f"model {name} is served from a remote host")
    pin = (pin or "").lower()
    if len(pin) < 12 or not identity["digest"].lower().startswith(pin):
        raise LocalOnlyError(f"installed digest of {name} ({identity['digest'][:12]}) does not match the pin {pin!r}")
    identity["pin"] = pin
    identity["digest_matches_pin"] = True
    return identity


def verify_app_isolation(sentinel_db: Any) -> dict[str, Any]:
    """Checks the application settings as loaded in this process. Raises LocalOnlyError if any check fails."""
    from app.core.config import settings
    from app.services.llm_client import GroqClient

    key = settings.groq_api_key.get_secret_value() if settings.groq_api_key is not None else ""
    problems = settings.engine_problems("groq")
    try:
        GroqClient(settings)
        groq_client_blocked, groq_block_code = False, None
    except LLMError as exc:
        groq_client_blocked, groq_block_code = True, exc.code
    expected_url = f"sqlite:///{sentinel_db.as_posix()}"
    result = {
        "allow_external_llm_calls": settings.allow_external_llm_calls,
        "groq_api_key_empty": key == "",
        "groq_engine_problems": [p["code"] for p in problems],
        "groq_client_constructible": not groq_client_blocked,
        "groq_client_block_code": groq_block_code,
        "database_url_is_sentinel": settings.database_url == expected_url,
        "sentinel_db_exists": sentinel_db.exists(),
    }
    ok = (result["allow_external_llm_calls"] is False and result["groq_api_key_empty"] and groq_client_blocked
          and result["database_url_is_sentinel"] and not result["sentinel_db_exists"])
    result["ok"] = ok
    if not ok:
        raise LocalOnlyError(f"application isolation check failed: {result}")
    return result


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------


class OllamaClient:
    """``LLMClient`` for a local Ollama model: strict JSON-schema output, temperature 0, fixed seed."""

    def __init__(self, model: str, endpoint: str, *, seed: int, num_predict: int, num_ctx: int = NUM_CTX,
                 monitor: NetworkMonitor | None = None, timeout: float = CALL_TIMEOUT_S) -> None:
        self.model = model
        self.endpoint = endpoint
        self.seed = seed
        self.num_predict = num_predict
        self.num_ctx = num_ctx
        self.monitor = monitor
        self.timeout = timeout
        #: One entry per call: timing, token counts, finish reason, error code and the raw answer (kept local).
        self.calls: list[dict[str, Any]] = []

    def options(self) -> dict[str, Any]:
        return {"temperature": 0, "seed": self.seed, "num_ctx": self.num_ctx, "num_predict": self.num_predict}

    def settings(self) -> dict[str, Any]:
        return {"model": self.model, "endpoint": self.endpoint, "options": self.options(), "stream": False,
                "format": "the application's strict JSON schema", "schema_in_system_message": True,
                "timeout_s": self.timeout}

    def _check_boundary(self) -> None:
        if self.monitor is not None and self.monitor.violation:
            raise LLMError("local_only_violation", "A non-local network connection was detected; the run was stopped.",
                           fatal=True)

    def complete(self, *, system: str, user: str, schema: dict[str, Any], schema_name: str) -> LLMResponse:
        self._check_boundary()
        body = {
            "model": self.model,
            "stream": False,
            "format": schema,
            "options": self.options(),
            "keep_alive": "30m",
            "messages": [
                {"role": "system", "content": system + SCHEMA_NOTE + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))},
                {"role": "user", "content": user},
            ],
        }
        started = time.perf_counter()
        record: dict[str, Any] = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "schema_name": schema_name}
        try:
            data = _http(self.endpoint, "/api/chat", body, timeout=self.timeout)
        except LLMError as exc:
            record.update({"error_code": exc.code, "latency_ms": int((time.perf_counter() - started) * 1000)})
            self.calls.append(record)
            raise
        latency_ms = int((time.perf_counter() - started) * 1000)
        reported = data.get("model")
        content = (data.get("message") or {}).get("content")
        done_reason = data.get("done_reason")
        finish = "length" if done_reason == "length" else ("stop" if data.get("done") else done_reason)
        record.update({
            "latency_ms": latency_ms, "reported_model": reported, "done_reason": done_reason,
            "prompt_eval_count": data.get("prompt_eval_count", 0), "eval_count": data.get("eval_count", 0),
            "load_ms": int((data.get("load_duration") or 0) / 1e6),
            "prompt_eval_ms": int((data.get("prompt_eval_duration") or 0) / 1e6),
            "eval_ms": int((data.get("eval_duration") or 0) / 1e6),
            "content": content,
        })
        self.calls.append(record)
        if reported and reported != self.model:
            raise LLMError("llm_model_mismatch", "Ollama answered with a different model.", fatal=True)
        self._check_boundary()
        return LLMResponse(content=content, finish_reason=finish, model=reported or self.model,
                           input_tokens=int(data.get("prompt_eval_count") or 0),
                           output_tokens=int(data.get("eval_count") or 0), latency_ms=latency_ms)


def unload(endpoint: str, model: str) -> None:
    """Free the model's memory (used between candidate models)."""
    try:
        _http(endpoint, "/api/generate", {"model": model, "keep_alive": 0}, timeout=120)
    except LLMError:
        pass


# --------------------------------------------------------------------------
# Network guards
# --------------------------------------------------------------------------


class SocketGuard:
    """While active, any socket connection from this process to a non-loopback address raises PermissionError."""

    def __init__(self, allow_remote: Any = None) -> None:
        self.blocked: list[dict[str, Any]] = []
        self.allowed = 0
        self.allowed_remote = 0
        #: Optional callable(ip) -> bool for a remote service the user explicitly authorised (for example Groq).
        self.allow_remote = allow_remote

    def _check(self, sock: socket.socket, address: Any) -> None:
        host = address[0] if isinstance(address, tuple) else str(address)
        if not is_loopback(str(host)) and self.allow_remote is not None and self.allow_remote(str(host)):
            self.allowed_remote += 1
            return
        if not is_loopback(str(host)):
            self.blocked.append({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "family": str(sock.family), "host": str(host)})
            raise PermissionError(f"blocked a connection to non-loopback address {host}")
        self.allowed += 1

    def __enter__(self) -> SocketGuard:
        guard = self
        self._saved = (socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto)
        connect, connect_ex, sendto = self._saved

        def guarded_connect(sock, address):
            guard._check(sock, address)
            return connect(sock, address)

        def guarded_connect_ex(sock, address):
            guard._check(sock, address)
            return connect_ex(sock, address)

        def guarded_sendto(sock, data, *args):
            guard._check(sock, args[-1])
            return sendto(sock, data, *args)

        socket.socket.connect = guarded_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
        socket.socket.sendto = guarded_sendto  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc: Any) -> None:
        socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto = self._saved  # type: ignore[method-assign]

    def summary(self) -> dict[str, Any]:
        return {"active_in_process": True, "allowed_loopback_connections": self.allowed,
                "allowed_authorised_remote_connections": self.allowed_remote, "blocked": self.blocked}


_ACTIVE_STATES = {"ESTABLISHED", "SYN_SENT", "SYN_RECEIVED", "FIN_WAIT_1", "FIN_WAIT_2", "CLOSE_WAIT", "LAST_ACK", "TIME_WAIT"}


def _netstat() -> list[tuple[str, str, str, int]]:
    """(local, remote, state, pid) for every TCP connection (IPv4 and IPv6)."""
    rows: list[tuple[str, str, str, int]] = []
    for proto in ("TCP", "TCPv6"):
        try:
            out = subprocess.run(["netstat", "-ano", "-p", proto], capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 5 and parts[0] == "TCP" and parts[4].isdigit():
                rows.append((parts[1], parts[2], parts[3], int(parts[4])))
    return rows


def _host(endpoint: str) -> str:
    return endpoint.rsplit(":", 1)[0].strip("[]")


def _pids_by_image(image: str) -> set[int]:
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    pids = set()
    for line in out.splitlines():
        cells = [c.strip('"') for c in line.split('","')]
        if len(cells) > 1 and cells[1].strip('"').isdigit():
            pids.add(int(cells[1].strip('"')))
    return pids


def _tray_pids() -> set[int]:
    return _pids_by_image("ollama app.exe")


def _child_pids(parents: set[int]) -> set[int]:
    """Direct children of the given processes (Ollama starts its model runner as a child process)."""
    if not parents:
        return set()
    query = " OR ".join(f"ParentProcessId={p}" for p in sorted(parents))
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"Get-CimInstance Win32_Process -Filter '{query}' | ForEach-Object {{ $_.ProcessId }}"],
                             capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {int(x) for x in out.split() if x.isdigit()}


class NetworkMonitor(threading.Thread):
    """Samples the TCP table every ``interval`` seconds. A non-loopback peer of this process or of any Ollama inference
    process (the server listening on the port, every ollama.exe and llama-server.exe, and the server's child processes,
    refreshed during the run because the model runner starts on demand) is a violation. A peer of the Ollama tray
    application (update checks, unrelated to inference) is recorded as background."""

    def __init__(self, ollama_port: int = 11434, interval: float = 2.0) -> None:
        super().__init__(daemon=True, name="network-monitor")
        self.interval = interval
        self.own_pid = os.getpid()
        self.ollama_port = ollama_port
        self.server_pids = self._server_pids()
        self.tray_pids = _tray_pids()
        self.ollama_pids: set[int] = set()
        self._refresh_family(children=True)
        self.samples = 0
        self.violations: list[dict[str, Any]] = []
        self.background: list[dict[str, Any]] = []
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._halt = threading.Event()

    def _server_pids(self) -> set[int]:
        return {pid for local, _remote, state, pid in _netstat()
                if state == "LISTENING" and local.rsplit(":", 1)[-1] == str(self.ollama_port) and is_loopback(_host(local))}

    def _refresh_family(self, children: bool) -> None:
        family = set(self.server_pids) | _pids_by_image("ollama.exe") | _pids_by_image("llama-server.exe")
        if children:
            family |= _child_pids(family)
        self.ollama_pids |= family - self.tray_pids

    @property
    def violation(self) -> bool:
        return bool(self.violations)

    def sample(self) -> None:
        self.samples += 1
        self._refresh_family(children=self.samples % 15 == 1)
        for local, remote, state, pid in _netstat():
            if state not in _ACTIVE_STATES:
                continue
            remote_host = _host(remote)
            if remote_host in ("0.0.0.0", "::", "*") or is_loopback(remote_host):
                continue
            entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "pid": pid, "remote": remote, "state": state}
            if pid == self.own_pid or pid in self.ollama_pids:
                entry["process"] = "experiment" if pid == self.own_pid else "ollama-inference"
                if entry not in self.violations:
                    self.violations.append(entry)
            elif pid in self.tray_pids:
                entry["process"] = "ollama-tray-app"
                if len(self.background) < 50 and entry not in self.background:
                    self.background.append(entry)

    def run(self) -> None:
        while not self._halt.is_set():
            self.sample()
            self._halt.wait(self.interval)

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=60)
        self.sample()  # one last look

    def summary(self) -> dict[str, Any]:
        return {
            "method": "netstat -ano sampling every %.0f s (can miss connections shorter than the interval)" % self.interval,
            "started": self.started_at, "samples": self.samples,
            "experiment_pid": self.own_pid, "ollama_server_pids": sorted(self.server_pids),
            "ollama_inference_pids_monitored": sorted(self.ollama_pids),
            "ollama_tray_pids": sorted(self.tray_pids),
            "violations": self.violations, "background_tray_connections": self.background,
            "ok": not self.violations and bool(self.server_pids),
        }


class FileAccessGuard:
    """A Python audit hook that blocks (PermissionError) and records any attempt by this process to open a protected
    file or anything inside a protected folder: the live statement database with its WAL/SHM files, the application
    database sentinel, the confidential statements and the uploads. Audit hooks cannot be removed once installed."""

    def __init__(self, files: list[Any], folders: list[Any]) -> None:
        self.files = {os.path.normcase(os.path.abspath(str(f))) for f in files}
        self.folders = [os.path.normcase(os.path.abspath(str(d))).rstrip(os.sep + "/") + os.sep for d in folders]
        self.blocked: list[dict[str, Any]] = []
        self.installed = False

    def _hook(self, event: str, args: tuple) -> None:
        if event not in ("open", "sqlite3.connect") or not args:
            return
        target = args[0]
        if not isinstance(target, (str, bytes, os.PathLike)):
            return
        try:
            path = os.path.normcase(os.path.abspath(os.fsdecode(target)))
        except (TypeError, ValueError):
            return
        if path in self.files or any(path.startswith(folder) for folder in self.folders):
            self.blocked.append({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event,
                                 "target": os.path.basename(path)})
            raise PermissionError("this experiment may not open protected statement data")

    def install(self) -> FileAccessGuard:
        import sys

        sys.addaudithook(self._hook)
        self.installed = True
        return self

    def summary(self) -> dict[str, Any]:
        return {"installed": self.installed, "protected_files": len(self.files),
                "protected_folders": [os.path.basename(f.rstrip(os.sep + "/")) for f in self.folders], "blocked": self.blocked}
