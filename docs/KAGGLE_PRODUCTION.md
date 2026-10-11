# Kaggle Production Notebook
> 🌐 Language / Ngôn ngữ: **English** | [Tiếng Việt](KAGGLE_PRODUCTION.vi.md)

The canonical production notebook for the public `v1.0.0` release is:

`notebooks/kaggle-production.ipynb`

It is an operator-focused Kaggle NVIDIA T4 x2 notebook that clones this fork's `main` branch, pins the exact resolved commit SHA for the run, and uses that checkout rather than reimplementing the serving stack.

## What the notebook validates

A complete run validates:

- exactly two NVIDIA T4 GPUs with the required visible VRAM;
- fork source identity, a clean worktree, and `SOURCE_MANIFEST.sha256`;
- Hugging Face-only target GGUF, DFlash2 draft and vision projector from immutable revisions;
- exact byte sizes and SHA-256 for all three assets before serving;
- the pinned llama.cpp CUDA runtime built from source if the exact build is not cached;
- actual Context/output limits loaded by the backend, including any resource-driven Context fallback;
- successful model-memory residency on both T4 GPUs after backend load;
- authenticated gateway handling and OpenAI-compatible agent request payloads;
- one real non-streaming generation and one real SSE generation ending with `[DONE]`;
- optional Cloudflare Quick Tunnel transport independently from local model/runtime health;
- pinned `cloudflared 2026.10.0` and its official architecture-specific SHA-256 before execution;
- deterministic evidence and cleanup, including deletion of a notebook-generated API token.
## Runtime boundaries

This notebook is a Kaggle T4x2 qualification workflow, not a 24/7 managed service. Model files are downloaded only from pinned Hugging Face revisions, and the notebook rejects assets with a mismatched size or SHA-256.

The server requests `CONTEXT_SIZE=131072`, `MAX_TOKENS=131072`, high reasoning strength and DFlash2. If recognized startup/resource failure prevents that Context from loading, smaller Context attempts may be used. The gateway reads successful runtime metadata and clamps client generation limits to the Context/output budget actually loaded. Evidence records the effective values rather than assuming 131K succeeded.

The notebook uses Cloudflare Quick Tunnel to qualify optional public transport. A stable Named Tunnel requires a Cloudflare hostname/DNS route and `TUNNEL_TOKEN`; configure it on a persistent host with `EXPOSURE_MODE=named`. Kaggle may stop when its session ends, so it is not a substitute for a persistent host for always-on personal work.
## Model sources (Hugging Face only)

Do **not** attach target, DFlash2 draft, projector or prebuilt runtime as Kaggle inputs. The notebook clones this fork, verifies the source manifest, downloads weights using immutable Hugging Face revisions, 32 Xet range requests per file plus up to 32 file workers, retries/resume and exact SHA-256 checks. The llama.cpp runtime is built from the configured immutable source commit if the exact build is not cached.

| Asset | Source | Revision | Local file |
| --- | --- | --- | --- |
| Target model | `bartowski/Muse-Glimmer-30B-GGUF` | `16f5840bef28e4018cf41a7ec9709d6fd89176f6` | `Muse-Glimmer-30B-Q4_K_M.gguf` |
| DFlash2 draft | `meta-models/Muse-Glimmer-30B-GGUF` | `43c7eadd41352a299ea8e0a36b3157978dd63596` | `dflash-Muse-Glimmer-30B-Q4_K_M.gguf` (renamed locally to the canonical filename after download) |
| Vision projector | `meta-models/Muse-Glimmer-30B-GGUF` | `43c7eadd41352a299ea8e0a36b3157978dd63596` | `mmproj-Muse-Glimmer-30B-Q4_K_M.gguf` |

Expected sizes and digests are maintained in `config/default.env` and verified by the downloader/startup gate. No weights are stored in Git.
## Recommended secret

Create a Kaggle Secret:

`MUSE_API_TOKEN`

Use at least 32 printable characters.

If the secret is missing, the notebook creates a cryptographically secure temporary token under:

`/kaggle/working/.muse-secrets/MUSE_API_TOKEN`

The directory uses mode `0700`, the file uses mode `0600`, the token value is never printed, and normal cleanup deletes the generated file.

## Run procedure

1. Select **GPU T4 x2**.
2. Enable Internet for the repository clone, Hugging Face downloads and optional Quick Tunnel.
3. Do not attach model/runtime inputs; the notebook downloads HF weights and builds the pinned CUDA runtime when needed.
4. Optionally configure `MUSE_API_TOKEN`.
5. Use **Restart Session → Run All**.
6. Review the final evidence and verdict lines before publishing notebook output.

A successful release qualification should include:

```text
KAGGLE_T4X2_GATE=PASS
FORK_SOURCE_IDENTITY=PASS
CANONICAL_ENVIRONMENT=PASS
HF_MODEL_INTEGRITY_GATE=PASS
DUAL_GPU_MEMORY_RESIDENCY=PASS
LOCAL_AUTH_GATEWAY=PASS
LOCAL_NONSTREAM_DEMO=PASS
LOCAL_SSE_DEMO=PASS
SEMANTIC_SANITY_GATE=PASS
DFLASH2_ACTIVITY_GATE=PASS
VISIBLE_ANSWER_GATE=PASS
CORE_PRODUCTION_DEMO=PASS
GATEWAY_RUNTIME_LIMITS_PROCESS_GATE=PASS
PUBLIC_QUICK_TUNNEL=PASS
CLOUDFLARED_SHA256_VERIFIED=PASS
PUBLICATION_SECRET_SCAN=PASS
PUBLICATION_ARTIFACT_BUNDLE=PASS
OVERALL_PRODUCTION_DEMO=PASS
AUTO_CLEANUP=PASS
GENERATED_TOKEN_CLEANUP=PASS
RERUN_SAME_NOTEBOOK=SUPPORTED
FINAL_NOTEBOOK_RESULT=PASS
NOTEBOOK_EXECUTION_COMPLETED=PASS
```

## Publication evidence

The canonical successful run is documented in
[`PUBLICATION_EVIDENCE.md`](PUBLICATION_EVIDENCE.md). The evidence bundle is a
GitHub Release asset, not a tracked repository blob, so Git history remains
small and the release-build evidence stays distinct from executed-run evidence.

The clean notebook source can be reused for repeated tests. Use `muse_stop()`
to stop notebook-owned services or `muse_reset(...)` to clear runtime state,
then run the notebook again. The recorded publication evidence represents one
successful qualification run and does not claim a separately qualified second
full Run All.

## Publication rule

The repository notebook is published clean, with no cell outputs and no embedded credentials.

Executed qualification notebooks and operational evidence are review artifacts; they are not committed as the canonical source notebook.

Before repository publication, verify the tracked notebook SHA-256 against:

`notebooks/kaggle-production.ipynb.sha256`
