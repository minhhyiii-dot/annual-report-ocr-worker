# START HERE — OCR Worker Home v2.1

Worker Home dùng runtime state nội bộ `2.1`; các Job ZIP vẫn giữ nguyên schema `2.0` và
không cần đóng lại.

Đây là hướng dẫn duy nhất có hiệu lực trong Worker Home. Worker Home được giữ lại trên
một máy để dùng cho nhiều job. Python, thư viện và model chỉ được tải một lần cho mỗi
profile CPU/GPU rồi tái sử dụng.

Worker chỉ làm đúng ba việc: nhận ảnh PNG đã được chia sẵn, OCR từng ảnh thành một file
Markdown và một file JSON, sau đó đóng `RESULT_<job_id>.zip`. Worker **không** crop,
ghép báo cáo, sửa Markdown thủ công, chấm điểm, promote hoặc upload kết quả.

## Quy tắc bắt buộc

- Luôn chạy `doctor` trước. `doctor` chỉ đọc trạng thái; không cài hoặc tạo file.
- Không dùng quyền admin, Docker, sửa driver, cài package global hoặc đổi `PATH` toàn hệ
  thống.
- Chỉ chạy `setup -ApprovedByUser` sau khi người dùng đã đồng ý rõ ràng với profile,
  dung lượng tải ước tính và các thư mục sẽ tạo.
- Không sửa file trong `worker/`, `job.json`, `job_checksums.sha256` hoặc ảnh input.
- Worker Home có thể đặt ở bất kỳ vị trí nào trên máy, kể cả thư mục do OneDrive quản lý. Không tự
  sao chép hoặc di chuyển Worker Home sang vị trí khác chỉ vì filesystem báo reparse point/cloud file.
- Agent không được tự tạo symlink, junction, filesystem link hoặc wrapper để thay đổi cách worker chạy.
  Không chỉnh sửa file trong `worker/` để né kiểm tra; nếu gặp lỗi thì báo nguyên lỗi cho người dùng.
- Không thay model/config/token cap và không sửa output OCR bằng tay.
- Thiếu GPU, driver, RAM, dung lượng hoặc mạng chỉ là cảnh báo. Agent báo hướng xử lý phù
  hợp với máy; chỉ checksum/static worker hỏng mới làm `doctor` trả `BLOCKED`.
- Nếu doctor cảnh báo Windows chưa bật long-path và setup sau đó báo đường dẫn quá dài,
  agent đề nghị người dùng tự giải nén lại Worker Home vào đường dẫn ngắn hơn. Agent không
  được tự di chuyển thư mục và cảnh báo này không được dùng để hard-block.
- Tất cả thay đổi của worker nằm trong Worker Home: `.runtime`, `jobs`, `outbox`, `logs`
  và `inbox`. Không ghi sang thư mục khác.
- Các biến hồ sơ/cache chỉ được đổi cho tiến trình worker và trỏ vào `.runtime`; hồ sơ
  Windows thật không bị sửa.

Chạy mọi lệnh từ thư mục gốc Worker Home. `-ExecutionPolicy Bypass` chỉ áp dụng cho tiến
trình PowerShell hiện tại, không đổi policy của máy.

## 1. Doctor — bắt buộc và read-only

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\worker\worker.ps1 doctor
```

`doctor` tự đề xuất `gpu` khi phát hiện `nvidia-smi`, nếu không sẽ đề xuất `cpu`. Có thể
yêu cầu báo cáo cụ thể bằng `doctor -Profile cpu` hoặc `doctor -Profile gpu`.

Trước setup, agent phải báo lại:

- profile được chọn và lý do;
- Python local, virtual environment, Paddle, PaddleOCR/PaddleX và model sẽ được tải;
- dung lượng runtime, model cache và tổng dung lượng lần dùng đầu tiên do `doctor` trả về;
- các thư mục sẽ tạo dưới `.runtime`;
- không dùng admin, Docker, sửa driver hoặc global `PATH`.

Sau đó hỏi người dùng có đồng ý setup hay không. Không được tự suy diễn sự đồng ý.

## 2. Setup — chỉ sau phê duyệt

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\worker\worker.ps1 setup -ApprovedByUser -Profile gpu
```

Thay `gpu` bằng đúng profile `cpu` nếu đó là profile vừa được báo và được người dùng
phê duyệt. Không dùng `auto` ở bước setup: profile thực thi phải đúng profile cụ thể
đã được người dùng duyệt, kể cả khi trạng thái GPU thay đổi sau lệnh doctor. Hai profile được
cài trong các đường dẫn nội bộ ngắn `.runtime\v\g` và `.runtime\v\c` để tránh giới hạn
đường dẫn DLL của Windows; model cache dùng chung tại
`.runtime\paddlex_cache`. Chuyển hoặc cài thêm profile cũng cần phê duyệt mới.

Setup có thể chạy lại sau khi mất mạng. Quyền setup này cũng bao gồm lần tải model đã được
ước tính và báo trước: model PaddleOCR-VL được tải vào cache dùng chung ở lần `run` đầu tiên
và không tải lại cho các job sau nếu cache còn nguyên.

Các lần cài package luôn dùng từng nguồn độc lập. Model chỉ dùng nguồn BOS; nếu BOS lỗi,
worker báo lỗi để chạy lại đúng lệnh sau đó, không tự chuyển sang Hugging Face hoặc nguồn khác.

Nếu Worker Home trước đó được giải nén từ một Starter cũ có lỗi hoặc đã bị agent sửa
`worker/`, không vá tiếp hay chép ngược script/runtime dở dang. Hãy giải nén Starter hiện
tại thành một Worker Home sạch, chạy lại `doctor`, rồi xin phê duyệt setup như bình thường.

## 3. Nhận job

Chép nguyên ZIP `OCR_JOB_<job_id>.zip` vào `inbox`, rồi chạy:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\worker\worker.ps1 import
```

Lệnh không tham số chỉ tự chọn khi `inbox` có đúng một ZIP khớp tên. Nếu có nhiều ZIP
(ví dụ job cũ vẫn được giữ lại), agent phải dùng `-JobZip` để chỉ định rõ, không tự nhập
hàng loạt.

Hoặc chỉ định một ZIP nằm bên trong Worker Home:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\worker\worker.ps1 import -JobZip .\inbox\OCR_JOB_<job_id>.zip
```

Import kiểm tra ZIP, schema và toàn bộ checksum trước khi đưa vào `jobs\<job_id>`. Job ID
đã tồn tại sẽ không bị ghi đè.

## 4. Xem trạng thái và chạy OCR

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\worker\worker.ps1 status
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\worker\worker.ps1 run -JobId <job_id>
```

- Probe chạy trước. Probe lỗi/OOM/timeout thì dừng job.
- Mỗi ảnh sau đó chạy batch 1; lỗi một ảnh được ghi vào failed list rồi worker tiếp tục.
- Mỗi checkpoint hợp lệ gồm đúng `<asset_id>.md` và `<asset_id>_res.json`.
- Chạy lại cùng lệnh để resume; chỉ checkpoint hợp lệ mới được bỏ qua.
- Nhấn `Ctrl+C` một lần để dừng. Không xóa job hoặc runtime; chạy lại để resume.

## 5. Đóng kết quả

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\worker\worker.ps1 pack -JobId <job_id>
```

Kết quả nằm tại `outbox\RESULT_<job_id>.zip`. Agent báo số ảnh hoàn tất/thất bại và đường
dẫn ZIP nhưng không tự upload. ZIP kết quả không chứa input, Python, thư viện hoặc model.

## 6. Báo lỗi đúng cách

Nếu `setup`, `run` hoặc `pack` lỗi, dừng tại lỗi đó. Không sửa `worker/`, đổi index/package,
thêm flag, tạo wrapper hoặc đổi model để thử né lỗi. Báo lại nguyên văn:

- lệnh vừa chạy, profile và job ID liên quan;
- lỗi cuối cùng mà worker trả về;
- kết quả `doctor` hoặc `status` gần nhất nếu đã có.

Khi lỗi khởi tạo model/engine có cụm `engine stderr tail`, chép nguyên cả cụm đó. Đây là
phần cuối stderr đã được worker giới hạn độ dài và che đường dẫn nhạy cảm. Các event stderr
đã che đường dẫn cũng nằm trong `jobs\<job_id>\logs\run.jsonl`; chỉ đọc phần liên quan để
báo lỗi, không sửa log và không tự upload log. Nếu lỗi mạng tạm thời, chỉ chạy lại đúng lệnh
sau khi báo người dùng; không tự thay nguồn tải hoặc cấu hình.
