# Postmortem không quy trách nhiệm: DR drill ngày 10/10/2026

Drill mô phỏng Region A ngừng trả lời khi Edge vẫn chuyển request đến A. Runbook khôi phục snapshot vào Region B, chờ B sẵn sàng rồi chuyển Edge sang B. Thời gian từ outage đến request thành công đầu tiên ở B là 25.7s. Các thời điểm dưới đây theo giờ Việt Nam, UTC+7.

## Timeline

| Thời điểm | Sự kiện | Bằng chứng |
|---|---|---|
| 23:42:45.890 | Outage của A bắt đầu. | `chaos/chaos-events.jsonl:4` |
| 23:42:46.035 | Người dùng nhận lỗi đầu tiên. | `reports/drill-2-withdr.jsonl:22` |
| 23:43:02.328 | Health checker ghi A `UNHEALTHY` sau 3 lần lỗi liên tiếp. | `reports/health-events.jsonl:2` |
| 23:43:02.335 | Runbook xác nhận outage và ghi thời điểm thông báo incident. Lượt drill dùng `--auto`. | `reports/runbook-run.jsonl:2` |
| 23:43:02.345 | Snapshot được restore vào B; RPO là 2.02s và mất 1 document. | `reports/failover-events.jsonl:2` |
| 23:43:08.485 | B sẵn sàng sau khi pool warmup. | `reports/failover-events.jsonl:4` |
| 23:43:08.501 | Edge được cutover sang B. | `reports/failover-events.jsonl:5` |
| 23:43:11.570 | Request đầu tiên thành công từ B, kết thúc RTO. | `reports/drill-2-withdr.jsonl:35` |
| 23:43:11.725 | Runbook xác nhận 10/10 request kiểm tra thành công. | `reports/runbook-run.jsonl:6` |

## Gap analysis

| Chỉ số | Mục tiêu | Đo được | Khoảng còn dư so với mục tiêu | Bằng chứng |
|---|---:|---:|---:|---|
| RTO | 300s | 25.7s | 274.3s | `chaos/chaos-events.jsonl:4`, `reports/drill-2-withdr.jsonl:35` |
| RPO | 300s | 2.02s, 1 document | 297.98s | `reports/failover-events.jsonl:2` |

Bước tốn nhiều thời gian nhất là phát hiện outage: 16.4s, bằng 63.8% RTO. Ngưỡng cấu hình `5s × 3 = 15.0s` chiếm 58.4% RTO. Sau phát hiện, pool warmup và cutover tốn khoảng 6.2s; Edge cache cùng nhịp đo traffic thêm 3.1s. Các mốc và phép cộng nằm trong `reports/rto-evidence.md`.

## Root cause: 5 whys

1. Người dùng gặp lỗi vì Edge vẫn gửi request đến A sau khi A không trả lời.
2. Edge chưa đổi ngay vì runbook phải xác nhận outage bằng ba lần kiểm tra liên tiếp.
3. Sau khi xác nhận, B vẫn cần restore snapshot và bật pool trước khi có thể nhận traffic.
4. B bắt đầu với dữ liệu rỗng và pool `warm` vì lab chọn mô hình active-passive để giảm tài nguyên dự phòng.
5. Vì thế, thời gian phục hồi gồm phát hiện, chuẩn bị B và thời gian cache Edge. Đây là giới hạn của thiết kế dự phòng đang được đo, không phải lỗi của một cá nhân.

Trong baseline, không có health checker và runbook nên hệ thống không phục hồi trong cửa sổ đo; request cuối vẫn lỗi ở `reports/drill-1-nodr.jsonl:33`.

## Action items

| Việc cần làm | Owner | Hạn hoàn thành | Ảnh hưởng dự kiến và cách kiểm tra |
|---|---|---|---|
| Thử interval 2s, threshold 3 trên nhiều lần drill và lỗi mạng ngắn. | Platform on-call | 17/10/2026 | Floor lý thuyết giảm từ 15s xuống 6s, có thể tiết kiệm khoảng 9s. Chỉ giữ cấu hình nếu số báo động sai chấp nhận được. |
| Đo chi phí giữ pool B ở `full` và chạy lại drill so với pool `warm`. | AI serving owner | 24/10/2026 | Có thể loại khoảng 6.14s warmup đã đo ở `reports/failover-events.jsonl:4`; phải ghi thêm chi phí tài nguyên. |
| Kiểm tra backup và khôi phục model version cùng Vector DB trong mỗi drill định kỳ. | Data platform owner | 24/10/2026 | Giữ RPO có thể đo được bằng giây và số document, đồng thời tránh restore index với model không phù hợp. |

## Câu hỏi sau drill

1. `interval × threshold` là `5 × 3 = 15.0s`, bằng 58.4% RTO đo được. Thời gian phát hiện thực tế là 16.4s.
2. Nếu hạ interval xuống 1s và giữ threshold 3, floor lý thuyết còn 3s, có thể giảm khoảng 12s. Health checker sẽ gửi request nhiều gấp 5 lần và có thể coi lỗi mạng kéo dài 3s là outage, nên cần thử nhiều lần trước khi đổi cấu hình.
3. Nếu A mất dữ liệu vĩnh viễn, `docs_lost=1` nghĩa là một document mới đã có ở A nhưng chưa vào bản khôi phục ở B. Khách hàng có thể không tìm thấy thông tin đó cho đến khi hệ thống nhập lại từ nguồn gốc, nếu nguồn còn giữ dữ liệu.
