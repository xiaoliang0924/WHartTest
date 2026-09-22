#!/usr/bin/env bash
# Run ON the server (e.g. /home/automation) after `git pull` custom-v2.8 from Gitea.
set -euo pipefail

ROOT="${WHARTTEST_ROOT:-/home/automation}"
cd "$ROOT"

echo "=== backup .env ==="
cp -a .env "/tmp/wharttest.env.bak.$(date +%Y%m%d%H%M%S)"

echo "=== update code ==="
git fetch origin custom-v2.8 2>/dev/null || git fetch baiyun custom-v2.8
git checkout -f custom-v2.8
git pull --ff-only origin custom-v2.8 2>/dev/null || git pull --ff-only baiyun custom-v2.8

git log -1 --oneline
mkdir -p data/playwright-browsers

echo "=== build custom backend (official base + repo Django) ==="
docker build -f Dockerfile.backend-custom -t automation-backend:custom-v2.8 .

echo "=== build frontend ==="
docker build -t automation-frontend:custom-v2.8 -f WHartTest_Vue/Dockerfile WHartTest_Vue

FRONTEND_PORT="${WHARTTEST_FRONTEND_PORT:-10095}"
cat > docker-compose.override.yml <<YAML
services:
  backend:
    image: automation-backend:custom-v2.8
    volumes:
      - ./data/playwright-browsers:/root/.cache/ms-playwright
    environment:
      - DJANGO_CORS_ALLOWED_ORIGINS=http://120.79.129.209:${FRONTEND_PORT},http://localhost:${FRONTEND_PORT},http://localhost:80,http://localhost:3000,http://localhost:5173
      - FRONTEND_BASE_URL=http://120.79.129.209:${FRONTEND_PORT}
  frontend:
    image: automation-frontend:custom-v2.8
    ports:
      - "0.0.0.0:${FRONTEND_PORT}:80"
YAML

echo "=== migrate ==="
docker compose -f docker-compose.yml -f docker-compose.override.yml run --rm backend python manage.py migrate --noinput

echo "=== restart ==="
docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --remove-orphans

sleep 10
docker compose -f docker-compose.yml -f docker-compose.override.yml exec -T backend python manage.py makemigrations --check --dry-run

echo "DEPLOY_OK"
