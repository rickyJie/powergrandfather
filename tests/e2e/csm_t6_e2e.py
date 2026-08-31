"""T6 E2E: M5 Token — GETs against real data + alert-rules CRUD + fresh usage event injection."""
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8001"
PROJECTS = Path.home() / ".claude" / "projects"
TEST_PROJ = PROJECTS / "-tmp-csm-t6-test"
SID = str(uuid.uuid4())
JSONL = TEST_PROJ / f"{SID}.jsonl"


def main():
    failures = []
    rule_id = None
    try:
        with httpx.Client(base_url=BASE, timeout=15) as c:
            # 1) /current
            cur = c.get("/api/tokens/current").json()
            print(f"[1] current 5h msg={cur['msg_count']} total_tok={cur['total_tokens']:,} cost=${cur['estimated_cost_usd']:.2f}")
            for f in ("window_hours", "msg_count", "input_tokens", "cache_read_tokens", "output_tokens", "estimated_cost_usd"):
                if f not in cur:
                    failures.append(f"current missing field {f}")

            # 2) /history (granularity=hour)
            hist = c.get("/api/tokens/history?hours=6").json()
            print(f"[2] history 6h buckets={len(hist['items'])}")
            if not hist["items"]:
                failures.append("history empty")

            # 3) /top
            top = c.get("/api/tokens/top?hours=24&limit=5").json()
            print(f"[3] top 24h entities={len(top['items'])} first={top['items'][0]['entity'][:12] if top['items'] else 'n/a'}")

            # 4) /hit-observations
            hits = c.get("/api/tokens/hit-observations").json()
            print(f"[4] hit_observations count={len(hits['items'])}")

            # 5) Alert rule CRUD
            r = c.post("/api/tokens/alert-rules", json={
                "name": "t6-smoke-msg-count",
                "threshold": 9999,
                "condition_type": "absolute",
                "scope": "window",
                "cooldown_minutes": 60,
                "metric": "msg_count",
            })
            r.raise_for_status()
            rule_id = r.json()["id"]
            print(f"[5] created rule id={rule_id} name={r.json()['name']}")

            rl = c.get("/api/tokens/alert-rules").json()
            ours = [x for x in rl["items"] if x["id"] == rule_id]
            print(f"[6] list rules count={len(rl['items'])} ours_found={bool(ours)}")
            if not ours:
                failures.append("created rule not in list")

            # 7) Inject usage event via JSONL
            TEST_PROJ.mkdir(parents=True, exist_ok=True)
            import datetime
            now_ts = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            rec = {
                "type": "assistant",
                "timestamp": now_ts,
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-7",
                    "content": [{"type": "text", "text": "T6"}],
                    "usage": {
                        "input_tokens": 1234,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "output_tokens": 567,
                    },
                },
                "sessionId": SID,
            }
            JSONL.write_text(json.dumps(rec) + "\n")
            print("[7] wrote jsonl input=1234 output=567 → sleep 7s")
            time.sleep(7)

            cur2 = c.get("/api/tokens/current").json()
            delta_msg = cur2["msg_count"] - cur["msg_count"]
            delta_in = cur2["input_tokens"] - cur["input_tokens"]
            delta_out = cur2["output_tokens"] - cur["output_tokens"]
            print(f"[8] after-inject delta: msg=+{delta_msg} input=+{delta_in} output=+{delta_out}")
            if delta_msg < 1:
                failures.append("msg_count did not increase after inject")
            if delta_in < 1234:
                failures.append(f"input_tokens did not increase by 1234: +{delta_in}")
            if delta_out < 567:
                failures.append(f"output_tokens did not increase by 567: +{delta_out}")

            # 9) (top sorts by cache_creation_tokens — our minimal inject has 0 there, so we
            #     verify presence via the trend delta from [8] instead, which we already did)
            print("[9] (skip top check — inject sets cache_creation=0; trend delta in [8] proves persistence)")

            # 10) Cleanup rule
            r = c.delete(f"/api/tokens/alert-rules/{rule_id}")
            r.raise_for_status()
            print(f"[10] deleted rule -> {r.json()}")
            rule_id = None
    finally:
        try:
            JSONL.unlink(missing_ok=True)
            TEST_PROJ.rmdir()
        except Exception as e:
            print(f"[cleanup-jsonl] {e}")
        if rule_id:
            try:
                httpx.delete(f"{BASE}/api/tokens/alert-rules/{rule_id}", timeout=5)
            except Exception:
                pass

    print("\n==============")
    if failures:
        print("FAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("PASS: M5 Token E2E")
    sys.exit(0)


if __name__ == "__main__":
    main()
