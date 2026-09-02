"""造数缺口分析与计划生成。"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Set

from api_environments.models import ApiEnvironmentVariable
from testcases.models import TestCase, TestSuite
from ui_automation.models import UiPublicData

from .templates import get_builtin_templates

_VAR_PATTERN = re.compile(
    r'\$\{\{([^}]+)\}\}|\{\{([^}]+)\}\}|\$(\w+)',
)


def _collect_text_from_testcase(testcase: TestCase) -> str:
    parts = [
        testcase.name or '',
        testcase.precondition or '',
        testcase.notes or '',
    ]
    for step in testcase.steps.all().order_by('step_number'):
        parts.append(step.description or '')
        parts.append(step.expected_result or '')
    return '\n'.join(parts)


def extract_variable_names(text: str) -> Set[str]:
    names: Set[str] = set()
    for match in _VAR_PATTERN.finditer(text or ''):
        name = next((g for g in match.groups() if g), None)
        if not name:
            continue
        name = name.strip()
        if name and not name.startswith('faker.'):
            names.add(name)
    return names


def analyze_suite_variable_gaps(
    suite: TestSuite,
    *,
    environment_id: int | None = None,
) -> Dict[str, Any]:
    required: Set[str] = set()
    testcase_details: List[Dict[str, Any]] = []

    for testcase in suite.testcases.all():
        text = _collect_text_from_testcase(testcase)
        vars_in_case = extract_variable_names(text)
        required.update(vars_in_case)
        if vars_in_case:
            testcase_details.append({
                'id': testcase.id,
                'name': testcase.name,
                'variables': sorted(vars_in_case),
            })

    public_keys = set(
        UiPublicData.objects.filter(
            project_id=suite.project_id,
            is_enabled=True,
        ).values_list('key', flat=True)
    )
    env_keys: Set[str] = set()
    if environment_id:
        env_keys = set(
            ApiEnvironmentVariable.objects.filter(
                environment_id=environment_id,
            ).values_list('name', flat=True)
        )

    available = public_keys | env_keys
    missing = sorted(name for name in required if name not in available)

    suggestions: List[Dict[str, Any]] = []
    if any(key in required for key in ('ticketId', 'work_order_id', 'ticketNo')):
        suggestions.append({
            'template_key': 'create_ticket_type_a',
            'reason': '套件用例引用了工单相关变量，建议使用「创建待分配工单 TYPE_A」模板',
        })
    if missing:
        suggestions.append({
            'action': 'bind_pre_data_plan',
            'reason': f'仍有 {len(missing)} 个变量未在公共数据/环境变量中找到',
        })

    return {
        'suite_id': suite.id,
        'suite_name': suite.name,
        'required_variables': sorted(required),
        'available_variables': sorted(available),
        'missing_variables': missing,
        'testcases': testcase_details,
        'suggestions': suggestions,
    }


def generate_plan_from_description(
    description: str,
    *,
    default_environment_id: int | None = None,
) -> Dict[str, Any]:
    text = (description or '').strip()
    lower = text.lower()

    if any(k in text for k in ('工单', 'ticket', '待分配')):
        template = next(
            (t for t in get_builtin_templates() if t['template_key'] == 'create_ticket_type_a'),
            None,
        )
        if template:
            return {
                'name': template['name'],
                'description': text or template['description'],
                'target_type': template['target_type'],
                'default_environment': default_environment_id or 4,
                'steps': json.loads(json.dumps(template['steps'])),
                'cleanup_steps': template.get('cleanup_steps') or [],
                'source': 'template:create_ticket_type_a',
            }

    if any(k in lower for k in ('sql', '数据库', '清理', 'delete')):
        return {
            'name': 'SQL 造数计划',
            'description': text,
            'target_type': 'api',
            'default_environment': default_environment_id,
            'steps': [
                {
                    'type': 'sql',
                    'name': '执行 SQL',
                    'database_config_id': None,
                    'sql': 'SELECT 1',
                    'method': 'fetchone',
                }
            ],
            'cleanup_steps': [],
            'source': 'generated:sql_stub',
            'hint': '请补充 database_config_id 与 SQL 语句',
        }

    return {
        'name': '新建造数计划',
        'description': text,
        'target_type': 'both',
        'default_environment': default_environment_id,
        'steps': [
            {
                'type': 'api_call',
                'name': '调用接口',
                'interface_id': None,
                'environment_id': default_environment_id,
                'variables': {},
                'extract': {},
            },
            {
                'type': 'set_public_data',
                'name': '写入 UI 公共数据',
                'items': [{'key': 'sample_key', 'value': '{{sample_value}}', 'type': 0}],
            },
        ],
        'cleanup_steps': [],
        'source': 'generated:default',
        'hint': '已生成基础骨架，请补充 interface_id 与 extract 映射',
    }
