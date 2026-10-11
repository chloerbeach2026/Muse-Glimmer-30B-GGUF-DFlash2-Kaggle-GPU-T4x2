# Changelog
> 🌐 Language / Ngôn ngữ: **English** | [Tiếng Việt](CHANGELOG.vi.md)

All notable changes to this public release are documented here. This
repository maintains a single public release; development history is not
recorded in this file.

## Unreleased

### Changed — sustained agentic-work hardening

- **Explicit agent-serving flags in `serve.sh`:** the persistent backend now
  starts with `-n $MAX_TOKENS` (explicit maximum output budget), flash
  attention (`-fa on`, both flag spellings auto-detected from `--help`), and
  `--cache-reuse $KV_CACHE_REUSE` (default 256) so cached KV chunks are
  recycled across agent turns after prompt divergence. Every new flag is
  added only when the pinned runtime advertises it, so runtime updates can
  never break startup.
- **SSE keepalive in the gateway:** while streaming `text/event-stream`
  responses, `auth_proxy.py` injects `: keepalive` comment lines whenever the
  backend is silent (default 30 s, `GATEWAY_SSE_KEEPALIVE_SECONDS`). This
  keeps Cloudflare Quick/Named Tunnel traffic alive across the edge's ~100 s
  idle timeout during long silent prompt prefills. Comment lines are ignored
  by every SSE client; idle/hard timeout semantics are unchanged.
- **Longer session bounds:** gateway message-history limit raised from 256 to
  512 messages (`MAX_MESSAGES`) and the hard per-request deadline from 2 to
  4 hours (`GATEWAY_HARD_DEADLINE=14400`), sized for a full 131072-token
  output budget at T4-class decode speed.
- **HF-only downloads enforced:** the downloader now always uses the official
  `https://huggingface.co` endpoint and keeps Xet concurrent range downloads
  enabled; inherited `HF_ENDPOINT` mirror overrides and `HF_HUB_DISABLE_XET`
  can no longer silently weaken the 32-connection download path
  (`HF_DOWNLOAD_WORKERS=32`, `HF_XET_NUM_CONCURRENT_RANGE_GETS=32`).
- **Named Tunnel hardening:** `TUNNEL_TOKEN` is validated as the real base64
  Cloudflare tunnel credential (base64url JSON with fields `a`, `t`, `s`)
  before launching `cloudflared`; startup waits for the
  `Registered tunnel connection` edge marker before probing the public URL;
  the named-mode public readiness window defaults to 120 s
  (`EXPOSURE_NAMED_READY_TIMEOUT`); `cloudflared` runs with
  `--no-autoupdate`.

### Added

- `tests/test_agent_hardening.py`: deterministic CPU-only coverage for the
  SSE keepalive (live fake-backend gateway test), the 512-message policy,
  the 4-hour deadline, the explicit serve flags, the HF-only download
  enforcement, and the named-tunnel token validation.

## [v1.0.0] - 2026-09-05

### Added

- **Self-hosted reference serving stack** for `Muse-Glimmer-30B` GGUF models
  on a Kaggle notebook session with two NVIDIA T4 GPUs (Kaggle "GPU T4 x2").
- **Persistent loopback backend:** `./serve.sh` starts, supervises, and stops a
  `llama-server` bound to `127.0.0.1:8088` with loopback-only enforcement,
  layer-split `1,1`, all layers on GPU, `PARALLEL_SLOTS=1`, and pinned model /
  runtime identity checks (size and SHA-256) before every start.
- **Authenticated gateway:** `./expose.sh` runs `scripts/auth_proxy.py` on
  `127.0.0.1:8090` with Bearer auth (`MUSE_API_TOKEN`, 32+ printable
  characters), a strict endpoint allowlist, single admission slot, token-bucket
  rate limiting, request validation, deterministic `muse_*` error envelopes
  with opaque request IDs, and SSE streaming preservation.
- **DFlash2 speculative decoding:** `SERVE_PROFILE=dflash2` (default) uses an
  exact pinned GGUF draft model; `SERVE_PROFILE=baseline` disables it.
- **One-command orchestration:** `./external.sh` provides `preflight`, `start`,
  `status`, `endpoint`, `local-endpoint`, `stop`, and `stop-all` with
  `--format text|env|json`.
- **Readiness probe:** `./readiness.sh` performs strictly read-only checks of
  source integrity, configuration, credentials, and external mode readiness
  without starting processes or consuming GPU compute.
- **Operator tooling:** `./doctor.sh` (machine-readable status authority),
  `./release-export.sh` (canonical release bundle export with verifiable
  source inventory), and `./package-llama-runtime.sh` (reproducible llama.cpp
  runtime packaging).
- **Source integrity contract:** `scripts/source_manifest.py check|write` and
  the committed `SOURCE_MANIFEST.sha256`; enforced in strict mode at backend
  startup (`SOURCE_INTEGRITY_MODE=strict|warn|off`).
- **Deterministic, CPU-only tests:** `./demo-smoke.sh` drives the real gateway
  against a fake loopback backend and asserts the full public contract (auth,
  allowlist, forwarding, SSE, busy 429, rate limits, timeouts, telemetry,
  slot release) with no model, GPU, tunnel, or network required.
- **Bilingual documentation:** English (`*.md`) and Vietnamese (`*.vi.md`)
  pairs for README, changelog, security, contributing, code of conduct, and
  support.
- **Community infrastructure:** `.github` issue templates, contribution
  guidelines, code of conduct, security policy, dependabot configuration, and
  a CI workflow that runs on `actions/checkout@v7` (and `actions/setup-python@v7`).

- **Canonical Kaggle production notebook:** a bilingual guided notebook validates the exact T4 x2 hardware, target/draft/runtime inputs, authenticated gateway, real non-stream and SSE generation, optional public transport, evidence, and cleanup.
- **Explicit public input identities and release packaging:** exact Kaggle resources/variations are linked, and the GitHub Release publishes deterministic source, notebook, evidence, and checksum assets.
- **Qualified publication-run evidence:** the successful real Kaggle T4 x2 run is published as a separately checksummed release asset with reviewer summary, model hashes, dual-GPU residency, real non-stream/SSE output, semantic sanity, DFlash2 counters, secret scanning, and cleanup evidence.
- **Reusable notebook lifecycle:** the clean production notebook includes explicit Stop / Reset / Re-run controls so operators can repeat tests in the same Kaggle notebook without creating a replacement notebook.

### Key contract

- **API:** `GET /health`, `GET /ready`, `GET /v1/models`,
  `POST /v1/chat/completions` (OpenAI-compatible, SSE streaming supported).
- **Concurrency:** one request at a time; a busy gateway responds
  `429` with `Retry-After: 5` and a `muse_demo_busy` envelope.
- **Persistent serving profile (`agent`):** `TEMPERATURE=1.0`, `TOP_P=0.95`,
  `TOP_K=64`, `SEED=42`, `CONTEXT_SIZE=131072`, `MAX_TOKENS=131072`,
  `REASONING_STRENGTH=high`, `REASONING_BUDGET=0`, `MIN_ANSWER_TOKENS=1024`,
  `PROMPT_LIMIT=0`, `REQUEST_TIMEOUT=3600`, `AUTO_CONTEXT_FALLBACK=1`.
  The requested 131,072-token Context is the model target; the server attempts
  it first with quantized KV cache and falls back to smaller values only on
  recognized startup/resource failure (OOM, allocation failure, KV cache
  failure). The effective Context/output is reported by `./serve.sh status`
  and the `X-Muse-Effective-Context-Size` / `X-Muse-Max-Output-Tokens`
  response headers.
- **Smoke profile (CI/fake-backend):** `CONTEXT_SIZE=4096`, `MAX_TOKENS=256`,
  `REASONING_STRENGTH=low`, `REASONING_BUDGET=64`. This profile is used only
  by `./demo-smoke.sh` against a fake loopback backend and never by the
  persistent serving stack.

### Security

- Gateway telemetry records counters only; bearer tokens, prompts,
  completions, reasoning, and raw bodies are never logged or persisted.
- The backend is loopback-only; external access always passes through the
  authenticated gateway.
- Model weights and the llama.cpp runtime are pinned by SHA-256 / commit and
  re-verified on every start.
- See `SECURITY.md` for supported version and responsible-disclosure details.

### Scope

- Reference implementation; users run the stack on their own Kaggle
  account/infrastructure and consume their own GPU quota.
- MIT license covers repository source and documentation only; model weights
  are third-party artifacts under their own upstream terms.