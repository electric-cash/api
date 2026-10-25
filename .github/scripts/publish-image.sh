#!/usr/bin/env bash
set -euo pipefail

: "${ECR_REGISTRY:?Missing registry}"
: "${ECR_REPOSITORY:?Missing repository}"
: "${IMAGE_TAG:?Missing source SHA}"
: "${BUILD_NAME:?Missing local image}"
: "${GITHUB_STEP_SUMMARY:?Missing publication summary}"

if [[ ! "$IMAGE_TAG" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Image tag must be the full source commit SHA" >&2
  exit 1
fi
revision=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$BUILD_NAME")
if [[ "$revision" != "$IMAGE_TAG" ]]; then
  echo "Image revision does not match source SHA" >&2
  exit 1
fi

image="$ECR_REGISTRY/$ECR_REPOSITORY"
docker tag "$BUILD_NAME" "$image:$IMAGE_TAG"
docker push "$image:$IMAGE_TAG"

# Read the registry manifest rather than relying on local Docker metadata.
manifest=$(docker buildx imagetools inspect --format '{{json .Manifest}}' "$image:$IMAGE_TAG")
digest=$(python3 -c 'import json, sys; print(json.load(sys.stdin)["digest"])' <<< "$manifest")
if [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "Push completed but the registry digest could not be verified" >&2
  exit 1
fi
printf -- '- Published `%s:%s` as `%s@%s`\n' "$image" "$IMAGE_TAG" "$image" "$digest" >> "$GITHUB_STEP_SUMMARY"

docker tag "$BUILD_NAME" "$image:latest"
docker push "$image:latest"
manifest=$(docker buildx imagetools inspect --format '{{json .Manifest}}' "$image:latest")
latest_digest=$(python3 -c 'import json, sys; print(json.load(sys.stdin)["digest"])' <<< "$manifest")
if [[ "$latest_digest" != "$digest" ]]; then
  echo "Registry latest digest does not match the published source SHA digest" >&2
  exit 1
fi
printf -- '- Verified `%s:latest` as `%s@%s` from source `%s`\n' "$image" "$image" "$digest" "$IMAGE_TAG" >> "$GITHUB_STEP_SUMMARY"
