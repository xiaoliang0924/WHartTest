"""内置造数模板（快速造数）。"""

from __future__ import annotations

from typing import Any, Dict, List

# 逻辑引用名（interface_ref / environment_ref），运行时由 template_resolver 按项目解析为实际 ID。
# 项目 1 工单系统的默认绑定见 scripts/setup_business_templates.py 中的 TEMPLATE_BINDINGS。
ENV_REF_DEFAULT = 'default'
REF_CREATE_TICKET = 'create_ticket'
REF_ASSIGN_TICKET = 'assign_ticket'
REF_TRANSFER_TICKET = 'transfer_ticket'
REF_CLAIM_TICKET = 'claim_ticket'
REF_RESOLVE_TICKET = 'resolve_ticket'
REF_UPDATE_SUBJECT = 'update_subject'
REF_TICKET_DETAIL = 'ticket_detail'


def _create_ticket_step() -> Dict[str, Any]:
    return {
        'type': 'api_call',
        'name': '创建工单',
        'interface_ref': REF_CREATE_TICKET,
        'environment_ref': ENV_REF_DEFAULT,
        'variables': {
            'summary': '{{summary}}',
            'ticketType': '{{ticketType}}',
        },
        'extract': {'ticketId': 'ticketId', 'ticketNo': 'ticketNo'},
    }


def _write_env_vars_step(**extra: str) -> Dict[str, Any]:
    variables = {
        'ticketId': '{{ticketId}}',
        'ticketNo': '{{ticketNo}}',
        'processingTicketId': '{{ticketId}}',
        'work_order_id': '{{ticketId}}',
    }
    variables.update(extra)
    return {
        'type': 'set_env_var',
        'name': '写入环境变量',
        'environment_ref': ENV_REF_DEFAULT,
        'variables': variables,
    }


def _write_public_data_step(**extra: str) -> Dict[str, Any]:
    items = [
        {'key': 'ticketId', 'value': '{{ticketId}}', 'type': 0},
        {'key': 'ticketNo', 'value': '{{ticketNo}}', 'type': 0},
        {'key': 'work_order_id', 'value': '{{ticketId}}', 'type': 0},
        {'key': 'processingTicketId', 'value': '{{ticketId}}', 'type': 0},
    ]
    for key, value in extra.items():
        items.append({'key': key, 'value': value, 'type': 0})
    return {
        'type': 'set_public_data',
        'name': '写入 UI 公共数据',
        'items': items,
    }


BUILTIN_BUSINESS_TEMPLATES: List[Dict[str, Any]] = [
    {
        'template_key': 'biz_create_type_a',
        'name': '创建待分配工单 TYPE_A',
        'description': 'POST /api/tickets 创建 TYPE_A 工单（状态 pending_process），写入 ticketId/ticketNo。',
        'target_type': 'both',
        'icon': 'file',
        'params_schema': {
            'summary': {'type': 'string', 'label': '工单摘要', 'default': '待分配测试工单'},
            'ticketType': {'type': 'string', 'label': '工单类型', 'default': 'TYPE_A'},
        },
        'steps': [
            _create_ticket_step(),
            _write_env_vars_step(),
            _write_public_data_step(),
        ],
        'cleanup_steps': [],
    },
    {
        'template_key': 'biz_create_type_b',
        'name': '创建 TYPE_B 工单',
        'description': '创建 TYPE_B 类型工单，适用于非待分配类工单场景测试。',
        'target_type': 'both',
        'icon': 'file',
        'params_schema': {
            'summary': {'type': 'string', 'label': '工单摘要', 'default': 'TYPE_B测试工单'},
            'ticketType': {'type': 'string', 'label': '工单类型', 'default': 'TYPE_B'},
        },
        'steps': [
            _create_ticket_step(),
            _write_env_vars_step(),
            _write_public_data_step(),
        ],
        'cleanup_steps': [],
    },
    {
        'template_key': 'biz_create_and_assign',
        'name': '创建并分配工单',
        'description': '创建 TYPE_A 工单后调用「分配工单」接口，写入 processingTicketId 供处理中场景使用。',
        'target_type': 'both',
        'icon': 'user-add',
        'params_schema': {
            'summary': {'type': 'string', 'label': '工单摘要', 'default': '分配测试工单'},
            'ticketType': {'type': 'string', 'label': '工单类型', 'default': 'TYPE_C'},
            'assigneeUserId': {'type': 'number', 'label': '分配用户 ID', 'default': 46},
            'assigneeName': {'type': 'string', 'label': '分配用户名', 'default': '李亮'},
            'assigneeDepartment': {'type': 'string', 'label': '分配部门', 'default': '客服部'},
            'assigneeRole': {'type': 'string', 'label': '分配角色', 'default': 'customer_service'},
        },
        'steps': [
            _create_ticket_step(),
            {
                'type': 'api_call',
                'name': '分配工单',
                'interface_ref': REF_ASSIGN_TICKET,
                'environment_ref': ENV_REF_DEFAULT,
                'variables': {
                    'assigneeUserId': '{{assigneeUserId}}',
                    'assigneeName': '{{assigneeName}}',
                    'assigneeDepartment': '{{assigneeDepartment}}',
                    'assigneeRole': '{{assigneeRole}}',
                },
            },
            _write_env_vars_step(ticketStatus='assigned'),
            _write_public_data_step(ticketStatus='assigned'),
        ],
        'cleanup_steps': [],
    },
    {
        'template_key': 'biz_create_and_transfer',
        'name': '创建并转派工单',
        'description': '创建 TYPE_C 工单，分配并领取后调用「转派工单」接口，将工单转派给指定处理人。',
        'target_type': 'both',
        'icon': 'swap',
        'params_schema': {
            'summary': {'type': 'string', 'label': '工单摘要', 'default': '转派测试工单'},
            'ticketType': {'type': 'string', 'label': '工单类型', 'default': 'TYPE_C'},
            'assigneeUserId': {'type': 'number', 'label': '目标用户 ID', 'default': 46},
            'assigneeName': {'type': 'string', 'label': '目标用户名', 'default': '李亮'},
            'assigneeDepartment': {'type': 'string', 'label': '目标部门', 'default': '客服部'},
            'assigneeRole': {'type': 'string', 'label': '目标角色', 'default': 'customer_service'},
            'transferReason': {'type': 'string', 'label': '转派原因', 'default': '自动化测试转派'},
            'sourceAssigneeUserId': {'type': 'number', 'label': '当前处理人 ID', 'default': 39},
            'sourceAssigneeName': {'type': 'string', 'label': '当前处理人', 'default': '李清云'},
            'sourceAssigneeDepartment': {
                'type': 'string',
                'label': '当前处理人部门',
                'default': 'AI与数字化中心/数据质量与测试组',
            },
            'sourceAssigneeRole': {
                'type': 'string',
                'label': '当前处理人角色',
                'default': 'customer_service',
            },
        },
        'steps': [
            _create_ticket_step(),
            {
                'type': 'api_call',
                'name': '分配给当前处理人',
                'interface_ref': REF_ASSIGN_TICKET,
                'environment_ref': ENV_REF_DEFAULT,
                'variables': {
                    'assigneeUserId': '{{sourceAssigneeUserId}}',
                    'assigneeName': '{{sourceAssigneeName}}',
                    'assigneeDepartment': '{{sourceAssigneeDepartment}}',
                    'assigneeRole': '{{sourceAssigneeRole}}',
                },
            },
            {
                'type': 'api_call',
                'name': '当前处理人领取工单',
                'interface_ref': REF_CLAIM_TICKET,
                'environment_ref': ENV_REF_DEFAULT,
                'variables': {},
            },
            {
                'type': 'api_call',
                'name': '转派工单',
                'interface_ref': REF_TRANSFER_TICKET,
                'environment_ref': ENV_REF_DEFAULT,
                'variables': {
                    'targetUserId': '{{assigneeUserId}}',
                    'targetRole': '{{assigneeRole}}',
                    'reason': '{{transferReason}}',
                },
            },
            _write_env_vars_step(ticketStatus='assigned'),
            _write_public_data_step(ticketStatus='assigned'),
        ],
        'cleanup_steps': [],
    },
    {
        'template_key': 'biz_create_and_claim',
        'name': '创建并领取工单',
        'description': '创建 TYPE_C 工单 → 分配给处理人 → 领取，适用于「我的工单」列表测试（需环境变量 assigneeToken）。',
        'target_type': 'both',
        'icon': 'user',
        'params_schema': {
            'summary': {'type': 'string', 'label': '工单摘要', 'default': '领取测试工单'},
            'ticketType': {'type': 'string', 'label': '工单类型', 'default': 'TYPE_C'},
            'assigneeUserId': {'type': 'number', 'label': '分配用户 ID', 'default': 46},
            'assigneeName': {'type': 'string', 'label': '分配用户名', 'default': '李亮'},
            'assigneeDepartment': {'type': 'string', 'label': '分配部门', 'default': '客服部'},
            'assigneeRole': {'type': 'string', 'label': '分配角色', 'default': 'customer_service'},
        },
        'steps': [
            _create_ticket_step(),
            {
                'type': 'api_call',
                'name': '分配工单',
                'interface_ref': REF_ASSIGN_TICKET,
                'environment_ref': ENV_REF_DEFAULT,
                'variables': {
                    'assigneeUserId': '{{assigneeUserId}}',
                    'assigneeName': '{{assigneeName}}',
                    'assigneeDepartment': '{{assigneeDepartment}}',
                    'assigneeRole': '{{assigneeRole}}',
                },
            },
            {
                'type': 'api_call',
                'name': '领取工单',
                'interface_ref': REF_CLAIM_TICKET,
                'environment_ref': ENV_REF_DEFAULT,
                'variables': {},
            },
            _write_env_vars_step(ticketStatus='claimed'),
            _write_public_data_step(ticketStatus='claimed'),
        ],
        'cleanup_steps': [],
    },
    {
        'template_key': 'biz_create_assign_resolve',
        'name': '创建→分配→领取→完成工单',
        'description': '完整闭环：TYPE_C 创建 → 分配 → 领取(assigneeToken) → 完成，适用于已完成状态回归。',
        'target_type': 'both',
        'icon': 'check-circle',
        'params_schema': {
            'summary': {'type': 'string', 'label': '工单摘要', 'default': '完成闭环测试工单'},
            'ticketType': {'type': 'string', 'label': '工单类型', 'default': 'TYPE_C'},
            'assigneeUserId': {'type': 'number', 'label': '分配用户 ID', 'default': 46},
            'assigneeName': {'type': 'string', 'label': '分配用户名', 'default': '李亮'},
            'assigneeDepartment': {'type': 'string', 'label': '分配部门', 'default': '客服部'},
            'assigneeRole': {'type': 'string', 'label': '分配角色', 'default': 'customer_service'},
        },
        'steps': [
            _create_ticket_step(),
            {
                'type': 'api_call',
                'name': '分配工单',
                'interface_ref': REF_ASSIGN_TICKET,
                'environment_ref': ENV_REF_DEFAULT,
                'variables': {
                    'assigneeUserId': '{{assigneeUserId}}',
                    'assigneeName': '{{assigneeName}}',
                    'assigneeDepartment': '{{assigneeDepartment}}',
                    'assigneeRole': '{{assigneeRole}}',
                },
            },
            {
                'type': 'api_call',
                'name': '领取工单',
                'interface_ref': REF_CLAIM_TICKET,
                'environment_ref': ENV_REF_DEFAULT,
                'variables': {},
            },
            {
                'type': 'api_call',
                'name': '完成工单',
                'interface_ref': REF_RESOLVE_TICKET,
                'environment_ref': ENV_REF_DEFAULT,
                'variables': {},
            },
            _write_env_vars_step(ticketStatus='resolved'),
            _write_public_data_step(ticketStatus='resolved'),
        ],
        'cleanup_steps': [],
    },
    {
        'template_key': 'biz_create_update_subject',
        'name': '创建并修改工单主题',
        'description': '创建工单后 PATCH 修改主题，适用于工单详情/编辑类 UI 测试。',
        'target_type': 'both',
        'icon': 'edit',
        'params_schema': {
            'summary': {'type': 'string', 'label': '工单摘要', 'default': '改主题测试工单'},
            'ticketType': {'type': 'string', 'label': '工单类型', 'default': 'TYPE_A'},
            'subject': {'type': 'string', 'label': '新主题', 'default': '造数更新的工单主题'},
        },
        'steps': [
            _create_ticket_step(),
            {
                'type': 'api_call',
                'name': '修改工单主题',
                'interface_ref': REF_UPDATE_SUBJECT,
                'environment_ref': ENV_REF_DEFAULT,
                'variables': {'subject': '{{subject}}'},
            },
            _write_env_vars_step(updatedSubject='{{subject}}'),
            _write_public_data_step(updatedSubject='{{subject}}'),
        ],
        'cleanup_steps': [],
    },
    {
        'template_key': 'biz_create_with_delay',
        'name': '创建工单并等待同步',
        'description': '创建工单后等待列表刷新，适用于下游列表延迟场景。',
        'target_type': 'both',
        'icon': 'clock-circle',
        'params_schema': {
            'summary': {'type': 'string', 'label': '工单摘要', 'default': '延迟同步测试工单'},
            'ticketType': {'type': 'string', 'label': '工单类型', 'default': 'TYPE_A'},
            'delay_seconds': {'type': 'number', 'label': '等待秒数', 'default': 2},
        },
        'steps': [
            _create_ticket_step(),
            {
                'type': 'delay',
                'name': '等待列表同步',
                'seconds': '{{delay_seconds}}',
            },
            _write_env_vars_step(),
            _write_public_data_step(),
        ],
        'cleanup_steps': [],
    },
    {
        'template_key': 'biz_query_ticket_detail',
        'name': '创建并查询工单详情',
        'description': '创建工单后 GET 详情验证状态，extract ticketStatus 到变量。',
        'target_type': 'both',
        'icon': 'search',
        'params_schema': {
            'summary': {'type': 'string', 'label': '工单摘要', 'default': '详情查询测试工单'},
            'ticketType': {'type': 'string', 'label': '工单类型', 'default': 'TYPE_A'},
        },
        'steps': [
            _create_ticket_step(),
            {
                'type': 'api_call',
                'name': '查询工单详情',
                'interface_ref': REF_TICKET_DETAIL,
                'environment_ref': ENV_REF_DEFAULT,
                'variables': {},
                'extract': {'ticketStatus': 'status'},
            },
            _write_env_vars_step(),
            _write_public_data_step(ticketStatus='{{ticketStatus}}'),
        ],
        'cleanup_steps': [],
    },
]

BUILTIN_STEP_TEST_TEMPLATES: List[Dict[str, Any]] = [
    {
        'template_key': 'test_step_api_call',
        'name': '【步骤测试】API 调用',
        'description': '仅测试 api_call：调用「创建工单」接口并 extract ticketId/ticketNo。',
        'target_type': 'api',
        'icon': 'thunderbolt',
        'params_schema': {
            'summary': {'type': 'string', 'label': '工单摘要', 'default': 'API步骤测试'},
            'ticketType': {'type': 'string', 'label': '工单类型', 'default': 'TYPE_A'},
        },
        'steps': [_create_ticket_step()],
        'cleanup_steps': [],
    },
    {
        'template_key': 'test_step_set_env_var',
        'name': '【步骤测试】写入环境变量',
        'description': '仅测试 set_env_var：向测试环境写入 dg_test_env_var / dg_test_uuid。',
        'target_type': 'api',
        'icon': 'storage',
        'params_schema': {
            'marker': {'type': 'string', 'label': '测试标记', 'default': 'env_var_测试'},
        },
        'steps': [
            {
                'type': 'set_env_var',
                'name': '写入测试环境变量',
                'environment_ref': ENV_REF_DEFAULT,
                'variables': {
                    'dg_test_env_var': '{{marker}}',
                    'dg_test_uuid': '{{uuid}}',
                    'dg_test_timestamp': '{{timestamp}}',
                },
            },
        ],
        'cleanup_steps': [],
    },
    {
        'template_key': 'test_step_set_public_data',
        'name': '【步骤测试】写入 UI 公共数据',
        'description': '仅测试 set_public_data：写入 dg_test_public / dg_test_uuid 到 UI 公共数据。',
        'target_type': 'ui',
        'icon': 'desktop',
        'params_schema': {
            'marker': {'type': 'string', 'label': '测试标记', 'default': 'public_data_测试'},
        },
        'steps': [
            {
                'type': 'set_public_data',
                'name': '写入 UI 公共数据',
                'items': [
                    {'key': 'dg_test_public', 'value': '{{marker}}', 'type': 0},
                    {'key': 'dg_test_uuid', 'value': '{{uuid}}', 'type': 0},
                ],
            },
        ],
        'cleanup_steps': [],
    },
    {
        'template_key': 'test_step_sql',
        'name': '【步骤测试】SQL 执行',
        'description': '仅测试 sql：对 PostgreSQL 执行 SELECT 1，验证数据库连通与 extract。',
        'target_type': 'api',
        'icon': 'code',
        'params_schema': {},
        'steps': [
            {
                'type': 'sql',
                'name': 'SQL 连通性探测',
                'database_config_ref': 'default',
                'sql': 'SELECT 1 AS dg_ok',
                'method': 'fetchone',
                'extract': {'dg_sql_ok': 'dg_ok'},
                'output_var': 'dg_sql_result',
            },
        ],
        'cleanup_steps': [],
    },
    {
        'template_key': 'test_step_custom_function',
        'name': '【步骤测试】自定义函数',
        'description': '仅测试 custom_function：调用 dg_echo_test 并输出 dg_func_result。',
        'target_type': 'api',
        'icon': 'code-square',
        'params_schema': {
            'message': {'type': 'string', 'label': '传入消息', 'default': 'custom_function_测试'},
        },
        'steps': [
            {
                'type': 'custom_function',
                'name': '执行 echo 测试函数',
                'function_ref': 'default',
                'args': {'message': '{{message}}'},
                'output_var': 'dg_func_result',
            },
        ],
        'cleanup_steps': [],
    },
    {
        'template_key': 'test_step_delay',
        'name': '【步骤测试】等待',
        'description': '仅测试 delay：等待指定秒数后继续（可在执行记录看耗时）。',
        'target_type': 'api',
        'icon': 'clock-circle',
        'params_schema': {
            'delay_seconds': {'type': 'number', 'label': '等待秒数', 'default': 2},
        },
        'steps': [
            {
                'type': 'delay',
                'name': '等待',
                'seconds': '{{delay_seconds}}',
            },
        ],
        'cleanup_steps': [],
    },
]

BUILTIN_CLEANUP_TEMPLATES: List[Dict[str, Any]] = [
    {
        'template_key': 'cleanup_ticket_by_sql',
        'name': 'SQL 清理测试工单',
        'description': '通过 SQL 删除指定工单（清理步骤模板，不在快速造数展示）。',
        'target_type': 'api',
        'icon': 'delete',
        'params_schema': {
            'database_config_id': {'type': 'number', 'label': '数据库配置 ID', 'required': True},
            'ticketId': {'type': 'string', 'label': '工单 ID', 'required': True},
        },
        'steps': [],
        'cleanup_steps': [
            {
                'type': 'sql',
                'name': '删除测试工单',
                'database_config_id': '{{database_config_id}}',
                'sql': 'DELETE FROM ticket WHERE id = {{ticketId}}',
                'method': 'delete',
            },
        ],
    },
]

# 兼容旧 template_key（run_template 仍能找到历史计划）
LEGACY_TEMPLATE_KEYS = {
    'create_score_test_ticket_type_a': 'biz_create_type_a',
    'create_ticket_type_a': 'biz_create_type_a',
    'create_ticket_with_delay': 'biz_create_with_delay',
}

BUILTIN_TEMPLATES: List[Dict[str, Any]] = (
    BUILTIN_BUSINESS_TEMPLATES + BUILTIN_STEP_TEST_TEMPLATES + BUILTIN_CLEANUP_TEMPLATES
)


def get_builtin_templates() -> List[Dict[str, Any]]:
    return BUILTIN_TEMPLATES


def get_template_by_key(
    template_key: str,
    *,
    project_id: int | None = None,
    default_environment_id: int | None = None,
    plan_bindings: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    mapped_key = LEGACY_TEMPLATE_KEYS.get(template_key, template_key)
    for item in BUILTIN_TEMPLATES:
        if item.get('template_key') == mapped_key:
            result = dict(item)
            result['template_key'] = template_key
            if project_id is not None:
                from .template_resolver import resolve_template_definition

                return resolve_template_definition(
                    result,
                    project_id=project_id,
                    plan_bindings=plan_bindings,
                    default_environment_id=default_environment_id,
                )
            return result
    return None
