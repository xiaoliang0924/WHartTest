#!/bin/bash

# 确保脚本在任何命令失败时退出
set -e

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
ensure_playwright_skill_browsers() {
  local pkg_path
  pkg_path=$(find /app/data/media/skills -path '*/node_modules/playwright/package.json' 2>/dev/null | head -1 || true)
  if [ -z "$pkg_path" ]; then
    return 0
  fi
  local skill_dir
  skill_dir=$(dirname "$(dirname "$pkg_path")")
  if [ ! -f "${skill_dir}/run.js" ]; then
    return 0
  fi
  echo "Ensuring Playwright browsers for ${skill_dir}..."
  (cd "$skill_dir" && npx playwright install chromium) || echo "Warning: playwright browser install failed, UI automation may fail offline"
}
# 持久化覆盖：init_skills 会用镜像内 bundled 覆盖 media，热修 helpers 放此路径即可在每次启动后生效
PLAYWRIGHT_HELPERS_OVERRIDE="/app/data/media/skill_runtime_overrides/playwright-skill/lib/helpers.js"
PLAYWRIGHT_HELPERS_TARGET="/app/data/media/skills/1/20/lib/helpers.js"
if [ -f "$PLAYWRIGHT_HELPERS_OVERRIDE" ] && [ -f "$PLAYWRIGHT_HELPERS_TARGET" ]; then
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