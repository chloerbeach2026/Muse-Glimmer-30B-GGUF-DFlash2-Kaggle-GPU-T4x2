#!/usr/bin/env python3
"""Loopback-only Bearer-auth reverse proxy for Muse-Glimmer external ingress.

Production-demo gateway: enforces the exact public contract
(allowlist, limits, single admission slot, token-bucket rate limit,
timeouts), preserves SSE streaming, and emits opaque request ids with a
structured ``muse_*`` error envelope while never logging or persisting
bearer tokens, prompts, completions, reasoning, or raw bodies.
"""
from __future__ import annotations

import argparse
import hmac
import http.client
import http.server
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
from typing import Iterable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo_policy as P  # noqa: E402

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "proxy-connection",
}
SENSITIVE_FORWARD_HEADERS = {"authorization", "proxy-authorization"}
CORS_RESPONSE_PREFIX = "access-control-"
READY_PUBLIC_PATH = "/ready"
READY_BACKEND_PATH = "/health"
MODELS_PUBLIC_PATH = "/v1/models"
MUSE_MODEL_ID = "muse-glimmer-30B"


def valid_secret(secret: str) -> bool:
    return len(secret) >= 32 and all(ord(ch) >= 32 and ord(ch) != 127 for ch in secret)


def json_error(
    handler: http.server.BaseHTTPRequestHandler,
    status: int,
    error_body: dict,
    *,
    authenticate: bool = False,
    extra_headers: Iterable[tuple[str, str]] = (),
) -> None:
    payload = json.dumps(error_body, separators=(",", ":")).encode() + b"\n"
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    if authenticate:
        handler.send_header("WWW-Authenticate", "Bearer")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Connection", "close")
    for key, value in extra_headers:
        handler.send_header(key, value)
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(payload)
    handler.close_connection = True


def unauthorized(handler: http.server.BaseHTTPRequestHandler, invalid: bool = False) -> None:
    req_id = P.new_request_id()
    if getattr(handler.server, "telemetry", None) is not None:
        handler.server.telemetry.note_request(req_id, 401)
    code = P.ERROR_AUTH_INVALID if invalid else P.ERROR_AUTH_REQUIRED
    msg = "Invalid bearer token." if invalid else "A valid bearer token is required."
    error_body = P.build_error_body(
        message=msg, type_="auth_error", code=code, request_id=req_id,
    )
    json_error(handler, 401, error_body, authenticate=True)


def safe_request_headers(headers: Iterable[tuple[str, str]], backend_host: str, backend_port: int) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers:
        lower = key.lower()
        # Content-Length is reconstructed from the exact body forwarded below.
        # Strip it case-insensitively to prevent lower-case duplicate header variants.
        if lower in HOP_BY_HOP or lower in SENSITIVE_FORWARD_HEADERS or lower in {"host", "content-length"}:
            continue
        out[key] = value
    out["Host"] = f"{backend_host}:{backend_port}"
    out["Connection"] = "close"
    return out


class DemoTelemetry:
    """Thread-safe sanitized counter/telemetry surface for the demo gateway."""

    def __init__(self, telemetry_file: str | None = None) -> None:
        self._lock = threading.Lock()
        self.telemetry_file = telemetry_file
        self.snapshot = P.new_telemetry_snapshot()

    def _flush_locked(self) -> None:
        if not self.telemetry_file:
            return
        path = self.telemetry_file
        data = json.dumps(self.snapshot, sort_keys=True) + "\n"
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def snapshot_json(self) -> str:
        with self._lock:
            return json.dumps(self.snapshot, sort_keys=True) + "\n"

    def note_request(self, request_id: str, status: int) -> None:
        with self._lock:
            s = self.snapshot
            s["requests_total"] += 1
            s["last_request_id"] = request_id
            s["last_http_status"] = status
            s["last_finish_class"] = "rejected"
            self._flush_locked()

    def note_admitted(self, request_id: str) -> None:
        with self._lock:
            s = self.snapshot
            s["requests_total"] += 1
            s["inference_admitted_total"] += 1
            s["last_request_id"] = request_id
            s["active_inference"] = True
            self._flush_locked()

    def note_validation_error(self, request_id: str, status: int = 400) -> None:
        """Record rejected client payloads separately from backend failures."""
        with self._lock:
            s = self.snapshot
            s["requests_total"] += 1
            s["validation_error_total"] += 1
            s["last_request_id"] = request_id
            s["last_http_status"] = status
            s["last_finish_class"] = "validation_error"
            self._flush_locked()

    def note_completed(self, status: int, duration_ms: float, ttft_ms: float, finish: str) -> None:
        with self._lock:
            s = self.snapshot
            s["active_inference"] = False
            s["last_http_status"] = status
            s["last_request_duration_ms"] = round(duration_ms, 1) if duration_ms is not None else None
            s["last_ttft_ms"] = round(ttft_ms, 1) if ttft_ms is not None else None
            s["last_finish_class"] = finish
            self._flush_locked()

    def note_busy(self, request_id: str) -> None:
        with self._lock:
            s = self.snapshot
            s["requests_total"] += 1
            s["busy_rejected_total"] += 1
            s["last_request_id"] = request_id
            s["last_http_status"] = 429
            s["last_finish_class"] = "busy"
            self._flush_locked()

    def note_rate_limited(self, request_id: str) -> None:
        with self._lock:
            s = self.snapshot
            s["requests_total"] += 1
            s["rate_limited_total"] += 1
            s["last_request_id"] = request_id
            s["last_http_status"] = 429
            s["last_finish_class"] = "rate_limited"
            self._flush_locked()

    def note_timeout(self, request_id: str) -> None:
        with self._lock:
            s = self.snapshot
            s["timeout_total"] += 1
            s["last_request_id"] = request_id
            s["last_finish_class"] = "timeout"
            self._flush_locked()

    def note_backend_error(self) -> None:
        with self._lock:
            self.snapshot["backend_error_total"] += 1
            self._flush_locked()


class ProxyServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, address, handler_cls, *, token: str, backend_host: str, backend_port: int,
        timeout: float, max_request_bytes: int, idle_timeout: float, hard_deadline: float,
        telemetry_file: str | None = None, queue_size: int = P.MAX_QUEUED_INFERENCE,
        queue_wait_seconds: float = P.QUEUE_WAIT_SECONDS,
        effective_context_size: int = P.MAX_TOKENS, max_output_tokens: int | None = None,
    ):
        super().__init__(address, handler_cls)
        self.auth_token = token
        self.backend_host = backend_host
        self.backend_port = backend_port
        self.backend_timeout = timeout
        self.max_request_bytes = max_request_bytes
        self.idle_timeout = idle_timeout
        self.hard_deadline = hard_deadline
        self.queue_size = max(0, int(queue_size))
        self.queue_wait_seconds = max(0.0, float(queue_wait_seconds))
        self.effective_context_size = max(1, int(effective_context_size))
        requested_output_cap = self.effective_context_size if max_output_tokens is None else int(max_output_tokens)
        self.max_output_tokens = max(1, min(P.MAX_TOKENS, self.effective_context_size, requested_output_cap))
        self.admission = P.AdmissionSlot(max_queued=self.queue_size, wait_seconds=self.queue_wait_seconds)
        self.rate_bucket = P.TokenBucket(capacity=P.RATE_CAPACITY, refill_per_sec=P.RATE_REFILL_PER_SEC)
        self.telemetry = DemoTelemetry(telemetry_file=telemetry_file)


class AuthProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MuseExternalProxy/1.0.0"
    sys_version = ""

    def log_message(self, *_args) -> None:
        # Do not log headers/query strings; they may contain user data or credentials.
        return

    def _authorized(self) -> bool:
        value = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not value.startswith(prefix):
            return False
        supplied = value[len(prefix):]
        return hmac.compare_digest(supplied, self.server.auth_token)

    def _reject_json(self, status: int, error_body: dict, *, authenticate: bool = False,
                     extra_headers: Iterable[tuple[str, str]] = ()) -> None:
        json_error(self, status, error_body, authenticate=authenticate, extra_headers=extra_headers)

    def _serve_ready(self, request_id: str, started: float, admitted_here: bool = False) -> None:
        """Serve gateway readiness by probing the real backend health route.

        llama-server exposes ``/health`` but not the Muse public ``/ready``
        contract.  Never forward the public readiness path verbatim and never
        expose the backend response body.

        Note: ``admitted_here`` is always False for GET /ready (it is a
        non-inference route), but the parameter is accepted for contract
        symmetry with the inference path.
        """

        backend_host = self.server.backend_host
        backend_port = self.server.backend_port
        conn = http.client.HTTPConnection(backend_host, backend_port, timeout=self.server.backend_timeout)
        try:
            try:
                conn.request(
                    "GET",
                    READY_BACKEND_PATH,
                    headers={
                        "Host": f"{backend_host}:{backend_port}",
                        "Connection": "close",
                    },
                )
                response = conn.getresponse()
            except (OSError, http.client.HTTPException, socket.timeout):
                self.server.telemetry.note_backend_error()
                self.server.telemetry.note_completed(503, (time.monotonic() - started) * 1000, None, "backend_unavailable")
                self._reject_json(503, P.build_error_body(
                    "Backend unavailable.", "server_error", P.ERROR_BACKEND_UNAVAILABLE,
                    request_id=request_id))
                return

            if not (200 <= response.status < 300):
                self.server.telemetry.note_backend_error()
                self.server.telemetry.note_completed(503, (time.monotonic() - started) * 1000, None, "backend_unavailable")
                self._reject_json(503, P.build_error_body(
                    "Backend unavailable.", "server_error", P.ERROR_BACKEND_UNAVAILABLE,
                    request_id=request_id))
                return

            payload = b'{"status":"ready"}\n'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True
            self.server.telemetry.note_completed(200, (time.monotonic() - started) * 1000, None, "done")
        finally:
            conn.close()

    def _serve_models(self, request_id: str, started: float) -> None:
        """Serve ``GET /v1/models`` with the gateway's effective limits.

        Instead of forwarding the backend's raw ``/v1/models`` response (which
        does not carry the effective Context/output budget the gateway is
        actually enforcing), the gateway returns a single canonical model
        entry that exposes the effective Context, the max output tokens, the
        default reasoning strength, and the supported capabilities. This lets
        OpenCode IDE / Hermes Agent / Claude Code + OmniRoute configure their
        local Context/output limits without having to send a probe request
        first or parse the ``X-Muse-*`` response headers on every call.
        """
        effective_context = self.server.effective_context_size
        max_output = self.server.max_output_tokens
        body = {
            "object": "list",
            "data": [
                {
                    "id": MUSE_MODEL_ID,
                    "object": "model",
                    "created": 0,
                    "owned_by": "muse-glimmer",
                    # Non-standard but OpenAI-compatible extensions that
                    # carry the runtime-effective limits the gateway clamps
                    # client requests to.
                    "context_window": effective_context,
                    "max_output_tokens": max_output,
                    "reasoning_strength_default": P.DEFAULT_REASONING_STRENGTH,
                    "capabilities": ["tool_calls", "streaming", "vision", "multi_turn"],
                    # Hints for agent harnesses that want to auto-tune.
                    "config": {
                        "parallel_slots": 1,
                        "admission_queue_size": self.server.queue_size,
                        "idle_timeout_seconds": self.server.idle_timeout,
                        "hard_deadline_seconds": self.server.hard_deadline,
                    },
                }
            ],
        }
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8") + b"\n"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Muse-Effective-Context-Size", str(effective_context))
        self.send_header("X-Muse-Max-Output-Tokens", str(max_output))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
        self.close_connection = True
        self.server.telemetry.note_completed(200, (time.monotonic() - started) * 1000, None, "done")

    def _proxy(self) -> None:
        if not self._authorized():
            supplied = self.headers.get("Authorization", "")
            unauthorized(self, invalid=supplied.startswith("Bearer "))
            return

        try:
            target = urllib.parse.urlsplit(self.path)
        except ValueError:
            self._reject_json(400, P.build_error_body(
                "Invalid request target.", "invalid_request_error", P.ERROR_INVALID_REQUEST))
            return
        if target.scheme or target.netloc or target.fragment:
            self._reject_json(400, P.build_error_body(
                "Absolute-form request targets are rejected.", "invalid_request_error",
                P.ERROR_INVALID_REQUEST))
            return
        request_target = target.path + (("?" + target.query) if target.query else "")

        decision = P.route_allows(self.command, target.path)
        if decision == 404:
            self._reject_json(404, P.build_error_body(
                "Endpoint not exposed.", "invalid_request_error", P.ERROR_ENDPOINT_NOT_FOUND))
            return
        if decision == 405:
            self._reject_json(405, P.build_error_body(
                "Method not allowed on this endpoint.", "invalid_request_error",
                P.ERROR_METHOD_NOT_ALLOWED))
            return

        transfer_encodings = self.headers.get_all("Transfer-Encoding", [])
        if any(value.strip() for value in transfer_encodings):
            self._reject_json(400, P.build_error_body(
                "Transfer-Encoding request bodies are not supported.", "invalid_request_error",
                P.ERROR_INVALID_REQUEST))
            return

        content_lengths = self.headers.get_all("Content-Length", [])
        if len(content_lengths) > 1:
            self._reject_json(400, P.build_error_body(
                "Multiple Content-Length headers are not supported.", "invalid_request_error",
                P.ERROR_INVALID_REQUEST))
            return
        length_header = content_lengths[0] if content_lengths else "0"
        try:
            length = int(length_header or "0")
        except ValueError:
            self._reject_json(400, P.build_error_body(
                "Invalid Content-Length.", "invalid_request_error", P.ERROR_INVALID_REQUEST))
            return
        if length < 0:
            self._reject_json(400, P.build_error_body(
                "Invalid Content-Length.", "invalid_request_error", P.ERROR_INVALID_REQUEST))
            return
        if length > self.server.max_request_bytes:
            self._reject_json(413, P.build_error_body(
                "Request body exceeds the allowed limit.", "invalid_request_error",
                P.ERROR_PAYLOAD_TOO_LARGE))
            return
        body = self.rfile.read(length) if length else None

        request_id = P.new_request_id()
        admitted_here = False
        started = time.monotonic()
        ttft_ms = None
        inference = P.is_inference_route(self.command, target.path)

        if inference:
            if not self.server.admission.try_acquire(wait_seconds=self.server.queue_wait_seconds):
                self.server.telemetry.note_busy(request_id)
                self._reject_json(429, P.build_error_body(
                    "The inference queue is full or its wait deadline expired; retry after the indicated delay.",
                    "invalid_request_error", P.ERROR_BUSY, request_id=request_id),
                    extra_headers=[("Retry-After", P.BUSY_RETRY_AFTER)])
                return
            admitted_here = True
            if not self.server.rate_bucket.try_consume():
                self.server.admission.release()
                self.server.telemetry.note_rate_limited(request_id)
                self._reject_json(429, P.build_error_body(
                    "Rate limit exceeded; try again later.",
                    "invalid_request_error", P.ERROR_RATE_LIMITED, request_id=request_id),
                    extra_headers=[("Retry-After", "10")])
                return

        # For non-inference public routes, allocate a request id and record telemetry.
        if not admitted_here:
            self.server.telemetry.note_request(request_id, self.command == "HEAD" and 200 or None)

        if self.command == "GET" and target.path == READY_PUBLIC_PATH:
            self._serve_ready(request_id, started, admitted_here)
            return

        # GET /v1/models is served by the gateway so the effective Context/output
        # budget and agent-facing metadata are visible without parsing headers.
        if self.command == "GET" and target.path == MODELS_PUBLIC_PATH:
            self._serve_models(request_id, started)
            return

        # Validate JSON payload for chat/completions before forwarding.
        if inference:
            try:
                payload = json.loads((body or b"").decode("utf-8", "strict"))
            except (ValueError, UnicodeDecodeError):
                if admitted_here:
                    self.server.admission.release()
                self.server.telemetry.note_validation_error(request_id, 400)
                self._reject_json(400, P.build_error_body(
                    "Request body must be valid JSON.", "invalid_request_error",
                    P.ERROR_INVALID_PAYLOAD, request_id=request_id))
                return
            ok, err = P.validate_payload(payload)
            if not ok:
                if admitted_here:
                    self.server.admission.release()
                self.server.telemetry.note_validation_error(request_id, 400)
                err["request_id"] = request_id
                self._reject_json(400, err)
                return

            # Normalize modern OpenAI SDK naming to llama.cpp's chat-completions shape.
            modern_limit = payload.pop("max_completion_tokens", None)
            legacy_limit = payload.get("max_tokens")
            if modern_limit is not None and legacy_limit is not None and legacy_limit != modern_limit:
                if admitted_here:
                    self.server.admission.release()
                self.server.telemetry.note_validation_error(request_id, 400)
                self._reject_json(400, P.build_error_body(
                    "'max_tokens' and 'max_completion_tokens' must match when both are supplied.",
                    "invalid_request_error", P.ERROR_INVALID_MAX_TOKENS, request_id=request_id))
                return

            requested_limit = modern_limit if modern_limit is not None else legacy_limit
            # Clients often carry a static 128K limit, but adaptive GPU startup may have
            # selected a smaller actual context. Clamp rather than fail a valid agent request.
            if requested_limit is None:
                effective_limit = self.server.max_output_tokens
            else:
                effective_limit = min(int(requested_limit), self.server.max_output_tokens)
            payload["max_tokens"] = effective_limit

            # Apply the model's documented reasoning default consistently to all clients.
            chat_kwargs = payload.get("chat_template_kwargs")
            if chat_kwargs is None:
                chat_kwargs = {}
            if not isinstance(chat_kwargs, dict):
                if admitted_here:
                    self.server.admission.release()
                self.server.telemetry.note_validation_error(request_id, 400)
                self._reject_json(400, P.build_error_body(
                    "'chat_template_kwargs' must be an object.",
                    "invalid_request_error", P.ERROR_INVALID_PAYLOAD, request_id=request_id))
                return
            reasoning = chat_kwargs.get("reasoning_strength", P.DEFAULT_REASONING_STRENGTH)
            if reasoning not in {"low", "medium", "high"}:
                if admitted_here:
                    self.server.admission.release()
                self.server.telemetry.note_validation_error(request_id, 400)
                self._reject_json(400, P.build_error_body(
                    "'reasoning_strength' must be low, medium, or high.",
                    "invalid_request_error", P.ERROR_INVALID_PAYLOAD, request_id=request_id))
                return
            chat_kwargs["reasoning_strength"] = reasoning
            payload["chat_template_kwargs"] = chat_kwargs

            # Normalize permissive client SDKs that serialize tool arguments as objects.
            # Chat Completions wire format expects function.arguments to be a JSON string.
            for message in payload["messages"]:
                calls = message.get("tool_calls") if isinstance(message, dict) else None
                if not isinstance(calls, list):
                    continue
                for call in calls:
                    function = call.get("function") if isinstance(call, dict) else None
                    if not isinstance(function, dict):
                        continue
                    arguments = function.get("arguments")
                    if isinstance(arguments, (dict, list)):
                        function["arguments"] = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))

            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            # Record admission only after all validation passes and the request
            # is about to be forwarded to the backend.
            self.server.telemetry.note_admitted(request_id)

        backend_host = self.server.backend_host
        backend_port = self.server.backend_port
        timeout = self.server.backend_timeout
        conn = http.client.HTTPConnection(backend_host, backend_port, timeout=timeout)
        deadline = started + self.server.hard_deadline
        try:
            try:
                forward_headers = safe_request_headers(self.headers.items(), backend_host, backend_port)
                if body is not None:
                    forward_headers["Content-Length"] = str(len(body))
                conn.request(
                    self.command,
                    request_target,
                    body=body,
                    headers=forward_headers,
                )
            except socket.timeout:
                if admitted_here:
                    self.server.admission.release()
                self.server.telemetry.note_timeout(request_id)
                self.server.telemetry.note_completed(504, (time.monotonic() - started) * 1000, ttft_ms, "timeout")
                self._reject_json(504, P.build_error_body(
                    "Backend request timed out.", "server_error", P.ERROR_REQUEST_TIMEOUT,
                    request_id=request_id))
                return
            except (OSError, http.client.HTTPException) as exc:
                if admitted_here:
                    self.server.admission.release()
                self.server.telemetry.note_backend_error()
                self.server.telemetry.note_completed(502, (time.monotonic() - started) * 1000, ttft_ms, "backend_unavailable")
                self._reject_json(502, P.build_error_body(
                    "Backend unavailable.", "server_error", P.ERROR_BACKEND_UNAVAILABLE,
                    request_id=request_id))
                return
            backend_sock = getattr(conn, "sock", None)
            try:
                response = conn.getresponse()
            except socket.timeout:
                if admitted_here:
                    self.server.admission.release()
                self.server.telemetry.note_timeout(request_id)
                self.server.telemetry.note_completed(504, (time.monotonic() - started) * 1000, ttft_ms, "timeout")
                self._reject_json(504, P.build_error_body(
                    "Backend request timed out.", "server_error", P.ERROR_REQUEST_TIMEOUT,
                    request_id=request_id))
                return
            except (OSError, http.client.HTTPException) as exc:
                if admitted_here:
                    self.server.admission.release()
                self.server.telemetry.note_backend_error()
                self.server.telemetry.note_completed(502, (time.monotonic() - started) * 1000, ttft_ms, "backend_unavailable")
                self._reject_json(502, P.build_error_body(
                    "Backend unavailable.", "server_error", P.ERROR_BACKEND_UNAVAILABLE,
                    request_id=request_id))
                return
            self.send_response(response.status, response.reason)
            has_length = False
            has_cache_control = False
            has_context_header = False
            has_output_header = False
            for key, value in response.getheaders():
                lower = key.lower()
                if lower in HOP_BY_HOP or lower.startswith(CORS_RESPONSE_PREFIX):
                    continue
                if lower == "content-length":
                    has_length = True
                if lower == "cache-control":
                    has_cache_control = True
                if lower == "x-muse-effective-context-size":
                    has_context_header = True
                if lower == "x-muse-max-output-tokens":
                    has_output_header = True
                self.send_header(key, value)
            if not has_cache_control:
                self.send_header("Cache-Control", "no-store")
            if not has_context_header:
                self.send_header("X-Muse-Effective-Context-Size", str(self.server.effective_context_size))
            if not has_output_header:
                self.send_header("X-Muse-Max-Output-Tokens", str(self.server.max_output_tokens))
            if not has_length:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if self.command == "HEAD":
                if admitted_here:
                    self.server.admission.release()
                self.server.telemetry.note_completed(response.status, (time.monotonic() - started) * 1000, ttft_ms, "done")
                return
            finish_class = "backend_error" if response.status >= 500 else "done"
            if response.status >= 500:
                self.server.telemetry.note_backend_error()
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self.server.telemetry.note_timeout(request_id)
                        finish_class = "timeout"
                        self.close_connection = True
                        return
                    self.connection.settimeout(min(max(remaining, 0.001), self.server.idle_timeout))
                    # Bound the blocking backend read by the remaining hard
                    # deadline, the idle timeout, and the connect timeout so a
                    # silent backend can never wedge the stream.
                    if backend_sock is not None:
                        try:
                            backend_sock.settimeout(min(remaining, self.server.idle_timeout, self.server.backend_timeout))
                        except OSError:
                            pass
                    chunk = response.read1(64 * 1024)
                    if not chunk:
                        break
                    if ttft_ms is None:
                        ttft_ms = (time.monotonic() - started) * 1000
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except socket.timeout:
                # Stall on the backend or expired hard deadline: fail closed.
                self.server.telemetry.note_timeout(request_id)
                finish_class = "timeout"
                self.close_connection = True
                return
            except (BrokenPipeError, ConnectionResetError):
                # A peer closing the connection is not a backend failure.
                finish_class = "client_disconnect"
                self.close_connection = True
                return
            except (OSError, http.client.HTTPException):
                self.server.telemetry.note_backend_error()
                finish_class = "backend_error"
                self.close_connection = True
                return
            finally:
                if admitted_here:
                    self.server.admission.release()
                self.server.telemetry.note_completed(response.status, (time.monotonic() - started) * 1000, ttft_ms, finish_class)
        except (OSError, http.client.HTTPException) as exc:
            if admitted_here:
                self.server.admission.release()
            self.server.telemetry.note_backend_error()
            self.server.telemetry.note_completed(502, (time.monotonic() - started) * 1000, ttft_ms, "backend_error")
            if not self.wfile.closed:
                try:
                    payload = json.dumps({
                        "error": {"message": "Backend unavailable.", "type": "server_error",
                                  "param": None, "code": P.ERROR_BACKEND_UNAVAILABLE},
                        "request_id": request_id,
                    }).encode() + b"\n"
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass
            self.close_connection = True
        finally:
            conn.close()

    do_GET = _proxy
    do_POST = _proxy
    do_PUT = _proxy
    do_PATCH = _proxy
    do_DELETE = _proxy
    do_HEAD = _proxy
    do_OPTIONS = _proxy


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--listen-host", default="127.0.0.1")
    ap.add_argument("--listen-port", type=int, default=8090)
    ap.add_argument("--backend-host", default="127.0.0.1")
    ap.add_argument("--backend-port", type=int, default=8088)
    ap.add_argument("--backend-timeout", type=float, default=P.CONNECT_TIMEOUT)
    ap.add_argument("--max-request-bytes", type=int, default=P.MAX_BODY_BYTES)
    ap.add_argument("--queue-size", type=int, default=P.MAX_QUEUED_INFERENCE)
    ap.add_argument("--queue-wait-seconds", type=float, default=P.QUEUE_WAIT_SECONDS)
    ap.add_argument("--effective-context-size", type=int, default=P.MAX_TOKENS)
    ap.add_argument("--max-output-tokens", type=int, default=None)
    ap.add_argument("--idle-timeout", type=float, default=P.IDLE_TIMEOUT)
    ap.add_argument("--hard-deadline", type=float, default=P.HARD_DEADLINE)
    ap.add_argument("--telemetry-file", default=None)
    args = ap.parse_args()

    token = os.environ.get("MUSE_API_TOKEN", "")
    if not valid_secret(token):
        print("ERROR: MUSE_API_TOKEN must be present, >=32 characters, and contain no control characters", file=sys.stderr)
        return 20
    if args.listen_host != "127.0.0.1":
        print("ERROR: auth proxy must bind exactly to 127.0.0.1", file=sys.stderr)
        return 21
    if args.backend_host != "127.0.0.1":
        print("ERROR: backend must remain on 127.0.0.1", file=sys.stderr)
        return 22
    if not (1 <= args.listen_port <= 65535 and 1 <= args.backend_port <= 65535):
        print("ERROR: invalid port", file=sys.stderr)
        return 23
    if args.max_request_bytes < 1:
        print("ERROR: --max-request-bytes must be >= 1", file=sys.stderr)
        return 24
    if args.queue_size < 0 or args.queue_wait_seconds < 0:
        print("ERROR: queue size and wait time must be non-negative", file=sys.stderr)
        return 25
    if args.effective_context_size < 1 or (args.max_output_tokens is not None and args.max_output_tokens < 1):
        print("ERROR: effective context/output limits must be positive", file=sys.stderr)
        return 26

    server = ProxyServer(
        (args.listen_host, args.listen_port), AuthProxyHandler,
        token=token, backend_host=args.backend_host, backend_port=args.backend_port,
        timeout=max(0.1, args.backend_timeout), max_request_bytes=args.max_request_bytes,
        idle_timeout=max(0.1, args.idle_timeout), hard_deadline=max(0.1, args.hard_deadline),
        telemetry_file=args.telemetry_file, queue_size=args.queue_size,
        queue_wait_seconds=args.queue_wait_seconds,
        effective_context_size=args.effective_context_size,
        max_output_tokens=args.max_output_tokens,
    )
    print(
        f"Muse auth proxy READY http://{args.listen_host}:{args.listen_port} -> "
        f"http://{args.backend_host}:{args.backend_port} "
        f"context={server.effective_context_size} max_output={server.max_output_tokens}",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())