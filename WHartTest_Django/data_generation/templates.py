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
]


def get_builtin_templates() -> List[Dict[str, Any]]:
    return BUILTIN_TEMPLATES


def get_template_by_key(template_key: str) -> Dict[str, Any] | None:
    for item in BUILTIN_TEMPLATES:
        if item.get('template_key') == template_key:
            return item
    return None
