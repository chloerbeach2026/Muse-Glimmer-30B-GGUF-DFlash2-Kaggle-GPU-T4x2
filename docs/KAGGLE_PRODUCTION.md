# Kaggle / Persistent Agent Server Operator Guide
> 🌐 Language / Ngôn ngữ: **English** | [Tiếng Việt](KAGGLE_PRODUCTION.vi.md)

The notebook at `notebooks/kaggle-production.ipynb` is a **qualification workflow** for this fork. It uses the same repository serving scripts and downloads model assets from pinned Hugging Face revisions. Kaggle is not treated as a 24/7 host.

## What the notebook validates

A complete run should validate:
- NVIDIA T4 x2 hardware and usable GPU memory;
- exact Hugging Face target, DFlash2 draft and vision-projector revisions, file sizes and SHA-256;
- the fork commit, source manifest and pinned llama.cpp source build;
- requested and effective Context/output limits, including any resource-triggered fallback;
- Bearer-authenticated gateway, non-stream responses, SSE and multi-turn tool-call payloads;
- optional Cloudflare Quick Tunnel transport and safe cleanup.

The notebook's verdict applies only to that executed run. This guide does not claim that Kaggle has permanent availability or that a 131,072-token Context will fit every GPU runtime.

## Runtime boundaries

The personal-serving target is Linux x86-64 with two GPUs, Python 3.12 and a working CUDA build toolchain. The tested qualification target is Kaggle NVIDIA T4 x2. For day-to-day uptime, use a persistent GPU host; a Kaggle session can stop or be recycled.

The requested model Context is 131,072 tokens. The server attempts that first with quantized KV cache. On recognized startup/resource failure it can try a smaller Context and reports the actual value in `./serve.sh status`, response headers, and runtime metadata. The gateway clamps an overlarge output request to the effective output budget. This reduces configuration mismatch but cannot guarantee a particular speed or Context under every workload.

## Model sources (Hugging Face only)

Do not attach model weights to Kaggle. The server downloads exact files from immutable Hugging Face revisions and verifies size and SHA-256 before serving.

The only dataset you may attach is the prebuilt llama.cpp runtime (`muse-glimmer-30b-dflash2-llama-runtime`, packaged by `./package-llama-runtime.sh`). When it is attached under Kaggle Input, setup reuses its manifest-verified binaries and never compiles in-session (`LLAMA_SOURCE_MODE=kaggle`); when it is absent or fails verification, setup fails loudly instead of building, so every run serves exactly the pinned runtime commit.

| Asset | Repository | Revision | File | Expected bytes | SHA-256 |
| --- | --- | --- | --- | ---: | --- |
| Target | `bartowski/Muse-Glimmer-30B-GGUF` | `16f5840bef28e4018cf41a7ec9709d6fd89176f6` | `Muse-Glimmer-30B-Q4_K_M.gguf` | 17306324000 | `0d3fc85f61d10fdc84072f0bba6005d61c1ac5605a2627b0fd5ea4ff8194c384` |
| DFlash2 draft | `meta-models/Muse-Glimmer-30B-GGUF` | `43c7eadd41352a299ea8e0a36b3157978dd63596` | `dflash-Muse-Glimmer-30B-Q4_K_M.gguf` | 1631208128 | `b2e808bf656086fe86bd0d0bd990f01d33e377537a07c02d45371517c8b264ef` |
| Vision projector | `meta-models/Muse-Glimmer-30B-GGUF` | `43c7eadd41352a299ea8e0a36b3157978dd63596` | `mmproj-Muse-Glimmer-30B-Q4_K_M.gguf` | 1400328928 | `f48b452316f9b213758e8659444029b961a24a07f99a1abb2a9f88b06f7c00c6` |

Download uses `HF_DOWNLOAD_WORKERS=32`, `HF_XET_NUM_CONCURRENT_RANGE_GETS=32`, Xet high-performance mode, retries, and Hub cache resume. This targets 32 concurrent range requests for a large Xet file and up to 32 file workers; it does not imply that 32 workers simultaneously download one file. No Hugging Face token is required for public repositories; for gated assets use a read-only HF token via environment/secret manager, never a committed file.

The target, draft and projector cache is re-used only when the configured exact byte size and SHA-256 match. Damaged or incomplete files are removed and downloaded again; startup must fail closed if verification fails.

## Recommended secret

Set `MUSE_API_TOKEN` to a private value of at least 32 printable characters through a secret manager or the process environment. Do not commit it, print it to logs, or use the Cloudflare tunnel token as the API token.

## Run procedure

On a compatible GPU host (or for Kaggle qualification):

1. Select the required GPU hardware and enable network access for first-time source/model downloads.
2. Clone this fork and enter its directory.
3. Set `MUSE_API_TOKEN` in the shell or secret manager.
4. Run `./readiness.sh` and `./external.sh preflight`.
5. Run `./external.sh start`, then `./external.sh status` and `./external.sh endpoint`.
6. Read the effective Context/output fields; configure each desktop client to those actual values.
7. Test a multi-turn tool loop and streaming before unattended use.

Example:

```bash
export MUSE_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
./readiness.sh
./external.sh preflight
./external.sh start
./external.sh status
./external.sh endpoint
```

## Agent protocol contract

The gateway exposes an OpenAI-compatible `/v1/chat/completions` endpoint. It accepts `system`, `developer`, `user`, `assistant`, and `tool` message roles, assistant `tool_calls`, `tool_call_id` results, multimodal content-part arrays, both `max_tokens` and `max_completion_tokens`, and SSE. Run the CPU fake-backend suite to validate the contract before connecting an agent harness.

### OpenCode

Use an OpenAI-compatible provider with base URL `http://127.0.0.1:8090/v1`, model ID `muse-glimmer-30B`, API key from `MUSE_API_TOKEN`, and `limit.context` / `limit.output` matching the *effective* limits. The README contains a starter config.

### Hermes Agent

Use a custom OpenAI-compatible model/provider with the same base URL and model ID. Configure context/output using the actual runtime report, then validate assistant-tool-call → tool-result → assistant-final loops and a long multi-step session. Context declared by the client must not exceed the loaded server Context.

### Claude Code through OmniRoute

Use OmniRoute's Anthropic-compatible ingress for Claude Code and its OpenAI-compatible upstream to this gateway. Confirm the exact OmniRoute version converts tool-use messages, streamed events, cancellation, and multi-turn tool results correctly. This is a third-party protocol bridge, not a statement of official Anthropic support.

## Recommended Cloudflare exposure

The pinned `cloudflared 2026.10.0` installer verifies SHA-256 before executing the binary. Only the authenticated gateway is exposed; backend port 8088 remains loopback-only.

Quick Tunnel (ephemeral URL):

```bash
export EXPOSURE_MODE=quick
./external.sh start
./external.sh endpoint
```

Named Tunnel (stable configured hostname):

1. Create a Named Tunnel and Published Application/DNS route in Cloudflare Zero Trust.
2. Export `EXPOSURE_MODE=named`, `TUNNEL_TOKEN` and `EXPOSURE_PUBLIC_URL=https://your-hostname.example` in the environment/secret manager.
3. Run `./external.sh preflight` and `./external.sh start`.

The token is kept in a runtime-state file with mode 0600 and removed at shutdown/failed startup. A token by itself does not create a public hostname/DNS route. `EXPOSURE_MODE=proxy` is the bring-your-own-transport option.

## Publication rule

The committed notebook should be clean, contain no outputs/secrets, and pass its SHA-256 sidecar check. An executed Kaggle notebook is evidence for that one run only. Do not publish a PASS claim until a fresh run on the current fork succeeds.

## Safety and known limitations

This is a single-owner personal inference server, not a multi-tenant commercial service. It has one active inference slot and a bounded waiting queue, no high-availability cluster, and no guarantee of 24/7 uptime on Kaggle. Protect the API token, keep port 8088 private, and review current source/runtime identities before deploying.
