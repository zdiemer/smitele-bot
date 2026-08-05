#!/usr/bin/env bash
# Build the smitele-bot image and push it to GHCR.
#
# We ship via ghcr.io (public package) rather than side-loading into containerd:
# the cluster is multi-node, so a side-loaded image only exists on one node and
# every other node ImagePullBackOffs.
#
# Re-run after editing the Dockerfile, the Pipfile, or anything under src/, then
# run upgrade.sh.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(awk '/^  repository:/{print $2; exit}' "${HERE}/values.yaml" | tr -d '"')"
TAG="$(awk -F'"' '/^  tag:/{print $2; exit}' "${HERE}/values.yaml")"
IMAGE="${REPO}:${TAG}"

[[ -n "$REPO" && -n "$TAG" ]] || { echo "could not read image repository/tag from values.yaml"; exit 1; }

# The chart's appVersion is supposed to track image.tag; catch the drift here
# rather than wondering later which commit is actually running.
CHART_APPVERSION="$(awk -F'"' '/^appVersion:/{print $2; exit}' "${HERE}/Chart.yaml")"
if [[ "$CHART_APPVERSION" != "$TAG" ]]; then
  echo "WARN: Chart.yaml appVersion (${CHART_APPVERSION}) != values.yaml image.tag (${TAG})" >&2
fi

if command -v docker >/dev/null; then
  echo "==> Building ${IMAGE} (docker)"
  docker build -t "${IMAGE}" "${HERE}"
  echo "==> Pushing ${IMAGE}"
  docker push "${IMAGE}"
elif command -v buildctl >/dev/null; then
  # Workspace-pod path: remote build on the in-cluster buildkitd, which pushes
  # straight to GHCR. Auth is forwarded per-session from ~/.docker/config.json.
  [[ -f "${HOME}/.docker/config.json" ]] || {
    echo "missing ~/.docker/config.json — create the GHCR PAT file first"
    echo "(see dev/claude-workspace/README.md, Cluster powers)"; exit 1; }

  echo "==> Building + pushing ${IMAGE} (buildctl → ${BUILDKIT_HOST:-unset})"
  buildctl build \
    --frontend dockerfile.v0 \
    --local context="${HERE}" \
    --local dockerfile="${HERE}" \
    --output "type=image,\"name=${IMAGE}\",push=true"
else
  echo "docker or buildctl required"; exit 1
fi

echo "==> Done. Run upgrade.sh to roll the cluster onto the new image."
echo "    (First push only: set the ghcr.io/zdiemer/smitele-bot package"
echo "     visibility to Public so every node can pull it anonymously.)"
