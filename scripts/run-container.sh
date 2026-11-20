#!/usr/bin/env bash
set -euo pipefail

readonly ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mode="${1:-published}"

case "$mode" in
  published)
    export TAPO_ENHANCE_IMAGE="${TAPO_ENHANCE_IMAGE:-aaronrohrbacher/tapo-enhance}"
    export TAPO_ENHANCE_VERSION="${TAPO_ENHANCE_VERSION:-latest}"
    export TAPO_PULL_POLICY="${TAPO_PULL_POLICY:-always}"
    ;;
  local)
    ;;
  *)
    printf 'Usage: %s [published|local] [compose arguments...]\n' "${0##*/}" >&2
    exit 2
    ;;
esac
shift || true
compose_files=(-f "$ROOT_DIR/compose.yaml")

mkdir -p "$ROOT_DIR/custom-themes"

if [[ "$#" -gt 0 ]]; then
  docker compose "${compose_files[@]}" "$@"
elif [[ "$mode" == "local" ]]; then
  docker compose "${compose_files[@]}" up -d --build
else
  docker compose "${compose_files[@]}" up -d
fi
