"""SQL 步骤执行工具，复用 ApiDatabaseConfig 与 DBEngine。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from api_database_configs.models import ApiDatabaseConfig

from .exceptions import DataGenerationError


def execute_sql_step(
    *,
    database_config_id: int,
    project_id: int,
    sql: str,
    method: str = 'fetchall',
) -> Any:
    if not sql or not str(sql).strip():
        raise DataGenerationError('sql 步骤缺少 sql 语句')

    try:
        db_config = ApiDatabaseConfig.objects.get(
            id=database_config_id,
            project_id=project_id,
            is_active=True,
        )
    except ApiDatabaseConfig.DoesNotExist as exc:
        raise DataGenerationError(f'数据库配置不存在: {database_config_id}') from exc

    try:
        from httprunner.database.engine import DBEngine
    except ImportError as exc:
        raise DataGenerationError('SQL 依赖未安装，请安装 sqlalchemy 与 pymysql') from exc

    db_uri = db_config.connection_string
    engine = DBEngine(db_uri)
    normalized = (method or 'fetchall').strip().lower()
    query = str(sql).strip()

    try:
        if normalized == 'fetchone':
            return engine.fetchone(query)
        if normalized in ('insert', 'update', 'delete'):
            return engine.fetchall(query)
        return engine.fetchall(query)
    except TypeError:
        return None
    except Exception as exc:
        raise DataGenerationError(f'SQL 执行失败: {exc}') from exc


def extract_sql_result(source: Any, mapping: Dict[str, str]) -> Dict[str, Any]:
    extracted: Dict[str, Any] = {}
    for var_name, expr in mapping.items():
        if not expr:
            continue
        key = expr.strip()
        if key == 'rowcount' and isinstance(source, dict) and 'rowcount' in source:
            extracted[var_name] = source['rowcount']
        elif isinstance(source, dict) and key in source:
            extracted[var_name] = source[key]
        elif isinstance(source, list) and source and isinstance(source[0], dict) and key in source[0]:
            extracted[var_name] = [row[key] for row in source]
        elif len(mapping) == 1 and var_name == key:
            extracted[var_name] = source
        else:
            raise DataGenerationError(f'SQL 结果无法提取变量: {var_name}')
    return extracted
