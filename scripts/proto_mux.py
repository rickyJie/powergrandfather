#!/usr/bin/env python3
"""Single-port HTTP+HTTPS multiplexer for CSM (local:fc98b162).

Listens on 0.0.0.0:PUBLIC_PORT. Peeks the first byte of each connection:

  0x16 (TLS ClientHello) → transparent proxy to 127.0.0.1:INTERNAL_HTTPS_PORT
  anything else          → assume plain HTTP, respond with 301 to
                           `https://<Host>:PUBLIC_PORT/<path>`

Solves the "user types `http://ip:8000/…` and the browser shows
ERR_SSL_PROTOCOL_ERROR because uvicorn is TLS-only" problem without a
full L4 mux (sslh / Caddy-L4). Pure stdlib asyncio, no extra deps.

Usage
-----
    python scripts/proto_mux.py --public-port 8000 --internal-https-port 18443

Paired with `scripts/start-mux.sh`, which starts uvicorn on the internal
HTTPS port and this proxy on the public port. Existing `scripts/start.sh`
(direct uvicorn bind) still works and is untouched.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re

log = logging.getLogger("proto_mux")

# Absolute-form request URIs (e.g. `GET http://host:port/path HTTP/1.1`) still
# work — the browser follows Location as-is, and the Host header rewrite here
# is only used to build the redirect target when the client sent a relative
# request-line (the common case for browsers hitting our public IP).
_HOST_HEADER_RE = re.compile(rb"^Host:\s*([^\r\n]+)", re.IGNORECASE | re.MULTILINE)
_REQ_LINE_RE = re.compile(rb"^([A-Z]+)\s+(\S+)\s+HTTP/", re.MULTILINE)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _handle_tls(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    first_bytes: bytes,
    internal_port: int,
) -> None:
    """Transparent byte-proxy client ↔ 127.0.0.1:internal_port."""
    try:
        up_reader, up_writer = await asyncio.open_connection("127.0.0.1", internal_port)
    except OSError as e:
        log.warning("upstream connect failed: %s", e)
        client_writer.close()
        return
    # Replay the peeked bytes upstream so uvicorn's TLS layer sees the
    # full ClientHello.
    up_writer.write(first_bytes)
    await up_writer.drain()
    await asyncio.gather(
        _pipe(client_reader, up_writer),
        _pipe(up_reader, client_writer),
        return_exceptions=True,
    )


async def _handle_http(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    first_bytes: bytes,
    public_port: int,
) -> None:
    """Buffer enough of the request to grab Host + path, reply 301."""
    buf = bytearray(first_bytes)
    try:
        while b"\r\n\r\n" not in buf and len(buf) < 8192:
            chunk = await asyncio.wait_for(client_reader.read(4096), timeout=2.0)
            if not chunk:
                break
            buf.extend(chunk)
    except (TimeoutError, ConnectionResetError):
        pass

    host_m = _HOST_HEADER_RE.search(bytes(buf))
    req_m = _REQ_LINE_RE.search(bytes(buf))
    host = host_m.group(1).decode(errors="replace").strip() if host_m else f"localhost:{public_port}"
    # Strip client-supplied port from Host — we always redirect to our own
    # public port over HTTPS.
    if ":" in host:
        host = host.split(":", 1)[0]
    path = req_m.group(2).decode(errors="replace") if req_m else "/"
    location = f"https://{host}:{public_port}{path}"
    body = (
        f"<html><head><title>301 Moved</title></head>"
        f"<body><p>Moved to <a href=\"{location}\">{location}</a></p></body></html>"
    ).encode()
    response = (
        b"HTTP/1.1 301 Moved Permanently\r\n"
        + f"Location: {location}\r\n".encode()
        + b"Content-Type: text/html; charset=utf-8\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + body
    )
    try:
        client_writer.write(response)
        await client_writer.drain()
    finally:
        client_writer.close()
        try:
            await client_writer.wait_closed()
        except Exception:
            pass


async def _handle_connection(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    internal_https_port: int,
    public_port: int,
) -> None:
    try:
        try:
            first = await asyncio.wait_for(client_reader.read(3), timeout=3.0)
        except TimeoutError:
            client_writer.close()
            return
        if not first:
            client_writer.close()
            return
        if first[0] == 0x16:
            await _handle_tls(client_reader, client_writer, first, internal_https_port)
        else:
            await _handle_http(client_reader, client_writer, first, public_port)
    except Exception as e:
        log.warning("connection handler crashed: %s", e)
        try:
            client_writer.close()
        except Exception:
            pass


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--public-port", type=int, default=8000, help="Port exposed to clients")
    p.add_argument("--internal-https-port", type=int, default=18443, help="uvicorn HTTPS port on 127.0.0.1")
    p.add_argument("--host", default="0.0.0.0", help="Bind host for the public port")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [proto_mux] %(message)s")

    server = await asyncio.start_server(
        lambda r, w: _handle_connection(r, w, args.internal_https_port, args.public_port),
        args.host,
        args.public_port,
    )
    log.info(
        "listening on %s:%d — TLS proxied to 127.0.0.1:%d; plain HTTP → 301 https://<host>:%d",
        args.host, args.public_port, args.internal_https_port, args.public_port,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
