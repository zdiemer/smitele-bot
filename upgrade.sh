#!/usr/bin/env bash
# Install or upgrade smitele-bot on the cluster.
#
#   ./upgrade.sh                          # helm upgrade --install
#   ./upgrade.sh --dry-run                # render only
#   ./upgrade.sh --force-active-sessions  # roll even mid-game
#
# Secrets come from values.local.yaml, which is gitignored. build.sh first if
# the image changed.

set -euo pipefail

# Consumed here, not by helm.
FORCE_ACTIVE=0
ARGS=()
for arg in "$@"; do
  if [[ "$arg" == "--force-active-sessions" ]]; then
    FORCE_ACTIVE=1
  else
    ARGS+=("$arg")
  fi
done
set -- ${ARGS+"${ARGS[@]}"}

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

# Rolling the Deployment kills any Smite-le or trivia round in progress: both
# live entirely in memory in a coroutine, so players just watch the game stop
# answering. Ask the bot before restarting it.
#
# The image is python-slim with no curl, so the probe runs through python.
if [[ "$FORCE_ACTIVE" == "0" && " $* " != *" --dry-run "* ]]; then
  POD="$(kubectl -n "${NAMESPACE}" get pod \
    -l "app.kubernetes.io/name=smitele-bot,app.kubernetes.io/instance=${RELEASE},app.kubernetes.io/component=bot" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"

  if [[ -n "$POD" ]]; then
    STATUS="$(kubectl -n "${NAMESPACE}" exec "$POD" -- python -c \
      'import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:8080/healthz",timeout=5).read().decode())' \
      2>/dev/null || true)"

    if [[ -z "$STATUS" ]]; then
      # No endpoint yet (first install, or a build predating it). Nothing to
      # protect, so this is not a failure.
      echo "==> No status endpoint on ${POD}; skipping the active-session check"
    else
      ACTIVE="$(printf '%s' "$STATUS" | python3 -c 'import json,sys;print(json.load(sys.stdin)["active_sessions"])' 2>/dev/null || echo 0)"
      if [[ "$ACTIVE" != "0" ]]; then
        echo "REFUSING TO UPGRADE: ${ACTIVE} game session(s) in progress." >&2
        printf '  %s\n' "$STATUS" >&2
        echo "Wait for them to finish, or re-run with --force-active-sessions." >&2
        exit 1
      fi
      echo "==> No active game sessions; safe to roll"
    fi
  fi
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
