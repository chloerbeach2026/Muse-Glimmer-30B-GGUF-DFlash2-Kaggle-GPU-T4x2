# Kaggle Production Notebook
> 🌐 Language / Ngôn ngữ: **English** | [Tiếng Việt](KAGGLE_PRODUCTION.vi.md)

The canonical production notebook for the public `v1.0.0` release is:

`notebooks/kaggle-production.ipynb`

It is an operator-focused Kaggle NVIDIA T4 x2 notebook that clones this fork's `main` branch, pins the exact resolved commit SHA for the run, and uses that checkout rather than reimplementing the serving stack.

## What the notebook validates

A complete run validates:

- exactly two NVIDIA T4 GPUs with the required visible VRAM;
- the exact target GGUF, DFlash2 draft GGUF, and prebuilt llama.cpp runtime inputs;
- the resolved fork commit SHA, `VERSION=1.0.0`, a clean worktree, and `SOURCE_MANIFEST.sha256`;
- full SHA-256 verification of both target and DFlash2 GGUF files;
- successful model-memory residency on both T4 GPUs after backend load;
- one deterministic semantic sanity request in addition to deployment prompts;
- observed DFlash2 drafted/accepted counters for all real inference requests;
- Stop / Reset / Re-run lifecycle controls for repeated tests in the same notebook;
- secure `MUSE_API_TOKEN` handling, including a temporary-file fallback when a Kaggle Secret is unavailable;
- the canonical local backend and Bearer-authenticated gateway;
- one real non-streaming generation with visible assistant content;
- one real SSE generation with visible streamed content, a terminal finish reason, and `[DONE]`;
- optional Cloudflare Quick Tunnel transport independently from local model/runtime health;
- Cloudflare `cloudflared` pinned to release `2026.10.0` and accepted only when the official Linux AMD64 SHA-256 matches before execution;
- deterministic evidence and cleanup, including deletion of any notebook-generated token.

## Runtime boundaries

The notebook is a production-style reference workflow, not a managed hosted service.

The local model/runtime qualification is authoritative for the serving core. Quick Tunnel is an optional transport layer and is evaluated separately so external DNS or tunnel instability does not erase a valid local inference result.

The notebook keeps the public request cap at `max_tokens=512`, uses the model-native request template setting `reasoning_strength=low`, and applies a notebook-owned auth-proxy backend response timeout of 120 seconds without modifying the fetched fork checkout.

## Required Kaggle inputs

Attach the **exact** resources below before running the notebook:

| Role | Type | Resource | Required selection | Required identity |
| --- | --- | --- | --- | --- |
| Target model | Kaggle Model | [`dangkhoa2016/bartowski-muse-glimmer-30b-gguf`](https://www.kaggle.com/models/dangkhoa2016/bartowski-muse-glimmer-30b-gguf/Gguf/q4-k-m/1) | **GGUF / `q4-k-m` / version `1`** | `Muse-Glimmer-30B-Q4_K_M.gguf` — `0d3fc85f61d10fdc84072f0bba6005d61c1ac5605a2627b0fd5ea4ff8194c384` |
| DFlash2 draft | Kaggle Model | [`dangkhoa2016/incoai-muse-glimmer-30b-dflash2-gguf`](https://www.kaggle.com/models/dangkhoa2016/incoai-muse-glimmer-30b-dflash2-gguf/Gguf/q4-k-m/1) | **GGUF / `q4-k-m` / version `1`** | `Muse-Glimmer-30B-DFlash2-Q4_K_M.gguf` — `93dbfb6f88e4645dec1347cf93f9d6fc80b90d413038722385b2a8e53565c949` |
| llama.cpp runtime | Kaggle Dataset | [`dangkhoa2016/muse-glimmer-30b-dflash2-llama-runtime`](https://www.kaggle.com/datasets/dangkhoa2016/muse-glimmer-30b-dflash2-llama-runtime) | attach the dataset | pinned prebuilt CUDA runtime |

Use the dedicated DFlash2 `q4-k-m` variation for the canonical production notebook. It mounts only the required Q4_K_M draft (~1.65 GB). The `default` variation is a legacy all-in-one bundle containing Q4_K_M, Q8_0, and BF16 (about 10 GB total), so it is intentionally excluded from the production-demo input contract.

In Kaggle, choose **Add Input**, open each direct link, and attach the specified Model variation/version or Dataset. Models may appear under `/kaggle/input/models/...` and datasets under `/kaggle/input/datasets/...`; the notebook discovers both recursively.

## Recommended secret

Create a Kaggle Secret:

`MUSE_API_TOKEN`

Use at least 32 printable characters.

If the secret is missing, the notebook creates a cryptographically secure temporary token under:

`/kaggle/working/.muse-secrets/MUSE_API_TOKEN`

The directory uses mode `0700`, the file uses mode `0600`, the token value is never printed, and normal cleanup deletes the generated file.

## Run procedure

1. Select **GPU T4 x2**.
2. Enable Internet for the repository clone and optional Quick Tunnel.
3. Use **Add Input** to attach the exact target Model (`q4-k-m/1`), DFlash2 Model (`q4-k-m/1`), and runtime Dataset listed above.
4. Optionally configure `MUSE_API_TOKEN`.
5. Use **Restart Session → Run All**.
6. Review the final evidence and verdict lines before publishing notebook output.

A successful release qualification should include:

```text
KAGGLE_T4X2_GATE=PASS
ATTACHED_INPUT_GATE=PASS
FORK_SOURCE_IDENTITY=PASS
CANONICAL_ENVIRONMENT=PASS
MODEL_SHA256_GATE=PASS
DUAL_GPU_MEMORY_RESIDENCY=PASS
LOCAL_AUTH_GATEWAY=PASS
LOCAL_NONSTREAM_DEMO=PASS
LOCAL_SSE_DEMO=PASS
SEMANTIC_SANITY_GATE=PASS
DFLASH2_ACTIVITY_GATE=PASS
VISIBLE_ANSWER_GATE=PASS
CORE_PRODUCTION_DEMO=PASS
PUBLIC_QUICK_TUNNEL=PASS
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
