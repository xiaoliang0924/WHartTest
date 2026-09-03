"""Rule-based business intent routing for LLM plan generation."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, List, Optional

_TICKET_TYPE_PATTERN = re.compile(r'TYPE_[ABC]', re.IGNORECASE)
_ASSIGNEE_PATTERNS = (
    re.compile(r'(?:分配|指派|转派)给\s*([^\s，,。.；;]+)'),
    re.compile(r'处理人[是为：:]\s*([^\s，,。.；;]+)'),
)

# Ordered rules: first match wins.
_INTENT_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (('转派',), 'biz_create_and_transfer'),
    (('完成', '闭环', 'resolve'), 'biz_create_assign_resolve'),
    (('领取', 'claimed'), 'biz_create_and_claim'),
    (('待处理', 'pending_process', '处理中', '我的工单'), 'biz_create_and_assign'),
    (('分配', '指派'), 'biz_create_and_assign'),
    (('待分配', 'pending_assign'), 'biz_create_type_a'),
)


def infer_ticket_type(description: str, fallback: str = 'TYPE_C') -> str:
    match = _TICKET_TYPE_PATTERN.search(description or '')
    if match:
        return match.group(0).upper()
    return fallback


def extract_assignee_name(description: str) -> Optional[str]:
    text = (description or '').strip()
    for pattern in _ASSIGNEE_PATTERNS:
        match = pattern.search(text)
        if match:
            name = match.group(1).strip()
            if name:
                return name
    return None


def infer_business_template_key(description: str) -> Optional[str]:
    text = (description or '').strip()
    if not text:
        return None

    for keywords, template_key in _INTENT_RULES:
        if any(keyword in text or keyword in text.lower() for keyword in keywords):
            if template_key == 'biz_create_type_a':
                ticket_type = infer_ticket_type(text)
                if ticket_type == 'TYPE_B':
                    return 'biz_create_type_b'
            return template_key

    if re.search(r'创建.*工单|工单.*创建', text):
        if any(keyword in text for keyword in ('仅创建', '只创建', '仅生成', '不要分配', '无需分配')):
            ticket_type = infer_ticket_type(text)
            return 'biz_create_type_b' if ticket_type == 'TYPE_B' else 'biz_create_type_a'

    return None


def _collect_step_params(steps: List[Any]) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        for key in ('variables', 'input_params', 'body'):
            values = step.get(key)
            if isinstance(values, dict):
                params.update(values)
    return params


def _apply_param_aliases(params: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(params)
    aliases = {
        'targetUserId': 'assigneeUserId',
        'targetUserName': 'assigneeName',
        'targetRole': 'assigneeRole',
        'reason': 'transferReason',
    }
    for source, target in aliases.items():
        if source in merged and target not in merged:
            merged[target] = merged[source]
    return merged


def build_input_params(description: str, llm_payload: Dict[str, Any]) -> Dict[str, Any]:
    params = dict(llm_payload.get('input_params') or {})
    params.update(_collect_step_params(llm_payload.get('steps') or []))

    ticket_type = infer_ticket_type(description)
    params.setdefault('ticketType', ticket_type)

    assignee_name = extract_assignee_name(description)
    if assignee_name:
        params.setdefault('assigneeName', assignee_name)

    if '待处理' in description and 'summary' not in params:
        params.setdefault('summary', f'{ticket_type}待处理测试工单')

    return _apply_param_aliases(params)


def normalize_custom_steps(steps: List[Any]) -> List[Dict[str, Any]]:
    """Fix common LLM step shape mistakes before validation/execution."""
    normalized: List[Dict[str, Any]] = []
    for index, step in enumerate(steps or [], start=1):
        if not isinstance(step, dict):
            continue
        item = deepcopy(step)
        item.setdefault('name', item.get('name') or f'步骤{index}')

        if item.get('type') != 'api_call':
            normalized.append(item)
            continue

        variables = item.get('variables')
        if not isinstance(variables, dict):
            variables = {}

        for legacy_key in ('input_params', 'body'):
            legacy = item.pop(legacy_key, None)
            if isinstance(legacy, dict):
                variables.update(legacy)

        if variables:
            item['variables'] = variables

        extracts = item.pop('extracts', None)
        if isinstance(extracts, dict) and not item.get('extract'):
            item['extract'] = extracts

        item.pop('url_params', None)
        normalized.append(item)
    return normalized


def route_llm_payload(
    description: str,
    llm_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Prefer executable built-in templates over fragile custom LLM output."""
    routed = dict(llm_payload)
    template_key = infer_business_template_key(description)

    llm_template_key = str(routed.get('template_key') or '').strip()
    mode = str(routed.get('generation_mode') or 'template').strip().lower()
    should_override = (
        template_key is not None
        and (
            mode != 'template'
            or not llm_template_key
            or llm_template_key != template_key
        )
    )

    if should_override:
        routed['generation_mode'] = 'template'
        routed['template_key'] = template_key

    if routed.get('generation_mode', '').strip().lower() == 'template':
        input_params = build_input_params(description, routed)
        if input_params:
            routed['input_params'] = input_params
        return routed

    steps = normalize_custom_steps(routed.get('steps') or [])
    if steps:
        routed['steps'] = steps
    return routed
