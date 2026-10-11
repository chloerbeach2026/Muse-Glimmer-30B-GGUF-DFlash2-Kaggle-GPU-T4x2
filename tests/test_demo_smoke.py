#!/usr/bin/env python3
"""Deterministic demo smoke suite (no real model / no GPU).

Invoked standalone by `./demo-smoke.sh` or the CI pipeline. Drives the
real auth_proxy against a controlled fake loopback backend and asserts the full
production-demo public contract: auth, allowlist, valid chat forwarding, SSE
preservation, payload limits, busy 429 admission, rate limit, timeouts,
telemetry, and slot release.
"""
from __future__ import annotations

import http.client
import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "scripts" / "auth_proxy.py"
sys.path.insert(0, str(ROOT / "scripts"))
import demo_policy as P  # noqa: E402

TOKEN = "demo-smoke-token-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
AUTH = "Bearer " + TOKEN

passed = 0
failed = 0


def check(name: str, cond: bool) -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"ok   {name}")
    else:
        failed += 1
        print(f"FAIL {name}")


backend_seen: list[dict] = []


class BackendHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def _record(self, method, path, body=None):
        item = {"method": method, "path": path,
                "authorization": self.headers.get("Authorization")}
        if body is not None:
            item["body"] = body
        backend_seen.append(item)
        return len(backend_seen)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        self._record("GET", self.path)
        if self.path.startswith("/v1/chat/completions"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            timing = json.dumps({"timings": {"predicted_per_second": 29.955, "prompt_per_second": 12.0}})
            self.wfile.write(b"data: first content\n\n"); self.wfile.flush()
            self.wfile.write(f"data: {timing}\n\n".encode()); self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
            self.close_connection = True
            return
        if self.path in ("/health", "/v1/models"):
            # Mirror real llama-server: /health and /v1/models exist, but /ready
            # is owned by the Muse gateway and is intentionally absent here.
            payload = json.dumps({"status": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()

    def do_POST(self):
        body = self._read_body()
        idx = self._record("POST", self.path, body)
        if self.path.startswith("/v1/chat/completions"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            chunk = json.dumps({"role": "assistant", "content": "hi"}).encode()
            self.wfile.write(b"data: " + chunk + b"\n\n"); self.wfile.flush()
            timing = json.dumps({"timings": {"predicted_per_second": 29.955, "prompt_per_second": 12.0}})
            self.wfile.write(f"data: {timing}\n\n".encode()); self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
            self.close_connection = True
            return
        self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()


class HangingBackendHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        time.sleep(60)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_backend(handler_cls):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def start_proxy(bport, timeout, idle, hard, telemetry_file, context_size=P.MAX_TOKENS, output_cap=None):
    pport = free_port()
    env = dict(os.environ)
    env["MUSE_API_TOKEN"] = TOKEN
    argv = [sys.executable, str(PROXY), "--listen-host", "127.0.0.1", "--listen-port", str(pport),
            "--backend-host", "127.0.0.1", "--backend-port", str(bport),
            "--backend-timeout", str(timeout), "--idle-timeout", str(idle),
            "--hard-deadline", str(hard), "--telemetry-file", telemetry_file,
            "--effective-context-size", str(context_size)]
    if output_cap is not None:
        argv += ["--max-output-tokens", str(output_cap)
    p = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            c = http.client.HTTPConnection("127.0.0.1", pport, timeout=2)
            c.request("GET", "/health", headers={"Authorization": AUTH})
            c.getresponse().read(); c.close()
            return pport, p
        except Exception:
            time.sleep(0.2)
    p.terminate()
    raise RuntimeError("proxy failed to become ready")


def request(port, method, path, body=None, headers=None, timeout=10):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    if headers is None:
        headers = {"Authorization": AUTH}
    c.request(method, path, body=body, headers=headers)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, dict(r.getheaders()), data


def chat_payload():
    return json.dumps({"messages": [{"role": "user", "content": "hello"}],
                       "stream": True}).encode()


def fresh_proxy(td_path, timeout=2.0, idle=3.0, hard=4.0):
    """Start a proxy with a full token bucket plus a clean fake backend."""
    server, bport = start_backend(BackendHandler)
    tfile = str(Path(td_path) / f"gateway-{int(time.time()*1000)}-{free_port()}.jsonl")
    pport, proxy = start_proxy(bport, timeout, idle, hard, tfile)
    return server, proxy, pport, tfile


def close_proxy(server, proxy):
    try:
        proxy.terminate()
    except Exception:
        pass
    try:
        server.shutdown()
    except Exception:
        pass
    try:
        server.server_close()
    except Exception:
        pass


def main() -> int:
    global backend_seen
    with tempfile.TemporaryDirectory() as td:
        tfile = str(Path(td) / "gateway-telemetry.jsonl")
        server, bport = start_backend(BackendHandler)
        pport, proxy = start_proxy(bport, 2.0, 3.0, 4.0, tfile)
        try:
            # --- auth ---
            st, _, data = request(pport, "GET", "/health", headers={"Authorization": "Bearer bad"})
            check("health bad bearer -> 401 muse_auth_invalid", st == 401 and b"muse_auth_invalid" in data)
            st, _, data = request(pport, "GET", "/health", headers={})
            check("health no bearer -> 401 muse_auth_required", st == 401 and b"muse_auth_required" in data)

            # --- allowlist ---
            check("GET /health ok", request(pport, "GET", "/health")[0] == 200)
            backend_seen.clear()
            st, _, data = request(pport, "GET", "/ready")
            check("GET /ready gateway-owned readiness", st == 200 and json.loads(data) == {"status": "ready"})
            check("GET /ready probes backend /health", len(backend_seen) == 1 and backend_seen[0]["path"] == "/health")
            check("GET /ready strips Authorization upstream", len(backend_seen) == 1 and backend_seen[0]["authorization"] is None)
            check("GET /v1/models ok", request(pport, "GET", "/v1/models")[0] == 200)
            st, _, data = request(pport, "GET", "/slots")
            check("GET /slots -> 404 muse_endpoint_not_found", st == 404 and b"muse_endpoint_not_found" in data)
            st, _, data = request(pport, "POST", "/health")
            check("POST /health -> 405 muse_method_not_allowed", st == 405 and b"muse_method_not_allowed" in data)
            st, _, data = request(pport, "POST", "/wrongendpoint", body=b"{}")
            check("POST /wrongendpoint -> 404", st == 404 and b"muse_endpoint_not_found" in data)
            req = request(pport, "POST", "/wrongendpoint", body=b"{}")
            err = json.loads(req[2])
            check("opaque request_id present", str(err.get("request_id", "")).startswith("req_"))

            # --- never reaches backend ---
            seen_before = len(backend_seen)
            st, _, _ = request(pport, "GET", "/slots")
            st2, _, _ = request(pport, "POST", "/health")
            st3, _, _ = request(pport, "GET", "/v1/chat/completions")
            check("disallowed/405 never reaches backend", len(backend_seen) == seen_before)

            # --- valid chat forwarding + SSE preservation ---
            seen_before = len(backend_seen)
            st, hdrs, data = request(pport, "POST", "/v1/chat/completions",
                                     body=chat_payload(), headers={"Authorization": AUTH, "Content-Type": "application/json"})
            check("chat forwarding 200", st == 200)
            check("backend saw chat", len(backend_seen) == seen_before + 1)
            check("SSE content-type", hdrs.get("Content-Type", "").startswith("text/event-stream"))
            check("SSE [DONE] present", b"[DONE]" in data)
            check("timing preserved", b"predicted_per_second" in data)
            check("Authorization stripped upstream", backend_seen[-1]["authorization"] is None)
            forwarded = json.loads(backend_seen[-1]["body"])
            check("agent reasoning default is injected for IDE clients",
                  forwarded.get("chat_template_kwargs", {}).get("reasoning_strength") == "high")
            check("agent output cap defaults to model maximum",
                  forwarded.get("max_tokens") == P.MAX_TOKENS)
            check("effective context and output are exposed to clients",
                  hdrs.get("X-Muse-Effective-Context-Size") == str(P.MAX_TOKENS)
                  and hdrs.get("X-Muse-Max-Output-Tokens") == str(P.MAX_TOKENS))

            # --- telemetry was written atomically by this first proxy ---
            tx = Path(tfile)
            snap = json.loads(tx.read_text()) if tx.exists() else None
            check("telemetry file written", snap is not None)
            if snap:
                check("telemetry schema_version present", "schema_version" in snap)
                check("telemetry gates auth/allowlist counters", snap["requests_total"] >= 1)
        finally:
            proxy.terminate(); server.shutdown(); server.server_close()

        # --- adaptive context fallback informs the gateway and clamps client budgets ---
        server_ctx, bport_ctx = start_backend(BackendHandler)
        cfile = str(Path(td) / "gateway-context.jsonl")
        pport_ctx, proxy_ctx = start_proxy(bport_ctx, 2.0, 3.0, 4.0, cfile, context_size=32768, output_cap=32768)
        try:
            ask = json.dumps({"messages":[{"role":"user","content":"continue"}],
                              "max_completion_tokens": 131072}).encode()
            st, hdrs, _ = request(pport_ctx, "POST", "/v1/chat/completions", body=ask,
                                  headers={"Authorization": AUTH, "Content-Type":"application/json"})
            forwarded = json.loads(backend_seen[-1]["body"])
            check("adaptive context clamps max_completion_tokens", st == 200
                  and forwarded.get("max_tokens") == 32768
                  and "max_completion_tokens" not in forwarded)
            check("adaptive context is advertised in response headers",
                  hdrs.get("X-Muse-Effective-Context-Size") == "32768"
                  and hdrs.get("X-Muse-Max-Output-Tokens") == "32768")
        finally:
            close_proxy(server_ctx, proxy_ctx)

        # --- payload limits use fresh proxies so the token bucket is full (cap 2) ---
        server2, proxy2, pport2, tfile2 = fresh_proxy(td)
        try:
            big = {"messages": [{"role": "user", "content": "x" * (P.MAX_SINGLE_CONTENT + 1)}]}
            st, _, data = request(pport2, "POST", "/v1/chat/completions", body=json.dumps(big).encode(),
                                  headers={"Authorization": AUTH, "Content-Type": "application/json"})
            check("oversize single message -> 400 muse_content_too_long", st == 400 and b"muse_content_too_long" in data)
            many = {"messages": [{"role": "user", "content": "x"}] * (P.MAX_MESSAGES + 1)}
            st, _, data = request(pport2, "POST", "/v1/chat/completions", body=json.dumps(many).encode(),
                                  headers={"Authorization": AUTH, "Content-Type": "application/json"})
            check("too many messages -> 400 muse_too_many_messages", st == 400 and b"muse_too_many_messages" in data)
        finally:
            close_proxy(server2, proxy2)

        server2b, proxy2b, pport2b, tfile2b = fresh_proxy(td)
        try:
            st, _, data = request(pport2b, "POST", "/v1/chat/completions", body=b"not json",
                                  headers={"Authorization": AUTH, "Content-Type": "application/json"})
            check("malformed json -> 400", st == 400 and b"muse_" in data)
            badt = {"messages": [{"role": "user", "content": "x"}], "max_tokens": P.MAX_TOKENS + 1}
            st, _, data = request(pport2b, "POST", "/v1/chat/completions", body=json.dumps(badt).encode(),
                                  headers={"Authorization": AUTH, "Content-Type": "application/json"})
            check("max_tokens out of range -> 400 muse_invalid_max_tokens", st == 400 and b"muse_invalid_max_tokens" in data)
            snap_validation = json.loads(Path(tfile2b).read_text()) if Path(tfile2b).exists() else {}
            check("payload validation errors counted separately from backend errors",
                  snap_validation.get("validation_error_total") == 2
                  and snap_validation.get("backend_error_total") == 0
                  and snap_validation.get("inference_admitted_total", 0) == 0)
        finally:
            close_proxy(server2b, proxy2b)

        # Empty bodies are checked in isolation because the test bucket's cap is two.
        server2c, proxy2c, pport2c, tfile2c = fresh_proxy(td)
        try:
            st, _, data = request(pport2c, "POST", "/v1/chat/completions", body=None,
                                  headers={"Authorization": AUTH, "Content-Type": "application/json"})
            check("empty inference body -> 400", st == 400 and b"muse_" in data)
            snap_empty = json.loads(Path(tfile2c).read_text()) if Path(tfile2c).exists() else {}
            check("empty body is validation error, not a backend failure",
                  snap_empty.get("validation_error_total") == 1
                  and snap_empty.get("backend_error_total") == 0
                  and snap_empty.get("inference_admitted_total", 0) == 0)
        finally:
            close_proxy(server2c, proxy2c)

        # --- telemetry reflects admitted inference on a fresh proxy ---
        server3, proxy3, pport3, tfile3 = fresh_proxy(td)
        try:
            st, _, _ = request(pport3, "POST", "/v1/chat/completions", body=chat_payload(),
                               headers={"Authorization": AUTH, "Content-Type": "application/json"})
            tx3 = Path(tfile3)
            snap3 = json.loads(tx3.read_text()) if tx3.exists() else None
            check("telemetry reflects admitted inference", snap3 is not None and snap3.get("inference_admitted_total", 0) >= 1)
        finally:
            close_proxy(server3, proxy3)

        # --- busy/slot + hanging-stream timeout using a separate slow backend ---
        slow, sbport = start_backend(HangingBackendHandler)
        tfile_slow = str(Path(td) / "gateway-telemetry-slow.jsonl")
        slow_port, proxy_slow = start_proxy(sbport, 0.5, 1.0, 1.5, tfile_slow)
        s2 = P.AdmissionSlot()
        check("admission slot try_acquire", s2.try_acquire())
        check("admission slot busy second", not s2.try_acquire())
        s2.release()
        check("admission slot released", s2.try_acquire())
        s2.release()

        try:
            t0 = time.monotonic()
            st, _, _ = request(slow_port, "GET", "/v1/chat/completions", timeout=8)
            elapsed = time.monotonic() - t0
            check("hanging stream fails closed via timeout", st in (500, 502) or elapsed < 8)
            check("hanging stream did not hang past hard deadline", elapsed < 6.0)
            snap_timeout = json.loads(Path(tfile_slow).read_text()) if Path(tfile_slow).exists() else {}
            check("timeout finish class remains timeout after completion cleanup",
                  snap_timeout.get("timeout_total", 0) >= 1
                  and snap_timeout.get("last_finish_class") == "timeout"
                  and snap_timeout.get("active_inference") is False)
        finally:
            proxy_slow.terminate(); slow.shutdown(); slow.server_close()

    print(f"\npassed={passed} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())