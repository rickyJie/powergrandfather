#!/usr/bin/env bash
# Generate a self-signed TLS cert + key for local CSM use.
#
# Why self-signed and not mkcert / Let's Encrypt:
#   - CSM is a single-user local tool; the target audience is one machine
#     accessing a LAN IP. A CA-signed cert would need public DNS + ACME.
#   - mkcert adds a third-party dep and installs a root CA into the trust
#     store — heavier than what this use case warrants.
# The tradeoff: the browser shows an "advanced → proceed" warning on the
# FIRST visit per browser. Chrome will remember the exception; Safari
# prompts once per session unless you import the cert. See docs/https_setup.md.
#
# Usage:  ./scripts/gen-cert.sh [extra-san-ip ...]
#   Auto-detects all non-loopback IPv4 addresses on the host and includes
#   them in subjectAltName; pass extra IPs on the command line if you
#   access CSM from an IP the host itself can't see (e.g., NATed setups).

set -e

cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"
OUT_DIR="$PROJECT_ROOT/secrets"
CERT="$OUT_DIR/csm-cert.pem"
KEY="$OUT_DIR/csm-key.pem"

if ! command -v openssl >/dev/null 2>&1; then
  echo "[gen-cert] openssl not found. Install it (apt/brew install openssl) and rerun." >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"

# Collect LAN IPv4 addresses so we can include them in SAN. Falls back cleanly
# if `ip` isn't available (BSD / macOS).
detect_ips() {
  if command -v ip >/dev/null 2>&1; then
    ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1
  elif command -v ifconfig >/dev/null 2>&1; then
    ifconfig 2>/dev/null | awk '/inet /{print $2}' | grep -v '^127\.'
  fi
}

SAN_ENTRIES=("DNS:localhost" "IP:127.0.0.1" "IP:0.0.0.0")
while IFS= read -r ip; do
  [ -n "$ip" ] && SAN_ENTRIES+=("IP:$ip")
done < <(detect_ips)
# Extra IPs from CLI args
for ip in "$@"; do
  SAN_ENTRIES+=("IP:$ip")
done

# De-duplicate.
mapfile -t SAN_ENTRIES < <(printf "%s\n" "${SAN_ENTRIES[@]}" | awk '!seen[$0]++')
SAN_STR="$(IFS=,; echo "${SAN_ENTRIES[*]}")"

echo "[gen-cert] SAN: $SAN_STR"

# One-shot key + cert with the SAN embedded via extension. RSA 2048 is fine
# for a local tool; EC would be marginally faster but adds compat friction on
# older stacks with zero real benefit here.
openssl req -x509 -newkey rsa:2048 \
  -sha256 -days 3650 -nodes \
  -keyout "$KEY" -out "$CERT" \
  -subj "/CN=csm-local" \
  -addext "subjectAltName=$SAN_STR" >/dev/null 2>&1

chmod 600 "$KEY" "$CERT"

echo "[gen-cert] wrote:"
echo "  cert: $CERT"
echo "  key : $KEY"
echo ""
echo "Next: restart the server ('./scripts/stop.sh && ./scripts/start.sh')."
echo "The first browser visit will warn about the self-signed cert —"
echo "click 'Advanced → Proceed' (Chrome) or import the cert. See docs/https_setup.md."
