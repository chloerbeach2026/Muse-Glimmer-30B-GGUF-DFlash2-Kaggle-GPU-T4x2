# Contributing to Muse-Glimmer-30B
> 🌐 Language / Ngôn ngữ: **English** | [Tiếng Việt](CONTRIBUTING.vi.md)

Thank you for considering a contribution. This is a small, deliberately
deterministic reference implementation, so changes are welcome when they keep
the frozen runtime contract intact and stay honest about scope.

## Ground rules

- **Never change the frozen runtime behavior** (generation defaults, SSE
  order, `[DONE]`, `finish_reason`, busy/429 semantics, admission slot,
  model selection, GPU split, CUDA flags, llama.cpp revision, quantization,
  prompt/template behavior) without declaring it explicitly and explaining why.
- **No secrets.** Never commit tokens, passwords, cookies, SSH keys, private
  URLs, `.env` values, or any generated state from `artifacts/`.
- **No model binaries.** Never commit GGUF, safetensors, `.pt`/`.pth`, or other
  weight files; this repository stays a source-only reference.
- **Keep the determinism.** Tests must run CPU-only with no GPU, Kaggle,
  tunnel, or network access.
- **Keep sizes sane.** Avoid adding large generated blobs; if something must be
  large, discuss it first.

## Before you start

- Check open issues/PRs to avoid duplicated work.
- For behavior-affecting ideas, open an issue to discuss scope first.
- State the change motivation and, for logic changes, the impact on runtime
  behavior in the PR description.

## Development workflow

```bash
# Fresh repo
git clone https://github.com/chloerbeach2026/Muse-Glimmer-30B-GGUF-DFlash2-Kaggle-GPU-T4x2.git
cd Muse-Glimmer-30B-GGUF-DFlash2-Kaggle-GPU-T4x2

# Deterministic CPU-only contract suite
./demo-smoke.sh

# Static checks (no GPU/model needed)
for f in serve.sh expose.sh external.sh readiness.sh doctor.sh release-export.sh \
         demo-smoke.sh package-llama-runtime.sh scripts/*.sh; do bash -n "$f"; done
python3 -m py_compile scripts/*.py tests/*.py
```

## Tests

- Every PR must keep `./demo-smoke.sh` green (31+ assertions).
- Add/extend `tests/test_demo_smoke.py` or `tests/test_public_contract.py` for
  the behavior you touch when a regression test is feasible on CPU.
- The CI pipeline runs the full CPU-safe suite on every push/PR.

## Documentation

- All public Markdown documentation has an English `*.md` and a Vietnamese
  `*.vi.md` counterpart (see the language line directly under each H1).
- When you change a command, port, filename, limitation, or security warning,
  update **both** language versions with the same values.
- Keep the English and Vietnamese documents semantically parallel.

## Source integrity

- Runtime files are covered by `SOURCE_MANIFEST.sha256`. If you add/modify a
  file under `config/`, `scripts/`, or `tests/` (or the tracked root files),
  regenerate the manifest:

```bash
python3 -B scripts/source_manifest.py write --root .
```

  and include the updated `SOURCE_MANIFEST.sha256` in the same commit.

## Opening a PR

Use the pull-request template; it asks you to confirm:

- tests still pass;
- scope of the change;
- EN/VI documentation parity is kept;
- no secrets are introduced;
- no model binaries are introduced;
- docs are updated;
- any behavior change is explicitly declared.

Keep PRs focused. Large changes should be split into reviewable commits that
follow the existing history style (professional imperative subjects, no
internal version labels).

## Review expectations

- Maintainers may ask for justification of any behavioral change.
- The source-integrity manifest must stay in sync with the tree.
- Contributions are tracked under their own author identity; contributor work
  is never rewritten into the maintainer's identity.

## Code of conduct

Participation is governed by our
[Code of Conduct](CODE_OF_CONDUCT.md).