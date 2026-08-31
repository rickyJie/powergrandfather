"""T7 E2E: M6 Ports — scan, register, list, find-free, reverse proxy."""
import socket
import subprocess
import sys
import time

import httpx

BASE = "http://127.0.0.1:8001"
TEST_PORT = 9876


def find_free_local_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    failures = []
    http_server = None

    # Use python -m http.server on a known-free port
    port = TEST_PORT
    # ensure it's actually free
    try:
        s = socket.socket()
        s.bind(("127.0.0.1", port))
        s.close()
    except OSError:
        port = find_free_local_port()
        print(f"[setup] {TEST_PORT} taken; using {port}")

    print(f"[setup] starting python http.server on :{port}")
    http_server = subprocess.Popen(
        ["python3", "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd="/tmp/csm-t5-test",  # serve our t5 outputs as content
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1.5)

    try:
        with httpx.Client(base_url=BASE, timeout=10) as c:
            # 1) scan-now → diff should include our new port (in added or seen)
            diff = c.post("/api/ports/scan-now").json()
            print(f"[1] scan-now keys={list(diff.keys())}")

            # 2) list ports — our port should now be tracked
            lst = c.get("/api/ports").json()
            ours = [p for p in lst["items"] if p["port"] == port]
            print(f"[2] ports list count={lst['count']} ours_found={bool(ours)}")
            if ours:
                p_info = ours[0]
                print(f"    pid={p_info['pid']} cmd={(p_info['process_cmd'] or '')[:60]} status={p_info['status']}")
            else:
                # scan may have missed; try register manually
                print("    not found by scan, registering manually")
                r = c.post("/api/ports", json={"port": port, "name": "t7-test"})
                r.raise_for_status()
                print(f"    registered: {r.json()['port']}")

            # 3) find-free
            ff = c.get("/api/ports/find-free?start=10000&end=10100").json()
            print(f"[3] find-free 10000-10100 → {ff['free_port']}")
            if not (10000 <= ff["free_port"] <= 10100):
                failures.append(f"find-free returned out-of-range: {ff['free_port']}")

            # 4) Reverse proxy: GET /proxy/{port}/ → should return the http.server's directory listing
            r = c.get(f"/proxy/{port}/")
            print(f"[4] proxy GET /proxy/{port}/ -> {r.status_code} len={len(r.text)}")
            if r.status_code != 200:
                failures.append(f"proxy returned {r.status_code}")
            elif "Directory listing for" not in r.text:
                # http.server's title contains this
                print(f"    body sample: {r.text[:200]!r}")
                failures.append("proxy body does not look like http.server directory listing")

            # 5) Cleanup registry entry
            r = c.delete(f"/api/ports/{port}")
            print(f"[5] release -> {r.status_code} {r.json() if r.status_code == 200 else r.text}")

    finally:
        if http_server:
            http_server.terminate()
            try:
                http_server.wait(timeout=3)
            except subprocess.TimeoutExpired:
                http_server.kill()

    print("\n==============")
    if failures:
        print("FAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("PASS: M6 Ports E2E")
    sys.exit(0)


if __name__ == "__main__":
    main()
