#!/usr/bin/env bash
set -euo pipefail

readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly VERSION_FILE="$ROOT/VERSION"
readonly DOCKERHUB_NAMESPACE="${DOCKERHUB_NAMESPACE:-aaronrohrbacher}"
readonly IMAGE_NAME="${DOCKERHUB_IMAGE:-tapo-enhance}"
readonly PLATFORM="${TAPO_DOCKER_PLATFORM:-linux/amd64}"

keep_version=false
case "${1:-}" in
  "") ;;
  --keep-version) keep_version=true ;;
  -h|--help)
    printf 'Usage: %s [--keep-version]\n' "${0##*/}"
    printf 'By default, prompts for and records the next release version.\n'
    printf '%s\n' '--keep-version republishes the VERSION currently on disk.'
    exit 0
    ;;
  *)
    printf 'Unknown option: %s\n' "$1" >&2
    printf 'Usage: %s [--keep-version]\n' "${0##*/}" >&2
    exit 2
    ;;
esac

command -v docker >/dev/null || {
  printf 'Docker is required.\n' >&2
  exit 1
}

if ! docker info >/dev/null 2>&1; then
  printf 'The Docker daemon is not available. Start Docker and try again.\n' >&2
  exit 1
fi

if [[ ! -f "$VERSION_FILE" ]]; then
  printf 'Missing version file: %s\n' "$VERSION_FILE" >&2
  exit 1
fi

if [[ "$keep_version" == false ]]; then
  "$ROOT/scripts/deploy.py"
fi

tapo_version="$(tr -d '[:space:]' < "$VERSION_FILE")"
if [[ ! "$tapo_version" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
  printf 'VERSION must use major.minor.patch format, for example 0.1.2; got: %s\n' \
    "$tapo_version" >&2
  exit 1
fi

version_image="${DOCKERHUB_NAMESPACE}/${IMAGE_NAME}:${tapo_version}"
latest_image="${DOCKERHUB_NAMESPACE}/${IMAGE_NAME}:latest"

printf 'Publishing Docker Hub images:\n  %s\n  %s\n' \
  "$version_image" "$latest_image"
docker login

docker buildx build \
  --platform "$PLATFORM" \
  --build-arg "APP_VERSION=$tapo_version" \
  --provenance=false \
  --tag "$version_image" \
  --tag "$latest_image" \
  --push \
  "$ROOT"

printf 'Verifying remote tags...\n'
docker buildx imagetools inspect "$version_image" >/dev/null
docker buildx imagetools inspect "$latest_image" >/dev/null

printf 'Published and verified:\n  %s\n  %s\n' \
  "$version_image" "$latest_image"
