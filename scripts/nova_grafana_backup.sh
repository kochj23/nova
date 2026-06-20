#!/usr/bin/env bash
#
# nova_grafana_backup.sh
# Back up nova-core's ~/grafana (dashboards + provisioning + recovered sets +
# docker-compose) into this PUBLIC repo, scrubbing secrets from the repo copy.
# The live nova-core files keep their real values; only the repo copy is scrubbed.
#
# Usage: scripts/nova_grafana_backup.sh
#
set -euo pipefail

NOVA_CORE_HOST="${NOVA_CORE_HOST:-kochj@192.168.1.2}"
REMOTE_DIR="${NOVA_CORE_GRAFANA:-~/grafana}"

# Resolve repo root relative to this script so it works from anywhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEST_DIR="${REPO_ROOT}/grafana"

PLACEHOLDER="__SET_VIA_ENV__"

mkdir -p "${DEST_DIR}"

echo "==> Syncing ${NOVA_CORE_HOST}:${REMOTE_DIR} -> ${DEST_DIR}"

# Pull only the config/dashboard pieces; never the live data volume.
rsync -av --delete \
  --exclude 'data/' \
  --exclude 'grafana-data/' \
  --exclude '*.db' \
  --exclude '*.bak' \
  -e ssh \
  "${NOVA_CORE_HOST}:${REMOTE_DIR}/dashboards" \
  "${NOVA_CORE_HOST}:${REMOTE_DIR}/provisioning" \
  "${NOVA_CORE_HOST}:${REMOTE_DIR}/recovered_from_dot7" \
  "${NOVA_CORE_HOST}:${REMOTE_DIR}/recovered_dashboards" \
  "${NOVA_CORE_HOST}:${REMOTE_DIR}/docker-compose.yml" \
  "${DEST_DIR}/"

echo "==> Scrubbing secrets in repo copy"

# Replace Grafana admin password with a placeholder in the committed copy.
COMPOSE="${DEST_DIR}/docker-compose.yml"
if [ -f "${COMPOSE}" ]; then
  # GNU/BSD sed compatible: write to temp then move.
  sed -E "s/^([[:space:]]*-[[:space:]]*GF_SECURITY_ADMIN_PASSWORD=).*/\1${PLACEHOLDER}/" \
    "${COMPOSE}" > "${COMPOSE}.tmp" && mv "${COMPOSE}.tmp" "${COMPOSE}"
fi

# Scrub any secureJsonData / password fields that might appear in datasource YAMLs.
if [ -d "${DEST_DIR}/provisioning" ]; then
  while IFS= read -r -d '' yml; do
    sed -E \
      -e "s/^([[:space:]]*password:[[:space:]]*).*/\1${PLACEHOLDER}/" \
      -e "s/^([[:space:]]*basicAuthPassword:[[:space:]]*).*/\1${PLACEHOLDER}/" \
      "${yml}" > "${yml}.tmp" && mv "${yml}.tmp" "${yml}"
  done < <(find "${DEST_DIR}/provisioning" -type f \( -name '*.yml' -o -name '*.yaml' \) -print0)
fi

echo "==> Verifying no secrets leaked"
LEAKS="$(grep -rniE '(admin-password|GF_SECURITY_ADMIN_PASSWORD=(admin|[^_]))|secureJsonData|(^|[^a-zA-Z])password:[[:space:]]*[^[:space:]_]' \
  "${DEST_DIR}" 2>/dev/null | grep -v "${PLACEHOLDER}" || true)"
if [ -n "${LEAKS}" ]; then
  echo "!! Potential secret(s) detected after scrub:" >&2
  echo "${LEAKS}" >&2
  exit 1
fi

echo "==> Done. Grafana config backed up and scrubbed at ${DEST_DIR}"
