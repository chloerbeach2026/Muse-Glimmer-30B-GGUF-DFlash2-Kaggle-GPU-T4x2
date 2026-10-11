#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$ROOT/config/muse-agent-server.service.in"
DEST="/etc/systemd/system/muse-agent-server.service"
ENV_FILE="/etc/muse-agent-server.env"
SERVICE_USER="${MUSE_SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
SERVICE_GROUP="$(id -gn "$SERVICE_USER" 2>/dev/null || true)"

if [[ "$(id -u)" != 0 ]]; then
  echo "ERROR: run with sudo: sudo bash $ROOT/scripts/install_systemd_service.sh" >&2
  exit 2
fi
[[ -f "$TEMPLATE" ]] || { echo "ERROR: unit template missing: $TEMPLATE" >&2; exit 3; }
command -v systemctl >/dev/null 2>&1 || { echo "ERROR: systemd/systemctl is not available on this host." >&2; exit 4; }
[[ -n "$SERVICE_USER" && -n "$SERVICE_GROUP" ]] || { echo "ERROR: service user/group not found." >&2; exit 5; }
case "$ROOT" in
  *[!A-Za-z0-9_./-]*)
    echo "ERROR: project path contains characters not supported by this installer: $ROOT" >&2
    echo "Move the checkout to a path containing only letters, digits, dot, underscore, slash and hyphen." >&2
    exit 6 ;;
esac
[[ -f "$ROOT/scripts/agent_supervisor.sh" && -x "$ROOT/external.sh" && -x "$ROOT/serve.sh" ]] || {
  echo "ERROR: project entrypoints are not executable. Check the checkout and file modes." >&2
  exit 7
}

if [[ ! -e "$ENV_FILE" ]]; then
  install -o root -g root -m 0600 /dev/null "$ENV_FILE"
  cat > "$ENV_FILE" <<'EOF'
# systemd environment for Muse Glimmer Agent Server.
# Set a private API token (at least 32 printable ASCII characters):
# MUSE_API_TOKEN=replace-with-a-private-token
# Optional Cloudflare exposure:
# EXPOSURE_MODE=quick
# For Named Tunnel, configure a Published Application/DNS route first, then set:
# EXPOSURE_MODE=named
# TUNNEL_TOKEN=replace-with-cloudflare-tunnel-token
# EXPOSURE_PUBLIC_URL=https://llm.example.com
EOF
  chmod 0600 "$ENV_FILE"
fi

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
sed \
  -e "s|@PROJECT_ROOT@|$ROOT|g" \
  -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
  -e "s|@SERVICE_GROUP@|$SERVICE_GROUP|g" \
  -e "s|@ENV_FILE@|$ENV_FILE|g" \
  "$TEMPLATE" > "$tmp"
install -o root -g root -m 0644 "$tmp" "$DEST"
systemctl daemon-reload

cat <<EOF
SYSTEMD_UNIT_INSTALLED=$DEST
SYSTEMD_ENV_FILE=$ENV_FILE
SYSTEMD_SERVICE_USER=$SERVICE_USER
Next:
  1. Edit $ENV_FILE and set MUSE_API_TOKEN to a private value (>=32 printable characters).
  2. If using a Cloudflare Named Tunnel, set EXPOSURE_MODE=named, TUNNEL_TOKEN and EXPOSURE_PUBLIC_URL.
  3. Ensure $SERVICE_USER can use the NVIDIA GPUs and write to $ROOT.
  4. Start at boot: systemctl enable --now muse-agent-server.service
  5. Check: systemctl status muse-agent-server.service; journalctl -u muse-agent-server.service -f
This installer does not start the model or read/print your secrets.
EOF
