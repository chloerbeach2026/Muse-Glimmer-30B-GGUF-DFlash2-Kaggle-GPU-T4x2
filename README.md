# Muse-Glimmer-30B — Personal Agent Server (HF-only)
> 🌐 Language / Ngôn ngữ: **English** | [Tiếng Việt](README.vi.md)

![Release](https://img.shields.io/badge/release-v1.0.0-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![CI](https://github.com/chloerbeach2026/Muse-Glimmer-30B-GGUF-DFlash2-Kaggle-GPU-T4x2/actions/workflows/ci.yml/badge.svg)
![Kaggle T4x2](https://img.shields.io/badge/kaggle-T4x2-orange)
![NVIDIA T4 x2](https://img.shields.io/badge/nvidia-T4%20x2-lightgray)
![llama.cpp](https://img.shields.io/badge/runtime-llama.cpp-606060)
![DFlash2](https://img.shields.io/badge/speculative-DFlash2-606060)
![OpenAI compatible](https://img.shields.io/badge/API-OpenAI--compatible-606060)
![Self-hosted](https://img.shields.io/badge/reference-self--hosted-606060)

**Self-hosted Muse-Glimmer-30B inference server for sustained agentic coding through OpenAI-compatible clients, with DFlash2 speculative decoding, HF-only pinned assets, tool-call history support, full-context target, bounded request queue, and optional Cloudflare Quick or Named Tunnel.**

> **This is not the upstream Muse-Glimmer-30B model repository.**
> This repository provides the reproducible serving stack, pinned runtime, authenticated gateway, Kaggle production notebook, and operator workflow required to run the model on your own compute.

### Canonical inference stack

- **Target model:** `Muse-Glimmer-30B-Q4_K_M.gguf`
- **Draft model:** `Muse-Glimmer-30B-DFlash2-Q4_K_M.gguf`
- **Inference runtime:** `llama.cpp` with CUDA
- **Acceleration:** DFlash2 speculative decoding
- **Target compute profile:** NVIDIA **T4 x2** (qualification still required after these changes)
- **API:** OpenAI-compatible chat completions with SSE streaming
- **Access layer:** Bearer-authenticated gateway
- **Optional public transport:** Cloudflare Quick Tunnel or token-based Named Tunnel

> **This repository does not provide a shared hosted inference service.**
> **Users run the stack on their own Kaggle account/infrastructure and consume their own GPU quota.**

---

## Table of contents

1. [Overview](#overview)
2. [Scope / What this is](#scope--what-this-is)
3. [What this is not](#what-this-is-not)
4. [Qualification target environment](#qualification-target-environment)
5. [Architecture](#architecture)
6. [Requirements](#requirements)
7. [Model files](#model-files)
8. [Integrity / hash verification](#integrity--hash-verification)
9. [Kaggle T4x2 quick start](#kaggle-t4x2-quick-start)
10. [Configuration](#configuration)
11. [Backend startup](#backend-startup)
12. [Gateway startup](#gateway-startup)
13. [Readiness check](#readiness-check)
14. [API usage](#api-usage)
15. [Agent client compatibility](#agent-client-compatibility)
16. [SSE example](#sse-example)
17. [Busy/concurrency behavior](#busyconcurrency-behavior)
18. [Optional public tunnel](#optional-public-tunnel)
19. [Shutdown / cleanup](#shutdown--cleanup)
20. [Troubleshooting](#troubleshooting)
21. [Security](#security)
22. [Known limitations](#known-limitations)
23. [Reproducibility](#reproducibility)
24. [License](#license)
25. [Contributing / support](#contributing--support)

---

## Overview

A personal-serving stack for `Muse-Glimmer-30B` GGUF models:

- **Persistent loopback backend** — `llama-server` bound to `127.0.0.1:8088`,
  started and supervised by `./serve.sh`.
- **DFlash2 speculative decoding** — a pinned Hugging Face draft GGUF is downloaded and hash-verified; `SERVE_PROFILE=baseline` disables draft decoding.
- **Authenticated gateway** — `./expose.sh` runs `scripts/auth_proxy.py`, a
  loopback Bearer-auth proxy on `127.0.0.1:8090` that enforces a strict public
  contract and preserves SSE streaming.
- **One-command orchestration** — `./external.sh` drives download/build preflight, backend readiness, exposure startup, status, endpoint discovery, and shutdown.

## Scope / What this is

- A **self-hosted reference implementation** and reproducible runbook.
- An **agent-serving gateway**: one active inference, a bounded wait queue, token-bucket rate limiting, opaque request IDs, structured `muse_*` errors, multi-turn tool-call history, and SSE streaming.
- Designed for **user-owned compute**: use a persistent Linux GPU host for day-to-day work; Kaggle T4 x2 is a qualification target, not a 24/7 service.
- Community-reviewable: everything is deterministic and CPU-testable without a
  GPU (see [Reproducibility](#reproducibility)).

## What this is not

- Not a hosted/managed API service.
- Not covered by any SLA, HA promise, or uptime guarantee.
- Not an "enterprise production" platform.
- Not a model-training or fine-tuning toolkit.
- Not an introduction to `llama.cpp` or GGUF in general.

## Qualification target environment

> This is the intended hardware/runtime contract, not evidence that the current fork commit has completed a new Kaggle run. The current commit still needs end-to-end qualification.

| Item | Value |
| --- | --- |
| Platform | Persistent Linux GPU host for daily use; Kaggle notebook for qualification |
| Qualification accelerator | NVIDIA T4 x2 (Kaggle "GPU T4 x2") |
| GPUs required | 2, each ≥ 14000 MiB VRAM (`GPU_REQUIRED_COUNT=2`, `GPU_MIN_VRAM_MIB=14000`) |
| CUDA devices | `CUDA_VISIBLE_DEVICES=0,1` |
| GPU split | layer-split `1,1`, all layers on GPU (`GPU_SPLIT_MODE=layer`, `GPU_TENSOR_SPLIT=1,1`, `GPU_LAYERS=999`) |
| llama.cpp | pinned runtime commit `64f765f5adefa4620dddda436ce56f1430435536` (CUDA, sm_75) |
| Python | 3.12 (runtime venv bootstrapped by `scripts/setup.sh`) |
| Shell | bash with `set -Eeuo pipefail` |

## Architecture

```text
  client
    |
    |  HTTPS (optional cloudflared quick/named tunnel)
    v
  scripts/auth_proxy.py           127.0.0.1:8090   Bearer auth gateway
    |                                  |            one active inference,
    |                                  |            bounded wait queue,
    v                                  v            SSE-preserving
  llama-server (loopback only)    127.0.0.1:8088   persistent backend
    |
    v
  GGUF target + DFlash2 draft + vision projector (pinned Hugging Face assets)
```

Controller entrypoints:

- `./serve.sh` — start / status / stop / restart the persistent backend.
- `./expose.sh` — start / status / stop the authenticated gateway (and
  optional cloudflared tunnel).
- `./external.sh` — one-command orchestration: `preflight`, `start`, `status`,
  `endpoint`, `local-endpoint`, `stop`, `stop-all`, with
  `--format text|env|json`.
- `./readiness.sh` — read-only fresh-notebook readiness probe.

## Requirements

- Linux x86-64, Python 3.12, Bash, Git, CMake/build tools, a working NVIDIA CUDA toolchain and the target GPU layout required by `config/default.env` (currently 2 GPUs with at least 14,000 MiB each; Kaggle T4 x2 is the qualification target).
- Internet access to Hugging Face for first download and to the pinned llama.cpp source for a first runtime build.
- A private `MUSE_API_TOKEN` of **at least 32 printable characters** for Bearer authentication.
- For `EXPOSURE_MODE=named`, a Cloudflare Tunnel token and a configured public HTTPS hostname/DNS route. The pinned `cloudflared` binary is installed and hash-verified automatically; no system install is required.
- Weights are downloaded only from immutable Hugging Face revisions; no Kaggle Model attachments are required. The CPU-only tests need no GPU, model weights, tunnel, or network.

## Model assets (Hugging Face only)

The server refuses Kaggle-mounted weights and downloads exact target, DFlash2 draft, and vision projector files from immutable Hugging Face revisions. Each asset is verified by expected byte size and SHA-256 before serving.

| Asset | Hugging Face repository | Pinned revision | File | Expected size | SHA-256 |
| --- | --- | --- | --- | ---: | --- |
| Target | `bartowski/Muse-Glimmer-30B-GGUF` | `16f5840bef28e4018cf41a7ec9709d6fd89176f6` | `Muse-Glimmer-30B-Q4_K_M.gguf` | 17,306,324,000 B | `0d3fc85f61d10fdc84072f0bba6005d61c1ac5605a2627b0fd5ea4ff8194c384` |
| DFlash2 draft | `meta-models/Muse-Glimmer-30B-GGUF` | `43c7eadd41352a299ea8e0a36b3157978dd63596` | `dflash-Muse-Glimmer-30B-Q4_K_M.gguf` | 1,631,208,128 B | `b2e808bf656086fe86bd0d0bd990f01d33e377537a07c02d45371517c8b264ef` |
| Vision projector | `meta-models/Muse-Glimmer-30B-GGUF` | `43c7eadd41352a299ea8e0a36b3157978dd63596` | `mmproj-Muse-Glimmer-30B-Q4_K_M.gguf` | 1,400,328,928 B | `f48b452316f9b213758e8659444029b961a24a07f99a1abb2a9f88b06f7c00c6` |

Download policy: `HF_DOWNLOAD_WORKERS=32`, `HF_XET_NUM_CONCURRENT_RANGE_GETS=32`, Xet high-performance mode, retry and native Hub cache resume. These settings target 32 concurrent Xet range requests per file plus up to 32 file workers; they do not promise that all 32 file workers are active while downloading a single file.

No model files are committed to Git. A local `models/` cache is reused only after exact identity verification. A missing, truncated or corrupted cache is re-downloaded from the pinned HF revision; the server will not start if size or SHA-256 differs.

## Integrity / hash verification

Every `./serve.sh start` verifies the source manifest, exact target model identity, exact DFlash2 identity, vision-projector SHA-256, and the pinned llama.cpp source commit. Model assets come only from the immutable Hugging Face revisions in [Model assets](#model-assets-hugging-face-only); Kaggle-mounted model weights are not read.

The target, draft and projector must match both their configured byte size and SHA-256. A missing or corrupt cached artifact is removed and downloaded again using native Hugging Face Hub cache/resume, `HF_DOWNLOAD_WORKERS=32`, and `HF_XET_NUM_CONCURRENT_RANGE_GETS=32`. A mismatch stops startup before the model is served. The notebook writes the verified asset identities and the actually loaded Context/output values into runtime evidence.

For reproducible CPU-only checks, run `./demo-smoke.sh`; for a T4x2 hardware qualification, use the notebook below. Neither static configuration nor a successful CPU test is a substitute for a real GPU run.
## Canonical production notebook

For a guided production-style Kaggle run, use [`notebooks/kaggle-production.ipynb`](notebooks/kaggle-production.ipynb).

The notebook validates the T4 x2 hardware gate, attached model/runtime inputs, this fork's `main` commit identity, secure Bearer-token handling, real non-stream and SSE generation, optional Quick Tunnel transport, evidence, and cleanup.

See [`docs/KAGGLE_PRODUCTION.md`](docs/KAGGLE_PRODUCTION.md) for the operator contract and release qualification gates.

## Publication evidence

The successful Kaggle T4 x2 run described in the historical publication record belongs to the original upstream `v1.0.0` source. It does **not** qualify this fork's current code. This fork needs a fresh Kaggle run before it can publish its own verified evidence bundle.

- [Releases for this fork](https://github.com/chloerbeach2026/Muse-Glimmer-30B-GGUF-DFlash2-Kaggle-GPU-T4x2/releases) (publish only evidence generated from a fresh run of this fork)

See [the publication evidence documentation](docs/PUBLICATION_EVIDENCE.md) for the historical claim boundary and verification process.

## Start the personal agent server

```bash
git clone https://github.com/chloerbeach2026/Muse-Glimmer-30B-GGUF-DFlash2-Kaggle-GPU-T4x2.git
cd Muse-Glimmer-30B-GGUF-DFlash2-Kaggle-GPU-T4x2

# Set secrets only in the shell/secret manager; never commit them.
export MUSE_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"

# Read-only checks, then start the pinned HF-only model/runtime and gateway.
./readiness.sh
./external.sh preflight
./external.sh start
./external.sh endpoint
./external.sh status
```

The first start downloads the target, DFlash2 draft and projector from Hugging Face, builds the pinned CUDA llama.cpp runtime if not cached, and attempts `CONTEXT_SIZE=131072`. On startup resource failure, it may fall back through smaller Context values; the effective Context/output limit is shown by status and response headers. Subsequent starts reuse files only after size/hash verification.

For an actual long-running personal service, run on a persistent Linux GPU host; Kaggle is a qualification/runtime session and does not guarantee 24/7 uptime. The external endpoint must remain behind the authenticated gateway.
### Optional systemd supervision (persistent Linux host)

For start-at-boot and automatic recovery after backend/gateway failure:

```bash
sudo bash scripts/install_systemd_service.sh
sudoedit /etc/muse-agent-server.env
# Set MUSE_API_TOKEN; optionally set EXPOSURE_MODE=named, TUNNEL_TOKEN,
# and EXPOSURE_PUBLIC_URL after configuring the Named Tunnel/DNS route.
sudo systemctl enable --now muse-agent-server.service
systemctl status muse-agent-server.service
journalctl -u muse-agent-server.service -f
```

The optional systemd supervisor checks backend provenance, gateway health and exposure status, then restarts the stack with bounded exponential backoff. The installer does not start the model and never reads or prints secret values. The environment file is created with mode `0600`; keep it root-owned. The service account must have GPU access and write access to the project cache directories. This is an operator-managed personal service, not a high-availability cluster.

## Configuration

Defaults live in `config/default.env`; profile values in `config/profiles.json` are applied by `serve.sh` unless a corresponding environment override is explicitly set.

| Variable | Default | Purpose |
| --- | --- | --- |
| `RUN_PROFILE` | `agent` for persistent serving | Full Context/output target and high reasoning |
| `MODEL_SOURCE_MODE` | `hf` | Model weights are HF-only |
| `HF_DOWNLOAD_WORKERS` | `32` | Parallel file downloads |
| `HF_XET_NUM_CONCURRENT_RANGE_GETS` | `32` | Concurrent Xet range downloads per file |
| `CONTEXT_SIZE` / `MAX_TOKENS` | `131072` / `131072` | Desired Context and API output ceiling; actual values may be reduced after a GPU resource startup failure |
| `REASONING_STRENGTH` | `high` | Model chat-template reasoning setting |
| `PARALLEL_SLOTS` | `1` | One inference at a time; other requests wait in a bounded queue |
| `ADMISSION_QUEUE_SIZE` / `ADMISSION_QUEUE_WAIT_SECONDS` | `8` / `600` | Maximum queued requests / maximum wait |
| `EXPOSURE_MODE` | `quick` | `quick`, `named`, or `proxy` (bring your own transport) |
| `MUSE_API_TOKEN` | — | Private Bearer secret, at least 32 printable characters |
| `TUNNEL_TOKEN` / `EXPOSURE_PUBLIC_URL` | — | Named Tunnel credential and preconfigured HTTPS hostname |
| `SOURCE_INTEGRITY_MODE` | `strict` | Enforces the source manifest |

The requested 131,072 Context is the model target, **not a guarantee that every GPU configuration can load it**. The server attempts it first with quantized KV cache and then only falls back on recognized startup/resource failure. The gateway clamps oversized client output limits to the Context/output budget that the runtime actually loaded. It does not offer multiple simultaneous generations: the bounded queue protects one GPU-resident model from uncontrolled concurrency.
## Backend startup

```bash
./serve.sh start      # start persistent llama-server on 127.0.0.1:8088
./serve.sh status     # machine-readable env blob (PROFILE/HOST/PORT/PROVENANCE/...)
./serve.sh restart
./serve.sh stop
```

The backend is loopback-only: `SERVER_HOST` must be `127.0.0.1` and
`SERVER_ALLOW_NONLOOPBACK` must be `0`. Externally reachable access is never
the server itself — it is always the authenticated gateway.

## Gateway startup

```bash
./expose.sh start     # start auth gateway (and tunnel in quick/named mode)
./expose.sh status
./expose.sh endpoint  # public HTTPS URL when a tunnel is up
./expose.sh stop
```

The gateway (`scripts/auth_proxy.py`) enforces:

- Bearer auth with `MUSE_API_TOKEN` for every protected endpoint;
- an endpoint allowlist (`GET /health`, `GET /ready`, `GET /v1/models`,
  `POST /v1/chat/completions`);
- one active inference with a bounded queue (8 waiting requests / up to 600 seconds) and a token-bucket rate limit;
- request body size, message-count, and `max_tokens` validation;
- deterministic `muse_*` JSON error envelopes with opaque request IDs;
- SSE streaming pass-through with `[DONE]`.

For BYO transport, set `EXPOSURE_MODE=proxy` and terminate your own tunnel or
load balancer in front of `127.0.0.1:8090`; `./external.sh local-endpoint`
then prints the canonical local gateway URL.

## Readiness check

```bash
./readiness.sh                # default quick mode
./readiness.sh --mode quick   # source/config checks (no GPU/model needed)
./readiness.sh --mode proxy   # also requires EXPOSURE_MODE=proxy readiness
./readiness.sh --mode named   # also validates TUNNEL_TOKEN / EXPOSURE_PUBLIC_URL
./readiness.sh --format env   # machine-readable output
```

`./readiness.sh` is strictly read-only and never starts processes, loads
models, or consumes GPU compute.

## API usage

Base URL: the value of `./external.sh endpoint` (public) or
`./external.sh local-endpoint` (proxy mode, `http://127.0.0.1:8090`).

```bash
curl -sS https://<public-endpoint>/v1/models \
  -H "Authorization: Bearer $MUSE_API_TOKEN"
```

```bash
curl -sS https://<public-endpoint>/v1/chat/completions \
  -H "Authorization: Bearer $MUSE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "muse-glimmer-30B",
        "messages": [{"role": "user", "content": "Explain this project in two sentences."}],
        "max_tokens": 256
      }'
```

### Example response (non-streaming)

```json
{
  "id": "muse-...",
  "object": "chat.completion",
  "model": "muse-glimmer-30B",
  "choices": [
    {
      "index": 0,
      "message": { "role": "assistant", "content": "..." },
      "finish_reason": "stop"
    }
  ],
  "usage": { "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0 }
}
```

`GET /health` is public and reports gateway health; `GET /ready` is public,
probes the backend, and strips `Authorization` before contacting it.

## Agent client compatibility

The gateway exposes an OpenAI-compatible `/v1/chat/completions` API, preserves streaming, accepts assistant `tool_calls` and subsequent `tool` messages, and injects `reasoning_strength=high` unless the client sends an allowed value. Client context/output settings must mirror the *effective* runtime values, not assume the requested 131,072 always loaded. Read `./serve.sh status` and check `X-Muse-Effective-Context-Size` / `X-Muse-Max-Output-Tokens` response headers.

### OpenCode IDE

For a client on the same host, set the OpenAI-compatible provider's `baseURL` to `http://127.0.0.1:8090/v1`, model ID to `muse-glimmer-30B`, and API key to the environment variable `MUSE_API_TOKEN`. Set `limit.context` and `limit.output` to the effective values reported by the server. A starting configuration (replace limits if startup fell back):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "muse/muse-glimmer-30B",
  "provider": {
    "muse": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Muse Glimmer (local)",
      "options": {
        "baseURL": "http://127.0.0.1:8090/v1",
        "apiKey": "{env:MUSE_API_TOKEN}"
      },
      "models": {
        "muse-glimmer-30B": {
          "name": "Muse Glimmer 30B",
          "limit": { "context": 131072, "output": 131072 }
        }
      }
    }
  }
}
```

### Hermes Agent

Create a custom OpenAI-compatible provider/model with base URL `http://127.0.0.1:8090/v1`, API token from `MUSE_API_TOKEN`, model ID `muse-glimmer-30B`, and the actual Context/output limits shown by the gateway. Do not copy 131,072 into the client after an automatic Context fallback. Verify a complete tool loop (assistant tool call → tool result → assistant final response) before relying on unattended tasks.

### Claude Code Desktop through OmniRoute

Use OmniRoute as the Anthropic-compatible ingress for Claude Code, and configure the OmniRoute provider/upstream to call this server's OpenAI-compatible endpoint. In a same-host setup, the upstream is `http://127.0.0.1:8090/v1` with `MUSE_API_TOKEN`; when using a tunnel, use the HTTPS origin from `./external.sh endpoint` plus `/v1`. This is a protocol bridge, not a guarantee of official Anthropic support. Test `tool_use`, streamed events, cancellation, and multi-turn tool results with the exact OmniRoute version you run.

**Security:** use loopback when the desktop shares the model host. For remote clients use the authenticated gateway over HTTPS, never expose port 8088 directly, and never put either token in a committed config file.

## SSE example

```bash
curl -sSN https://<public-endpoint>/v1/chat/completions \
  -H "Authorization: Bearer $MUSE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "muse-glimmer-30B",
        "messages": [{"role": "user", "content": "Count from 1 to 5."}],
        "stream": true
      }'
```

Streaming is preserved end-to-end: `text/event-stream` chunks carry
`choices[].delta.content`, the stream terminates with `data: [DONE]`, and
`finish_reason` (`stop`/`length`) is emitted before `[DONE]`.

## Busy/concurrency behavior

Only one generation runs at a time (`PARALLEL_SLOTS=1`). Up to 8 requests can wait for at most 600 seconds. If the queue is full or the wait deadline expires, the gateway returns:

```text
HTTP/1.1 429
Retry-After: 5
```

```json
{
  "error": {
    "type": "muse_demo_busy",
    "message": "The inference queue is full or its wait deadline expired; retry after the indicated delay."
  }
}
```

Repeated admission is enforced by an exact slot lock and is covered by the
deterministic test suite. Rate limits, payload limits, and timeouts are also
enforced by the gateway before any request reaches the backend.

## Optional Cloudflare tunnel

The repository bootstraps the pinned `cloudflared 2026.10.0` binary and checks its official architecture-specific SHA-256 before executing it. Both modes expose only the authenticated gateway, never backend port 8088.

**Quick Tunnel (ephemeral URL):**

```bash
export EXPOSURE_MODE=quick
./external.sh start
./external.sh endpoint
```

**Named Tunnel (stable hostname):**

1. In Cloudflare Zero Trust, create a Named Tunnel and configure a published application route/DNS hostname.
2. Export the token and exact public origin configured for that tunnel; never store them in Git.
3. Start and inspect the endpoint:

```bash
export EXPOSURE_MODE=named
export TUNNEL_TOKEN='paste-token-from-cloudflare'
export EXPOSURE_PUBLIC_URL='https://llm.example.com'
./external.sh preflight
./external.sh start
./external.sh endpoint
```

`expose.sh` writes the Named Tunnel token into a mode-`0600` runtime-state file and deletes it during shutdown/failed startup. The hostname, DNS and tunnel route must be configured in Cloudflare; a token alone cannot create them.

For either mode, public traffic passes through Bearer auth. Never expose `llama-server` directly.
## Shutdown / cleanup

```bash
./external.sh stop       # stop exposure (gateway/tunnel); keep the backend running
./external.sh stop-all   # stop exposure, then the owned backend
./expose.sh stop         # gateway/tunnel only
./serve.sh stop          # backend only
```

State and logs live under `artifacts/runtime-state/` (ignored by Git). To
fully reset a session, stop everything and delete `artifacts/` plus the
runtime dirs (`vendor/`, `models/`, `runs/`).

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| `readiness.sh` reports `SOURCE_MANIFEST` blocker | Modify runtime files → rerun `bash scripts/source-manifest.sh write` to regenerate `SOURCE_MANIFEST.sha256` |
| `preflight` fails on credentials | Set `MUSE_API_TOKEN` ≥ 32 printable chars; for `named`, also `TUNNEL_TOKEN`/`EXPOSURE_PUBLIC_URL` |
| `cloudflared` bootstrap failure | Inspect the download/hash error; the pinned installer needs HTTPS access to the official GitHub release. A custom `CLOUDFLARED_BIN` is operator-managed. |
| Backend fails identity checks | Check the pinned HF revision, byte size and SHA-256 in [Model files](#model-files); remove the corrupt cache and start again |
| `429 muse_demo_busy` | A generation is already running; retry after the `Retry-After` window |
| First start is slow | Runtime bootstrap + model state preparation is one-time work |

## Security

See [SECURITY.md](SECURITY.md). Highlights:

- The gateway **never** logs or persists bearer tokens, prompts, completions,
  reasoning, or raw request bodies; telemetry carries only counters.
- Models and runtime are pinned by SHA-256 / commit and re-verified on start.
- The production notebook pins Cloudflare `cloudflared` to release `2026.10.0` and verifies the official SHA-256 before execution.
- Loopback-only backend: external access is always through the authenticated
  gateway.
- A public tunnel publishes the gateway to the Internet — that is your
  responsibility as the operator, and you must protect your own token.

## Known limitations

- One active generation at a time (`PARALLEL_SLOTS=1`); up to eight requests may wait in the bounded gateway queue.
- The current canonical GPU split gate targets two GPUs with ≥ 14,000 MiB each; Kaggle T4 x2 still needs an actual re-qualification run after these changes.
- Kaggle is not an always-on host. A personal always-running service needs a persistent host/process supervisor; a Cloudflare tunnel cannot keep an ephemeral notebook alive.
- No multi-user tenant isolation; the gateway is a single-owner demo.
- No model fine-tuning, no training, no embedding endpoints.
- Pinned runtime/model identities are exact; free substitution is not
  supported by the persistent server.
- This is a reference implementation maintained on a best-effort community
  model, not a commercial service.

## Reproducibility

- **Source integrity:** runtime source files are covered by `SOURCE_MANIFEST.sha256`
  (see `scripts/source_manifest.py`); runtime integrity is enforced in strict
  mode at startup.
- **Deterministic tests (CPU-only, no GPU/model/tunnel/secrets):**
  `./demo-smoke.sh` drives the real gateway against a fake loopback backend
  and asserts the full public contract (auth, allowlist, forwarding, SSE,
  busy 429, rate limits, timeouts, telemetry, slot release). The CI pipeline
  runs it on every push/PR.
- **Pinned runtime:** llama.cpp commit
  `64f765f5adefa4620dddda436ce56f1430435536`, CUDA target sm_75.
- **Fresh clone** reproduces the exact contract without any model, GPU, or
  network access.

## License

- **Repository source and documentation:** MIT —
  see [LICENSE](LICENSE). Copyright (c) 2026 Đăng Khoa.
- **Model weights:** the GGUF files are third-party artifacts with their own
  upstream license terms (e.g., from the `bartowski/Muse-Glimmer-30B-GGUF`
  repository and the attached DFlash2 input). This repository does **not**
  claim the model weights are MIT-licensed, and the MIT license in this repo
  does not extend to the weights.

## Contributing / support

- [CONTRIBUTING.md](CONTRIBUTING.md) — how to contribute (bilingual docs,
  boolean behavior-change declaration, tests, no secrets, no model blobs).
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) — community standards.
- [SUPPORT.md](SUPPORT.md) — where to ask questions and report problems.
- Security issues: see [SECURITY.md](SECURITY.md).