#!/usr/bin/env bash
# Install or upgrade smitele-bot on the cluster.
#
#   ./upgrade.sh              # helm upgrade --install
#   ./upgrade.sh --dry-run    # render only
#
# Secrets come from values.local.yaml, which is gitignored. build.sh first if
# the image changed.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
RELEASE="${RELEASE:-smitele-bot}"
NAMESPACE="${NAMESPACE:-discord}"

[[ -f "${HERE}/values.local.yaml" ]] || {
  echo "missing values.local.yaml — copy values.local.yaml.example and fill in the"
  echo "Discord token and Hi-Rez dev ID / auth key."; exit 1; }

# The namespace is created once, by hand, and never managed by a chart.
kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1 || {
  echo "namespace ${NAMESPACE} does not exist — create it first:"
  echo "  kubectl create namespace ${NAMESPACE}"; exit 1; }

# The collector needs the SMB share to exist and csi-driver-smb to be installed.
# Catch the missing driver here rather than as a pod stuck in ContainerCreating.
if grep -qE '^\s*enabled:\s*true' <(awk '/^matchData:/,/^[a-z]/' "${HERE}/values.local.yaml" 2>/dev/null); then
  kubectl get csidriver smb.csi.k8s.io >/dev/null 2>&1 || {
    echo "matchData.enabled is set but csi-driver-smb is not installed on this cluster."
    echo "See README §Prerequisites."; exit 1; }
fi

echo "==> helm upgrade --install ${RELEASE} (namespace ${NAMESPACE})"
helm upgrade --install "${RELEASE}" "${HERE}" \
  --namespace "${NAMESPACE}" \
  -f "${HERE}/values.yaml" \
  -f "${HERE}/values.local.yaml" \
  "$@"

if [[ " $* " != *" --dry-run "* ]]; then
  echo "==> Waiting for the bot to come up"
  kubectl -n "${NAMESPACE}" rollout status "deployment/${RELEASE}" --timeout=300s
  echo "==> Logs:  kubectl -n ${NAMESPACE} logs -f deployment/${RELEASE}"
fi
