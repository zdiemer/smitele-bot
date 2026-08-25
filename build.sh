#!/usr/bin/env bash
# Build the smitele-bot image and push it to the in-cluster registry (infra/registry).
#
# We ship via the in-cluster registry (infra/registry) rather than side-loading into containerd:
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
  # straight to the registry. Auth is forwarded per-session from ~/.docker/config.json.
  [[ -f "${HOME}/.docker/config.json" ]] || {
    echo "missing ~/.docker/config.json — add the registry credential first (selfhosted/infra/registry/README.md)"
    echo "(see selfhosted/infra/registry/README.md)"; exit 1; }

  echo "==> Building + pushing ${IMAGE} (buildctl → ${BUILDKIT_HOST:-unset})"
  buildctl build \
    --frontend dockerfile.v0 \
    --local context="${HERE}" \
    --local dockerfile="${HERE}" \
    --output "type=image,\"name=${IMAGE}\",push=true"
else
  echo "docker or buildctl required"; exit 1
fi

# The trainer is the bot image plus torch. It is built from the tag just
# pushed, so the model is always produced by the same code that scores with it.
# Skipped with SKIP_TRAINER=1, since it is a slow ~800MB layer and most changes
# don't touch training.
if [[ "${SKIP_TRAINER:-0}" != "1" ]]; then
  # Match the `repository:` key, not the image name anywhere in the file, and
  # stop at the first hit. Grepping for the name alone also matched a *comment*
  # mentioning the image, and the two lines were joined into
  # "…trainer\nsmitele-bot-trainer:1.8.25;:1.9.0" — a tag docker rejects, after
  # the bot image had already built and pushed.
  TRAINER_REPO="$(awk '/^[[:space:]]*repository:[[:space:]]*.*smitele-bot-trainer/{print $2; exit}' "${HERE}/values.yaml" | tr -d '"')"
  TRAINER_IMAGE="${TRAINER_REPO:-registry.zachd.duckdns.org/zdiemer/smitele-bot-trainer}:${TAG}"

  if command -v docker >/dev/null; then
    echo "==> Building ${TRAINER_IMAGE} (docker)"
    docker build -f "${HERE}/Dockerfile.train" \
      --build-arg "BASE_IMAGE=${REPO}" --build-arg "BASE_TAG=${TAG}" \
      -t "${TRAINER_IMAGE}" "${HERE}"
    echo "==> Pushing ${TRAINER_IMAGE}"
    docker push "${TRAINER_IMAGE}"
  else
    echo "==> Building + pushing ${TRAINER_IMAGE} (buildctl)"
    buildctl build \
      --frontend dockerfile.v0 \
      --local context="${HERE}" \
      --local dockerfile="${HERE}" \
      --opt filename=Dockerfile.train \
      --opt "build-arg:BASE_IMAGE=${REPO}" \
      --opt "build-arg:BASE_TAG=${TAG}" \
      --output "type=image,\"name=${TRAINER_IMAGE}\",push=true"
  fi
fi

# The Smite 2 crawler is the bot image plus Camoufox — a patched Firefox and
# the X libraries around it, needed only to solve a Cloudflare challenge once
# per cookie. Skipped with SKIP_S2COLLECTOR=1, on the same reasoning as the
# trainer: a slow layer that most changes don't touch.
if [[ "${SKIP_S2COLLECTOR:-0}" != "1" ]]; then
  S2_REPO="$(awk -F'"' '/smitele-bot-s2collector/{print $0}' "${HERE}/values.yaml" | awk '{print $2}' | tr -d '"')"
  S2_IMAGE="${S2_REPO:-registry.zachd.duckdns.org/zdiemer/smitele-bot-s2collector}:${TAG}"

  if command -v docker >/dev/null; then
    echo "==> Building ${S2_IMAGE} (docker)"
    docker build -f "${HERE}/Dockerfile.s2collector" \
      --build-arg "BASE_IMAGE=${REPO}" --build-arg "BASE_TAG=${TAG}" \
      -t "${S2_IMAGE}" "${HERE}"
    echo "==> Pushing ${S2_IMAGE}"
    docker push "${S2_IMAGE}"
  else
    echo "==> Building + pushing ${S2_IMAGE} (buildctl)"
    buildctl build \
      --frontend dockerfile.v0 \
      --local context="${HERE}" \
      --local dockerfile="${HERE}" \
      --opt filename=Dockerfile.s2collector \
      --opt "build-arg:BASE_IMAGE=${REPO}" \
      --opt "build-arg:BASE_TAG=${TAG}" \
      --output "type=image,\"name=${S2_IMAGE}\",push=true"
  fi
fi

# smite.diemer.codes is the bot image plus a built SPA. The node toolchain lives
# in a build stage and contributes no layers, so this is quick — but it is still
# the only image here that needs a network fetch of somebody else's dependency
# tree, hence SKIP_WEB=1 for changes that don't touch the site.
if [[ "${SKIP_WEB:-0}" != "1" ]]; then
  WEB_REPO="$(awk -F'"' '/smitele-bot-web/{print $0}' "${HERE}/values.yaml" | awk '{print $2}' | tr -d '"')"
  WEB_IMAGE="${WEB_REPO:-registry.zachd.duckdns.org/zdiemer/smitele-bot-web}:${TAG}"

  if command -v docker >/dev/null; then
    echo "==> Building ${WEB_IMAGE} (docker)"
    docker build -f "${HERE}/Dockerfile.web" \
      --build-arg "BASE_IMAGE=${REPO}" --build-arg "BASE_TAG=${TAG}" \
      -t "${WEB_IMAGE}" "${HERE}"
    echo "==> Pushing ${WEB_IMAGE}"
    docker push "${WEB_IMAGE}"
  else
    echo "==> Building + pushing ${WEB_IMAGE} (buildctl)"
    buildctl build \
      --frontend dockerfile.v0 \
      --local context="${HERE}" \
      --local dockerfile="${HERE}" \
      --opt filename=Dockerfile.web \
      --opt "build-arg:BASE_IMAGE=${REPO}" \
      --opt "build-arg:BASE_TAG=${TAG}" \
      --output "type=image,\"name=${WEB_IMAGE}\",push=true"
  fi
fi

echo "==> Done. Run upgrade.sh to roll the cluster onto the new image."
