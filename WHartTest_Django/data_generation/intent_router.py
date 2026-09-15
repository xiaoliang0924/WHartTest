"""Rule-based business intent routing for LLM plan generation."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, List, Optional

_TICKET_TYPE_PATTERN = re.compile(r'TYPE_[ABC]', re.IGNORECASE)
_EXPLICIT_TEMPLATE_KEY = re.compile(r'\b((?:biz|test_step)_[a-z0-9_]+)\b', re.IGNORECASE)
_ASSIGNEE_PATTERNS = (
    re.compile(r'(?:分配|指派|转派)给\s*([^\s，,。.；;]+)'),
    re.compile(r'处理人[是为：:]\s*([^\s，,。.；;]+)'),
    re.compile(r'使用\s*([^\s(（]+)账号'),
)

_CREATE_ONLY_KEYWORDS = ('仅创建', '只创建', '仅生成', '不要分配', '无需分配', '不分配', '不要指派', '无需指派')
_NEGATED_ASSIGN_PATTERN = re.compile(r'(?:不要|无需|不(?:要)?)(?:分配|指派)')
_ASSIGN_PERMISSION_PHRASES = (
    '已分配访问权限',
    '分配访问权限',
    '已分配访问',
    '访问权限',
    '权限分配',
    '分配权限',
    '权限验证',
    '权限校验',
)


def _wants_create_only(text: str) -> bool:
    return any(keyword in text for keyword in _CREATE_ONLY_KEYWORDS)


def _strip_permission_assign_phrases(text: str) -> str:
    cleaned = text or ''
    for phrase in _ASSIGN_PERMISSION_PHRASES:
        cleaned = cleaned.replace(phrase, '')
    return cleaned


def _mentions_assign_action(text: str) -> bool:
    """True when user wants assign/指派, excluding 待分配 and 不要分配."""
    if (
        '待分配' in text
        or '未分配' in text
        or 'pending_assign' in text.lower()
        or 'unassigned' in text.lower()
    ):
        return False
    if _NEGATED_ASSIGN_PATTERN.search(text):
        return False
    cleaned = _strip_permission_assign_phrases(text)
    return any(
        keyword in cleaned or keyword in cleaned.lower()
        for keyword in ('分配', '指派', 'assign')
    )


def _is_ticket_status_filter(text: str) -> bool:
    """Return whether the case verifies filtering a work-order list by status.

    Status-filter cases need both matching and non-matching records.  They
    cannot be prepared reliably by a single state-transition template.
    """
    lowered = (text or '').lower()
    return (
        '工单状态' in text
        and any(keyword in text or keyword in lowered for keyword in ('筛选', '查询', '下拉'))
        and not _mentions_assign_action(text)
    )


def _needs_unassigned_pending_ticket(text: str) -> bool:
    """Return whether a case needs a pending ticket that can be claimed.

    A phrase such as “不得出现已完成” is an assertion, not a request to
    complete a ticket.  Likewise, “未分配” describes the required fixture;
    routing either phrase to an assignment/resolve workflow is incorrect.
    """
    lowered = (text or '').lower()
    return (
        ('待处理' in text or 'pending_process' in lowered)
        and any(keyword in text or keyword in lowered for keyword in ('未分配', '可领取', '领取工单'))
    )


# Ordered rules: first match wins (more specific keywords before generic ones).
_INTENT_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (('审批工单', 'approval'), 'biz_create_approval_processing'),
    (('转派',), 'biz_create_and_transfer'),
    (('完成工单', '工单完成', '闭环', 'resolve', '已完成', '已关闭'), 'biz_create_assign_resolve'),
    (('领取', 'claimed'), 'biz_create_and_claim'),
    (('处理中',), 'biz_create_and_claim'),
    (('待处理', 'pending_process', '我的工单'), 'biz_create_and_assign'),
    (('待分配', 'pending_assign'), 'biz_create_type_a'),
)


def infer_ticket_type(description: str, fallback: str = 'TYPE_C') -> str:
    text = description or ''
    if '审批工单' in text or re.search(r'\bapproval\b', text, re.IGNORECASE):
        return 'approval'
    match = _TICKET_TYPE_PATTERN.search(text)
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

    explicit = _EXPLICIT_TEMPLATE_KEY.search(text)
    if explicit:
        return explicit.group(1).lower()

    if _needs_unassigned_pending_ticket(text):
        return 'biz_create_type_a'

    if _is_ticket_status_filter(text):
        return 'biz_prepare_status_filter_data'

    if _wants_create_only(text):
        ticket_type = infer_ticket_type(text, fallback='TYPE_A')
        return 'biz_create_type_b' if ticket_type == 'TYPE_B' else 'biz_create_type_a'

    for keywords, template_key in _INTENT_RULES:
        if any(keyword in text or keyword in text.lower() for keyword in keywords):
            if template_key == 'biz_create_type_a':
                ticket_type = infer_ticket_type(text)
                if ticket_type == 'TYPE_B':
                    return 'biz_create_type_b'
            return template_key

    if _mentions_assign_action(text):
        return 'biz_create_and_assign'

    if re.search(r'创建.*工单|工单.*创建', text):
        ticket_type = infer_ticket_type(text)
        if ticket_type == 'TYPE_C':
            return 'biz_create_and_assign'

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

    claimable_pending = _needs_unassigned_pending_ticket(description or '')
    ticket_type = 'TYPE_A' if claimable_pending else infer_ticket_type(description)
    if _TICKET_TYPE_PATTERN.search(description or ''):
        params['ticketType'] = ticket_type
    else:
        params.setdefault('ticketType', ticket_type)

    assignee_name = extract_assignee_name(description)
    if assignee_name and not claimable_pending:
        params.setdefault('assigneeName', assignee_name)

    if claimable_pending:
        params['ticketType'] = 'TYPE_A'
        params['summary'] = 'TYPE_A待处理未分配测试工单'
    elif '待处理' in description and 'summary' not in params:
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
