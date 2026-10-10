"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append one timestamped failover event."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = time.time()
    event = {"ts": ts, "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)), **kw}
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(json.dumps(event, ensure_ascii=False), flush=True)
    return event


def state_of(region: str) -> dict:
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """Restore and warm the target before switching the edge pointer."""
    result = {"ok": False, "target": target, "completed_steps": [],
              "rpo_seconds": None, "docs_lost": None}
    if target not in URL or wait <= 0:
        result["error"] = "invalid target or wait time"
        return result

    try:
        before = state_of(target)
        if before.get("region", target) != target:
            raise ValueError("target region does not match the requested region")
        emit(step="1_verify_target", region=target, state=before)
        result["completed_steps"].append("1_verify_target")

        restored = snapshot.get(target, backend)
        source = "b" if target == "a" else "a"
        rpo = snapshot.rpo(pathlib.Path(f"state/region-{source}/vectors.sqlite"),
                           pathlib.Path(f"state/region-{target}/vectors.sqlite"))
        result.update(rpo_seconds=rpo["rpo_seconds"], docs_lost=rpo["docs_lost"],
                      embed_model_version=restored["embed_model_version"])
        emit(step="2_restore_snapshot", region=target, snapshot_at=restored["snapshot_at"],
             rpo_seconds=rpo["rpo_seconds"], docs_lost=rpo["docs_lost"],
             embed_model_version=restored["embed_model_version"])
        result["completed_steps"].append("2_restore_snapshot")

        pool_file = pathlib.Path(f"state/region-{target}/pool_state")
        pool_file.write_text("full", encoding="utf-8")
        emit(step="3_scale_pool", region=target, pool_state="full")
        result["completed_steps"].append("3_scale_pool")

        started = time.monotonic()
        deadline = started + wait
        last_reason = "target not ready"
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"{URL[target]}/readyz",
                                     timeout=min(2.0, max(0.1, deadline - time.monotonic())))
                if response.status_code == 200:
                    ready = response.json()
                    waited = round(time.monotonic() - started, 2)
                    emit(step="4_wait_ready", region=target, waited_s=waited,
                         vectors=ready.get("vectors"), pool_state=ready.get("pool_state"))
                    result["completed_steps"].append("4_wait_ready")
                    break
                last_reason = str(response.json().get("reasons", response.status_code))
            except (httpx.RequestError, ValueError) as exc:
                last_reason = str(exc)
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
        else:
            emit(step="4_wait_ready", region=target, ok=False,
                 waited_s=round(time.monotonic() - started, 2), reason=last_reason)
            result["error"] = f"target did not become ready: {last_reason}"
            return result

        result["target_state"] = state_of(target)
        pathlib.Path("edge/active_region").write_text(target, encoding="utf-8")
        emit(step="5_dns_cutover", region=target, active_region=target)
        result["completed_steps"].append("5_dns_cutover")
        result["ok"] = True
        return result
    except (OSError, ValueError, KeyError, httpx.RequestError, SystemExit) as exc:
        result["error"] = str(exc)
        emit(step="abort", region=target, reason=str(exc))
        return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
