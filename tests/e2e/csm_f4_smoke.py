"""F4 smoke: spin up an echo WS server on a free port, connect through /proxy/{port}/ws/, verify round-trip."""
import asyncio
import socket

import websockets


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def echo_server(port: int):
    """Upstream WS server that echoes back what it receives."""
    async def handler(ws):
        async for msg in ws:
            await ws.send(msg)
    return await websockets.serve(handler, "127.0.0.1", port, max_size=2**20)


async def main():
    port = free_port()
    print(f"[setup] starting echo WS server on :{port}")
    srv = await echo_server(port)
    failures = []
    try:
        # 1) Direct connection works (sanity)
        async with websockets.connect(f"ws://127.0.0.1:{port}/") as ws:
            await ws.send("direct-hi")
            r = await asyncio.wait_for(ws.recv(), timeout=2)
            print(f"[1] direct echo: {r}")
            if r != "direct-hi":
                failures.append("direct echo mismatch")

        # 2) Through proxy (text frame)
        url = f"ws://127.0.0.1:8001/proxy/{port}/ws/"
        print(f"[2] connecting through proxy: {url}")
        async with websockets.connect(url) as ws:
            await ws.send("hello-through-proxy")
            r = await asyncio.wait_for(ws.recv(), timeout=3)
            print(f"[2] text echo: {r}")
            if r != "hello-through-proxy":
                failures.append(f"proxied text echo mismatch: {r!r}")

            # 3) binary frame
            await ws.send(b"\x00\x01\x02BINARY")
            r2 = await asyncio.wait_for(ws.recv(), timeout=3)
            print(f"[3] binary echo: {r2!r}")
            if r2 != b"\x00\x01\x02BINARY":
                failures.append(f"proxied binary echo mismatch: {r2!r}")

            # 4) many frames burst
            for i in range(20):
                await ws.send(f"msg-{i}")
            got = []
            for _ in range(20):
                got.append(await asyncio.wait_for(ws.recv(), timeout=2))
            print(f"[4] burst 20 frames roundtrip: first={got[0]} last={got[-1]}")
            if got != [f"msg-{i}" for i in range(20)]:
                failures.append("burst order/contents wrong")
    finally:
        srv.close()
        await srv.wait_closed()

    print("\n==============")
    if failures:
        print("FAILURES:")
        for f in failures: print(" -", f)
        exit(1)
    print("PASS: F4 WS reverse proxy")


asyncio.run(main())
