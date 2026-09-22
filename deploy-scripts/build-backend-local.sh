#!/usr/bin/env bash
# 本地构建 backend 镜像并写入 .env，避免重建容器后 docker cp 热补丁丢失。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

TAG="${WHARTTEST_BACKEND_TAG:-wharttest-backend:local-pre-data-$(date +%Y%m%d)}"
echo "Building backend image: ${TAG}"

docker build \
  -f WHartTest_Django/Dockerfile \
  -t "${TAG}" \
  WHartTest_Django

ENV_FILE="${ROOT}/.env"
touch "${ENV_FILE}"

if grep -q '^DOCKER_BACKEND_IMAGE=' "${ENV_FILE}" 2>/dev/null; then
  if [[ "$(uname -s)" == "Darwin" ]]; then
    sed -i '' "s|^DOCKER_BACKEND_IMAGE=.*|DOCKER_BACKEND_IMAGE=${TAG}|" "${ENV_FILE}"
  else
    sed -i "s|^DOCKER_BACKEND_IMAGE=.*|DOCKER_BACKEND_IMAGE=${TAG}|" "${ENV_FILE}"
  fi
else
  echo "DOCKER_BACKEND_IMAGE=${TAG}" >> "${ENV_FILE}"
fi

echo "Updated .env DOCKER_BACKEND_IMAGE=${TAG}"
echo "Recreate backend (use pre-built image, no rebuild):"
echo "  docker compose -f docker-compose.yml up -d backend --force-recreate --no-build"
