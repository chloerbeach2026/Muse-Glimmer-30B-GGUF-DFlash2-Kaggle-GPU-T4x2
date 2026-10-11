# Đóng góp cho Muse-Glimmer-30B
> 🌐 Language / Ngôn ngữ: [English](CONTRIBUTING.md) | **Tiếng Việt**

Cảm ơn bạn đã cân nhắc đóng góp. Đây là một reference implementation nhỏ,
cố ý deterministic, nên các thay đổi được hoan nghênh khi giữ nguyên frozen
runtime contract và trung thực về phạm vi.

## Nguyên tắc cơ bản

- **Không bao giờ thay đổi hành vi runtime đã khóa** (generation defaults,
  thứ tự SSE, `[DONE]`, `finish_reason`, ngữ nghĩa busy/429, admission slot,
  model selection, GPU split, CUDA flags, revision llama.cpp, quantization,
  hành vi prompt/template) nếu không khai báo tường minh và giải thích lý do.
- **Không secrets.** Không bao giờ commit tokens, mật khẩu, cookies, SSH keys,
  URL riêng tư, giá trị `.env`, hay bất kỳ state sinh ra từ `artifacts/`.
- **Không model binaries.** Không bao giờ commit GGUF, safetensors,
  `.pt`/`.pth`, hay tệp weights khác; repository này là nguồn reference
  source-only.
- **Giữ determinism.** Test phải chạy chỉ-CPU, không cần GPU, Kaggle, tunnel
  hay mạng.
- **Giữ kích thước hợp lý.** Tránh thêm blob sinh ra cỡ lớn; nếu thứ gì đó bắt
  buộc phải lớn, hãy bàn trước.

## Trước khi bắt đầu

- Kiểm tra các issue/PR đang mở để tránh trùng việc.
- Với ý tưởng ảnh hưởng hành vi, mở issue để thảo luận phạm vi trước.
- Nêu động cơ thay đổi và, với thay đổi logic, ảnh hưởng tới runtime behavior
  trong mô tả PR.

## Quy trình phát triển

```bash
# Fresh repo
git clone https://github.com/chloerbeach2026/Muse-Glimmer-30B-GGUF-DFlash2-Kaggle-GPU-T4x2.git
cd Muse-Glimmer-30B-GGUF-DFlash2-Kaggle-GPU-T4x2

# Bộ test contract deterministic chỉ-CPU
./demo-smoke.sh

# Kiểm tra tĩnh (không cần GPU/mô hình)
for f in serve.sh expose.sh external.sh readiness.sh doctor.sh release-export.sh \
         demo-smoke.sh package-llama-runtime.sh scripts/*.sh; do bash -n "$f"; done
python3 -m py_compile scripts/*.py tests/*.py
```

## Tests

- Mỗi PR phải giữ `./demo-smoke.sh` xanh (31+ assertion).
- Thêm/mở rộng `tests/test_demo_smoke.py` hoặc `tests/test_public_contract.py`
  cho hành vi bạn chạm tới khi có thể viết regression test trên CPU.
- CI chạy toàn bộ bộ suite CPU-safe trên mỗi push/PR.

## Tài liệu

- Mọi tài liệu Markdown public có cặp tiếng Anh `*.md` và tiếng Việt
  `*.vi.md` (xem language line ngay dưới mỗi H1).
- Khi bạn đổi một lệnh, port, tên tệp, hạn chế, hay cảnh báo bảo mật, hãy cập
  nhật **cả hai** bản ngôn ngữ với cùng giá trị.
- Giữ tài liệu tiếng Anh và tiếng Việt song song về nghĩa.

## Toàn vẹn nguồn

- Các tệp runtime được phủ bởi `SOURCE_MANIFEST.sha256`. Nếu bạn thêm/sửa tệp
  dưới `config/`, `scripts/`, `tests/` (hoặc các tệp root được theo dõi), hãy
  tái tạo manifest:

```bash
python3 -B scripts/source_manifest.py write --root .
```

  và đưa `SOURCE_MANIFEST.sha256` đã cập nhật vào cùng commit.

## Mở PR

Dùng pull-request template; nó yêu cầu bạn xác nhận:

- tests vẫn qua;
- phạm vi thay đổi;
- giữ tài liệu EN/VI song song;
- không đưa secrets;
- không đưa model binaries;
- docs đã cập nhật;
- mọi thay đổi hành vi được khai báo tường minh.

Giữ PR tập trung. Thay đổi lớn nên được tách thành các commit dễ review theo
phong cách lịch sử hiện có (subject mệnh lệnh chuyên nghiệp, không nhãn
version nội bộ).

## Kỳ vọng khi review

- Người duy trì có thể yêu cầu giải trình cho mọi thay đổi hành vi.
- Source-integrity manifest phải đồng bộ với cây.
- Đóng góp được ghi nhận dưới đúng định danh tác giả của người đóng góp; công
  việc của người đóng góp không bao giờ bị viết lại thành định danh người duy trì.

## Code of conduct

Tham gia được điều chỉnh bởi
[Code of Conduct](CODE_OF_CONDUCT.md).