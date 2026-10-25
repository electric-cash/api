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

# Docker records repository digests after push; ignore other registry aliases.
references=$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$image:$IMAGE_TAG")
digest=''
while IFS= read -r reference; do
  if [[ "$reference" == "$image@sha256:"* ]]; then
    digest=${reference#"$image@"}
  fi
done <<< "$references"
if [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "Push completed but no matching repository digest was recorded" >&2
  exit 1
fi
printf -- '- Published `%s:%s` as `%s@%s`\n' "$image" "$IMAGE_TAG" "$image" "$digest" >> "$GITHUB_STEP_SUMMARY"

docker tag "$BUILD_NAME" "$image:latest"
docker push "$image:latest"
printf -- '- Updated `%s:latest` from source `%s`\n' "$image" "$IMAGE_TAG" >> "$GITHUB_STEP_SUMMARY"
