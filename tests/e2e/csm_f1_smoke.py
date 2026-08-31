"""F1 smoke: TaskDef param substitution end-to-end with a synthetic template."""
import asyncio
import os
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8001"
TPL = str(Path(__file__).resolve().parents[2] / "tasks" / "_f1_smoke.yaml")


async def main():
    failures = []
    # Create a smoke task YAML with parameters
    os.makedirs("/tmp/csm-f1-test", exist_ok=True)
    with open(TPL, "w") as f:
        f.write("""name: _f1_smoke
description: F1 param-substitution smoke
cwd: "/tmp/csm-f1-test"
prompt: |
  echo "group={group_id} iter={iter_n}" > result-{group_id}.txt && exit 0
parameters:
  - {name: group_id, type: str, required: true}
  - {name: iter_n, type: int, default: 5}
output_globs: ["result-*.txt"]
tags: [test]
""")

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        # reload
        r = await c.post("/api/tasks/reload")
        td = next((t for t in r.json()["items"] if t["name"] == "_f1_smoke"), None)
        assert td is not None
        print(f"[1] loaded td_id={td['id']} params={td['parameters']}")

        # launch with valid params
        r = await c.post("/api/runs/launch", json={
            "task_def_id": td["id"],
            "parameters": {"group_id": "ALPHA", "iter_n": 7},
        })
        run = r.json()
        run_id = run["id"]
        print(f"[2] launched run {run_id} parameters={run['parameters']}")

        # poll
        deadline = time.time() + 20
        terminal = None
        while time.time() < deadline:
            await asyncio.sleep(1)
            cur = (await c.get(f"/api/runs/{run_id}")).json()
            if cur["status"] not in ("running", "pending"):
                terminal = cur
                break
        print(f"[3] terminal status={terminal['status']} outputs={len(terminal.get('outputs') or [])}")
        if terminal["status"] != "succeeded":
            failures.append(f"run not succeeded: {terminal['status']}")

        # check on-disk file
        expected = "/tmp/csm-f1-test/result-ALPHA.txt"
        if not os.path.exists(expected):
            failures.append(f"expected file {expected} not created → substitution failed")
        else:
            content = open(expected).read().strip()
            print(f"[4] file content: {content!r}")
            if "group=ALPHA" not in content or "iter=7" not in content:
                failures.append(f"substitution wrong: {content}")

        # negative: missing required param
        r = await c.post("/api/runs/launch", json={
            "task_def_id": td["id"],
            "parameters": {"iter_n": 1},  # missing group_id
        })
        print(f"[5] launch w/o required param: status={r.status_code} body={r.text[:200]}")
        if r.status_code == 200:
            failures.append("expected 4xx for missing required param, got 200")

        # negative: bad type
        r = await c.post("/api/runs/launch", json={
            "task_def_id": td["id"],
            "parameters": {"group_id": "X", "iter_n": "not-a-number"},
        })
        print(f"[6] launch with bad type: status={r.status_code}")
        if r.status_code == 200:
            failures.append("expected 4xx for bad type, got 200")

    # cleanup
    try: os.unlink(TPL)
    except Exception: pass

    print("\n==============")
    if failures:
        print("FAILURES:")
        for f in failures: print(" -", f)
        exit(1)
    print("PASS: F1 param substitution")


asyncio.run(main())
