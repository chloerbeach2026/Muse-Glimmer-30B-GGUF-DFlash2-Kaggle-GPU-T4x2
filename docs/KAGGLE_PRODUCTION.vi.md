# Notebook Production trên Kaggle
> 🌐 Language / Ngôn ngữ: [English](KAGGLE_PRODUCTION.md) | **Tiếng Việt**

Notebook production canonical cho public release `v1.0.0` là:

`notebooks/kaggle-production.ipynb`

Đây là notebook vận hành dành cho Kaggle NVIDIA T4 x2, sử dụng repository public đã khóa làm runtime source thay vì tự triển khai lại serving stack.

## Notebook xác minh những gì

Một lần chạy đầy đủ xác minh:

- đúng hai GPU NVIDIA T4 với lượng VRAM khả dụng theo yêu cầu;
- exact target GGUF, DFlash2 draft GGUF và prebuilt llama.cpp runtime inputs;
- SHA commit đã resolve của fork, `VERSION=1.0.0`, worktree sạch và `SOURCE_MANIFEST.sha256`;
- full SHA-256 verification cho cả target và DFlash2 GGUF;
- model-memory residency thành công trên cả hai GPU T4 sau khi backend load;
- một deterministic semantic sanity request ngoài các deployment prompt;
- counter DFlash2 drafted/accepted quan sát được cho mọi inference request thực;
- Stop / Reset / Re-run lifecycle controls để test lặp lại trên cùng notebook;
- xử lý `MUSE_API_TOKEN` an toàn, bao gồm fallback bằng file tạm khi Kaggle Secret không khả dụng;
- backend local canonical và gateway xác thực bằng Bearer;
- một real non-streaming generation có visible assistant content;
- một real SSE generation có visible streamed content, finish reason cuối và `[DONE]`;
- Cloudflare Quick Tunnel tùy chọn được đánh giá độc lập với sức khỏe model/runtime local;
- `cloudflared` được pin ở release `2026.10.0` và chỉ được thực thi khi SHA-256 chính thức của bản Linux AMD64 khớp;
- evidence và cleanup xác định, bao gồm xóa token do notebook tự tạo.

## Biên runtime

Notebook là production-style reference workflow, không phải dịch vụ hosted/managed.

Qualification model/runtime local là authoritative cho serving core. Quick Tunnel chỉ là tầng transport tùy chọn và được đánh giá riêng để lỗi DNS/tunnel bên ngoài không làm mất hiệu lực của một kết quả inference local hợp lệ.

Notebook giữ public request cap ở `max_tokens=512`, dùng model-native request template setting `reasoning_strength=low`, và áp dụng auth-proxy backend response timeout 120 giây do notebook quản lý mà không thay đổi frozen repository source tree.

## Kaggle inputs bắt buộc

Hãy attach **chính xác** các resource dưới đây trước khi chạy notebook:

| Vai trò | Loại | Resource | Lựa chọn bắt buộc | Identity bắt buộc |
| --- | --- | --- | --- | --- |
| Target model | Kaggle Model | [`dangkhoa2016/bartowski-muse-glimmer-30b-gguf`](https://www.kaggle.com/models/dangkhoa2016/bartowski-muse-glimmer-30b-gguf/Gguf/q4-k-m/1) | **GGUF / `q4-k-m` / version `1`** | `Muse-Glimmer-30B-Q4_K_M.gguf` — `0d3fc85f61d10fdc84072f0bba6005d61c1ac5605a2627b0fd5ea4ff8194c384` |
| DFlash2 draft | Kaggle Model | [`dangkhoa2016/incoai-muse-glimmer-30b-dflash2-gguf`](https://www.kaggle.com/models/dangkhoa2016/incoai-muse-glimmer-30b-dflash2-gguf/Gguf/q4-k-m/1) | **GGUF / `q4-k-m` / version `1`** | `Muse-Glimmer-30B-DFlash2-Q4_K_M.gguf` — `93dbfb6f88e4645dec1347cf93f9d6fc80b90d413038722385b2a8e53565c949` |
| llama.cpp runtime | Kaggle Dataset | [`dangkhoa2016/muse-glimmer-30b-dflash2-llama-runtime`](https://www.kaggle.com/datasets/dangkhoa2016/muse-glimmer-30b-dflash2-llama-runtime) | attach dataset | pinned prebuilt CUDA runtime |

Canonical production notebook sử dụng variation DFlash2 chuyên biệt `q4-k-m`, chỉ mount draft Q4_K_M cần thiết (~1,65 GB). Variation `default` là legacy all-in-one bundle chứa Q4_K_M, Q8_0 và BF16 (tổng khoảng 10 GB), nên bị loại khỏi production-demo input contract.

Trong Kaggle, chọn **Add Input**, mở từng link trực tiếp rồi attach đúng Model variation/version hoặc Dataset đã chỉ định. Model có thể được mount dưới `/kaggle/input/models/...` và dataset dưới `/kaggle/input/datasets/...`; notebook tự động tìm đệ quy cả hai loại.

## Secret khuyến nghị

Tạo Kaggle Secret:

`MUSE_API_TOKEN`

Sử dụng tối thiểu 32 ký tự in được.

Nếu secret bị thiếu, notebook tạo token tạm thời an toàn bằng cơ chế mật mã tại:

`/kaggle/working/.muse-secrets/MUSE_API_TOKEN`

Thư mục dùng mode `0700`, file dùng mode `0600`, giá trị token không bao giờ được in và cleanup bình thường sẽ xóa file được tạo.

## Quy trình chạy

1. Chọn **GPU T4 x2**.
2. Bật Internet để clone repository và dùng Quick Tunnel tùy chọn.
3. Dùng **Add Input** để attach đúng target Model (`q4-k-m/1`), DFlash2 Model (`q4-k-m/1`) và runtime Dataset đã liệt kê ở trên.
4. Tùy chọn cấu hình `MUSE_API_TOKEN`.
5. Chạy **Restart Session → Run All**.
6. Xem final evidence và các verdict trước khi publish notebook output.

Một release qualification thành công nên có:

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
