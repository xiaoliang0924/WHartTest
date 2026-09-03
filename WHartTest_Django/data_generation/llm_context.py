"""为 LLM 造数计划生成组装项目上下文。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from api_database_configs.models import ApiDatabaseConfig
from api_environments.models import ApiEnvironment
from api_functions.models import ApiCustomFunction
from api_interfaces.models import ApiInterface

from .templates import BUILTIN_BUSINESS_TEMPLATES, BUILTIN_STEP_TEST_TEMPLATES


def _serialize_interface(iface: ApiInterface) -> Dict[str, Any]:
    data = iface.get_interface_data()
    return {
        'id': iface.id,
        'name': iface.name,
        'method': data.get('method') or '',
        'url': data.get('url') or '',
    }


def build_llm_generation_context(
    project_id: int,
    *,
    default_environment_id: Optional[int] = None,
    suite_gap: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    templates: List[Dict[str, Any]] = []
    for item in BUILTIN_BUSINESS_TEMPLATES + BUILTIN_STEP_TEST_TEMPLATES:
        templates.append({
            'template_key': item.get('template_key'),
            'name': item.get('name'),
            'description': item.get('description'),
            'target_type': item.get('target_type'),
            'params_schema': item.get('params_schema') or {},
        })

    interfaces = [
        _serialize_interface(iface)
        for iface in ApiInterface.objects.filter(project_id=project_id, type=ApiInterface.TYPE_HTTP)
        .order_by('id')[:80]
    ]

    environments = list(
        ApiEnvironment.objects.filter(project_id=project_id)
        .order_by('id')
        .values('id', 'name')
    )

    database_configs = list(
        ApiDatabaseConfig.objects.filter(project_id=project_id, is_active=True)
        .order_by('id')
        .values('id', 'name', 'db_type')
    )

    custom_functions = list(
        ApiCustomFunction.objects.filter(project_id=project_id, is_active=True)
        .order_by('id')
        .values('id', 'name')
    )

    context: Dict[str, Any] = {
        'project_id': project_id,
        'default_environment_id': default_environment_id,
        'templates': templates,
        'interfaces': interfaces,
        'environments': environments,
        'database_configs': database_configs,
        'custom_functions': custom_functions,
        'step_types': [
            'api_call',
            'set_env_var',
            'set_public_data',
            'sql',
            'custom_function',
            'delay',
        ],
        'business_rules': [
            '工单状态流转：仅创建=待分配(pending_assign)；创建+分配=待处理(pending_process)',
            '描述含「待处理」时必须使用 biz_create_and_assign，不能只调用创建接口',
            '描述含「转派」时使用 biz_create_and_transfer；含「领取」用 biz_create_and_claim',
            '工单分配/领取/完成流程：创建时使用 ticketType=TYPE_C（除非用户指定 TYPE_A/B）',
            '分配接口变量使用 assigneeUserId、assigneeName、assigneeDepartment、assigneeRole',
            'api_call 的 interface_id 只能来自 interfaces 列表',
            'set_env_var 的 environment_id 只能来自 environments 列表',
            '前一步 extract 的变量，后续步骤用 {{变量名}} 引用',
        ],
    }
    if suite_gap:
        context['suite_gap_analysis'] = suite_gap
    return context
