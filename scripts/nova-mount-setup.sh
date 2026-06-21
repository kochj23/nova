#!/bin/bash
# nova-mount-setup.sh — configure the /nova shared-artifacts SMB mount on this node.
#
# /nova is the fleet's shared ARTIFACTS filesystem (models/media/trans/artifacts),
# exported by the Synology (192.168.1.11) over SMB. See docs/nova-shared-mount.md.
#
# Idempotent. Linux -> CIFS via /etc/fstab. macOS -> autofs via /etc/auto_nova.
# NO secret is hardcoded here: the SMB password is read from the secret backend
# (~/.openclaw/secrets.env  NOVA_SMB_PASSWORD=...) or prompted.
#
# Usage:  sudo -E ./nova-mount-setup.sh        (Linux)
#         ./nova-mount-setup.sh                (macOS — will sudo for /etc writes)
#
# Do NOT run on the flaky .7 node.
set -euo pipefail

HOST="192.168.1.11"
SHARE="nova"
SMB_USER="${NOVA_SMB_USER:-kochj}"

# --- resolve the SMB password from the secret backend (never hardcode) ---
if [ -z "${NOVA_SMB_PASSWORD:-}" ] && [ -f "$HOME/.openclaw/secrets.env" ]; then
  # shellcheck disable=SC1091
  set +u; . "$HOME/.openclaw/secrets.env"; set -u
fi
SMB_PASS="${NOVA_SMB_PASSWORD:-}"
if [ -z "$SMB_PASS" ]; then
  read -rsp "SMB password for ${SMB_USER}@${HOST}: " SMB_PASS; echo
fi

os="$(uname -s)"

if [ "$os" = "Linux" ]; then
  CREDS="/etc/cifs-nova.creds"
  printf 'username=%s\npassword=%s\n' "$SMB_USER" "$SMB_PASS" | sudo tee "$CREDS" >/dev/null
  sudo chmod 600 "$CREDS"; sudo chown root:root "$CREDS"
  sudo mkdir -p /nova
  FSTAB_LINE="//${HOST}/${SHARE} /nova cifs credentials=${CREDS},uid=${SMB_USER},gid=${SMB_USER},iocharset=utf8,vers=3.0,_netdev,nofail 0 0"
  if ! grep -q "${HOST}/${SHARE} " /etc/fstab; then
    echo "$FSTAB_LINE" | sudo tee -a /etc/fstab >/dev/null
  fi
  sudo mount /nova || true
  echo "[ok] Linux: mounted /nova via CIFS (fstab persistent)."

elif [ "$os" = "Darwin" ]; then
  # autofs direct map — survives reboot, mounts on demand, manages mountpoint.
  sudo tee /etc/auto_nova >/dev/null <<EOF
# Nova shared-artifacts autofs map (Synology ${HOST} over SMB). chmod 600.
/nova -fstype=smbfs,soft ://${SMB_USER}:${SMB_PASS}@${HOST}/${SHARE}
EOF
  sudo chmod 600 /etc/auto_nova; sudo chown root:wheel /etc/auto_nova
  if ! grep -q "auto_nova" /etc/auto_master; then
    echo "/-                      auto_nova       -nosuid" | sudo tee -a /etc/auto_master >/dev/null
  fi
  sudo automount -vc >/dev/null 2>&1 || true
  ls /nova >/dev/null 2>&1 || true
  echo "[ok] macOS: configured autofs for /nova (access /nova to mount)."

else
  echo "Unsupported OS: $os" >&2; exit 1
fi

echo "Verify: ls /nova/models/  (should show the shared seeded model)"
