# Changelog
> 🌐 Language / Ngôn ngữ: [English](CHANGELOG.md) | **Tiếng Việt**

Mọi thay đổi đáng chú ý của public release này được ghi tại đây. Repository
này duy trì một public release duy nhất; lịch sử phát triển không được ghi
trong tệp này.

## [v1.0.0] - 2026-09-05

### Đã thêm

- **Stack serving reference tự-host** cho các mô hình GGUF `Muse-Glimmer-30B`
  trên phiên notebook Kaggle với hai GPU NVIDIA T4 (Kaggle "GPU T4 x2").
- **Backend loopback bền vững:** `./serve.sh` khởi động, giám sát và dừng
  `llama-server` bind tại `127.0.0.1:8088` với cưỡng chế chỉ-loopback,
  layer-split `1,1`, toàn bộ layer trên GPU, `PARALLEL_SLOTS=1`, và kiểm tra
  định danh mô hình/runtime cố định (kích thước và SHA-256) trước mỗi lần start.
- **Gateway xác thực:** `./expose.sh` chạy `scripts/auth_proxy.py` trên
  `127.0.0.1:8090` với Bearer auth (`MUSE_API_TOKEN`, ≥ 32 ký tự in được),
  allowlist endpoint nghiêm ngặt, single admission slot, token-bucket rate
  limiting, kiểm tra yêu cầu, envelop lỗi `muse_*` xác định với request ID
  không tiết lộ, và bảo toàn streaming SSE.
- **DFlash2 speculative decoding:** `SERVE_PROFILE=dflash2` (mặc định) dùng một
  GGUF draft model cố định chính xác; `SERVE_PROFILE=baseline` tắt nó.
- **Điều phối một lệnh:** `./external.sh` cung cấp `preflight`, `start`,
  `status`, `endpoint`, `local-endpoint`, `stop`, và `stop-all` với
  `--format text|env|json`.
- **Bộ kiểm tra readiness:** `./readiness.sh` thực hiện kiểm tra chỉ-đọc về
  source integrity, cấu hình, credentials và mức sẵn sàng của external mode,
  không khởi động tiến trình hay tiêu thụ GPU.
- **Công cụ operator:** `./doctor.sh` (trạng thái máy), `./release-export.sh`
  (xuất bundle release chuẩn với nguồn kiểm tra được), và
  `./package-llama-runtime.sh` (đóng gói runtime llama.cpp tái lập được).
- **Ràng buộc toàn vẹn nguồn:** `scripts/source_manifest.py check|write` và
  `SOURCE_MANIFEST.sha256` được commit; thực thi ở chế độ strict khi start
  backend (`SOURCE_INTEGRITY_MODE=strict|warn|off`).
- **Test deterministic, chỉ CPU:** `./demo-smoke.sh` chạy gateway thật với một
  fake loopback backend và kiểm tra toàn bộ public contract (auth, allowlist,
  forwarding, SSE, busy 429, rate limits, timeout, telemetry, slot release),
  không cần mô hình, GPU, tunnel hay mạng.
- **Tài liệu song ngữ:** cặp tiếng Anh (`*.md`) và tiếng Việt (`*.vi.md`) cho
  README, changelog, security, contributing, code of conduct và support.
- **Hạ tầng cộng đồng:** `.github` issue templates, hướng dẫn đóng góp, code
  of conduct, security policy, cấu hình dependabot, và CI chạy trên
  `actions/checkout@v7` (và `actions/setup-python@v7`).

- **Canonical Kaggle production notebook:** notebook song ngữ có hướng dẫn xác minh exact T4 x2 hardware, target/draft/runtime inputs, authenticated gateway, real non-stream và SSE generation, public transport tùy chọn, evidence và cleanup.
- **Public input identity và release packaging tường minh:** link chính xác Kaggle resource/variation được công bố, và GitHub Release publish deterministic source, notebook, evidence cùng checksum assets.

- **Publication-run evidence đã qualification:** successful run thực trên Kaggle T4 x2 được publish thành release asset có checksum riêng cùng reviewer summary, model hash, dual-GPU residency, non-stream/SSE output thực, semantic sanity, DFlash2 counter, secret scanning và cleanup evidence.
- **Lifecycle notebook tái sử dụng:** production notebook sạch có Stop / Reset / Re-run control tường minh để operator test lặp lại trên cùng notebook Kaggle mà không phải tạo notebook thay thế.

### Contract chính

- **API:** `GET /health`, `GET /ready`, `GET /v1/models`,
  `POST /v1/chat/completions` (tương thích OpenAI, hỗ trợ streaming SSE).
- **Đồng thời:** một yêu cầu tại một thời điểm; lúc busy gateway trả
  `429` kèm `Retry-After: 5` và envelop `muse_demo_busy`.
- **Profile serving liên tục (`agent`):** `TEMPERATURE=1.0`, `TOP_P=0.95`,
  `TOP_K=64`, `SEED=42`, `CONTEXT_SIZE=131072`, `MAX_TOKENS=131072`,
  `REASONING_STRENGTH=high`, `REASONING_BUDGET=0`, `MIN_ANSWER_TOKENS=1024`,
  `PROMPT_LIMIT=0`, `REQUEST_TIMEOUT=3600`, `AUTO_CONTEXT_FALLBACK=1`.
  Context 131.072 token là mục tiêu của model; server thử trước với KV cache
  lượng hóa và chỉ fallback về giá trị nhỏ hơn khi gặp lỗi startup/resource
  (OOM, allocation failure, KV cache failure). Context/output hiệu dụng được
  báo cáo bởi `./serve.sh status` và các response header
  `X-Muse-Effective-Context-Size` / `X-Muse-Max-Output-Tokens`.
- **Profile smoke (CI/fake-backend):** `CONTEXT_SIZE=4096`, `MAX_TOKENS=256`,
  `REASONING_STRENGTH=low`, `REASONING_BUDGET=64`. Profile này chỉ được dùng
  bởi `./demo-smoke.sh` với fake loopback backend, không bao giờ dùng cho
  serving stack liên tục.

### Bảo mật

- Telemetry gateway chỉ ghi counter; bearer tokens, prompts, completions,
  reasoning và raw bodies không bao giờ được ghi log hay lưu trữ.
- Backend chỉ-loopback; truy cập ngoài luôn qua gateway xác thực.
- Model weights và llama.cpp runtime được khóa bằng SHA-256 / commit và kiểm
  lại mỗi lần start.
- Xem `SECURITY.md` để biết version được hỗ trợ và quy trình báo lỗi.

### Phạm vi

- Reference implementation; người dùng tự chạy stack trên tài khoản/hạ tầng
  Kaggle của chính họ và tự tiêu thụ GPU quota.
- Giấy phép MIT chỉ bao phủ source và tài liệu repository; model weights là
  artifact của bên thứ ba theo điều khoản upstream riêng.