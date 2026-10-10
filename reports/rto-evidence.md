# Bằng chứng RTO và RPO, Lab 23

Các mốc thời gian dưới đây lấy từ log của hai lượt chạy ngày 10/10/2026. Mỗi tham chiếu chỉ đúng một dòng log. Thời gian tính từ sự kiện `kill` của lượt chạy tương ứng.

## Drill 1: Không có DR

| Chỉ số | Kết quả | Bằng chứng |
|---|---:|---|
| Outage bắt đầu | 0 giây | `chaos/chaos-events.jsonl:1` |
| Request đầu tiên lỗi | +0.2s | `reports/drill-1-nodr.jsonl:17` |
| Request cuối trong cửa sổ đo vẫn lỗi | +31.1s | `reports/drill-1-nodr.jsonl:33` |
| Số request lỗi | 17/33 | `reports/drill-1-nodr.jsonl:17`, `reports/drill-1-nodr.jsonl:33` |
| RTO | NO_RECOVERY | `reports/drill-1-nodr.jsonl:33` |

Không có health checker hay cutover trong cửa sổ của drill 1, nên không có request thành công nào sau khi A bị tạm dừng.

## Drill 2: Có DR

| Mốc | Giây từ outage | Bằng chứng |
|---|---:|---|
| Outage bắt đầu | 0 | `chaos/chaos-events.jsonl:4` |
| User thấy lỗi đầu tiên | +0.1s | `reports/drill-2-withdr.jsonl:22` |
| Health checker đánh dấu A `UNHEALTHY` | +16.4s | `reports/health-events.jsonl:2` |
| Snapshot restore xong, RPO 2.02s và mất 1 document | +16.5s | `reports/failover-events.jsonl:2` |
| Region B trả `/readyz` thành công | +22.6s | `reports/failover-events.jsonl:4` |
| Edge chuyển sang B | +22.6s | `reports/failover-events.jsonl:5` |
| Request đầu tiên thành công từ B | +25.7s | `reports/drill-2-withdr.jsonl:35` |

| Chỉ số | Đo được | Mục tiêu | Kết quả | Bằng chứng |
|---|---:|---:|---|---|
| RTO của inference API | 25.7s | 300s | PASS | `chaos/chaos-events.jsonl:4`, `reports/drill-2-withdr.jsonl:35` |
| RPO của Vector DB | 2.02s, 1 document | 300s | PASS | `reports/failover-events.jsonl:2` |

Health checker dùng `interval_s=5` và `threshold=3`, nên ngưỡng phát hiện theo cấu hình là 15.0s. Sự kiện thực tế xuất hiện sau 16.4s. Dữ liệu này nằm ở `reports/health-events.jsonl:2`.

## Các phần tạo nên RTO

| Thành phần | Thời gian | Cách đo và cách giảm | Bằng chứng |
|---|---:|---|---|
| Phát hiện outage, gồm ngưỡng cấu hình 15.0s | 16.4s | Từ `kill` đến `UNHEALTHY`. Có thể giảm interval, nhưng phải đánh giá nguy cơ báo động do lỗi ngắn. | `chaos/chaos-events.jsonl:4`, `reports/health-events.jsonl:2` |
| Khôi phục snapshot | 0.0s khi làm tròn 0.1s | Từ phát hiện đến sự kiện restore. Có thể giảm bằng bản sao mới hơn hoặc restore trước. | `reports/health-events.jsonl:2`, `reports/failover-events.jsonl:2` |
| Warmup pool và ghi cutover | 6.2s | Từ restore đến cutover. `4_wait_ready` ghi `waited_s=6.14`. Có thể giữ pool B ở trạng thái `full`, đổi lại tốn tài nguyên. | `reports/failover-events.jsonl:2`, `reports/failover-events.jsonl:4`, `reports/failover-events.jsonl:5` |
| Cache Edge và nhịp lấy mẫu của traffic | 3.1s | Từ cutover đến request thành công đầu tiên được ghi. Có thể giảm TTL và tăng tần suất đo. | `reports/failover-events.jsonl:5`, `reports/drill-2-withdr.jsonl:35` |
| Tổng | 25.7s | Bằng RTO đo từ request của người dùng. | `chaos/chaos-events.jsonl:4`, `reports/drill-2-withdr.jsonl:35` |
