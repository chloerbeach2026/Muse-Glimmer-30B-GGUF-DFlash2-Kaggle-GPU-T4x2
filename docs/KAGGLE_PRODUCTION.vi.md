# Hướng dẫn vận hành Kaggle / Personal Agent Server
> 🌐 Language / Ngôn ngữ: [English](KAGGLE_PRODUCTION.md) | **Tiếng Việt**

Notebook `notebooks/kaggle-production.ipynb` là quy trình **qualification** cho fork này. Notebook dùng chung các script serving của repository và tải model từ revision Hugging Face đã pin. Kaggle không được xem là máy chủ bảo đảm chạy 24/7.

## Notebook xác minh những gì

Một lần chạy đầy đủ cần xác minh:
- phần cứng NVIDIA T4 x2 và bộ nhớ GPU khả dụng;
- revision Hugging Face chính xác của target, draft DFlash2 và vision projector, dung lượng và SHA-256;
- commit của fork, source manifest và build llama.cpp từ source đã pin;
- Context/output được yêu cầu và hiệu lực, bao gồm fallback nếu thiếu tài nguyên;
- gateway Bearer-auth, phản hồi non-stream, SSE và payload tool-call nhiều lượt;
- Cloudflare Quick Tunnel tùy chọn và cleanup an toàn.

Verdict của notebook chỉ áp dụng cho lần chạy đã thực hiện. Tài liệu này không khẳng định Kaggle luôn sẵn sàng hoặc Context 131.072 token chắc chắn vừa mọi cấu hình GPU.

## Biên runtime

Mục tiêu serving cá nhân là Linux x86-64 với hai GPU, Python 3.12 và toolchain CUDA hoạt động. Môi trường qualification mục tiêu là Kaggle NVIDIA T4 x2. Để dùng hàng ngày ổn định, hãy triển khai trên GPU host có vòng đời liên tục; session Kaggle có thể bị dừng hoặc tái tạo.

Context model được yêu cầu là 131.072 token. Server thử mức đó trước với KV cache quantized. Khi startup thất bại vì tài nguyên, server có thể thử Context nhỏ hơn và báo giá trị thực tế trong `./serve.sh status`, response headers và runtime metadata. Gateway giới hạn output request vượt mức theo budget hiệu lực. Cơ chế này giảm lỗi lệch cấu hình nhưng không đảm bảo tốc độ hoặc Context cụ thể cho mọi workload.

## Nguồn model (chỉ Hugging Face)

Không attach model weights vào Kaggle. Server tải đúng file từ revision Hugging Face bất biến và xác minh dung lượng cùng SHA-256 trước khi serving.

Dataset duy nhất bạn có thể attach là runtime llama.cpp dựng sẵn (`muse-glimmer-30b-dflash2-llama-runtime`, đóng gói bằng `./package-llama-runtime.sh`). Khi nó được attach dưới Kaggle Input, setup sẽ dùng lại binary đã xác minh manifest và không bao giờ compile trong phiên (`LLAMA_SOURCE_MODE=kaggle`); khi dataset vắng mặt hoặc fail xác minh, setup dừng ngay với lỗi rõ ràng thay vì build, nên mọi lần chạy phục vụ đúng commit runtime đã ghim.

| Asset | Repository | Revision | File | Byte mong đợi | SHA-256 |
| --- | --- | --- | --- | ---: | --- |
| Target | `bartowski/Muse-Glimmer-30B-GGUF` | `16f5840bef28e4018cf41a7ec9709d6fd89176f6` | `Muse-Glimmer-30B-Q4_K_M.gguf` | 17306324000 | `0d3fc85f61d10fdc84072f0bba6005d61c1ac5605a2627b0fd5ea4ff8194c384` |
| Draft DFlash2 | `meta-models/Muse-Glimmer-30B-GGUF` | `43c7eadd41352a299ea8e0a36b3157978dd63596` | `dflash-Muse-Glimmer-30B-Q4_K_M.gguf` | 1631208128 | `b2e808bf656086fe86bd0d0bd990f01d33e377537a07c02d45371517c8b264ef` |
| Vision projector | `meta-models/Muse-Glimmer-30B-GGUF` | `43c7eadd41352a299ea8e0a36b3157978dd63596` | `mmproj-Muse-Glimmer-30B-Q4_K_M.gguf` | 1400328928 | `f48b452316f9b213758e8659444029b961a24a07f99a1abb2a9f88b06f7c00c6` |

Download sử dụng `HF_DOWNLOAD_WORKERS=32`, `HF_XET_NUM_CONCURRENT_RANGE_GETS=32`, Xet high-performance, retry và Hub cache resume. Mục tiêu là 32 range request đồng thời cho file Xet lớn và tối đa 32 worker tải file; điều này không có nghĩa 32 worker sẽ cùng tải một file. Repository public không cần HF token; nếu asset gated, dùng token chỉ đọc qua biến môi trường/secret manager, không commit token.

Cache target, draft và projector chỉ được dùng lại nếu dung lượng byte chính xác và SHA-256 khớp. File hỏng hoặc tải dở sẽ bị xóa và tải lại; startup phải từ chối tiếp tục nếu kiểm tra không đạt.

## Secret khuyến nghị

Đặt `MUSE_API_TOKEN` thành secret riêng tư dài ít nhất 32 ký tự in được, thông qua secret manager hoặc biến môi trường tiến trình. Không commit hay in token vào log và không dùng Cloudflare tunnel token làm API token.

## Quy trình chạy

Trên GPU host tương thích (hoặc để qualification trên Kaggle):

1. Chọn GPU phù hợp và bật network cho lần tải source/model đầu tiên.
2. Clone fork này và vào thư mục repository.
3. Đặt `MUSE_API_TOKEN` trong shell hoặc secret manager.
4. Chạy `./readiness.sh` và `./external.sh preflight`.
5. Chạy `./external.sh start`, sau đó `./external.sh status` và `./external.sh endpoint`.
6. Đọc Context/output hiệu lực; cấu hình từng desktop client theo giá trị thực tế đó.
7. Kiểm tra tool loop nhiều lượt và streaming trước khi cho chạy unattended.

Ví dụ:

```bash
export MUSE_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
./readiness.sh
./external.sh preflight
./external.sh start
./external.sh status
./external.sh endpoint
```

## Hợp đồng giao thức agent

Gateway cung cấp OpenAI-compatible `/v1/chat/completions`. API chấp nhận role `system`, `developer`, `user`, `assistant`, `tool`, assistant `tool_calls`, kết quả `tool_call_id`, content-part array đa phương thức, cả `max_tokens` và `max_completion_tokens`, cùng SSE. Chạy fake-backend CPU test trước khi kết nối harness agent.

### OpenCode

Dùng provider OpenAI-compatible với base URL `http://127.0.0.1:8090/v1`, model ID `muse-glimmer-30B`, API key lấy từ `MUSE_API_TOKEN`, và `limit.context` / `limit.output` khớp giá trị hiệu lực. README có cấu hình khởi đầu.

### Hermes Agent

Dùng custom OpenAI-compatible model/provider với cùng base URL và model ID. Cấu hình Context/output từ runtime report thực tế, rồi thử chu trình assistant-tool-call → tool-result → assistant-final và session nhiều bước. Context phía client không được lớn hơn Context server đã load.

### Claude Code qua OmniRoute

Dùng OmniRoute làm ingress Anthropic-compatible cho Claude Code và cấu hình upstream OpenAI-compatible về gateway này. Xác nhận đúng phiên bản OmniRoute chuyển đổi chính xác tool-use, streaming events, cancellation và kết quả tool nhiều lượt. Đây là protocol bridge bên thứ ba, không phải tuyên bố hỗ trợ chính thức từ Anthropic.

## Cloudflare exposure khuyến nghị

Installer pin `cloudflared 2026.10.0` và xác minh SHA-256 trước khi thực thi. Chỉ gateway đã xác thực được public; cổng backend 8088 luôn ở loopback.

Quick Tunnel (URL tạm thời):

```bash
export EXPOSURE_MODE=quick
./external.sh start
./external.sh endpoint
```

Named Tunnel (hostname ổn định đã cấu hình):

1. Tạo Named Tunnel và Published Application/DNS route trong Cloudflare Zero Trust.
2. Export `EXPOSURE_MODE=named`, `TUNNEL_TOKEN` và `EXPOSURE_PUBLIC_URL=https://your-hostname.example` qua environment/secret manager.
3. Chạy `./external.sh preflight` rồi `./external.sh start`.

Token được giữ trong file runtime-state quyền 0600 và xóa khi shutdown hoặc startup thất bại. Token một mình không tạo hostname/DNS route. `EXPOSURE_MODE=proxy` là lựa chọn tự quản lý transport.

## Quy tắc publication

Notebook được commit phải sạch, không có output/secret nhúng và vượt qua kiểm tra SHA-256 sidecar. Notebook đã chạy trên Kaggle là bằng chứng cho riêng lần chạy đó. Không công bố PASS trước khi một lần chạy mới trên fork hiện tại thành công.

## An toàn và hạn chế đã biết

Đây là server inference cá nhân một chủ sở hữu, không phải dịch vụ thương mại đa tenant. Chỉ có một inference slot hoạt động cùng hàng đợi giới hạn; không có cụm high availability và Kaggle không bảo đảm uptime 24/7. Bảo vệ API token, giữ cổng 8088 ở private network và kiểm tra lại identity của source/runtime trước khi triển khai.
