"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Record one completed incident-response step."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = time.time()
    event = {"ts": ts, "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
             "step": n, "name": name, **kw}
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(json.dumps(event, ensure_ascii=False), flush=True)
    return event


def confirm(auto: bool, msg: str) -> bool:
    """Ask the operator before failover unless this is an automated drill."""
    if auto:
        return True
    return input(f"{msg} [y/N]: ").strip().lower() == "y"


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Confirm an outage, fail over once, then check user-facing requests."""
    if primary not in URL or target not in URL or primary == target:
        return {"ok": False, "error": "primary and target must be different valid regions"}

    started = time.monotonic()
    chaos_log = pathlib.Path("chaos/chaos-events.jsonl")
    outage_ts = None
    if chaos_log.exists():
        events = [json.loads(line) for line in chaos_log.read_text().splitlines() if line.strip()]
        outage_ts = next((event["ts"] for event in reversed(events)
                          if event.get("action") == "kill" and event.get("region") == primary), None)

    # The secondary can be alive yet unready before its snapshot is restored.
    target_ready, target_reason = health_checker.probe(target, 2)
    try:
        target_alive = httpx.get(f"{URL[target]}/healthz", timeout=2).status_code == 200
    except httpx.RequestError:
        target_alive = False

    reasons = []
    for attempt in range(3):
        ready, reason = health_checker.probe(primary, 2)
        reasons.append(reason)
        if ready:
            step(1, "xac_nhan_outage", ok=False, primary=primary,
                 reason="primary responded to /readyz", attempts=attempt + 1)
            return {"ok": False, "error": "primary is ready"}
        if attempt < 2:
            time.sleep(5)
    step(1, "xac_nhan_outage", ok=target_alive, primary=primary, target=target,
         consecutive_fails=3, reasons=reasons, target_alive=target_alive,
         target_ready_before_restore=target_ready, target_reason=target_reason)
    if not target_alive:
        return {"ok": False, "error": "target process is not alive"}

    incident = step(2, "thong_bao_incident", outage_ts=outage_ts,
                    notification_delay_s=None if outage_ts is None else
                    round(time.time() - outage_ts, 2))
    if not confirm(auto, f"Region {primary} is down. Fail over to {target}?"):
        step(7, "post_incident", ok=False, reason="operator declined failover")
        return {"ok": False, "error": "operator declined failover"}

    outcome = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", ok=outcome["ok"],
         failover_steps=outcome["completed_steps"], error=outcome.get("error"))
    step(4, "verify_state_replica", ok=outcome["ok"],
         target_state=outcome.get("target_state"),
         rpo_seconds=outcome.get("rpo_seconds"), docs_lost=outcome.get("docs_lost"),
         embed_model_version=outcome.get("embed_model_version"))
    cutover = outcome["ok"] and pathlib.Path("edge/active_region").read_text().strip() == target
    step(5, "dns_cutover", ok=cutover, active_region=target if cutover else None)
    if not cutover:
        step(7, "post_incident", ok=False, elapsed_s=round(time.monotonic() - started, 2),
             reason=outcome.get("error", "cutover failed"))
        return {"ok": False, "error": outcome.get("error", "cutover failed")}

    # Wait for the edge's cached region to expire before checking user traffic.
    edge_deadline = time.monotonic() + 12
    while time.monotonic() < edge_deadline:
        try:
            edge_state = httpx.get("http://127.0.0.1:8080/edge/state", timeout=2).json()
            if edge_state.get("active_region") == target:
                break
        except (httpx.RequestError, ValueError):
            pass
        time.sleep(0.5)

    latencies = []
    successes = 0
    for i in range(10):
        t0 = time.monotonic()
        try:
            response = httpx.get("http://127.0.0.1:8080/v1/infer",
                                 params={"q": f"dr verification {i}"}, timeout=3)
            body = response.json()
            successes += (response.status_code == 200 and body.get("edge_region") == target
                          and body.get("region") == target)
        except (httpx.RequestError, ValueError):
            pass
        latencies.append((time.monotonic() - t0) * 1000)
    p95 = round(sorted(latencies)[9], 1)
    error_rate = round((10 - successes) / 10, 2)
    signals_ok = successes == 10 and p95 < 2500
    step(6, "verify_golden_signals", ok=signals_ok, requests=10,
         successes=successes, error_rate=error_rate, p95_latency_ms=p95)
    elapsed = round(time.monotonic() - started, 2)
    step(7, "post_incident", ok=signals_ok, elapsed_s=elapsed,
         incident_ts=incident["ts"],
         measure_command="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")
    return {"ok": signals_ok, "failover": outcome, "elapsed_s": elapsed,
            "p95_latency_ms": p95, "error_rate": error_rate}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
