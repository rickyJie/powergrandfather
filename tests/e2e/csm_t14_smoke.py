"""T14 smoke: B1 exit code (-SIGNUM), B5 bind, B3 async DELETE."""
import asyncio
import time
import uuid

import httpx

BASE = "http://127.0.0.1:8001"

async def main():
    failures = []
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        # ---- B1: graceful DELETE on bash → SIGINT/SIGTERM kills it → expect -2 (SIGINT) or -15 (SIGTERM)
        r = await c.post("/api/sessions", json={"cwd":"/tmp","type":"interactive","title":"t14-b1","argv":["bash","-i"]})
        sid = r.json()["id"]
        print(f"[B1] created {sid}")
        await asyncio.sleep(0.5)
        r = await c.delete(f"/api/sessions/{sid}", params={"graceful":"true"})
        code = r.json()["exit_code"]
        print(f"[B1] exit_code={code} (expect negative SIGNUM like -2 / -15)")
        if code is None:
            failures.append("B1: exit_code still None after graceful stop")
        elif code >= 0:
            failures.append(f"B1: exit_code {code} is non-negative (expected -SIGNUM)")

        # ---- B5: bind
        r = await c.post("/api/sessions", json={"cwd":"/tmp","type":"interactive","title":"t14-b5","argv":["bash","-i"]})
        sid = r.json()["id"]
        fake_jsonl_uuid = str(uuid.uuid4())
        r = await c.post(f"/api/sessions/{sid}/bind", json={"external_session_id": fake_jsonl_uuid})
        print(f"[B5] bind status={r.status_code} body={r.json()}")
        if r.status_code != 200:
            failures.append(f"B5: bind returned {r.status_code}")
        elif r.json().get("external_session_id") != fake_jsonl_uuid:
            failures.append(f"B5: external_session_id not updated to {fake_jsonl_uuid}")
        await c.delete(f"/api/sessions/{sid}", params={"graceful":"false"})

        # ---- B3: async DELETE returns 202 quickly
        r = await c.post("/api/sessions", json={"cwd":"/tmp","type":"interactive","title":"t14-b3","argv":["bash","-i"]})
        sid = r.json()["id"]
        t0 = time.time()
        r = await c.delete(f"/api/sessions/{sid}", params={"graceful":"true","async_":"true"})
        elapsed = time.time() - t0
        print(f"[B3] async DELETE status={r.status_code} body={r.json()} elapsed={elapsed:.2f}s")
        if r.status_code != 202:
            failures.append(f"B3: async DELETE returned {r.status_code} not 202")
        if elapsed > 1.0:
            failures.append(f"B3: async DELETE blocked {elapsed:.2f}s (should return immediately)")
        # wait for background ladder to finalize (SIGINT 5s → SIGTERM 5s → SIGKILL 5s)
        await asyncio.sleep(20)
        r = await c.get(f"/api/sessions/{sid}")
        print(f"[B3] post-bg status={r.json()['status']}")
        if r.json()["status"] == "running":
            failures.append("B3: session still running after background task should have finished")

    print("\n==============")
    if failures:
        print("FAILURES:")
        for f in failures: print(" -", f)
        exit(1)
    print("PASS: T14 (B1+B5+B3)")

asyncio.run(main())
