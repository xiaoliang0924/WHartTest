"""内置造数模板（快速造数）。"""

from __future__ import annotations

from typing import Any, Dict, List


BUILTIN_TEMPLATES: List[Dict[str, Any]] = [
    {
        'template_key': 'create_score_test_ticket_type_a',
        'name': '创建得分测试工单 TYPE_A',
        'description': '一键创建 TYPE_A 工单并写入 ticketId/ticketNo，适用于得分测试、UI/功能回归。',
        'target_type': 'both',
        'icon': 'star',
        'params_schema': {
            'summary': {'type': 'string', 'label': '工单摘要', 'default': '得分测试工单'},
        },
        'steps': [
            {
                'type': 'api_call',
                'name': '创建工单 TYPE_A',
                'interface_id': 445,
                'environment_id': 4,
                'variables': {'summary': '{{summary}}'},
                'extract': {'ticketId': 'ticketId', 'ticketNo': 'ticketNo'},
            },
            {
                'type': 'set_env_var',
                'name': '写入环境变量',
                'environment_id': 4,
                'variables': {
                    'ticketId': '{{ticketId}}',
                    'ticketNo': '{{ticketNo}}',
                    'processingTicketId': '{{ticketId}}',
                    'work_order_id': '{{ticketId}}',
                },
            },
            {
                'type': 'set_public_data',
                'name': '写入 UI 公共数据',
                'items': [
                    {'key': 'ticketId', 'value': '{{ticketId}}', 'type': 0},
                    {'key': 'ticketNo', 'value': '{{ticketNo}}', 'type': 0},
                    {'key': 'work_order_id', 'value': '{{ticketId}}', 'type': 0},
                ],
            },
        ],
        'cleanup_steps': [],
    },
    {
        'template_key': 'create_ticket_type_a',
        'name': '创建待分配工单 TYPE_A',
        'description': '调用创建工单接口，写入环境变量与 UI 公共数据，适用于功能/UI 回归。',
        'target_type': 'both',
        'icon': 'file',
        'params_schema': {
            'summary': {'type': 'string', 'label': '工单摘要', 'default': '造数测试工单'},
        },
        'steps': [
            {
                'type': 'api_call',
                'name': '创建工单 TYPE_A',
                'interface_id': 445,
                'environment_id': 4,
                'variables': {'summary': '{{summary}}'},
                'extract': {'ticketId': 'ticketId', 'ticketNo': 'ticketNo'},
            },
            {
                'type': 'set_env_var',
                'name': '写入环境变量',
                'environment_id': 4,
                'variables': {
                    'ticketId': '{{ticketId}}',
                    'ticketNo': '{{ticketNo}}',
                    'processingTicketId': '{{ticketId}}',
                    'work_order_id': '{{ticketId}}',
                },
            },
            {
                'type': 'set_public_data',
                'name': '写入 UI 公共数据',
                'items': [
                    {'key': 'ticketId', 'value': '{{ticketId}}', 'type': 0},
                    {'key': 'ticketNo', 'value': '{{ticketNo}}', 'type': 0},
                    {'key': 'work_order_id', 'value': '{{ticketId}}', 'type': 0},
                ],
            },
        ],
        'cleanup_steps': [],
    },
    {
        'template_key': 'create_ticket_with_delay',
        'name': '创建工单并等待同步',
        'description': '创建工单后延迟 2 秒，适用于下游列表有延迟刷新的场景。',
        'target_type': 'both',
        'icon': 'clock-circle',
        'params_schema': {
            'summary': {'type': 'string', 'label': '工单摘要', 'default': '造数延迟测试'},
            'delay_seconds': {'type': 'number', 'label': '等待秒数', 'default': 2},
        },
        'steps': [
            {
                'type': 'api_call',
                'name': '创建工单',
                'interface_id': 445,
                'environment_id': 4,
                'variables': {'summary': '{{summary}}'},
                'extract': {'ticketId': 'ticketId', 'ticketNo': 'ticketNo'},
            },
            {
                'type': 'delay',
                'name': '等待同步',
                'seconds': '{{delay_seconds}}',
            },
            {
                'type': 'set_public_data',
                'name': '写入 UI 公共数据',
                'items': [
                    {'key': 'ticketId', 'value': '{{ticketId}}', 'type': 0},
                    {'key': 'work_order_id', 'value': '{{ticketId}}', 'type': 0},
                ],
            },
        ],
        'cleanup_steps': [],
    },
    {
        'template_key': 'cleanup_ticket_by_sql',
        'name': 'SQL 清理测试工单（模板）',
        'description': '通过 SQL 删除指定工单，需配置 database_config_id 与 ticketId。',
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
                'sql': "DELETE FROM ticket WHERE id = {{ticketId}}",
                'method': 'delete',
            },
        ],
    },
    # --- 步骤能力测试模板（每种执行步骤类型各一条，便于逐项验证） ---
    {
        'template_key': 'test_step_api_call',
        'name': '【步骤测试】API 调用',
        'description': '仅测试 api_call：调用「创建工单」接口并 extract ticketId/ticketNo。',
        'target_type': 'api',
        'icon': 'thunderbolt',
        'params_schema': {
            'summary': {'type': 'string', 'label': '工单摘要', 'default': 'API步骤测试'},
        },
        'steps': [
            {
                'type': 'api_call',
                'name': '调用创建工单接口',
                'interface_id': 445,
                'environment_id': 4,
                'variables': {'summary': '{{summary}}'},
                'extract': {'ticketId': 'ticketId', 'ticketNo': 'ticketNo'},
            },
        ],
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
                'environment_id': 4,
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
                'database_config_id': 1,
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
                'function_id': 12,
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


def get_builtin_templates() -> List[Dict[str, Any]]:
    return BUILTIN_TEMPLATES


def get_template_by_key(template_key: str) -> Dict[str, Any] | None:
    for item in BUILTIN_TEMPLATES:
        if item.get('template_key') == template_key:
            return item
    return None
