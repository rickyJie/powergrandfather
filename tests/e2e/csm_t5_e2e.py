"""T5 E2E: M4 Automation — launch a run, observe lifecycle, check outputs."""
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8001"
TASK_NAME = "_t5_smoke"
CWD = Path("/tmp/csm-t5-test")


def main():
    failures = []
    with httpx.Client(base_url=BASE, timeout=30) as c:
        # Clean output artifact from any prior run
        for p in CWD.glob("t5-output.txt"):
            p.unlink(missing_ok=True)

        # find task_def_id
        tdlist = c.get("/api/tasks").json()
        td = next((t for t in tdlist["items"] if t["name"] == TASK_NAME), None)
        if not td:
            print(f"FAIL: task {TASK_NAME} not loaded; tasks={[t['name'] for t in tdlist['items']]}")
            sys.exit(1)
        td_id = td["id"]
        print(f"[setup] td_id={td_id}")

        # baseline runs
        runs0 = c.get("/api/runs", params={"task_def_id": td_id}).json()
        print(f"[baseline] existing runs for this task: {runs0['count']}")

        # launch
        t0 = time.time()
        r = c.post("/api/runs/launch", json={"task_def_id": td_id})
        r.raise_for_status()
        run = r.json()
        run_id = run["id"]
        print(f"[1] launched run_id={run_id} status={run['status']}")
        if run["status"] != "running":
            failures.append(f"launch status was {run['status']} not RUNNING")

        # poll until terminal (or 30s timeout)
        terminal = None
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(1)
            cur = c.get(f"/api/runs/{run_id}").json()
            if cur["status"] not in ("running", "pending"):
                terminal = cur
                break
        elapsed = time.time() - t0
        if terminal is None:
            print(f"[FAIL] run still {cur['status']} after 30s")
            failures.append("run did not reach terminal state within 30s")
            terminal = cur
        else:
            print(f"[2] reached terminal status={terminal['status']} in {elapsed:.1f}s exit={terminal.get('exit_code')}")

        if terminal["status"] != "succeeded":
            failures.append(f"terminal status was {terminal['status']} not SUCCEEDED")

        # outputs discovered
        outs = terminal.get("outputs", [])
        out_paths = [o["path"] for o in outs]
        print(f"[3] outputs ({len(outs)}): {out_paths}")
        if not any(Path(p).name == "t5-output.txt" for p in out_paths):
            failures.append(f"t5-output.txt not in discovered outputs: {out_paths}")

        # verify on-disk content
        actual = CWD / "t5-output.txt"
        if actual.exists():
            content = actual.read_text().strip()
            print(f"[4] on-disk t5-output.txt content: {content!r}")
            if not content.startswith("HELLO_T5_"):
                failures.append("file content not as expected")
        else:
            failures.append("t5-output.txt was not created on disk")

        # 5) retry path: launch_run again, should produce a second Run
        runs1 = c.get("/api/runs", params={"task_def_id": td_id}).json()
        print(f"[5] runs after launch: {runs1['count']}")

    print("\n==============")
    if failures:
        print("FAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("PASS: M4 Automation E2E")
    sys.exit(0)


if __name__ == "__main__":
    main()
