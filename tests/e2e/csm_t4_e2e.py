"""T4 E2E: M3 Notifications. Plan:
  1) Create an interactive PTY session.
  2) UPDATE session.external_session_id directly (no reconciler endpoint exists in v1).
  3) Write a fake JSONL with that uuid + assistant message.
  4) Wait > poll_interval.
  5) GET /api/notifications — expect a NEW_MESSAGE row for our session.
  6) GET /api/notifications/unread-summary — total_unread > 0.
  7) POST /api/notifications/{id}/read — mark read.
  8) GET /api/notifications/unread-summary — total_unread back to baseline.
  9) Cleanup PTY + jsonl.
"""
import json
import sqlite3
import time
import uuid
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8001"
REPO_ROOT = Path(__file__).resolve().parents[2]
DB = REPO_ROOT / "csm.db"
PROJECTS = Path.home() / ".claude" / "projects"
TEST_PROJ = PROJECTS / "-tmp-csm-t4-test"
CLAUDE_SID = str(uuid.uuid4())
JSONL = TEST_PROJ / f"{CLAUDE_SID}.jsonl"


def main():
    failures = []
    csm_sid = None
    notif_id = None
    try:
        with httpx.Client(base_url=BASE, timeout=30) as c:
            # baseline
            base_summary = c.get("/api/notifications/unread-summary").json()
            base_unread = base_summary["total_unread"]
            print(f"[baseline] total_unread={base_unread}")

            # 1) create interactive PTY session (bash so it doesn't need claude auth)
            r = c.post("/api/sessions", json={
                "cwd": "/tmp",
                "type": "interactive",
                "title": "t4-notif",
                "argv": ["bash", "-i"],
            })
            r.raise_for_status()
            csm_sid = r.json()["id"]
            print(f"[1] csm_sid={csm_sid}")

            # 2) direct DB update: bind external_session_id
            con = sqlite3.connect(DB)
            con.execute(
                "UPDATE session SET external_session_id=? WHERE id=?",
                (CLAUDE_SID, csm_sid),
            )
            con.commit()
            con.close()
            print(f"[2] bound external_session_id={CLAUDE_SID}")

            # 3) write fake jsonl
            TEST_PROJ.mkdir(parents=True, exist_ok=True)
            rec = {
                "type": "assistant",
                "timestamp": "2026-06-21T06:20:00.000Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "content": [{"type": "text", "text": "Hello from T4 notif test"}],
                },
                "sessionId": CLAUDE_SID,
            }
            with JSONL.open("w") as f:
                f.write(json.dumps(rec) + "\n")
            print(f"[3] wrote jsonl {JSONL}")

            # 4) wait for tail + dispatch
            print("[4] sleep 7s for tail + dispatch")
            time.sleep(7)

            # 5) list notifications
            lst = c.get("/api/notifications", params={"limit": 50}).json()
            ours = [n for n in lst["items"] if n.get("session_id") == csm_sid]
            print(f"[5] notif total={lst['count']} ours={len(ours)}")
            if not ours:
                failures.append("no NEW_MESSAGE notification for our csm session")
            else:
                notif_id = ours[0]["id"]
                print(f"    notif id={notif_id} type={ours[0]['type']} title={ours[0]['title']}")

            # 6) unread summary increased
            sum1 = c.get("/api/notifications/unread-summary").json()
            print(f"[6] total_unread={sum1['total_unread']} (was {base_unread})")
            if sum1["total_unread"] <= base_unread:
                failures.append(f"unread did not increase: {base_unread} → {sum1['total_unread']}")

            # 7a) mark-read on individual notif (validates 200 path; per design does NOT
            #     drop badge for NEW_MESSAGE since the badge is keyed on session.unread_count)
            if notif_id:
                r = c.post(f"/api/notifications/{notif_id}/read")
                print(f"[7a] /{{nid}}/read -> {r.status_code} {r.json()}")
                if r.status_code != 200:
                    failures.append(f"/{{nid}}/read returned {r.status_code}")

            # 7b) the badge-clearing API for session-bound notifications:
            r = c.post(f"/api/notifications/mark-session-read/{csm_sid}")
            print(f"[7b] /mark-session-read/{csm_sid[:8]}... -> {r.status_code} {r.json()}")
            if r.status_code != 200:
                failures.append(f"mark-session-read returned {r.status_code}")

            # 8) unread back down
            sum2 = c.get("/api/notifications/unread-summary").json()
            print(f"[8] total_unread after mark-session-read={sum2['total_unread']}")
            if sum2["total_unread"] >= sum1["total_unread"]:
                failures.append(f"unread did not drop after mark-session-read: {sum1['total_unread']} → {sum2['total_unread']}")

            # 9) cleanup PTY
            r = c.delete(f"/api/sessions/{csm_sid}", params={"graceful": "false"})
            print(f"[9] killed session -> {r.json()}")
    finally:
        try:
            JSONL.unlink()
            TEST_PROJ.rmdir()
        except Exception as e:
            print(f"[cleanup] {e}")

    print("\n==============")
    if failures:
        print("FAILURES:")
        for f in failures:
            print(" -", f)
        exit(1)
    print("PASS: M3 Notifications E2E")
    exit(0)


if __name__ == "__main__":
    main()
