#!/usr/bin/env python3
"""CPU-safe agent-hardening tests (no GPU, model, tunnel, or external network).

Covers the sustained-agentic-coding guarantees added on top of v1.0.0:

* policy: 512-message history window, 4-hour hard deadline, SSE keepalive knob,
* serve.sh: explicit flash-attention, KV cache-reuse and max-output flags,
* common.sh: official-HF-only download endpoint and protected 32-connection
  Xet range-get configuration,
* expose.sh: named-tunnel token validation, edge-registration wait, and the
  SSE keepalive knob forwarded to the gateway,
* live gateway: SSE keepalive injection during a silent backend window,
  absence of injection for non-SSE responses, and the max-output default.
"""
from __future__ import annotations

import http.client
import http.server
import json
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import auth_proxy  # noqa: E402
import demo_policy as P  # noqa: E402

TOKEN = "unit-test-bearer-token-0123456789abcdef"


def send_chunk(wfile, piece: bytes) -> None:
    wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
    wfile.flush()


class FakeLlamaHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:  # silence
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        RECEIVED.append(json.loads(body))
        request = RECEIVED[-1]
        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            first = b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
            last = b'data: {"choices":[{"delta":{"content":"b"}}]}\n\n'
            send_chunk(self.wfile, first)
            time.sleep(BACKEND_SILENCE_SECONDS)
            send_chunk(self.wfile, last)
            send_chunk(self.wfile, b"data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
            self.close_connection = True
            return
        time.sleep(BACKEND_SILENCE_SECONDS)
        data = json.dumps({
            "id": "fake", "object": "chat.completion", "model": "muse-glimmer-30B",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


RECEIVED: list[dict] = []
BACKEND_SILENCE_SECONDS = 1.2


class LiveGatewayBase(unittest.TestCase):
    """Boots a fake llama-server plus the real auth_proxy ProxyServer."""

    keepalive_seconds: float = 0.4

    @classmethod
    def setUpClass(cls) -> None:
        RECEIVED.clear()
        cls.backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLlamaHandler)
        cls.backend_port = cls.backend.server_address[1]
        threading.Thread(target=cls.backend.serve_forever, daemon=True).start()

        cls.proxy = auth_proxy.ProxyServer(
            ("127.0.0.1", 0), auth_proxy.AuthProxyHandler,
            token=TOKEN, backend_host="127.0.0.1", backend_port=cls.backend_port,
            timeout=5.0, max_request_bytes=P.MAX_BODY_BYTES,
            idle_timeout=3.0, hard_deadline=30.0,
            queue_size=4, queue_wait_seconds=5.0,
            effective_context_size=131072, max_output_tokens=4096,
            sse_keepalive_seconds=cls.keepalive_seconds,
        )
        cls.proxy_port = cls.proxy.server_address[1]
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proxy.shutdown()
        cls.proxy.server_close()
        cls.backend.shutdown()
        cls.backend.server_close()

    def post(self, payload: dict, timeout: float = 25.0) -> tuple[int, bytes, float]:
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy_port, timeout=timeout)
        started = time.monotonic()
        try:
            body = json.dumps(payload).encode()
            conn.request(
                "POST", "/v1/chat/completions", body=body,
                headers={"Authorization": f"Bearer {TOKEN}",
                         "Content-Type": "application/json",
                         "Content-Length": str(len(body))},
            )
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, data, time.monotonic() - started
        finally:
            conn.close()


class TestSseKeepalive(LiveGatewayBase):
    def test_silent_sse_window_receives_keepalive_comments(self) -> None:
        status, data, elapsed = self.post({
            "messages": [{"role": "user", "content": "hi"}], "stream": True,
        })
        self.assertEqual(status, 200)
        self.assertIn(b": keepalive", data)
        self.assertIn(b'"delta":{"content":"a"}', data)
        self.assertIn(b'"delta":{"content":"b"}', data)
        self.assertIn(b"data: [DONE]", data)
        self.assertGreaterEqual(elapsed, BACKEND_SILENCE_SECONDS * 0.8)

    def test_sse_events_remain_parseable_after_keepalive_injection(self) -> None:
        """Keepalive comment lines must not break OpenAI-style SSE parsing."""
        _, data, _ = self.post({
            "messages": [{"role": "user", "content": "hi"}], "stream": True,
        })
        events = []
        for raw in data.split(b"\n\n"):
            lines = [ln for ln in raw.decode("utf-8", "replace").splitlines()
                     if ln.startswith("data:")]
            if lines:
                events.append("".join(l[len("data:"):].strip() for l in lines))
        self.assertIn('{"choices":[{"delta":{"content":"a"}}]}', events)
        self.assertIn('{"choices":[{"delta":{"content":"b"}}]}', events)
        self.assertIn("[DONE]", events)

    def test_non_sse_response_is_never_decorated(self) -> None:
        status, data, elapsed = self.post({
            "messages": [{"role": "user", "content": "hi"}], "stream": False,
        })
        self.assertEqual(status, 200)
        self.assertNotIn(b": keepalive", data)
        body = json.loads(data)
        self.assertEqual(body["choices"][0]["message"]["content"], "ok")
        self.assertGreaterEqual(elapsed, BACKEND_SILENCE_SECONDS * 0.8)

    def test_omitted_max_tokens_defaults_to_gateway_output_budget(self) -> None:
        self.post({"messages": [{"role": "user", "content": "hi"}], "stream": False})
        self.assertEqual(RECEIVED[-1]["max_tokens"], 4096)

    def test_client_max_tokens_is_honored_when_present(self) -> None:
        self.post({"messages": [{"role": "user", "content": "hi"}],
                   "stream": False, "max_tokens": 256})
        self.assertEqual(RECEIVED[-1]["max_tokens"], 256)


class TestKeepaliveDisabled(unittest.TestCase):
    """keepalive_seconds=0 must restore the strict fail-closed behavior."""

    @classmethod
    def setUpClass(cls) -> None:
        RECEIVED.clear()
        cls.backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLlamaHandler)
        cls.backend_port = cls.backend.server_address[1]
        threading.Thread(target=cls.backend.serve_forever, daemon=True).start()
        cls.proxy = auth_proxy.ProxyServer(
            ("127.0.0.1", 0), auth_proxy.AuthProxyHandler,
            token=TOKEN, backend_host="127.0.0.1", backend_port=cls.backend_port,
            timeout=5.0, max_request_bytes=P.MAX_BODY_BYTES,
            idle_timeout=3.0, hard_deadline=30.0,
            queue_size=4, queue_wait_seconds=5.0,
            effective_context_size=131072, max_output_tokens=4096,
            sse_keepalive_seconds=0.0,
        )
        cls.proxy_port = cls.proxy.server_address[1]
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proxy.shutdown()
        cls.proxy.server_close()
        cls.backend.shutdown()
        cls.backend.server_close()

    def test_disabled_keepalive_leaves_sse_undecorated(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy_port, timeout=25.0)
        try:
            body = json.dumps({"messages": [{"role": "user", "content": "hi"}],
                               "stream": True}).encode()
            conn.request(
                "POST", "/v1/chat/completions", body=body,
                headers={"Authorization": f"Bearer {TOKEN}",
                         "Content-Type": "application/json",
                         "Content-Length": str(len(body))},
            )
            resp = conn.getresponse()
            data = resp.read()
            self.assertEqual(resp.status, 200)
            self.assertNotIn(b": keepalive", data)
            self.assertIn(b'"delta":{"content":"b"}', data)
        finally:
            conn.close()


class TestAgentPolicy(unittest.TestCase):
    def test_message_history_window_is_agent_sized(self) -> None:
        self.assertEqual(P.MAX_MESSAGES, 512)
        big = {"messages": [{"role": "user", "content": "x"}] * 400}
        ok, err = P.validate_payload(big)
        self.assertTrue(ok, err)
        over = {"messages": [{"role": "user", "content": "x"}] * (P.MAX_MESSAGES + 1)}
        ok, err = P.validate_payload(over)
        self.assertFalse(ok)
        self.assertEqual(P.error_code(err), P.ERROR_TOO_MANY_MESSAGES)

    def test_hard_deadline_covers_full_context_generation(self) -> None:
        self.assertEqual(P.HARD_DEADLINE, 14400.0)
        self.assertGreater(P.HARD_DEADLINE, 7200.0)

    def test_sse_keepalive_policy_knob(self) -> None:
        self.assertEqual(P.SSE_KEEPALIVE_SECONDS, 30.0)

    def test_output_ceiling_unchanged(self) -> None:
        self.assertEqual(P.MAX_TOKENS, 131072)
        self.assertEqual(P.DEFAULT_MAX_TOKENS, 131072)


class TestShellHardening(unittest.TestCase):
    def test_serve_sh_sets_explicit_agent_flags(self) -> None:
        serving = ROOT.joinpath("serve.sh").read_text(encoding="utf-8")
        self.assertIn('-n "$MAX_TOKENS"', serving)
        self.assertIn("--flash-attn", serving)
        self.assertIn("--cache-reuse", serving)
        self.assertIn('KV_CACHE_REUSE:-256', serving)
        # detection must support both llama.cpp flag spellings
        self.assertIn('-fa on', serving)
        self.assertIn("agent_flags+=(-fa)", serving)
        # the frozen 131072 ceilings must remain
        self.assertIn("CONTEXT_SIZE <= 131072", serving)
        self.assertIn("MAX_TOKENS <= 131072", serving)

    def test_common_sh_enforces_official_hf_endpoint(self) -> None:
        common = ROOT.joinpath("scripts/common.sh").read_text(encoding="utf-8")
        self.assertIn("HF_ENDPOINT='https://huggingface.co'", common)
        self.assertIn("HF_HUB_DISABLE_XET='0'", common)
        self.assertIn("HF_DOWNLOAD_CONNECTIONS=32", common)
        # the 32-connection guarantee lines stay intact
        self.assertIn("HF_XET_NUM_CONCURRENT_RANGE_GETS:-32", common)
        self.assertIn('--max-workers "${HF_DOWNLOAD_WORKERS:-32}"', common)

    def test_expose_sh_named_tunnel_hardening(self) -> None:
        expose = ROOT.joinpath("expose.sh").read_text(encoding="utf-8")
        self.assertIn("validate_tunnel_token", expose)
        self.assertIn("wait_named_registered", expose)
        self.assertIn("Registered tunnel connection", expose)
        self.assertIn("--token-file", expose)
        self.assertIn("--sse-keepalive-seconds", expose)
        self.assertIn("EXPOSURE_NAMED_READY_TIMEOUT:-120", expose)

    def test_auth_proxy_wires_keepalive_flag(self) -> None:
        source = ROOT.joinpath("scripts/auth_proxy.py").read_text(encoding="utf-8")
        self.assertIn("--sse-keepalive-seconds", source)
        self.assertIn("sse_keepalive_seconds", source)
        self.assertIn('b": keepalive\\n\\n"', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
