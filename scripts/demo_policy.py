#!/usr/bin/env python3
"""Production-demo gateway policy.

Single source of truth for the bounded public/operator demo contract:

* public route/method allowlist (GET /health, GET /ready, GET /v1/models,
  POST /v1/chat/completions) with per-method 404/405 semantics,
* structured error envelope with opaque request ids (code ``muse_*``),
* request body / input / output limits,
* single inference admission slot (``MAX_ACTIVE_INFERENCE=1``), busy 429,
* exact token-bucket rate limit with an injectable clock,
* connect / idle / hard timeout policy,
* a sanitized atomic telemetry snapshot surface.

This module is a pure policy library: it has no network side effects and is
safe to import in deterministic tests. It never stores or logs bearer tokens,
prompts, completions, reasoning, or raw request/response bodies.
"""
from __future__ import annotations

import json
import secrets
import time
import uuid
import threading
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Public contract constants (frozen public behavior).
#
# Tuned for sustained agentic coding through OpenAI-compatible clients (Hermes
# Agent, Claude Code + OmniRoute, OpenCode IDE). MAX_TOOL_CALLS and the
# admission queue are sized so a long multi-step tool loop can carry a large
# transcript without being rejected at the gateway, while still keeping a
# single-GPU-resident model safe from uncontrolled concurrency.
# ---------------------------------------------------------------------------
MAX_BODY_BYTES = 33554432
MAX_MESSAGES = 256
MAX_AGGREGATE_CONTENT = 30000000
MAX_SINGLE_CONTENT = 25000000
DEFAULT_MAX_TOKENS = 131072
MIN_MAX_TOKENS = 1
MAX_TOKENS = 131072
MAX_TOOL_CALLS = 256
DEFAULT_REASONING_STRENGTH = "high"

# Connect/idle/hard timeouts. The hard deadline is the upper bound a single
# inference request can occupy the admission slot; it must comfortably exceed
# the longest realistic agent tool loop (refactor across many files, long
# reasoning phase at strength=high).
CONNECT_TIMEOUT = 15.0
IDLE_TIMEOUT = 600.0
HARD_DEADLINE = 7200.0

RATE_CAPACITY = 30
RATE_REFILL_PER_SEC = 1.0
RATE_COST = 1

MAX_ACTIVE_INFERENCE = 1
MAX_QUEUED_INFERENCE = 16
QUEUE_WAIT_SECONDS = 1200.0
BUSY_RETRY_AFTER = "5"

TELEMETRY_SCHEMA_VERSION = 1

ROUTE_ALLOW = None

ALLOWED_ROUTES: dict[str, set[str]] = {
    "/health": {"GET"},
    "/ready": {"GET"},
    "/v1/models": {"GET"},
    "/v1/chat/completions": {"POST"},
}

INFERENCE_ROUTE = ("/v1/chat/completions", "POST")

# ---------------------------------------------------------------------------
# Error codes (stable machine-readable, never leak internals).
# ---------------------------------------------------------------------------
ERROR_AUTH_REQUIRED = "muse_auth_required"
ERROR_AUTH_INVALID = "muse_auth_invalid"
ERROR_BUSY = "muse_demo_busy"
ERROR_RATE_LIMITED = "muse_rate_limited"
ERROR_ENDPOINT_NOT_FOUND = "muse_endpoint_not_found"
ERROR_METHOD_NOT_ALLOWED = "muse_method_not_allowed"
ERROR_INVALID_REQUEST = "muse_invalid_request"
ERROR_PAYLOAD_TOO_LARGE = "muse_payload_too_large"
ERROR_INVALID_PAYLOAD = "muse_invalid_payload"
ERROR_TOO_MANY_MESSAGES = "muse_too_many_messages"
ERROR_CONTENT_TOO_LONG = "muse_content_too_long"
ERROR_INVALID_MAX_TOKENS = "muse_invalid_max_tokens"
ERROR_REQUEST_TIMEOUT = "muse_request_timeout"
ERROR_BACKEND_UNAVAILABLE = "muse_backend_unavailable"


def new_request_id() -> str:
    return "req_" + secrets.token_hex(8)


def is_inference_route(method: str, path: str) -> bool:
    return (path, method) == INFERENCE_ROUTE


def route_allows(method: str, path: str) -> Optional[int]:
    """Return ``None`` if the (method, path) is allowed, else an HTTP status.

    ``404`` means the path is unknown (never forwarded to the backend).
    ``405`` means the path exists but this method is not allowed.
    """
    methods = ALLOWED_ROUTES.get(path)
    if methods is None:
        return 404
    if method not in methods:
        return 405
    return ROUTE_ALLOW


def build_error_body(
    message: str,
    type_: str,
    code: str,
    param: Optional[str] = None,
    request_id: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "error": {
            "message": message,
            "type": type_,
            "param": param,
            "code": code,
        },
        "request_id": request_id or new_request_id(),
    }


def error_code(error: dict[str, Any]) -> str:
    return error.get("error", {}).get("code", "")


def effective_max_tokens(payload: dict[str, Any]) -> int:
    if isinstance(payload, dict):
        for key in ("max_completion_tokens", "max_tokens"):
            if key in payload:
                return int(payload[key])
    return DEFAULT_MAX_TOKENS


def _content_size(content: Any) -> tuple[bool, int]:
    """Return validity and conservative serialized character count for content."""
    if isinstance(content, str):
        return True, len(content)
    if content is None:
        return True, 0
    if isinstance(content, list):
        try:
            return all(isinstance(part, dict) for part in content), len(json.dumps(content, ensure_ascii=False))
        except (TypeError, ValueError):
            return False, 0
    return False, 0


def validate_payload(payload: Any) -> tuple[bool, dict[str, Any]]:
    """Validate OpenAI-compatible text, multimodal content, and tool-call history.

    Unknown optional fields are preserved so forward-compatible agent features can
    reach llama.cpp unchanged; this function validates structure and resource bounds.
    """
    def fail(message: str, code: str = ERROR_INVALID_PAYLOAD):
        return False, build_error_body(message, "invalid_request_error", code)

    if not isinstance(payload, dict):
        return fail("Request body must be a JSON object.")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return fail("Field 'messages' is required and must be a non-empty array.")
    if len(messages) > MAX_MESSAGES:
        return fail(f"Field 'messages' exceeds the maximum of {MAX_MESSAGES}.", ERROR_TOO_MANY_MESSAGES)

    aggregate = 0
    allowed_roles = {"system", "developer", "user", "assistant", "tool"}
    for index, msg in enumerate(messages):
        if not isinstance(msg, dict):
            return fail("Each message must be an object.")
        role = msg.get("role")
        if role not in allowed_roles:
            return fail(f"Message {index} has unsupported role.")
        content = msg.get("content")
        valid_content, content_size = _content_size(content)
        if not valid_content:
            return fail("Message content must be text, null, or an array of content-part objects.")
        if content is None and role != "assistant":
            return fail("Only assistant messages may have null content.")
        if content_size > MAX_SINGLE_CONTENT:
            return fail(f"Single message content exceeds {MAX_SINGLE_CONTENT} characters.", ERROR_CONTENT_TOO_LONG)
        aggregate += content_size

        if role == "tool" and not isinstance(msg.get("tool_call_id"), str):
            return fail("Tool result messages require a string 'tool_call_id'.")
        if "tool_call_id" in msg and not isinstance(msg["tool_call_id"], str):
            return fail("'tool_call_id' must be a string.")

        calls = msg.get("tool_calls")
        if calls is not None:
            if role != "assistant" or not isinstance(calls, list):
                return fail("'tool_calls' is allowed only as an array on assistant messages.")
            if len(calls) > MAX_TOOL_CALLS:
                return fail(f"Message has more than {MAX_TOOL_CALLS} tool calls.", ERROR_TOO_MANY_MESSAGES)
            for call in calls:
                if not isinstance(call, dict) or not isinstance(call.get("id"), str):
                    return fail("Each tool call needs a string id.")
                if call.get("type", "function") != "function":
                    return fail("Only function tool calls are supported by this endpoint.")
                function = call.get("function")
                if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                    return fail("Each tool call needs function.name.")
                arguments = function.get("arguments", "{}")
                if not isinstance(arguments, (str, dict, list)):
                    return fail("Tool function arguments must be JSON-compatible.")
                try:
                    aggregate += len(arguments) if isinstance(arguments, str) else len(json.dumps(arguments, ensure_ascii=False))
                except (TypeError, ValueError):
                    return fail("Tool function arguments must be JSON-compatible.")
        if aggregate > MAX_AGGREGATE_CONTENT:
            return fail(f"Aggregate message content exceeds {MAX_AGGREGATE_CONTENT} characters.", ERROR_CONTENT_TOO_LONG)

    for key in ("max_tokens", "max_completion_tokens"):
        if key in payload:
            mt = payload[key]
            if isinstance(mt, bool) or not isinstance(mt, int):
                return fail(f"Field '{key}' must be an integer.", ERROR_INVALID_MAX_TOKENS)
            if not (MIN_MAX_TOKENS <= mt <= MAX_TOKENS):
                return fail(f"Field '{key}' must be between {MIN_MAX_TOKENS} and {MAX_TOKENS}.", ERROR_INVALID_MAX_TOKENS)
    if "stream" in payload and not isinstance(payload["stream"], bool):
        return fail("Field 'stream' must be a boolean.")
    if "tools" in payload:
        tools = payload["tools"]
        if not isinstance(tools, list) or len(tools) > MAX_TOOL_CALLS:
            return fail(f"Field 'tools' must be an array of at most {MAX_TOOL_CALLS} items.")
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("type", "function") != "function":
                return fail("Only function tools are supported.")
            fn = tool.get("function")
            if not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
                return fail("Each tool must define function.name.")
    return True, {}


# ---------------------------------------------------------------------------
# Single inference admission slot (Task 3).
# ---------------------------------------------------------------------------
class AdmissionSlot:
    """Thread-safe single-inference slot with a bounded timed wait queue."""

    def __init__(self, max_queued: int = 0, wait_seconds: float = 0.0) -> None:
        self.max_queued = max(0, int(max_queued))
        self.wait_seconds = max(0.0, float(wait_seconds))
        self._active = False
        self._waiting = 0
        self._condition = threading.Condition()

    def try_acquire(self, wait_seconds: float | None = None) -> bool:
        timeout = self.wait_seconds if wait_seconds is None else max(0.0, float(wait_seconds))
        deadline = time.monotonic() + timeout
        with self._condition:
            if not self._active and self._waiting == 0:
                self._active = True
                return True
            if timeout <= 0 or self.max_queued <= self._waiting:
                return False
            self._waiting += 1
            try:
                while self._active:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._condition.wait(remaining)
                self._active = True
                return True
            finally:
                self._waiting -= 1
                self._condition.notify_all()

    def release(self) -> None:
        with self._condition:
            if self._active:
                self._active = False
                self._condition.notify_all()


# ---------------------------------------------------------------------------
# Token-bucket rate limit (Task 4), monotonic + injectable clock.
# ---------------------------------------------------------------------------
class RealClock:
    def monotonic(self) -> float:
        return time.monotonic()


class TokenBucket:
    def __init__(
        self,
        capacity: float = RATE_CAPACITY,
        refill_per_sec: float = RATE_REFILL_PER_SEC,
        clock: Optional[Any] = None,
    ) -> None:
        self.capacity = float(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self._clock = clock or RealClock()
        self._tokens = float(capacity)
        self._last = self._clock.monotonic()

    def _refill(self) -> None:
        now = self._clock.monotonic()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_sec)
            self._last = now

    def try_consume(self, cost: float = RATE_COST) -> bool:
        self._refill()
        if self._tokens >= cost:
            self._tokens -= cost
            return True
        return False

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens


# ---------------------------------------------------------------------------
# Sanitized atomic demo telemetry snapshot (Task 7).
# ---------------------------------------------------------------------------
def new_telemetry_snapshot() -> dict[str, Any]:
    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "active_inference": False,
        "rate_bucket_tokens": float(RATE_CAPACITY),
        "last_request_id": None,
        "last_http_status": None,
        "last_request_duration_ms": None,
        "last_ttft_ms": None,
        "last_finish_class": None,
        "requests_total": 0,
        "inference_admitted_total": 0,
        "validation_error_total": 0,
        "busy_rejected_total": 0,
        "rate_limited_total": 0,
        "timeout_total": 0,
        "backend_error_total": 0,
    }
