"""T3 E2E: M2 Event Stream — write fake JSONL, observe derived events via /api/events/recent."""
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8001"
PROJECTS = Path.home() / ".claude" / "projects"
TEST_PROJ = PROJECTS / "-tmp-csm-t3-test"
SID = str(uuid.uuid4())
JSONL = TEST_PROJ / f"{SID}.jsonl"


def write_records():
    TEST_PROJ.mkdir(parents=True, exist_ok=True)
    now_iso = "2026-06-21T06:10:00.000Z"
    records = [
        # user message
        {
            "type": "user",
            "timestamp": now_iso,
            "message": {"role": "user", "content": "T3 test prompt"},
            "sessionId": SID,
        },
        # assistant with usage (USAGE_RECORDED + MESSAGE_ASSISTANT_DONE)
        {
            "type": "assistant",
            "timestamp": "2026-06-21T06:10:01.000Z",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": "ack"}],
                "usage": {
                    "input_tokens": 12,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 100,
                    "output_tokens": 5,
                },
            },
            "sessionId": SID,
        },
        # API error w/ rate-limit-hit phrasing (API_ERROR + RATE_LIMIT_HIT)
        {
            "type": "assistant",
            "timestamp": "2026-06-21T06:10:02.000Z",
            "isApiErrorMessage": True,
            "message": {
                "role": "assistant",
                "content": "You've hit your limit ∙ resets 11:00pm (Asia/Shanghai)",
            },
            "sessionId": SID,
        },
    ]
    with JSONL.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def main():
    print(f"[setup] sid={SID}")
    print(f"[setup] writing 3 records to {JSONL}")

    with httpx.Client(base_url=BASE, timeout=10) as c:
        # baseline
        before = c.get("/api/events/recent", params={"limit": 200}).json()
        before_ids = {e["id"] for e in before["events"]}
        print(f"[before] events count={before['count']} baseline ids={len(before_ids)}")

        write_records()

        # wait > poll_interval (5s default)
        print("[wait] sleeping 7s for tail tick…")
        time.sleep(7)

        after = c.get("/api/events/recent", params={"limit": 200}).json()
        print(f"[after] events count={after['count']}")
        new = [e for e in after["events"] if e["id"] not in before_ids]
        print(f"[after] new events: {len(new)}")
        for e in new:
            print(f"  - type={e['type']} sid={e['session_id']} ts={e['ts']} payload_keys={list((e.get('payload') or {}).keys())}")

        ours = [e for e in new if e["session_id"] == SID]
        print(f"\n[match] events for our test sid={SID}: {len(ours)}")
        types_seen = sorted({e["type"] for e in ours})
        print(f"[match] types seen: {types_seen}")

    # cleanup
    try:
        JSONL.unlink()
        TEST_PROJ.rmdir()
    except Exception as e:
        print(f"[cleanup] warn: {e}")

    expected = {
        "session.started",
        "message.user_sent",
        "message.assistant_done",
        "usage.recorded",
        "api.error",
        "rate_limit.hit",
    }
    seen_set = set(types_seen)
    missing = expected - seen_set
    extra = seen_set - expected
    print(f"\n[verdict] expected={sorted(expected)}")
    print(f"[verdict] missing={sorted(missing)} extra={sorted(extra)}")
    sys.exit(0 if not missing else 2)


if __name__ == "__main__":
    main()
