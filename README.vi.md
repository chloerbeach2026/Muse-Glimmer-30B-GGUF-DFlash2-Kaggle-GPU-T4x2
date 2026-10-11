# Muse-Glimmer-30B — Personal Agent Server (chỉ Hugging Face)
> 🌐 Language / Ngôn ngữ: [English](README.md) | **Tiếng Việt**

![Release](https://img.shields.io/badge/release-v1.0.0-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![CI](https://github.com/chloerbeach2026/Muse-Glimmer-30B-GGUF-DFlash2-Kaggle-GPU-T4x2/actions/workflows/ci.yml/badge.svg)
![Kaggle T4x2](https://img.shields.io/badge/kaggle-T4x2-orange)
![NVIDIA T4 x2](https://img.shields.io/badge/nvidia-T4%20x2-lightgray)
![llama.cpp](https://img.shields.io/badge/runtime-llama.cpp-606060)
![DFlash2](https://img.shields.io/badge/speculative-DFlash2-606060)
![OpenAI compatible](https://img.shields.io/badge/API-OpenAI--compatible-606060)
![Self-hosted](https://img.shields.io/badge/reference-self--hosted-606060)

**Server inference Muse-Glimmer-30B tự host cho lập trình agentic dài hạn qua client tương thích OpenAI: tải asset từ HF có pin revision, DFlash2, tool-call history, Context mục tiêu tối đa, hàng đợi hữu hạn và Cloudflare Quick/Named Tunnel tùy chọn.**

> **Đây không phải repository upstream của model Muse-Glimmer-30B.**
> Repository này cung cấp serving stack tái lập được, runtime đã pin, authenticated gateway, Kaggle production notebook và operator workflow để chạy model trên compute của chính bạn.

### Inference stack canonical

- **Target model:** `Muse-Glimmer-30B-Q4_K_M.gguf`
- **Draft model:** `Muse-Glimmer-30B-DFlash2-Q4_K_M.gguf`
- **Inference runtime:** `llama.cpp` với CUDA
- **Tăng tốc:** DFlash2 speculative decoding
- **Compute mục tiêu:** NVIDIA **T4 x2** (cần qualification lại sau các thay đổi này)
- **API:** chat completions tương thích OpenAI với SSE streaming
- **Lớp truy cập:** gateway xác thực bằng Bearer token
- **Public transport tùy chọn:** Cloudflare Quick Tunnel hoặc Named Tunnel có token

> **Repository này không cung cấp dịch vụ inference hosted dùng chung.**
> **Người dùng tự chạy stack trên tài khoản/hạ tầng Kaggle của mình và tự sử dụng GPU quota của mình.**

---

## Mục lục

1. [Tổng quan](#tổng-quan)
2. [Phạm vi / Đây là gì](#phạm-vi--đây-là-gì)
3. [Đây không phải là gì](#đây-không-phải-là-gì)
4. [Môi trường đã kiểm thử](#môi-trường-đã-kiểm-thử)
5. [Kiến trúc](#kiến-trúc)
6. [Yêu cầu](#yêu-cầu)
7. [Tệp mô hình](#tệp-mô-hình)
8. [Xác minh toàn vẹn / hash](#xác-minh-toàn-vẹn--hash)
9. [Khởi động nhanh trên Kaggle T4x2](#khởi-động-nhanh-trên-kaggle-t4x2)
10. [Cấu hình](#cấu-hình)
11. [Khởi động backend](#khởi-động-backend)
12. [Khởi động gateway](#khởi-động-gateway)
13. [Kiểm tra readiness](#kiểm-tra-readiness)
14. [Sử dụng API](#sử-dụng-api)
15. [Ví dụ SSE](#ví-dụ-sse)
16. [Hành vi busy / đồng thời](#hành-vi-busy--đồng-thời)
17. [Tunnel công khai tùy chọn](#tunnel-công-khai-tùy-chọn)
18. [Tắt máy / dọn dẹp](#tắt-máy--dọn-dẹp)
19. [Xử lý sự cố](#xử-lý-sự-cố)
20. [Bảo mật](#bảo-mật)
21. [Hạn chế đã biết](#hạn-chế-đã-biết)
22. [Tái lập](#tái-lập)
23. [Giấy phép](#giấy-phép)
24. [Đóng góp / hỗ trợ](#đóng-góp--hỗ-trợ)

---

## Tổng quan

Một stack serving đầy đủ cho các mô hình GGUF `Muse-Glimmer-30B`:

- **Backend loopback bền vững** — `llama-server` bind tại `127.0.0.1:8088`,
  được khởi động và giám sát bởi `./serve.sh`.
- **DFlash2 speculative decoding** — một mô hình draft GGUF 1,6 GB đính kèm
  tăng tốc quá trình sinh token; tắt bằng `SERVE_PROFILE=baseline`.
- **Gateway xác thực** — `./expose.sh` chạy `scripts/auth_proxy.py`, một proxy
  Bearer-auth loopback tại `127.0.0.1:8090` thực thi một public contract nghiêm
  ngặt và bảo toàn streaming SSE.
- **Điều phối một lệnh** — `./external.sh` điều khiển readiness của backend,
  khởi động exposure, trạng thái, lấy endpoint và tắt máy.

## Phạm vi / Đây là gì

- Một **reference implementation tự-host** và runbook tái lập được.
- Một **production-style demo** gateway: giới hạn xác định, một admission slot,
  token-bucket rate limit, request ID không tiết lộ, envelop lỗi `muse_*` có
  cấu trúc và phản hồi streaming.
- Tối ưu cho **compute thuộc quyền người dùng**: bạn chạy trong chính phiên
  notebook Kaggle có accelerator "GPU T4 x2" và trả bằng quota của mình.
- Có thể được cộng đồng review: mọi thứ deterministic và test được trên CPU
  mà không cần GPU (xem [Tái lập](#tái-lập)).

## Đây không phải là gì

- Không phải dịch vụ API managed/hosted.
- Không có SLA, lời hứa HA, hay cam kết uptime.
- Không phải nền tảng "enterprise production".
- Không phải toolkit huấn luyện hay fine-tune mô hình.
- Không phải bài giới thiệu `llama.cpp` hay GGUF nói chung.

## Môi trường đã kiểm thử

| Mục | Giá trị |
| --- | --- |
| Nền tảng | Phiên notebook GPU Kaggle |
| Accelerator | NVIDIA T4 x2 (Kaggle "GPU T4 x2") |
| Số GPU yêu cầu | 2, mỗi GPU ≥ 14000 MiB VRAM (`GPU_REQUIRED_COUNT=2`, `GPU_MIN_VRAM_MIB=14000`) |
| CUDA devices | `CUDA_VISIBLE_DEVICES=0,1` |
| GPU split | layer-split `1,1`, toàn bộ layer trên GPU (`GPU_SPLIT_MODE=layer`, `GPU_TENSOR_SPLIT=1,1`, `GPU_LAYERS=999`) |
| llama.cpp | runtime commit cố định `64f765f5adefa4620dddda436ce56f1430435536` (CUDA, sm_75) |
| Python | 3.12 (venv runtime do `scripts/setup.sh` dựng) |
| Shell | bash với `set -Eeuo pipefail` |

## Kiến trúc

```text
  client
    |
    |  HTTPS (tùy chọn cloudflared quick/named tunnel)
    v
  scripts/auth_proxy.py           127.0.0.1:8090   gateway Bearer auth
    |                                  |            single admission slot,
    |                                  |            token-bucket rate limit,
    v                                  v            bảo toàn SSE
  llama-server (chỉ loopback)     127.0.0.1:8088   backend bền vững
    |
    v
  GGUF target + DFlash2 draft (Kaggle inputs đính kèm)
```

Các entrypoint điều khiển:

- `./serve.sh` — start / status / stop / restart backend bền vững.
- `./expose.sh` — start / status / stop gateway xác thực (và tunnel cloudflared
  tùy chọn).
- `./external.sh` — điều phối một lệnh: `preflight`, `start`, `status`,
  `endpoint`, `local-endpoint`, `stop`, `stop-all`, với `--format text|env|json`.
- `./readiness.sh` — bộ kiểm tra readiness chỉ-đọc cho notebook mới.

## Yêu cầu

- Linux x86-64, Python 3.12, Bash, Git, CMake/build tools, CUDA toolkit và layout GPU theo `config/default.env` (hiện cần 2 GPU, mỗi GPU ít nhất 14.000 MiB; mục tiêu qualification là Kaggle T4 x2).
- Internet để tải asset từ Hugging Face và clone/build llama.cpp từ commit đã pin trong lần chạy đầu.
- `MUSE_API_TOKEN` riêng có ít nhất 32 k## Model assets (chỉ tải từ Hugging Face)

Server từ chối dùng weights gắn từ Kaggle và tải target, draft DFlash2, vision projector từ revision Hugging Face bất biến. Mỗi asset được xác minh theo dung lượng byte và SHA-256 trước khi serving.

| Asset | Repository HF | Revision đã pin | File | Dung lượng mong đợi | SHA-256 |
| --- | --- | --- | --- | ---: | --- |
| Target | `bartowski/Muse-Glimmer-30B-GGUF` | `16f5840bef28e4018cf41a7ec9709d6fd89176f6` | `Muse-Glimmer-30B-Q4_K_M.gguf` | 17.306.324.000 B | `0d3fc85f61d10fdc84072f0bba6005d61c1ac5605a2627b0fd5ea4ff8194c384` |
| Draft DFlash2 | `meta-models/Muse-Glimmer-30B-GGUF` | `43c7eadd41352a299ea8e0a36b3157978dd63596` | `dflash-Muse-Glimmer-30B-Q4_K_M.gguf` | 1.631.208.128 B | `b2e808bf656086fe86bd0d0bd990f01d33e377537a07c02d45371517c8b264ef` |
| Vision projector | `meta-models/Muse-Glimmer-30B-GGUF` | `43c7eadd41352a299ea8e0a36b3157978dd63596` | `mmproj-Muse-Glimmer-30B-Q4_K_M.gguf` | 1.400.328.928 B | `f48b452316f9b213758e8659444029b961a24a07f99a1abb2a9f88b06f7c00c6` |

Politika download: `HF_DOWNLOAD_WORKERS=32`, `HF_XET_NUM_CONCURRENT_RANGE_GETS=32`, Xet high-performance, retry và resume qua Hub cache. Cấu hình nhắm tới 32 Xet range request đồng thời cho mỗi file và tối đa 32 worker tải file; khi tải một file đơn lẻ, không có nghĩa 32 worker file đều hoạt động.

Không commit model vào Git. Cache local `models/` chỉ được tái sử dụng sau khi xác minh exact identity. Cache thiếu, ngắn hoặc hỏng sẽ được tải lại từ revision HF đã pin; server không start nếu size hoặc SHA-256 không khớp.

gle có thể thay đổi lớp mount bên ngoài, vì vậy notebook và runtime chủ động tìm đệ quy các input lồng nhau. Backend đọc target model đính kèm **tại chỗ** và resolve draft theo exact filename. Serving vẫn là exact: cả hai model identity đều được kiểm lại theo kích thước và SHA-256 trước mỗi lần start.

## Xác minh toàn vẹn / hash

Mỗi lần `./serve.sh start` (và do đó mỗi `./external.sh start`) trong chế độ
strict đều xác minh:

1. `scripts/source_manifest.py check` — cây nguồn phải khớp
   `SOURCE_MANIFEST.sha256` (chỉ vô hiệu bằng `SOURCE_INTEGRITY_MODE=warn|off`).
2. Commit llama.cpp runtime cố định và manifest đã xác minh của nó
   (`LLAMA_RUNTIME_MANIFEST_VERIFIED=1` với runtime dựng sẵn).
3. Kích thước và SHA-256 của target model (`17306324000` /
   `0d3fc85f61d10fdc84072f0bba6005d61c1ac5605a2627b0fd5ea4ff8194c384`).
4. Kích thước và SHA-256 của DFlash2 draft (`1645657280` /
   `93dbfb6f88e4645dec1347cf93f9d6fc80b90d413038722385b2a8e53565c949`).

Xác minh thủ công tệp mô hình đính kèm:

```bash
find /kaggle/input -type f -name 'Muse-Glimmer-30B-Q4_K_M.gguf' -print -exec sha256sum {} \;
find /kaggle/input -type f -name 'Muse-Glimmer-30B-DFlash2-Q4_K_M.gguf' -print -exec sha256sum {} \;
```

## Notebook production canonical

Để chạy production-style trên Kaggle theo hướng dẫn đầy đủ, sử dụng [`notebooks/kaggle-production.ipynb`](notebooks/kaggle-production.ipynb).

Notebook xác minh T4 x2 hardware gate, model/runtime inputs đính kèm, commit `main` hiện tại của fork, xử lý Bearer token an toàn, real non-stream và SSE generation, Quick Tunnel transport tùy chọn, evidence và cleanup.

Xem [`docs/KAGGLE_PRODUCTION.vi.md`](docs/KAGGLE_PRODUCTION.vi.md) để biết operator contract và các release qualification gate.

## Bằng chứng publication

Run Kaggle T4 x2 trong publication record lịch sử thuộc source upstream `v1.0.0`. Nó **không** xác nhận chất lượng hoặc hành vi của code hiện tại trong fork này. Cần chạy Kaggle mới từ fork trước khi phát hành evidence bundle riêng.

- [Releases của fork này](https://github.com/chloerbeach2026/Muse-Glimmer-30B-GGUF-DFlash2-Kaggle-GPU-T4x2/releases) (chỉ publish evidence sau một run mới từ fork)

Xem [tài liệu publication evidence](docs/PUBLICATION_EVIDENCE.vi.md) để biết phạm vi các claim lịch sử và cách xác minh.

## Khởi động nhanh trên Kaggle T4x2

```bash
# 1) Tạo notebook/session Kaggle với accelerator "GPU T4 x2".
# 2) Đính kèm đúng các Kaggle resource/variation trong "Tệp mô hình".
# 3) Clone repository này vào /kaggle/working.

git clone https://github.com/chloerbeach2026/Muse-Glimmer-30B-GGUF-DFlash2-Kaggle-GPU-T4x2.git
cd Muse-Glimmer-30B-GGUF-DFlash2-Kaggle-GPU-T4x2

# 4) Kiểm tra notebook sẵn sàng (chỉ đọc; không cần GPU/mô hình).
./readiness.sh

# 5) Đặt secret gateway của bạn (>= 32 ký tự in được).
export MUSE_API_TOKEN='thay-bang-secret-it-nhat-32-ky-tu'

# 6) Kiểm tra cấu hình external mà không khởi động gì.
./external.sh preflight

# 7) Khởi động backend + gateway + tunnel công khai (EXPOSURE_MODE=quick mặc định).
./external.sh start

# 8) In endpoint HTTPS công khai.
./external.sh endpoint

# 9) Xem trạng thái tổng hợp backend/exposure.
./external.sh status
```

Lần start đầu dựng venv Python, giải quyết llama.cpp runtime cố định (dựng
sẵn hoặc build nguồn) và chuẩn bị trạng thái chính xác của mô hình; có thể mất
một vài phút. Các lần start sau tái sử dụng trạng thái ready.

## Cấu hình

Giá trị mặc định nằm trong `config/default.env` và bị ghi đè bởi biến môi
trường. Các nút bấm chính:

| Biến | Mặc định | Mục đích |
| --- | --- | --- |
| `SERVE_PROFILE` | `dflash2` | `dflash2` bật DFlash2; `baseline` tắt |
| `SERVER_PORT` / `SERVER_HOST` | `8088` / `127.0.0.1` | Bind backend loopback |
| `EXPOSURE_PROXY_PORT` | `8090` | Bind auth-gateway loopback |
| `EXPOSURE_MODE` | `quick` | `quick`, `named`, hoặc `proxy` (BYO tunnel) |
| `MUSE_API_TOKEN` | — | Bearer secret, ≥ 32 ký tự in được |
| `GPU_REQUIRED_COUNT` / `GPU_MIN_VRAM_MIB` | `2` / `14000` | Cổng kiểm GPU |
| `CONTEXT_SIZE` / `MAX_TOKENS` | `4096` / `256` | Cửa sổ sinh token |
| `REASONING_STRENGTH` / `REASONING_BUDGET` | `low` / `64` | Ngân sách reasoning cho chat |
| `REQUEST_TIMEOUT` | `3600` | Hạn chót cứng của yêu cầu gateway (giây) |
| `PROMPT_LIMIT` | `3` | Số message chat tối đa mỗi yêu cầu |
| `SOURCE_INTEGRITY_MODE` | `strict` | `strict` / `warn` / `off` cho kiểm tra source manifest |

Mặc định sinh token: `TEMPERATURE=1.0`, `TOP_P=0.95`, `TOP_K=64`, `SEED=42`.
Không thay đổi serving contract chuẩn (bind loopback, layer split,
`PARALLEL_SLOTS=1`, draft `DFLASH_DRAFT_N_MAX=15`) — `serve.sh` fail-fast nếu
vi phạm.

## Khởi động backend

```bash
./serve.sh start      # start llama-server bền vững tại 127.0.0.1:8088
./serve.sh status     # blob env dạng máy (PROFILE/HOST/PORT/PROVENANCE/...)
./serve.sh restart
./serve.sh stop
```

Backend chỉ-loopback: `SERVER_HOST` phải là `127.0.0.1` và
`SERVER_ALLOW_NONLOOPBACK` phải là `0`. Truy cập ngoài không bao giờ là chính
server — luôn đi qua gateway xác thực.

## Khởi động gateway

```bash
./expose.sh start     # start auth gateway (và tunnel ở chế độ quick/named)
./expose.sh status
./expose.sh endpoint  # URL HTTPS công khai khi tunnel đang chạy
./expose.sh stop
```

Gateway (`scripts/auth_proxy.py`) thực thi:

- Bearer auth bằng `MUSE_API_TOKEN` cho mọi endpoint được bảo vệ;
- allowlist endpoint (`GET /health`, `GET /ready`, `GET /v1/models`,
  `POST /v1/chat/completions`);
- một admission slot và token-bucket rate limit;
- kiểm tra kích thước body, số message và `max_tokens`;
- envelop lỗi JSON `muse_*` xác định với request ID không tiết lộ;
- pass-through streaming SSE với `[DONE]`.

Với transport BYO, đặt `EXPOSURE_MODE=proxy` và tự chấm dứt tunnel / load
balancer phía trước `127.0.0.1:8090`; khi đó `./external.sh local-endpoint` in
ra URL gateway cục bộ chuẩn.

## Kiểm tra readiness

```bash
./readiness.sh                # chế độ quick mặc định
./readiness.sh --mode quick   # kiểm tra source/config (không cần GPU/mô hình)
./readiness.sh --mode proxy   # thêm yêu cầu readiness EXPOSURE_MODE=proxy
./readiness.sh --mode named   # thêm kiểm tra TUNNEL_TOKEN / EXPOSURE_PUBLIC_URL
./readiness.sh --format env   # đầu ra dạng máy
```

`./readiness.sh` chỉ đọc tuyệt đối: không khởi động tiến trình, không tải mô
hình, không tiêu thụ GPU.

## Sử dụng API

Base URL: giá trị của `./external.sh endpoint` (công khai) hoặc
`./external.sh local-endpoint` (chế độ proxy, `http://127.0.0.1:8090`).

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
        "messages": [{"role": "user", "content": "Giải thích dự án này trong hai câu."}],
        "max_tokens": 256
      }'
```

### Ví dụ phản hồi (không streaming)

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

`GET /health` là công khai và báo sức khỏe gateway; `GET /ready` công khai,
probe backend và loại bỏ `Authorization` trước khi liên lạc với nó.

## Ví dụ SSE

```bash
curl -sSN https://<public-endpoint>/v1/chat/completions \
  -H "Authorization: Bearer $MUSE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "muse-glimmer-30B",
        "messages": [{"role": "user", "content": "Đếm từ 1 đến 5."}],
        "stream": true
      }'
```

Streaming được bảo toàn end-to-end: các chunk `text/event-stream` mang
`choices[].delta.content`, luồng kết thúc bằng `data: [DONE]`, và
`finish_reason` (`stop`/`length`) được phát ra trước `[DONE]`.

## Hành vi busy / đồng thời

Demo chỉ nhận **một yêu cầu tại một thời điểm** (`PARALLEL_SLOTS=1`). Khi một
yêu cầu đang xử lý, bạn nhận:

```text
HTTP/1.1 429
Retry-After: 5
```

```json
{
  "error": {
    "type": "muse_demo_busy",
    "message": "Another inference is in progress; the demo accepts one request at a time."
  }
}
```

Việc cấp admission lặp lại bị chặn bởi một slot lock chính xác và được kiểm
tra trong bộ test deterministic. Rate limit, giới hạn payload và timeout cũng
được gateway thực thi trước khi bất kỳ yêu cầu nào chạm tới backend.

## Tunnel công khai tùy chọn

- `EXPOSURE_MODE=quick` (mặc định) — cloudflared quick tunnel; in URL bằng
  `./external.sh endpoint`.
- `EXPOSURE_MODE=named` — yêu cầu `TUNNEL_TOKEN` và một `EXPOSURE_PUBLIC_URL`
  HTTPS hợp lệ (cấu hình Tunnel).
- `EXPOSURE_MODE=proxy` — tự mang tunnel/LB; gateway vẫn loopback trên
  `127.0.0.1:8090` và `./external.sh local-endpoint` in URL của nó.

Mọi đường công khai vẫn được gateway xác thực Bearer; tunnel chỉ là tầng vận
chuyển và không bao giờ phơi trực tiếp `llama-server`.

## Tắt máy / dọn dẹp

```bash
./external.sh stop       # tắt exposure (gateway/tunnel); giữ backend chạy
./external.sh stop-all   # tắt exposure, rồi tắt backend đang sở hữu
./expose.sh stop         # chỉ gateway/tunnel
./serve.sh stop          # chỉ backend
```

State và log nằm dưới `artifacts/runtime-state/` (bị Git bỏ qua). Để reset
hoàn toàn một phiên, tắt mọi thứ và xóa `artifacts/` cùng các thư mục runtime
(`vendor/`, `models/`, `runs/`).

## Xử lý sự cố

| Triệu chứng | Nguyên nhân / cách khắc phục |
| --- | --- |
| `readiness.sh` báo blocker `SOURCE_MANIFEST` | Đã sửa tệp runtime → chạy lại `bash scripts/source-manifest.sh write` để tái tạo `SOURCE_MANIFEST.sha256` |
| `preflight` lỗi credentials | Đặt `MUSE_API_TOKEN` ≥ 32 ký tự in được; với `named`, thêm `TUNNEL_TOKEN`/`EXPOSURE_PUBLIC_URL` |
| Không tìm thấy `cloudflared` | Cài đặt vào `PATH`, hoặc đặt `CLOUDFLARED_BIN` trỏ tới tệp thực thi |
| Backend lỗi kiểm định danh tính | Đính kèm đúng các Kaggle inputs trong [Tệp mô hình](#tệp-mô-hình) |
| `429 muse_demo_busy` | Đang có generation chạy; thử lại sau cửa sổ `Retry-After` |
| Lần start đầu chậm | Bootstrap runtime + chuẩn bị state mô hình là việc làm một lần |

## Bảo mật

Xem [SECURITY.md](SECURITY.md). Những điểm chính:

- Gateway **không bao giờ** ghi log hoặc lưu bearer tokens, prompts,
  completions, reasoning, hay raw request bodies; telemetry chỉ chứa counter.
- Mô hình và runtime được khóa bằng SHA-256 / commit và kiểm lại khi start.
- Production notebook pin Cloudflare `cloudflared` ở release `2026.10.0` và xác minh SHA-256 chính thức trước khi thực thi.
- Backend chỉ-loopback: truy cập ngoài luôn qua gateway xác thực.
- Tunnel công khai đưa gateway lên Internet — đó là trách nhiệm của bạn với tư
  cách operator, và bạn phải tự bảo vệ token của mình.

## Hạn chế đã biết

- Một yêu cầu đồng thời (`PARALLEL_SLOTS=1`) theo thiết kế.
- Yêu cầu đúng hồ sơ phần cứng đã kiểm thử (`GPU T4 x2` trên Kaggle,
  ≥ 14000 MiB mỗi GPU).
- Không có cô lập tenant đa người dùng; gateway là demo một chủ sở hữu.
- Không fine-tune mô hình, không training, không embedding endpoints.
- Runtime/model pinned là exact; không hỗ trợ thay thế tự do bởi persistent
  server.
- Đây là reference implementation được duy trì theo mô hình cộng đồng
  best-effort, không phải dịch vụ thương mại.

## Tái lập

- **Toàn vẹn nguồn:** các tệp runtime được bao phủ bởi
  `SOURCE_MANIFEST.sha256` (xem `scripts/source_manifest.py`); tính toàn vẹn
  runtime được thực thi ở chế độ strict khi start.
- **Test deterministic (chỉ CPU, không GPU/mô hình/tunnel/secrets):**
  `./demo-smoke.sh` chạy gateway thật với một fake loopback backend và kiểm
  tra toàn bộ public contract (auth, allowlist, forwarding, SSE, busy 429,
  rate limits, timeout, telemetry, slot release). CI chạy nó trên mỗi
  push/PR.
- **Runtime cố định:** llama.cpp commit
  `64f765f5adefa4620dddda436ce56f1430435536`, CUDA target sm_75.
- **Fresh clone** tái lập đúng contract mà không cần mô hình, GPU hay mạng.

## Giấy phép

- **Source và tài liệu repository:** MIT — xem [LICENSE](LICENSE).
  Copyright (c) 2026 Đăng Khoa.
- **Model weights:** các tệp GGUF là artifact của bên thứ ba với điều khoản
  giấy phép upstream riêng (ví dụ từ repository
  `bartowski/Muse-Glimmer-30B-GGUF` và input DFlash2 đính kèm). Repository
  này **không** tuyên bố model weights là MIT, và giấy phép MIT ở đây không mở
  rộng sang weights.

## Đóng góp / hỗ trợ

- [CONTRIBUTING.md](CONTRIBUTING.md) — cách đóng góp (tài liệu song ngữ, khai
  báo tường minh thay đổi hành vi, tests, không secrets, không model blobs).
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) — chuẩn mực cộng đồng.
- [SUPPORT.md](SUPPORT.md) — nơi đặt câu hỏi và báo sự cố.
- Vấn đề bảo mật: xem [SECURITY.md](SECURITY.md).