"""Verify B4 fix: individual mark-read on NEW_MESSAGE now decrements badge."""
import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8001"
REPO_ROOT = Path(__file__).resolve().parents[2]
DB = REPO_ROOT / "csm.db"
PROJECTS = Path.home() / ".claude" / "projects"
TEST_PROJ = PROJECTS / "-tmp-csm-b4-test"
CLAUDE_SID = str(uuid.uuid4())
JSONL = TEST_PROJ / f"{CLAUDE_SID}.jsonl"


def main():
    failures = []
    csm_sid = None
    try:
        with httpx.Client(base_url=BASE, timeout=30) as c:
            base_summary = c.get("/api/notifications/unread-summary").json()
            base_unread = base_summary["total_unread"]
            print(f"[baseline] total_unread={base_unread}")

            r = c.post("/api/sessions", json={
                "cwd": "/tmp", "type": "interactive", "title": "b4-verify",
                "argv": ["bash", "-i"],
            })
            csm_sid = r.json()["id"]
            sqlite3.connect(DB).execute(
                "UPDATE session SET external_session_id=? WHERE id=?", (CLAUDE_SID, csm_sid)
            ).connection.commit()
            con = sqlite3.connect(DB)
            con.execute("UPDATE session SET external_session_id=? WHERE id=?", (CLAUDE_SID, csm_sid))
            con.commit(); con.close()

            TEST_PROJ.mkdir(parents=True, exist_ok=True)
            rec = {"type": "assistant", "timestamp": "2026-06-21T07:00:00.000Z",
                   "message": {"role":"assistant","model":"claude-opus-4-7","content":[{"type":"text","text":"b4"}]},
                   "sessionId": CLAUDE_SID}
            JSONL.write_text(json.dumps(rec) + "\n")
            time.sleep(7)

            lst = c.get("/api/notifications", params={"limit": 50}).json()
            ours = [n for n in lst["items"] if n.get("session_id") == csm_sid]
            if not ours:
                failures.append("no notification appeared")
                return
            nid = ours[0]["id"]

            sum1 = c.get("/api/notifications/unread-summary").json()
            print(f"[1] after notif total_unread={sum1['total_unread']} (was {base_unread})")
            if sum1["total_unread"] <= base_unread:
                failures.append("unread did not bump")

            r = c.post(f"/api/notifications/{nid}/read")
            print(f"[2] mark-read -> {r.status_code} {r.json()}")

            sum2 = c.get("/api/notifications/unread-summary").json()
            print(f"[3] after mark-read total_unread={sum2['total_unread']}")
            if sum2["total_unread"] != base_unread:
                failures.append(f"mark-read did not return badge to baseline: {base_unread} → {sum2['total_unread']}")

            c.delete(f"/api/sessions/{csm_sid}", params={"graceful":"false"})
    finally:
        try: JSONL.unlink(); TEST_PROJ.rmdir()
        except Exception: pass

    print("\n==============")
    if failures:
        print("B4 VERIFY FAILED:")
        for f in failures: print(" -", f)
        sys.exit(1)
    print("B4 VERIFY: PASS — single mark-read on NEW_MESSAGE drops badge")


if __name__ == "__main__":
    main()
