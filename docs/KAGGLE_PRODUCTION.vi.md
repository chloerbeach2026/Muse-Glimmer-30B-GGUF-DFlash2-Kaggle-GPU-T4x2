# Notebook Production trên Kaggle
> 🌐 Language / Ngôn ngữ: [English](KAGGLE_PRODUCTION.md) | **Tiếng Việt**

Notebook production canonical cho public release `v1.0.0` là:

`notebooks/kaggle-production.ipynb`

Đây là notebook vận hành dành cho Kaggle NVIDIA T4 x2, sử dụng repository public đã khóa làm runtime source thay vì tự triển khai lại serving stack.

## Notebook xác minh những gì

Một lần chạy đầy đủ xác minh:

- đúng hai GPU NVIDIA T4 với lượng VRAM khả dụng theo yêu cầu;
- source identity của fork, worktree sạch và `SOURCE_MANIFEST.sha256`;
- target GGUF, draft DFlash2 và vision projector chỉ tải từ Hugging Face theo revision bất biến;
- dung lượng byte và SHA-256 chính xác của cả ba asset trước khi serving;
- runtime CUDA llama.cpp đã pin được build từ source nếu chưa có cache tương ứng;
- Context/output thực tế đã load, bao gồm fallback Context nếu tài nguyên GPU không đủ;
- model-memory residency thành công trên cả hai GPU T4 sau khi backend load;
- gateway xác thực xử lý payload agent OpenAI-compatible;
- một generation non-stream thực và một generation SSE thực kết thúc bằng `[DONE]`;
- Cloudflare Quick Tunnel tùy chọn được qualification riêng với sức khỏe model/runtime local;
- version `cloudflared 2026.10.0` đã pin và SHA-256 chính thức theo kiến trúc trước khi thực thi;
- evidence xác định và cleanup, bao gồm xóa API token do notebook tự tạo.
## Biên runtime

Notebook này là workflow qualification Kaggle T4x2, không phải dịch vụ hosted 24/7. Model files chỉ được tải từ revision Hugging Face đã pin và notebook từ chối asset sai size hoặc SHA-256.

Server yêu cầu `CONTEXT_SIZE=131072`, `MAX_TOKENS=131072`, reasoning strength high và DFlash2. Nếu lỗi startup/tài nguyên được nhận diện khiến Context này không load được, server có thể thử Context nhỏ hơn. Gateway đọc metadata runtime thành công và giới hạn generation của client theo Context/output thực tế đã load. Evidence ghi giá trị hiệu lực, không giả định 131K đã thành công.

Notebook dùng Cloudflare Quick Tunnel để qualification transport tùy chọn. Named Tunnel ổn định cần hostname/DNS route và `TUNNEL_TOKEN`; hãy cấu hình trên host bền vững với `EXPOSURE_MODE=named`. Kaggle có thể dừng khi session kết thúc nên không thay thế persistent host cho công việc cá nhân luôn bật.
## Nguồn model (chỉ Hugging Face)

**Không attach** target, draft DFlash2, projector hay runtime dựng sẵn thành Kaggle input. Notebook clone fork này, kiểm tra source manifest, tải weights theo revision Hugging Face bất biến, dùng 32 Xet range request mỗi file và tối đa 32 file worker, retry/resume và kiểm tra SHA-256 chính xác. Runtime llama.cpp được build từ source commit bất biến đã cấu hình nếu build tương ứng chưa có cache.

| Asset | Nguồn | Revision | File local |
| --- | --- | --- | --- |
| Target model | `bartowski/Muse-Glimmer-30B-GGUF` | `16f5840bef28e4018cf41a7ec9709d6fd89176f6` | `Muse-Glimmer-30B-Q4_K_M.gguf` |
| Draft DFlash2 | `meta-models/Muse-Glimmer-30B-GGUF` | `43c7eadd41352a299ea8e0a36b3157978dd63596` | `dflash-Muse-Glimmer-30B-Q4_K_M.gguf` (sau download đổi tên sang file canonical local) |
| Vision projector | `meta-models/Muse-Glimmer-30B-GGUF` | `43c7eadd41352a299ea8e0a36b3157978dd63596` | `mmproj-Muse-Glimmer-30B-Q4_K_M.gguf` |

Size và digest mong đợi được duy trì trong `config/default.env` và xác minh bởi downloader/startup gate. Không lưu weights trong Git.
## Secret khuyến nghị

Tạo Kaggle Secret:

`MUSE_API_TOKEN`

Sử dụng tối thiểu 32 ký tự in được.

Nếu secret bị thiếu, notebook tạo token tạm thời an toàn bằng cơ chế mật mã tại:

`/kaggle/working/.muse-secrets/MUSE_API_TOKEN`

Thư mục dùng mode `0700`, file dùng mode `0600`, giá trị token không bao giờ được in và cleanup bình thường sẽ xóa file được tạo.

## Quy trình chạy

1. Chọn **GPU T4 x2**.
2. Bật Internet để clone repository, tải weights HF và dùng Quick Tunnel tùy chọn.
3. Không attach model/runtime input; notebook tự tải weights từ HF và build runtime CUDA đã pin khi cần.
4. Tùy chọn cấu hình `MUSE_API_TOKEN`.
5. Chạy **Restart Session → Run All**.
6. Xem final evidence và các verdict trước khi publish notebook output.

Một release qualification thành công nên có:

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

## Bằng chứng publication

Successful run canonical được mô tả trong
[`PUBLICATION_EVIDENCE.vi.md`](PUBLICATION_EVIDENCE.vi.md). Evidence bundle là
GitHub Release asset chứ không phải blob được track trong repository, nhờ đó
Git history vẫn gọn và release-build evidence được tách rõ khỏi executed-run
evidence.

Notebook source sạch có thể dùng lại để test nhiều lần. Dùng `muse_stop()` để
dừng service thuộc ownership của notebook hoặc `muse_reset(...)` để reset
runtime state, sau đó chạy notebook lại. Publication evidence đã ghi nhận đại
diện cho một qualification run thành công và không claim một full Run All lần
hai đã được qualification riêng.

## Quy tắc publication

Notebook trong repository được publish ở trạng thái sạch, không có cell output và không nhúng credential.

Executed qualification notebook và operational evidence là artifact phục vụ review; chúng không được commit làm canonical source notebook.

Trước khi publication repository, xác minh SHA-256 của notebook tracked với:

`notebooks/kaggle-production.ipynb.sha256`
