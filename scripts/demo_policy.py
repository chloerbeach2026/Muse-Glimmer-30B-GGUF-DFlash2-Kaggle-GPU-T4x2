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
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Public contract constants (frozen public behavior).
# ---------------------------------------------------------------------------
MAX_BODY_BYTES = 131072
MAX_MESSAGES = 64
MAX_AGGREGATE_CONTENT = 32768
MAX_SINGLE_CONTENT = 16384
DEFAULT_MAX_TOKENS = 256
MIN_MAX_TOKENS = 1
MAX_TOKENS = 512

CONNECT_TIMEOUT = 5.0
IDLE_TIMEOUT = 60.0
HARD_DEADLINE = 180.0

RATE_CAPACITY = 2
RATE_REFILL_PER_SEC = 0.1
RATE_COST = 1

MAX_ACTIVE_INFERENCE = 1
MAX_QUEUED_INFERENCE = 0
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
    if isinstance(payload, dict) and "max_tokens" in payload:
        return int(payload["max_tokens"])
    return DEFAULT_MAX_TOKENS


def validate_payload(payload: Any) -> tuple[bool, dict[str, Any]]:
    """Validate a parsed chat/completions payload.

    Returns ``(True, {})`` on success or ``(False, error_body)`` on failure.
    """
    if not isinstance(payload, dict):
        return False, build_error_body(
            "Request body must be a JSON object.", "invalid_request_error",
            ERROR_INVALID_PAYLOAD,
        )
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return False, build_error_body(
            "Field 'messages' is required and must be a non-empty array.",
            "invalid_request_error", ERROR_INVALID_PAYLOAD,
        )
    if len(messages) > MAX_MESSAGES:
        return False, build_error_body(
            f"Field 'messages' exceeds the maximum of {MAX_MESSAGES}.",
            "invalid_request_error", ERROR_TOO_MANY_MESSAGES,
        )
    aggregate = 0
    for msg in messages:
        if not isinstance(msg, dict):
            return False, build_error_body(
                "Each message must be an object.", "invalid_request_error",
                ERROR_INVALID_PAYLOAD,
            )
        role = msg.get("role")
        content = msg.get("content")
        if role not in ("user", "assistant", "system") or not isinstance(content, str):
            return False, build_error_body(
                "Each message needs a valid 'role' and string 'content'.",
                "invalid_request_error", ERROR_INVALID_PAYLOAD,
            )
        if len(content) > MAX_SINGLE_CONTENT:
            return False, build_error_body(
                f"Single message content exceeds {MAX_SINGLE_CONTENT} chars.",
                "invalid_request_error", ERROR_CONTENT_TOO_LONG,
            )
        aggregate += len(content)
        if aggregate > MAX_AGGREGATE_CONTENT:
            return False, build_error_body(
                f"Aggregate message content exceeds {MAX_AGGREGATE_CONTENT} chars.",
                "invalid_request_error", ERROR_CONTENT_TOO_LONG,
            )
    if "max_tokens" in payload:
        mt = payload["max_tokens"]
        if isinstance(mt, bool) or not isinstance(mt, int) or isinstance(mt, float):
            return False, build_error_body(
                "Field 'max_tokens' must be an integer.", "invalid_request_error",
                ERROR_INVALID_MAX_TOKENS,
            )
        if not (MIN_MAX_TOKENS <= mt <= MAX_TOKENS):
            return False, build_error_body(
                f"Field 'max_tokens' must be between {MIN_MAX_TOKENS} and {MAX_TOKENS}.",
                "invalid_request_error", ERROR_INVALID_MAX_TOKENS,
            )
    return True, {}


# ---------------------------------------------------------------------------
# Single inference admission slot (Task 3).
# ---------------------------------------------------------------------------
class AdmissionSlot:
    """Single inference admission slot owned by the gateway policy layer."""

    def __init__(self) -> None:
        self._active = False

    def try_acquire(self) -> bool:
        if self._active:
            return False
        self._active = True
        return True

    def release(self) -> None:
        if self._active:
            self._active = False


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
