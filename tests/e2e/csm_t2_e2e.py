"""T2 E2E: M1 Session Manager flow against running backend on 127.0.0.1:8001."""
import asyncio
import json
import sys
import time

import httpx
import websockets

BASE = "http://127.0.0.1:8001"
WS_BASE = "ws://127.0.0.1:8001"


async def main():
    results: dict[str, object] = {}

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        # 1. CREATE bash session
        r = await c.post("/api/sessions", json={
            "cwd": "/tmp",
            "type": "interactive",
            "title": "t2-e2e-bash",
            "argv": ["bash", "-i"],
        })
        r.raise_for_status()
        sess = r.json()
        sid = sess["id"]
        results["1_create"] = {"sid": sid, "status": sess["status"], "pid": sess["pid"]}
        print(f"[1] created sid={sid} status={sess['status']} pid={sess['pid']}")

        # 2. LIST sessions
        r = await c.get("/api/sessions")
        r.raise_for_status()
        lst = r.json()
        ours = [s for s in lst["items"] if s["id"] == sid]
        results["2_list"] = {"count": lst["count"], "ours_found": bool(ours)}
        print(f"[2] list count={lst['count']} ours_found={bool(ours)}")

        # 3. GET single
        r = await c.get(f"/api/sessions/{sid}")
        r.raise_for_status()
        results["3_get"] = {"status": r.json()["status"]}
        print(f"[3] get status={r.json()['status']}")

        # 4. WS attach: receive ring buffer (initial bash prompt), send "echo HELLO_T2\n", receive output
        async with websockets.connect(f"{WS_BASE}/api/sessions/{sid}/ws") as ws:
            ring_chunks = []
            try:
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.5)
                    ring_chunks.append(msg if isinstance(msg, bytes) else msg.encode())
            except TimeoutError:
                pass
            ring_initial = b"".join(ring_chunks)
            results["4a_ring_initial_bytes"] = len(ring_initial)
            print(f"[4a] ring initial bytes={len(ring_initial)} preview={ring_initial[:80]!r}")

            await ws.send(b"echo HELLO_T2_$$\n")
            after_chunks = []
            try:
                deadline = time.time() + 3
                while time.time() < deadline:
                    msg = await asyncio.wait_for(ws.recv(), timeout=2)
                    after_chunks.append(msg if isinstance(msg, bytes) else msg.encode())
                    if b"HELLO_T2" in b"".join(after_chunks):
                        # one more drain
                        try:
                            extra = await asyncio.wait_for(ws.recv(), timeout=0.3)
                            after_chunks.append(extra if isinstance(extra, bytes) else extra.encode())
                        except TimeoutError:
                            pass
                        break
            except TimeoutError:
                pass
            after = b"".join(after_chunks)
            saw_hello = b"HELLO_T2" in after
            results["4b_echo_roundtrip"] = {"saw_hello": saw_hello, "after_bytes": len(after)}
            print(f"[4b] after echo bytes={len(after)} saw_HELLO_T2={saw_hello} preview={after[:200]!r}")

        # 5. STOP (graceful)
        r = await c.delete(f"/api/sessions/{sid}", params={"graceful": "true"})
        r.raise_for_status()
        results["5_stop"] = r.json()
        print(f"[5] stop -> {r.json()}")

        # 6. GET after stop
        r = await c.get(f"/api/sessions/{sid}")
        r.raise_for_status()
        post = r.json()
        results["6_post_status"] = {"status": post["status"], "ended_at": post["ended_at"], "exit_code": post["exit_code"]}
        print(f"[6] post-stop status={post['status']} ended_at={post['ended_at']} exit={post['exit_code']}")

    # PASS / FAIL summary
    failures = []
    if results["1_create"]["status"] != "running":
        failures.append("create: status != running")
    if not results["2_list"]["ours_found"]:
        failures.append("list: created session not in list")
    if not results["4b_echo_roundtrip"]["saw_hello"]:
        failures.append("ws: did not see echo HELLO_T2 output")
    if results["6_post_status"]["status"] == "running":
        failures.append("stop: session still RUNNING after DELETE")

    print("\n========================================")
    print("FAILURES:" if failures else "PASS: all M1 checks green")
    for f in failures:
        print("  -", f)
    print("========================================")
    json.dump(results, open("/tmp/csm_t2_results.json", "w"), indent=2)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
