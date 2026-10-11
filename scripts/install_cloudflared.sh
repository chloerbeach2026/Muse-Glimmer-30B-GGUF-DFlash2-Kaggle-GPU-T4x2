#!/usr/bin/env bash
set -Eeuo pipefail
# Pinned Linux cloudflared bootstrap for personal agent serving.
VERSION="2026.10.0"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-$ROOT/vendor/bin/cloudflared}"
URL_BASE="https://github.com/cloudflare/cloudflared/releases/download/$VERSION"
case "$(uname -m)" in
  x86_64|amd64) ASSET=cloudflared-linux-amd64; EXPECTED_SHA256=d33ff2d14475178d2012c2c56beba87389ac5ded27649519f198a7d3134a99db ;;
  *) echo "ERROR: pinned cloudflared asset is qualified only for Linux x86_64; got $(uname -m)" >&2; exit 2 ;;
esac
verify() {
  local candidate="$1" actual
  [[ -x "$candidate" ]] || return 1
  actual="$(sha256sum "$candidate" | awk '{print $1}')" || return 1
  [[ "$actual" == "$EXPECTED_SHA256" ]] || return 1
  "$candidate" --version 2>&1 | grep -Fq "$VERSION"
}
if verify "$DEST"; then
  echo "CLOUDFLARED_READY=PASS version=$VERSION sha256=$EXPECTED_SHA256 path=$DEST"
  exit 0
fi
mkdir -p "$(dirname "$DEST")"
tmp="$(mktemp "$(dirname "$DEST")/.cloudflared.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
curl --fail --location --retry 5 --retry-all-errors --connect-timeout 20 \
  --proto '=https' --proto-redir '=https' "$URL_BASE/$ASSET" -o "$tmp"
actual="$(sha256sum "$tmp" | awk '{print $1}')"
[[ "$actual" == "$EXPECTED_SHA256" ]] || {
  echo "ERROR: cloudflared SHA-256 mismatch expected=$EXPECTED_SHA256 actual=$actual" >&2
  exit 4
}
chmod 0755 "$tmp"
"$tmp" --version 2>&1 | grep -Fq "$VERSION" || {
  echo "ERROR: cloudflared version mismatch; refusing install." >&2
  exit 4
}
mv -f "$tmp" "$DEST"
trap - EXIT
chmod 0755 "$DEST"
verify "$DEST" || { echo "ERROR: installed cloudflared failed final verification." >&2; exit 4; }
echo "CLOUDFLARED_READY=PASS version=$VERSION sha256=$EXPECTED_SHA256 path=$DEST"
