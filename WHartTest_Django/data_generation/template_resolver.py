"""Resolve logical template refs (interface_ref / environment_ref) per project."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional

from api_database_configs.models import ApiDatabaseConfig
from api_environments.models import ApiEnvironment
from api_functions.models import ApiCustomFunction
from api_interfaces.models import ApiInterface

from .exceptions import DataGenerationError

# Logical ref -> interface name keywords (fallback when bindings missing).
INTERFACE_REF_HINTS: Dict[str, tuple[str, ...]] = {
    'create_ticket': ('创建工单', 'create ticket', '/tickets'),
    'assign_ticket': ('分配工单', 'assign'),
    'transfer_ticket': ('转派工单', 'transfer'),
    'claim_ticket': ('领取工单', 'claim'),
    'resolve_ticket': ('完成工单', 'resolve', 'close'),
    'update_subject': ('修改工单主题', 'update subject', 'subject'),
    'ticket_detail': ('查询工单详情', 'ticket detail', 'ticket detail'),
}

DATABASE_CONFIG_REF_HINTS: Dict[str, tuple[str, ...]] = {
    'default': ('default', '主库', 'ticket'),
}

FUNCTION_REF_HINTS: Dict[str, tuple[str, ...]] = {
    'default': ('default',),
}


def merge_bindings(*sources: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {
        'default_environment_id': None,
        'interfaces': {},
        'database_configs': {},
        'functions': {},
    }
    for source in sources:
        if not isinstance(source, dict):
            continue
        if source.get('default_environment_id'):
            merged['default_environment_id'] = source['default_environment_id']
        for key in ('interfaces', 'database_configs', 'functions'):
            bucket = source.get(key)
            if isinstance(bucket, dict):
                merged[key].update(bucket)
    return merged


def get_project_template_bindings(
    project_id: int,
    *,
    plan_bindings: Optional[Dict[str, Any]] = None,
    default_environment_id: Optional[int] = None,
) -> Dict[str, Any]:
    from .models import DataGenerationPlan

    project_bindings: Dict[str, Any] = {}
    template_plan = (
        DataGenerationPlan.objects.filter(
            project_id=project_id,
            is_template=True,
            is_active=True,
        )
        .exclude(template_bindings={})
        .order_by('-updated_at')
        .first()
    )
    if template_plan and isinstance(template_plan.template_bindings, dict):
        project_bindings = template_plan.template_bindings

    merged = merge_bindings(project_bindings, plan_bindings)
    if default_environment_id and not merged.get('default_environment_id'):
        merged['default_environment_id'] = default_environment_id
    if not merged.get('default_environment_id'):
        env = (
            ApiEnvironment.objects.filter(project_id=project_id, is_active=True)
            .order_by('id')
            .first()
        )
        if env is not None:
            merged['default_environment_id'] = env.id
    return merged


def _find_interface_id(project_id: int, ref: str, bindings: Dict[str, Any]) -> int:
    interfaces = bindings.get('interfaces') if isinstance(bindings.get('interfaces'), dict) else {}
    if ref in interfaces:
        return int(interfaces[ref])

    hints = INTERFACE_REF_HINTS.get(ref, (ref.replace('_', ' '),))
    queryset = ApiInterface.objects.filter(project_id=project_id, type=ApiInterface.TYPE_HTTP)
    for iface in queryset.order_by('id'):
        name = (iface.name or '').lower()
        url = ''
        try:
            url = (iface.get_interface_data().get('url') or '').lower()
        except Exception:
            url = ''
        for hint in hints:
            token = hint.lower()
            if token in name or token in url:
                return iface.id
    raise DataGenerationError(
        f'无法解析接口引用 "{ref}"：请在模板计划的 template_bindings.interfaces 中配置，'
        f'或在步骤中直接使用 interface_id'
    )


def _find_database_config_id(project_id: int, ref: str, bindings: Dict[str, Any]) -> int:
    configs = bindings.get('database_configs') if isinstance(bindings.get('database_configs'), dict) else {}
    if ref in configs:
        return int(configs[ref])

    hints = DATABASE_CONFIG_REF_HINTS.get(ref, (ref,))
    for config in ApiDatabaseConfig.objects.filter(project_id=project_id, is_active=True).order_by('id'):
        name = (config.name or '').lower()
        for hint in hints:
            if hint.lower() in name:
                return config.id
    raise DataGenerationError(f'无法解析 database_config_ref "{ref}"')


def _find_function_id(project_id: int, ref: str, bindings: Dict[str, Any]) -> int:
    functions = bindings.get('functions') if isinstance(bindings.get('functions'), dict) else {}
    if ref in functions:
        return int(functions[ref])

    hints = FUNCTION_REF_HINTS.get(ref, (ref,))
    for func in ApiCustomFunction.objects.filter(project_id=project_id, is_active=True).order_by('id'):
        name = (func.name or '').lower()
        for hint in hints:
            if hint.lower() in name:
                return func.id
    raise DataGenerationError(f'无法解析 function_ref "{ref}"')


def _resolve_environment_id(
    step: Dict[str, Any],
    bindings: Dict[str, Any],
    default_environment_id: Optional[int],
) -> Optional[int]:
    if step.get('environment_id'):
        return int(step['environment_id'])
    env_ref = step.get('environment_ref')
    if not env_ref or env_ref == 'default':
        return bindings.get('default_environment_id') or default_environment_id
    environments = bindings.get('environments') if isinstance(bindings.get('environments'), dict) else {}
    if env_ref in environments:
        return int(environments[env_ref])
    return bindings.get('default_environment_id') or default_environment_id


def resolve_step(
    step: Dict[str, Any],
    *,
    project_id: int,
    bindings: Dict[str, Any],
    default_environment_id: Optional[int] = None,
) -> Dict[str, Any]:
    if not isinstance(step, dict):
        return step

    resolved = deepcopy(step)
    step_type = (resolved.get('type') or '').strip()

    env_id = _resolve_environment_id(resolved, bindings, default_environment_id)
    if env_id and step_type in {'api_call', 'set_env_var'}:
        resolved['environment_id'] = env_id
    resolved.pop('environment_ref', None)

    if step_type == 'api_call':
        if not resolved.get('interface_id') and resolved.get('interface_ref'):
            resolved['interface_id'] = _find_interface_id(
                project_id,
                str(resolved['interface_ref']),
                bindings,
            )
        resolved.pop('interface_ref', None)
    elif step_type == 'sql':
        if not resolved.get('database_config_id') and resolved.get('database_config_ref'):
            resolved['database_config_id'] = _find_database_config_id(
                project_id,
                str(resolved['database_config_ref']),
                bindings,
            )
        resolved.pop('database_config_ref', None)
    elif step_type == 'custom_function':
        if not resolved.get('function_id') and resolved.get('function_ref'):
            resolved['function_id'] = _find_function_id(
                project_id,
                str(resolved['function_ref']),
                bindings,
            )
        resolved.pop('function_ref', None)

    return resolved


def resolve_template_steps(
    steps: Optional[List[Any]],
    *,
    project_id: int,
    bindings: Optional[Dict[str, Any]] = None,
    plan_bindings: Optional[Dict[str, Any]] = None,
    default_environment_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    if not isinstance(steps, list):
        return []

    merged_bindings = get_project_template_bindings(
        project_id,
        plan_bindings=plan_bindings or bindings,
        default_environment_id=default_environment_id,
    )
    resolved_steps: List[Dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        resolved_steps.append(
            resolve_step(
                step,
                project_id=project_id,
                bindings=merged_bindings,
                default_environment_id=default_environment_id,
            )
        )
    return resolved_steps


def resolve_template_definition(
    template: Dict[str, Any],
    *,
    project_id: int,
    plan_bindings: Optional[Dict[str, Any]] = None,
    default_environment_id: Optional[int] = None,
) -> Dict[str, Any]:
    result = deepcopy(template)
    bindings = merge_bindings(
        template.get('template_bindings'),
        plan_bindings,
    )
    result['steps'] = resolve_template_steps(
        result.get('steps'),
        project_id=project_id,
        plan_bindings=bindings,
        default_environment_id=default_environment_id,
    )
    result['cleanup_steps'] = resolve_template_steps(
        result.get('cleanup_steps'),
        project_id=project_id,
        plan_bindings=bindings,
        default_environment_id=default_environment_id,
    )
    return result
