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

# 3. 启动 supervisord 来管理所有服务
echo "Starting supervisord..."
exec supervisord -c /app/supervisord.conf