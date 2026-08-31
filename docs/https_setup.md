# HTTPS setup

CSM runs plain HTTP by default. Enable HTTPS when you access CSM from a
LAN IP and need browser features that require a secure context —
specifically **right-click paste in the terminal** (which uses
`navigator.clipboard.readText()`; Chrome / Safari refuse to expose this
API on `http://192.168.x.x` origins).

## One-time setup

```bash
./scripts/gen-cert.sh                   # or: ./scripts/gen-cert.sh 10.0.0.5
./scripts/stop.sh && ./scripts/start.sh
```

`gen-cert.sh` produces `secrets/csm-cert.pem` and `secrets/csm-key.pem`.
The cert:

- is valid for 10 years (self-signed, single-user tool);
- includes `localhost`, `127.0.0.1`, and every non-loopback IPv4 the host
  can see as `subjectAltName`;
- can be regenerated at any time — pass extra IPs on the command line if
  the host is NATed and can't see the address you actually browse to.

`start.sh` auto-detects the cert files. If they exist it boots uvicorn with
`--ssl-keyfile` / `--ssl-certfile` and the URL becomes `https://…:8000`;
otherwise it stays on HTTP.

## Trusting the cert in the browser

The cert is self-signed so the browser will warn on the first visit.

**Chrome** (per-machine, remembered):

1. First visit → "Your connection is not private" page.
2. Click **Advanced → Proceed to <host> (unsafe)**.
3. Chrome remembers the exception for this origin.

If you want zero warning, import `secrets/csm-cert.pem` into `chrome://certificate-manager` → Custom → Installed by you → Trust this certificate for identifying websites.

**Safari** (macOS):

1. First visit → warning page.
2. **Show Details → visit this website → Visit Website → enter password**.
3. Safari adds a permanent exception to the keychain.

**Firefox**:

1. First visit → warning.
2. **Advanced → Accept the Risk and Continue**.
3. Remembered per-origin.

## When to regenerate

- Machine's LAN IP changed and you browse to the new IP (SAN mismatch).
- Cert expired (10 years from generation).
- You want to reset trust (delete `secrets/` + re-run `gen-cert.sh`).

## Rollback to HTTP

```bash
rm -rf secrets/
./scripts/stop.sh && ./scripts/start.sh
```

No config change needed — the absence of cert files is the toggle.

## Single-port HTTP + HTTPS mode

By default `start.sh` binds uvicorn directly to `:8000` with TLS. That
works cleanly for `https://<ip>:8000/…` but *breaks* `http://<ip>:8000/…`
with an SSL protocol error — a common paper-cut when a colleague pastes
you an `http://` link (local:fc98b162).

`scripts/start-mux.sh` runs an alternate topology: uvicorn HTTPS on an
internal loopback port, and `scripts/proto_mux.py` on the public port
(default 8000) doing per-connection TLS-vs-plain-HTTP detection.

- TLS handshake → transparently proxied to uvicorn.
- Plain HTTP → `301 Moved Permanently → https://<host>:8000/<path>`.

Both `https://<ip>:8000/foo` and `http://<ip>:8000/foo` now land users on
the same TLS service, without needing an L4 mux like sslh.

### Usage

```bash
./scripts/gen-cert.sh              # if not already done
./scripts/stop.sh 2>/dev/null      # stop the plain start.sh instance if any
./scripts/start-mux.sh             # or: ./scripts/start-mux.sh 0.0.0.0 8000 18443
```

Two processes result:
- uvicorn on `127.0.0.1:18443` (PID → `csm.pid`, log → `csm.log`)
- proto_mux on `0.0.0.0:8000` (PID → `csm-mux.pid`, log → `csm-mux.log`)

`stop.sh` also kills the mux since it reads `csm.pid` for uvicorn; kill
`csm-mux.pid` alongside if you're stopping manually.

## Not covered here

- Public / CA-signed certs (out of scope; CSM is a local single-user tool).
- Mutual TLS / client certs (not a threat model CSM targets).
- Reverse-proxy termination (nginx / Caddy in front of CSM) — works fine
  but is your responsibility; keep CSM on HTTP behind it in that case.
