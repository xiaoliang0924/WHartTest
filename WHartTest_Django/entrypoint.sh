#!/bin/bash

# 确保脚本在任何命令失败时退出
set -e

# docker compose run backend <command> must be a one-shot process. Otherwise
# tests/migrations silently start another production worker on the same queue.
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

# 1. 数据库迁移
echo "Applying database migrations..."
python manage.py migrate --noinput

# 2. 创建默认管理员用户
echo "Creating default admin user if it does not exist..."
python manage.py init_admin

# 2.5 同步 bundled_skills -> media/skills（Playwright 实际从 media 目录加载）
if [ -d /app/bundled_skills ]; then
  echo "Syncing bundled skills..."
  python manage.py init_skills || echo "Warning: init_skills failed, continuing startup"
fi

# media 内 playwright-skill 版本（如 1.57 / build 1200）与镜像内旧版 Chromium 1223 不一致；
# 必须在 skill 目录执行 install，不能只看 cache 里是否存在任意 headless_shell。
seed_playwright_browser_cache_if_empty() {
  local cache_dir="/root/.cache/ms-playwright"
  local seed_dir="/opt/playwright-browser-seed"
  if [ ! -d "$seed_dir" ]; then
    return 0
  fi
  if [ -n "$(ls -A "$cache_dir" 2>/dev/null)" ]; then
    return 0
  fi
  echo "Seeding Playwright browsers from image into ${cache_dir}..."
  mkdir -p "$cache_dir"
  cp -a "${seed_dir}/." "$cache_dir/" || true
}

ensure_playwright_skill_browsers() {
  local pkg_path
  local download_host
  # 国内默认走 npmmirror CDN；勿用官方 cdn.playwright.dev（海外很慢）
  download_host="${PLAYWRIGHT_DOWNLOAD_HOST:-${DOCKER_PLAYWRIGHT_DOWNLOAD_HOST:-https://cdn.npmmirror.com/binaries/playwright}}"
  export PLAYWRIGHT_DOWNLOAD_HOST="$download_host"

  seed_playwright_browser_cache_if_empty

  pkg_path=$(find /app/data/media/skills -path '*/node_modules/playwright/package.json' 2>/dev/null | head -1 || true)
  if [ -z "$pkg_path" ]; then
    return 0
  fi
  local skill_dir
  skill_dir=$(dirname "$(dirname "$pkg_path")")
  if [ ! -f "${skill_dir}/run.js" ]; then
    return 0
  fi
  # 必须用 skill 目录内 Node Playwright，避免 PATH 落到 Python 版（版本/build 号不一致）
  if [ ! -f "${skill_dir}/node_modules/playwright/cli.js" ]; then
    echo "Warning: ${skill_dir}/node_modules/playwright/cli.js missing, skip browser install"
    return 0
  fi
  if [ -d /root/.cache/ms-playwright/chromium-1200 ] && [ -d /root/.cache/ms-playwright/chromium_headless_shell-1200 ]; then
    echo "Playwright browsers already present (chromium-1200), skip install"
    return 0
  fi
  echo "Ensuring Playwright browsers for ${skill_dir} (PLAYWRIGHT_DOWNLOAD_HOST=${PLAYWRIGHT_DOWNLOAD_HOST})..."
  (cd "$skill_dir" && node ./node_modules/playwright/cli.js install chromium) || echo "Warning: playwright browser install failed, UI automation may fail offline"
}
# 持久化覆盖：init_skills 会用镜像内 bundled 覆盖 media，热修 helpers 放此路径即可在每次启动后生效
PLAYWRIGHT_HELPERS_OVERRIDE="/app/data/media/skill_runtime_overrides/playwright-skill/lib/helpers.js"
PLAYWRIGHT_HELPERS_TARGET="/app/data/media/skills/1/20/lib/helpers.js"
PLAYWRIGHT_HELPERS_BUNDLED="/app/bundled_skills/playwright-skill/lib/helpers.js"
if [ -f "$PLAYWRIGHT_HELPERS_OVERRIDE" ] && [ -f "$PLAYWRIGHT_HELPERS_TARGET" ]; then
  # 运行时覆盖文件可能保留其他热修，但不能再把旧版工单完成流程盖回去。
  # 每次启动只从 bundled 合并这一段公共分发逻辑，其余覆盖内容保持原样。
  if [ -f "$PLAYWRIGHT_HELPERS_BUNDLED" ] && grep -q 'function completeTicketActionForStep(' "$PLAYWRIGHT_HELPERS_BUNDLED"; then
    python /app/scripts/merge_complete_runtime_helper.py "$PLAYWRIGHT_HELPERS_BUNDLED" "$PLAYWRIGHT_HELPERS_OVERRIDE"
  fi
  if [ -f "$PLAYWRIGHT_HELPERS_BUNDLED" ] && grep -q 'async function assertTableColumnEquals(' "$PLAYWRIGHT_HELPERS_BUNDLED"; then
    python /app/scripts/merge_table_runtime_helper.py "$PLAYWRIGHT_HELPERS_BUNDLED" "$PLAYWRIGHT_HELPERS_OVERRIDE"
  fi
  echo "Applying playwright helpers.js runtime override..."
  cp -a "$PLAYWRIGHT_HELPERS_OVERRIDE" "$PLAYWRIGHT_HELPERS_TARGET"
fi

# 2.6 工单造数模板与 API 环境凭据（用例执行前置数据依赖）
if [ "${WHARTTEST_SYNC_BUSINESS_TEMPLATES:-1}" = "1" ]; then
  echo "Syncing business data-generation templates..."
  python manage.py sync_business_templates || echo "Warning: sync_business_templates failed, continuing startup"
fi

ensure_playwright_skill_browsers

# 3. 启动 supervisord 来管理所有服务
echo "Starting supervisord..."
exec supervisord -c /app/supervisord.conf
