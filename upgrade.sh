#!/usr/bin/env bash
# Install or upgrade smitele-bot on the cluster.
#
#   ./upgrade.sh                          # helm upgrade --install
#   ./upgrade.sh --dry-run                # render only
#   ./upgrade.sh --force-active-sessions  # roll even mid-game
#
# Secrets are resolved from 1Password into RAM at deploy time. build.sh first if
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

# Resolve the secret from 1Password into RAM for the life of this run. It is
# never written to a persistent disk, and it is removed on exit.
#
# $SELFHOSTED_LOCAL_VALUES lets a caller supply a path instead. The on-disk
# values.local.yaml is the last resort, for a clone that predates this.
resolve_local_values() {
  local here="$1" rt="" d
  if [[ -n "${SELFHOSTED_LOCAL_VALUES:-}" ]]; then printf '%s\n' "$SELFHOSTED_LOCAL_VALUES"; return 0; fi
  if [[ -f "${here}/values.local.tpl.yaml" ]] && command -v op >/dev/null 2>&1; then
    # A tmpfs, asserted rather than assumed: /tmp is ext4 on some of these hosts,
    # so falling back to it would quietly reintroduce the file this removes.
    for d in "${XDG_RUNTIME_DIR:-}" "/run/user/$(id -u)" /dev/shm; do
      [[ -n "$d" && -d "$d" && -w "$d" ]] || continue
      case "$(stat -f -c %T "$d" 2>/dev/null)" in tmpfs|ramfs) rt="$d"; break ;; esac
    done
    [[ -n "$rt" ]] || { echo "FAIL: no tmpfs available; refusing to write the secret to a disk" >&2; return 1; }
    local f; f="$(mktemp "${rt}/values.local.XXXXXX")" || return 1
    chmod 600 "$f"
    op inject -i "${here}/values.local.tpl.yaml" -o "$f" -f >/dev/null 2>&1 \
      || { rm -f "$f"; echo "FAIL: op inject failed. Signed in?  eval \$(op signin)" >&2; return 1; }
    printf '%s\n' "$f"; return 0
  fi
  printf '%s\n' "${here}/values.local.yaml"
}
LOCAL_VALUES="$(resolve_local_values "$HERE")" || exit 1
[[ "$LOCAL_VALUES" == "${HERE}/"* ]] || trap 'rm -f "$LOCAL_VALUES"' EXIT INT TERM
[[ -f "$LOCAL_VALUES" ]] || {
  echo "no secrets resolved from 1Password (Discord token, Hi-Rez dev ID / auth key)."
  echo "  check with:  ~/Code/selfhosted/scripts/secrets.sh check discord/smitele-bot"; exit 1; }

# The namespace is created once, by hand, and never managed by a chart.
kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1 || {
  echo "namespace ${NAMESPACE} does not exist — create it first:"
  echo "  kubectl create namespace ${NAMESPACE}"; exit 1; }

# The collector needs the SMB share to exist and csi-driver-smb to be installed.
# Catch the missing driver here rather than as a pod stuck in ContainerCreating.
if grep -qE '^\s*enabled:\s*true' <(awk '/^matchData:/,/^[a-z]/' "$LOCAL_VALUES" 2>/dev/null); then
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
  -f "$LOCAL_VALUES" \
  "$@"

if [[ " $* " != *" --dry-run "* ]]; then
  echo "==> Waiting for the bot to come up"
  kubectl -n "${NAMESPACE}" rollout status "deployment/${RELEASE}" --timeout=300s
  echo "==> Logs:  kubectl -n ${NAMESPACE} logs -f deployment/${RELEASE}"
fi
