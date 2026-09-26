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
_INVALID_ASSIGNEE_NAMES = frozenset(
    {
        '当前登录用户',
        '登录用户',
        '当前用户',
        '当前处理人',
        '姓名',
        '工单号',
    }
)
_PLACEHOLDER_NAME_PATTERN = re.compile(r'[{}？?]|姓名|工单号')
# 造数里转派双方不能相同；模板默认亦按此区分。
_DEFAULT_TRANSFER_SOURCE_NAME = '李清云'
_DEFAULT_TRANSFER_TARGET_NAME = '李亮'

_CREATE_ONLY_KEYWORDS = ('仅创建', '只创建', '仅生成', '不要分配', '无需分配', '不分配', '不要指派', '无需指派')
_AUTO_ASSIGN_KEYWORDS = ('自动分配', '自动派单', '自动派发', '自动指派')
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


def uses_automatic_assignment(text: str) -> bool:
    """Whether the case verifies the system's own assignment rule.

    These cases must not run the manual assignment API after a ticket is
    created: the target system assigns the ticket itself.
    """
    return any(keyword in (text or '') for keyword in _AUTO_ASSIGN_KEYWORDS)


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


def _is_creation_time_filter(text: str) -> bool:
    """Date-range filter on list「创建时间」; relies on env data, not create+assign."""
    if '创建时间' not in text:
        return False
    return any(
        keyword in text
        for keyword in (
            '筛选',
            '查询',
            '日历',
            '日期',
            '区间',
            '开始日期',
            '结束日期',
            '指定区间',
        )
    )


def _is_ticket_status_filter(text: str) -> bool:
    """Return whether the case verifies filtering a work-order list by status.

    Status-filter cases need both matching and non-matching records.  They
    cannot be prepared reliably by a single state-transition template.
    """
    # 沟通/发送/领取类用例常会顺带筛「待处理」，不能整案走四状态造数模板。
    if any(
        keyword in (text or '')
        for keyword in ('沟通记录', '发送消息', '点击发送', '按Enter', '领取成功', '完成领取')
    ):
        return False
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
    # 明确领单文案，或「待处理+未分配+领取」组合（如详情领取后再发消息）。
    has_claim_intent = any(
        keyword in text or keyword in lowered
        for keyword in ('可领取', '领取工单', 'claim', '完成领取', '领取成功')
    )
    return (
        ('待处理' in text or 'pending_process' in lowered)
        and has_claim_intent
        and ('未分配' in text or '可领取' in text or '领取工单' in text)
    )


# Ordered rules: first match wins (more specific keywords before generic ones).
# 「转派」不在此表：需区分「仅准备可转派待处理工单」与「前置已完成转派」。
# 「点击工单完成/确认弹窗」也不走 resolve：那些用例要的是处理中可点完成的工单。
_INTENT_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (('审批工单', 'approval'), 'biz_create_approval_processing'),
    (('完成工单', '工单完成', '闭环', 'resolve', '已完成', '已关闭'), 'biz_create_assign_resolve'),
    (('领取', 'claimed'), 'biz_create_and_claim'),
    (('处理中',), 'biz_create_and_claim'),
    (('待处理', 'pending_process', '我的工单'), 'biz_create_and_assign'),
    (('待分配', 'pending_assign'), 'biz_create_type_a'),
)


def _needs_complete_button_ui_fixture(text: str) -> bool:
    """UI cases that click 「工单完成」 and assert the confirm dialog.

    These need a processing ticket still owned by the login user — not a ticket
    already resolved by the pre-data template.
    """
    blob = text or ''
    if '工单完成' not in blob and '完成工单' not in blob:
        return False
    markers = (
        '确认弹窗',
        '确认完成',
        '确定完成该工单',
        '点击绿色',
        '点击「工单完成」',
        '点击工单完成',
        '可见绿色',
    )
    return any(marker in blob for marker in markers)


def _is_valid_person_name(name: Optional[str]) -> bool:
    value = str(name or '').strip().strip('"\'「」')
    if not value or value in _INVALID_ASSIGNEE_NAMES:
        return False
    if _PLACEHOLDER_NAME_PATTERN.search(value):
        return False
    if len(value) > 20:
        return False
    return True


def _needs_completed_transfer_fixture(text: str) -> bool:
    """Whether pre-data must call the transfer API (ticket already transferred).

    Visibility / dialog-validation cases only need a pending ticket owned by the
    login user; calling transfer would change state or hit TRANSFER_TO_SELF.

    Case titles like「…二次确认-转派成功-正常流程」describe the expected UI
    outcome, not a precondition — those must stay on the assign-only path.
    """
    if '转派' not in (text or ''):
        return False

    # 前置已写明「已转派」：必须走转派接口造数。
    if any(
        keyword in text
        for keyword in (
            '已成功转派',
            '已按正常流程发起转派',
            '已转派给',
            '拒收转派',
            '转派系统消息',
            '原处理人我的工单',
            '被转派人我的工单',
        )
    ):
        return True

    first_line = (text.splitlines()[0] if text else '').strip()
    if first_line.startswith('转派成功') or first_line.startswith('被转派人'):
        return True

    # 造数描述「创建并转派给某人」需要接口转派；UI 弹窗/按钮类除外。
    ui_transfer_probe = any(
        keyword in text
        for keyword in (
            '转派按钮',
            '转派弹窗',
            '确认转派',
            '可被选择为转派目标',
            '未填写转派原因',
            '未选择转派对象',
        )
    )
    if ui_transfer_probe:
        return False

    if '并转派给' in text or re.search(r'(?:创建|生成).{0,30}转派给', text):
        return True

    return False


def _needs_transfer_ready_pending_ticket(text: str) -> bool:
    """Pending ticket assigned to operator so UI can show/use 转派."""
    if '转派' not in (text or ''):
        return False
    return not _needs_completed_transfer_fixture(text)


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
            if _is_valid_person_name(name):
                return name
    return None


def extract_login_operator_name(description: str) -> Optional[str]:
    """用例前置里「使用李亮账号(...)登录」中的当前操作人。"""
    text = description or ''
    match = re.search(r'使用\s*([^\s(（]+)\s*账号', text)
    if match:
        name = match.group(1).strip()
        if _is_valid_person_name(name):
            return name
    return None


def extract_current_processor_name(description: str) -> Optional[str]:
    """提取造数前的当前处理人（转派发起方），避免与登录后的被转派人混淆。"""
    text = description or ''
    patterns = (
        re.compile(r'当前处理人为\s*([^\s，,。.；;」"\']+)'),
        re.compile(r'处理人为\s*([^\s，,。.；;」"\']+)\s*[，,]?\s*(?:状态为)?「?待处理'),
        re.compile(r'已有一条\s*([^\s，,。.；;]+)处理的'),
        re.compile(r'一条\s*([^\s，,。.；;]+)处理的待处理工单'),
    )
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            name = match.group(1).strip().strip('"\'「」')
            if _is_valid_person_name(name):
                return name
    return None


def extract_transfer_target_name(description: str) -> Optional[str]:
    """转派目标处理人（如「转派给黎沅欣」「用户黎沅欣…可被选择为转派目标」）。"""
    text = description or ''
    patterns = (
        re.compile(r'已成功转派给\s*([^\s，,。.；;」"\']+)'),
        re.compile(r'发起转派给\s*([^\s，,。.；;」"\']+)'),
        re.compile(r'转派给\s*([^\s，,。.；;」"\']+)'),
        re.compile(r'用户\s*([^\s(（]+)[^。\n]{0,40}转派目标'),
        re.compile(r'系统中存在用户\s*([^\s(（]+)'),
        re.compile(r'可用处理人[“"\']([^”"\']+)'),
    )
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            name = match.group(1).strip().strip('"\'「」')
            if _is_valid_person_name(name):
                return name
    return None


def _pick_distinct_transfer_target(source_name: str) -> str:
    source = str(source_name or '').strip()
    if source and source != _DEFAULT_TRANSFER_TARGET_NAME:
        return _DEFAULT_TRANSFER_TARGET_NAME
    if source and source != _DEFAULT_TRANSFER_SOURCE_NAME:
        return _DEFAULT_TRANSFER_SOURCE_NAME
    return '黎沅欣'


def infer_business_template_key(description: str) -> Optional[str]:
    text = (description or '').strip()
    if not text:
        return None

    explicit = _EXPLICIT_TEMPLATE_KEY.search(text)
    if explicit:
        return explicit.group(1).lower()

    if _needs_unassigned_pending_ticket(text):
        # 领取类由前置数据层从真实可领取池挑选，不创建后再假定其仍未分配。
        return 'biz_create_type_a'

    if uses_automatic_assignment(text):
        return 'biz_create_type_a'

    if _is_ticket_status_filter(text):
        return 'biz_prepare_status_filter_data'

    if _is_creation_time_filter(text):
        return None

    if _wants_create_only(text):
        ticket_type = infer_ticket_type(text, fallback='TYPE_A')
        return 'biz_create_type_b' if ticket_type == 'TYPE_B' else 'biz_create_type_a'

    # 转派：按钮/弹窗类只造「可转派的待处理」；需已转派结果的才走转派接口。
    if _needs_completed_transfer_fixture(text):
        return 'biz_create_and_transfer'
    if _needs_transfer_ready_pending_ticket(text):
        # TYPE_A 创建后由系统自动分配；执行时按自动分配结果登录，不强制改派到文案里的人名。
        return 'biz_create_type_a'

    # 点击「工单完成」验确认弹窗：只要处理中工单，禁止走 resolve 造数把单结掉。
    if _needs_complete_button_ui_fixture(text):
        return 'biz_create_and_claim'

    for keywords, template_key in _INTENT_RULES:
        if any(keyword in text or keyword in text.lower() for keyword in keywords):
            if template_key == 'biz_create_type_a':
                ticket_type = infer_ticket_type(text)
                if ticket_type == 'TYPE_B':
                    return 'biz_create_type_b'
            return template_key

    if _mentions_assign_action(text):
        return 'biz_create_and_assign'

    if re.search(r'创建\s*工单|工单\s*创建', text):
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
    automatic_assignment = uses_automatic_assignment(description or '')
    ticket_type = 'TYPE_A' if claimable_pending or automatic_assignment else infer_ticket_type(description)
    if _TICKET_TYPE_PATTERN.search(description or ''):
        params['ticketType'] = ticket_type
    else:
        params.setdefault('ticketType', ticket_type)

    text = description or ''
    if '转派' in text:
        # TYPE_C 新建后无法分配；转派相关造数统一 TYPE_A。
        params['ticketType'] = 'TYPE_A'
        params.setdefault('summary', '转派测试工单')
        current_processor = extract_current_processor_name(text)
        login_operator = extract_login_operator_name(text)
        target = extract_transfer_target_name(text)

        if _needs_completed_transfer_fixture(text):
            # 发起方优先取「当前处理人/李亮处理的」，避免用被转派人登录账号当 source。
            source = current_processor or login_operator
            if (
                target
                and login_operator
                and login_operator == target
                and not current_processor
            ):
                source = None
            source = source or _DEFAULT_TRANSFER_SOURCE_NAME
            if not target or target == source:
                target = _pick_distinct_transfer_target(source)
            params['sourceAssigneeName'] = source
            params['assigneeName'] = target
        else:
            # 仅准备可转派待处理工单：TYPE_A 交给系统自动分配，不指定处理人。
            params.pop('assigneeName', None)
            params.pop('assigneeUserId', None)
            params.pop('sourceAssigneeName', None)
            params.pop('sourceAssigneeUserId', None)
    else:
        assignee_name = extract_assignee_name(description)
        if assignee_name and not claimable_pending:
            params.setdefault('assigneeName', assignee_name)

    if claimable_pending:
        params['ticketType'] = 'TYPE_A'
        params['summary'] = '可领取待处理测试工单'
    elif automatic_assignment:
        params['ticketType'] = 'TYPE_A'
        params.setdefault('summary', '自动分配测试工单')
    elif ticket_type == 'approval':
        params['ticketType'] = 'approval'
        params.setdefault('summary', '审批工单测试数据')
    elif '移交审批' in text or '处理中' in text:
        # 领取/处理中类：TYPE_C 自动分配到可登录处理人，再转派到目标人。
        params['ticketType'] = 'TYPE_C'
        params.setdefault('summary', '处理中测试工单')
    elif '转派' not in text and '待处理' in text and 'summary' not in params:
        params.setdefault('summary', f'{ticket_type}待处理测试工单')

    # 被测环境 summary 上限 20 字（创建工单校验）。
    summary = params.get('summary')
    if isinstance(summary, str) and len(summary) > 20:
        params['summary'] = summary[:20]

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
