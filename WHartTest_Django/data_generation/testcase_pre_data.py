"""单条用例（用例管理）执行前的自动造数。"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from django.contrib.auth import get_user_model
from projects.models import ProjectCredential

from testcases.models import TestCase, TestCaseModule, TestCaseStep

from .exceptions import DataGenerationError
from .intent_router import (
    build_input_params,
    infer_business_template_key,
    uses_automatic_assignment,
)
from .models import DataGenerationPlan, DataGenerationRun
from .plan_validation import ensure_plan_has_environment
from .services import apply_params_schema_defaults, execute_plan
from .templates import get_template_by_key

logger = logging.getLogger(__name__)

# 兼容早期保存的模板。领取类用例只使用系统中真实可领取的工单。
_LEGACY_CLAIMABLE_TEMPLATE_KEY = 'biz_create_claimable_pending'
_CLAIMABLE_AUTO_SOURCE = 'claimable-auto'
_CLAIMABLE_MANUAL_SOURCE = 'manual-fixture'
# 带 initialMessage 的创建接口会把发起人锁进沟通会话，领取时返回 INITIATOR_LOCKED。
# 领取类造数必须改用「无初始沟通消息」接口。
_CLAIMABLE_CREATE_INTERFACE_NAME = '创建工单-无初始沟通消息'
_DEFAULT_CREATE_INTERFACE_IDS = (445,)
# admin as initiator often hits INITIATOR_LOCKED during claim.
# Claimable pre-data creates tickets as env noCommonToken user instead.
_CLAIMABLE_CREATOR_TOKEN_VAR = 'noCommonToken'


def _inject_unlocked_creator_token(exec_params, *, environment_id):
    """Override accessToken so claimable tickets are not created by a locked initiator."""
    params = dict(exec_params or {})
    if not environment_id:
        return params
    try:
        from api_environments.models import ApiEnvironment
        from api_environments.token_refresh import refresh_environment_tokens

        env = ApiEnvironment.objects.filter(id=int(environment_id)).first()
        if env is None:
            return params
        variables = env.get_all_variables()
        if not isinstance(variables, dict):
            return params
        variables = refresh_environment_tokens(
            base_url=env.base_url,
            variables=variables,
            verify_ssl=getattr(env, 'verify_ssl', False),
            environment_id=env.id,
            persist=True,
            force_token_vars=[_CLAIMABLE_CREATOR_TOKEN_VAR],
        )
        token = str(variables.get(_CLAIMABLE_CREATOR_TOKEN_VAR) or '').strip()
        if not token:
            logger.warning(
                'Claimable pre-data: env var %s missing; create may hit INITIATOR_LOCKED',
                _CLAIMABLE_CREATOR_TOKEN_VAR,
            )
            return params
        params['accessToken'] = token
        params['adminToken'] = token
        logger.info(
            'Claimable pre-data: create auth overridden with %s to avoid INITIATOR_LOCKED',
            _CLAIMABLE_CREATOR_TOKEN_VAR,
        )
    except Exception:
        logger.exception('Claimable pre-data: failed to inject unlocked creator token')
    return params


def _inject_claim_creator_token(exec_params, *, project_id: int, environment_id: int):
    """Create a no-initial-chat claim fixture as its assignee, without replacing admin auth."""
    import jwt

    from api_environments.models import ApiEnvironment, ApiEnvironmentVariable
    from api_environments.token_refresh import _login_for_token, _token_still_valid

    params = dict(exec_params or {})
    desired_id = params.get('assigneeUserId')
    desired_name = str(params.get('assigneeName') or '').strip()
    env = ApiEnvironment.objects.filter(id=environment_id, project_id=project_id).first()
    if env is None or desired_id in (None, '') or not desired_name:
        raise DataGenerationError('领取类造数缺少环境或目标处理人')
    token_name = f'claimAssigneeToken_{desired_id}'
    cached = str(env.get_all_variables().get(token_name) or '')
    token = None
    if _token_still_valid(cached):
        try:
            if str(jwt.decode(cached, options={'verify_signature': False}).get('sub')) == str(desired_id):
                token = cached
        except jwt.PyJWTError:
            pass
    if token is None:
        credential = (
            ProjectCredential.objects.filter(project_id=project_id, user_role__iexact=desired_name)
            .exclude(password='').first()
        )
        if credential is None:
            raise DataGenerationError(f'目标处理人“{desired_name}”没有已保存的登录凭据')
        try:
            token = _login_for_token(
                env.base_url, credential.username, credential.password,
                verify_ssl=env.verify_ssl,
            )
        except Exception as exc:
            raise DataGenerationError(f'目标处理人“{desired_name}”登录失败，无法创建可领取工单: {exc}') from exc
        if str(jwt.decode(token, options={'verify_signature': False}).get('sub')) != str(desired_id):
            raise DataGenerationError('目标处理人登录令牌与预期用户 ID 不一致')
        ApiEnvironmentVariable.objects.update_or_create(
            environment=env, name=token_name,
            defaults={
                'value': token,
                'type': 'string',
                'is_sensitive': True,
                'description': '工单造数计划目标处理人短期登录令牌',
            },
        )
    # Pass only the variable name into the persisted run; never store the JWT
    # in input_params, output_snapshot, or step_logs.
    params['claimCreatorTokenVar'] = token_name
    return params


def _inject_case_creator_token(exec_params, *, project_id: int, environment_id: int):
    """Resolve a short-lived creator token for fixtures that assert creator permissions."""
    params = dict(exec_params or {})
    creator_id = params.get('creatorUserId')
    creator_name = str(params.get('creatorName') or '').strip()
    if creator_id in (None, ''):
        return params
    if not creator_name:
        raise DataGenerationError('指定造数发起人时必须同时提供 creatorName')

    token_params = _inject_claim_creator_token(
        {'assigneeUserId': creator_id, 'assigneeName': creator_name},
        project_id=project_id,
        environment_id=environment_id,
    )
    params['creatorTokenVar'] = token_params['claimCreatorTokenVar']
    return params


_TICKET_CONTEXT_KEYWORDS = (
    '工单',
    'ticket',
    '待分配',
    '待处理',
    '处理中',
    '转派',
    '派发',
    '领取',
    '审批',
    'approval',
    '筛选',
    '工单列表',
    '工单状态',
    'ticketid',
    'ticketno',
)

_PAGE_ACCESS_KEYWORDS = (
    '页面访问',
    '权限验证',
    '权限校验',
    '访问权限',
    '菜单权限',
    '无权限',
    '权限不足',
)

_EXPORT_VERIFY_KEYWORDS = (
    '导出全部',
    '导出文件',
    '核对导出',
    '文件仅含',
    '文件包含',
    '仅导出',
)

_ROW_MUTATION_KEYWORDS = (
    '领取',
    '转派',
    '派发',
    '点击处理',
    '确认领取',
    '关闭工单',
    '完成工单',
    '处理人弹窗',
)

_UI_DISPLAY_ONLY_KEYWORDS = (
    '入口展示',
    '页面入口',
    '区块',
    '页面展示',
    '均展示',
    '查看页面',
    '字段展示',
    '表单展示',
    '三大区块',
)

_EXISTING_TICKET_DATA_KEYWORDS = (
    '系统内存在',
    '系统中存在',
    '至少1个',
    '至少一条',
    '至少1条',
    '列表中存在',
    '已存在',
    '待分配',
    'pending_assign',
    '待处理',
    'pending_process',
    '处理中',
    '领取',
    'claimed',
    '转派',
    '派发',
    '筛选',
    '工单号',
    'ticketno',
    'ticket_no',
    '处理人',
    '审批工单',
    '已完成',
    '已关闭',
    '造数',
)

_TICKET_ROW_ACTION_KEYWORDS = (
    '工单号',
    'ticketno',
    'ticket_no',
    '筛选',
    '查询',
    '处理人',
    '待处理',
    '待分配',
    '领取',
    '转派',
    '派发',
    '我的工单',
    '工单列表',
    '工单状态',
    '审批工单',
    '处理中',
    '已完成',
    '已关闭',
)

_LOGIN_CREDENTIAL_RE = re.compile(
    r'(?:账号|用户名)\s*[（(]\s*([^/\s）)]+)\s*/\s*([^/\s）)]+)\s*[）)]'
)
_LOGIN_URL_RE = re.compile(r'(https?://[^\s)）]+)')


@dataclass
class PreDataResolution:
    plan: Optional[DataGenerationPlan]
    template_key: Optional[str]
    input_params: Dict[str, Any]
    default_environment_id: Optional[int]
    source: str
    fail_fast: bool
    skip_reason: str = ''


@dataclass
class TestcasePreDataResult:
    run: Optional[DataGenerationRun]
    resolution: PreDataResolution
    message_suffix: str = ''
    blocked: bool = False
    block_message: str = ''


def collect_testcase_text(testcase: TestCase) -> str:
    parts = [testcase.name or '', testcase.precondition or '']
    for step in testcase.steps.order_by('step_number'):
        parts.append(step.description or '')
        parts.append(step.expected_result or '')
    return '\n'.join(part for part in parts if part)


_FIXTURE_STATUS_TO_TEMPLATE = {
    '待分配': 'biz_create_type_a',
    '未分配': 'biz_create_type_a',
    '待处理': 'biz_create_and_assign',
    '处理中': 'biz_create_and_claim',
    '已解决': 'biz_create_assign_resolve',
    '已关闭': 'biz_create_assign_close',
}
_TERMINAL_PAIR_KEY = 'biz_prepare_terminal_pair'
_FIXTURE_STATUS_PATTERN = re.compile(
    r'(?:状态为|状态：|状态:|状态是)\s*[「“"\']?'
    r'(已关闭|已解决|处理中|待处理|待分配|未分配)'
)


def required_fixture_statuses(precondition: str) -> tuple[str, ...]:
    """Only explicit *preconditions* define starting ticket states.

    Titles and expected results often describe what the UI test will do later,
    so routing on all testcase text can prepare the wrong state up front.
    """
    return tuple(dict.fromkeys(_FIXTURE_STATUS_PATTERN.findall(precondition or '')))


def _required_statuses_from_navigation_steps(testcase: TestCase) -> tuple[str, ...]:
    """Fallback to navigation actions, never assertions about the UI outcome."""
    statuses = []
    target_pattern = re.compile(
        r'(?:找到|定位|点击|进入|搜索|筛选|选择).{0,35}?'
        r'(?:状态为|状态：|状态:|状态的|该)?\s*[「“"\']?'
        r'(已关闭|已解决|处理中|待处理|待分配|未分配)'
    )
    for step in testcase.steps.order_by('step_number')[:3]:
        description = step.description or ''
        for status in target_pattern.findall(description):
            if status not in statuses:
                statuses.append(status)
    return tuple(statuses)


def is_page_access_permission_case(text: str) -> bool:
    return any(keyword in (text or '') for keyword in _PAGE_ACCESS_KEYWORDS)


def is_overview_sla_detail_case(testcase: TestCase) -> bool:
    """总览页 SLA 预警表点击工单号/详情进入详情页的用例。"""
    text = collect_testcase_text(testcase)
    name = testcase.name or ''
    blob = f'{name}\n{text}'

    if not re.search(r'工单总览|数据总览|SLA|预警', blob):
        return False
    if re.search(r'工单详情|详情页|进入工单详情|跳转.*详情', blob):
        return True
    if re.search(r'点击.*工单ID|点击.*工单号|工单ID.*链接|工单号.*链接', blob):
        return True
    if re.search(r'SLA预警.*点击|预警明细.*点击', blob):
        return True
    return False


def is_overview_dashboard_case(testcase: TestCase) -> bool:
    """True only when the case under test is the dashboard/overview page itself."""
    if is_overview_sla_detail_case(testcase):
        return False

    text = collect_testcase_text(testcase)
    name = testcase.name or ''

    if re.search(r'工单列表|我的工单|通知记录', name):
        return False
    if is_page_access_permission_case(text) and re.search(
        r'工单列表|我的工单|通知记录', text
    ):
        return False

    if re.search(r'(进入|点击|访问|打开|查看).*工单总览', text):
        return True
    if re.search(r'(进入|点击|访问|打开|查看).*数据总览', text):
        return True
    if '/work-order/dashboard' in text:
        return True
    if re.search(r'工单总览页|数据总览页', text):
        return True
    if re.search(r'工单总览|数据总览', name) and not re.search(r'工单列表', name):
        return True
    return False


def is_ui_display_only_case(text: str) -> bool:
    return any(keyword in (text or '') for keyword in _UI_DISPLAY_ONLY_KEYWORDS)


def is_export_verify_case(text: str) -> bool:
    return any(keyword in (text or '') for keyword in _EXPORT_VERIFY_KEYWORDS)


def needs_row_mutation(text: str) -> bool:
    return any(keyword in (text or '') for keyword in _ROW_MUTATION_KEYWORDS)


def needs_existing_ticket_data(text: str) -> bool:
    lowered = (text or '').lower()
    return any(
        keyword in (text or '') or keyword in lowered
        for keyword in _EXISTING_TICKET_DATA_KEYWORDS
    )


def needs_ticket_row_action(text: str) -> bool:
    lowered = (text or '').lower()
    return any(
        keyword in (text or '') or keyword in lowered
        for keyword in _TICKET_ROW_ACTION_KEYWORDS
    )


def needs_ticket_pre_data(text: str) -> bool:
    if is_page_access_permission_case(text):
        return False
    existing = needs_existing_ticket_data(text)
    if is_ui_display_only_case(text) and not existing:
        return False
    if is_export_verify_case(text) and not needs_row_mutation(text):
        return False
    lowered = (text or '').lower()
    return any(keyword in text or keyword in lowered for keyword in _TICKET_CONTEXT_KEYWORDS)


def extract_login_credentials(text: str) -> Dict[str, str]:
    """Parse username/password/login URL from case precondition or steps."""
    result: Dict[str, str] = {}
    blob = text or ''
    cred = _LOGIN_CREDENTIAL_RE.search(blob)
    if cred:
        result['username'] = cred.group(1).strip()
        result['password'] = cred.group(2).strip()
    urls = _LOGIN_URL_RE.findall(blob)
    login_url = next((url for url in urls if 'login' in url.lower()), None)
    chosen = login_url or (urls[0] if urls else '')
    if chosen:
        result['login_url'] = chosen.rstrip('。，,;；')
    return result


def resolve_module_pre_data_plan(
    module: TestCaseModule,
) -> Tuple[Optional[DataGenerationPlan], Optional[TestCaseModule]]:
    current: Optional[TestCaseModule] = module
    while current is not None:
        plan_id = getattr(current, 'pre_data_plan_id', None)
        if plan_id:
            plan = DataGenerationPlan.objects.filter(
                id=plan_id,
                project_id=current.project_id,
                is_active=True,
            ).first()
            if plan is not None:
                return plan, current
        current = current.parent
    return None, None


def ensure_project_template_plan(
    *,
    project_id: int,
    template_key: str,
    created_by=None,
    default_environment_id: Optional[int] = None,
) -> Optional[DataGenerationPlan]:
    if template_key == _LEGACY_CLAIMABLE_TEMPLATE_KEY:
        template_key = 'biz_create_type_a'

    plan = DataGenerationPlan.objects.filter(
        project_id=project_id,
        template_key=template_key,
        is_template=True,
        is_active=True,
    ).first()
    if plan is not None:
        if template_key == 'biz_create_assign_close':
            from copy import deepcopy
            from api_interfaces.models import ApiInterface
            from .template_resolver import _find_interface_id, _is_single_close_interface

            steps = deepcopy(plan.steps or [])
            for step in steps:
                if step.get('type') != 'api_call' or step.get('name') != '关闭工单':
                    continue
                interface = ApiInterface.objects.filter(
                    pk=step.get('interface_id'), project_id=project_id,
                ).first()
                if interface is None or not _is_single_close_interface(interface):
                    step['interface_id'] = _find_interface_id(
                        project_id, 'close_ticket', plan.template_bindings or {},
                    )
                    plan.steps = steps
                    plan.save(update_fields=['steps'])
                break
        return plan

    template = get_template_by_key(
        template_key,
        project_id=project_id,
        default_environment_id=default_environment_id,
    )
    if template is None:
        return None

    from projects.models import Project

    project = Project.objects.filter(pk=project_id).first()
    if project is None:
        return None

    bindings = (
        template.get('template_bindings')
        if isinstance(template.get('template_bindings'), dict)
        else {}
    )
    return DataGenerationPlan.objects.create(
        project=project,
        name=template['name'],
        description=template.get('description', ''),
        target_type=template.get('target_type', 'both'),
        steps=template.get('steps') or [],
        cleanup_steps=template.get('cleanup_steps') or [],
        default_environment_id=default_environment_id,
        is_template=True,
        template_key=template_key,
        template_icon=template.get('icon', ''),
        template_params_schema=template.get('params_schema') or {},
        template_bindings=bindings,
        created_by=created_by,
    )


def extract_manual_ticket_fixture(params: Any) -> Dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    snapshot: Dict[str, Any] = {}
    for key in ('ticketNo', 'ticketId', 'ticketType', 'work_order_id', 'processingTicketId'):
        value = params.get(key)
        if value not in (None, ''):
            snapshot[key] = value
    ticket_no = str(snapshot.get('ticketNo') or '').strip()
    if not ticket_no:
        return {}
    if 'ticketId' not in snapshot and snapshot.get('work_order_id') not in (None, ''):
        snapshot['ticketId'] = snapshot['work_order_id']
    # Keep assignment facts so claim login can resolve browser credentials
    # when the case uses a manual/pool fixture instead of a data-generation run.
    assignment = params.get('autoAssignment')
    if isinstance(assignment, dict) and assignment:
        snapshot['autoAssignment'] = assignment
    return snapshot


def _list_tickets_from_api(
    *,
    project_id: int,
    environment_id: int,
    query_path: str,
) -> list[dict[str, Any]]:
    """Best-effort ticket list for pool picking (ignores strict interface validators)."""
    try:
        from api_environments.models import ApiEnvironment
        from api_environments.token_refresh import refresh_environment_tokens
        from api_interfaces.models import ApiInterface
        from api_interfaces.runner import InterfaceRunner
        from data_generation.services import (
            _collect_force_refresh_token_vars,
            _parse_response_body,
        )

        iface = ApiInterface.objects.filter(
            project_id=project_id,
            name='工单列表查询',
        ).first()
        if iface is None:
            return []
        env = ApiEnvironment.objects.filter(id=environment_id).first()
        if env is None:
            return []

        data = iface.get_interface_data()
        data['project_id'] = project_id
        data['base_url'] = env.base_url or ''
        data['verify'] = env.verify_ssl
        data['method'] = 'GET'
        data['url'] = query_path

        runner = InterfaceRunner(data)
        runner.variables = refresh_environment_tokens(
            base_url=env.base_url,
            variables=env.get_all_variables(),
            verify_ssl=env.verify_ssl,
            environment_id=env.id,
            persist=True,
            force_token_vars=_collect_force_refresh_token_vars(data),
        )
        runner.run_interface({})
        response = runner.get_response()
        body = _parse_response_body((response.get('response') or {}).get('content'))
        if isinstance(body, dict):
            block = body.get('data')
            if isinstance(block, list):
                return [row for row in block if isinstance(row, dict)]
        if isinstance(body, list):
            return [row for row in body if isinstance(row, dict)]
    except Exception as exc:
        logger.debug('list tickets for claimable pool skipped: %s', exc)
    return []


def _assignment_snapshot_from_row(row: dict[str, Any]) -> Dict[str, Any]:
    """Keep non-secret assignment facts needed to select a browser credential."""
    assignee = row.get('assignee') if isinstance(row.get('assignee'), dict) else {}
    return {
        'status': row.get('status'),
        'assigneeUserId': row.get('assigneeUserId') or assignee.get('id'),
        'assigneeName': row.get('assigneeName') or assignee.get('name') or assignee.get('username'),
        'assigneeUsername': row.get('assigneeUsername') or assignee.get('username'),
        'availableActions': row.get('availableActions') or [],
    }


def _find_assigned_ticket_snapshot(
    *,
    ticket_no: Any,
    project_id: int,
    environment_id: Optional[int],
) -> Dict[str, Any]:
    if not ticket_no or not environment_id:
        return {}
    rows = _list_tickets_from_api(
        project_id=project_id,
        environment_id=int(environment_id),
        query_path=f'/api/tickets?ticketNo={ticket_no}&page=1&pageSize=5',
    )
    row = next(
        (item for item in rows if str(item.get('ticketNo') or '') == str(ticket_no)),
        None,
    )
    if row is None or not _ticket_row_supports_claim(row):
        return {}
    return {
        'ticketId': row.get('id') or row.get('ticketId'),
        'ticketNo': str(row.get('ticketNo') or ticket_no),
        'work_order_id': row.get('id') or row.get('ticketId'),
        'ticketStatus': row.get('status'),
        'autoAssignment': _assignment_snapshot_from_row(row),
        'source': 'claimable-auto-assigned',
    }


def _wait_for_assigned_claim_snapshot(
    snapshot: Dict[str, Any],
    *,
    project_id: int,
    environment_id: Optional[int],
) -> Dict[str, Any]:
    """Wait briefly for the target system's asynchronous auto-assignment."""
    ticket_no = snapshot.get('ticketNo')
    for attempt in range(5):
        assigned = _find_assigned_ticket_snapshot(
            ticket_no=ticket_no,
            project_id=project_id,
            environment_id=environment_id,
        )
        if assigned:
            return {**snapshot, **assigned}
        if attempt < 4:
            time.sleep(1)
    return snapshot


def resolve_claim_login_credentials(
    testcase: TestCase,
    snapshot: Dict[str, Any],
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """Resolve the assigned handler to a configured project browser account.

    Passwords only flow into the transient Playwright environment; they are
    deliberately never added to a data-generation snapshot or chat message.
    """
    assignment = snapshot.get('autoAssignment') if isinstance(snapshot, dict) else None
    assignment = assignment if isinstance(assignment, dict) else {}
    assignee_name = str(assignment.get('assigneeName') or '').strip()
    assignee_username = str(assignment.get('assigneeUsername') or '').strip()
    if not assignee_name and not assignee_username:
        return None, '自动分配尚未返回处理人，请等待自动分配完成后重试。'

    credentials = ProjectCredential.objects.filter(project_id=testcase.project_id).exclude(
        username=''
    )
    matches = credentials.filter(username__iexact=assignee_username) if assignee_username else credentials.none()
    if not matches.exists() and assignee_name:
        matches = credentials.filter(user_role__iexact=assignee_name)
    count = matches.count()
    if count == 0:
        label = assignee_name or assignee_username
        return None, (
            f'未配置自动分配处理人“{label}”的测试账号。'
            '请在项目管理的项目凭据中新增该人员：角色填姓名、用户名和密码填实际登录凭据。'
        )
    if count > 1:
        label = assignee_name or assignee_username
        return None, f'处理人“{label}”匹配到多个项目凭据，请保留唯一的姓名/账号映射。'
    credential = matches.first()
    if credential is None or not str(credential.password or '').strip():
        return None, f'处理人“{assignee_name or assignee_username}”的项目凭据缺少密码。'
    return {
        'username': str(credential.username).strip(),
        'password': str(credential.password),
        'login_url': str(credential.system_url or '').strip(),
        'assignee_name': assignee_name or str(credential.user_role or credential.username),
    }, None


def _ticket_row_supports_claim(row: dict[str, Any]) -> bool:
    """A claim fixture is pending and has completed automatic assignment.

    In the target system, automatic assignment chooses the handler first.  The
    assigned handler then manually claims the ticket before processing it.
    ``availableActions`` is read with an administrator token, so it cannot be
    used to decide whether the assigned handler's UI can claim it.
    """
    status = str(row.get('status') or '')
    assignee = row.get('assigneeUserId')
    return status == 'pending_process' and assignee not in (None, '', 0)


def _build_claimable_steps_without_initial_chat(plan: DataGenerationPlan) -> Optional[list]:
    """Force claimable creation onto the no-initial-message create interface.

    Tickets created with a non-empty ``initialMessage`` leave the initiator in an
    active chat lock. Claim then returns HTTP 409 ``INITIATOR_LOCKED`` and the UI
    confirm button appears to "do nothing".
    """
    from copy import deepcopy

    from api_interfaces.models import ApiInterface
    from .template_resolver import resolve_template_steps

    no_chat = (
        ApiInterface.objects.filter(
            project_id=plan.project_id,
            name=_CLAIMABLE_CREATE_INTERFACE_NAME,
        )
        .order_by('id')
        .first()
    )
    if no_chat is None:
        logger.warning(
            'Claimable pre-data: interface %s not found; keep default create binding',
            _CLAIMABLE_CREATE_INTERFACE_NAME,
        )
        return None

    raw_steps = plan.steps if isinstance(plan.steps, list) else []
    resolved = resolve_template_steps(
        deepcopy(raw_steps),
        project_id=plan.project_id,
        plan_bindings=plan.template_bindings if isinstance(plan.template_bindings, dict) else None,
        default_environment_id=plan.default_environment_id,
        template_key=plan.template_key or None,
    )
    changed = False
    for step in resolved:
        if not isinstance(step, dict) or (step.get('type') or '').strip() != 'api_call':
            continue
        interface_id = step.get('interface_id')
        try:
            interface_id_int = int(interface_id) if interface_id is not None else None
        except (TypeError, ValueError):
            interface_id_int = None
        if interface_id_int in _DEFAULT_CREATE_INTERFACE_IDS or interface_id_int == no_chat.id:
            if interface_id_int != no_chat.id:
                step['interface_id'] = no_chat.id
                changed = True
            step_name = str(step.get('name') or '')
            if '创建' in step_name and '无初始' not in step_name:
                step['name'] = _CLAIMABLE_CREATE_INTERFACE_NAME
                changed = True
    if not changed:
        # Fallback: first api_call step is usually create for TYPE_A plans.
        for step in resolved:
            if isinstance(step, dict) and (step.get('type') or '').strip() == 'api_call':
                step['interface_id'] = no_chat.id
                step['name'] = _CLAIMABLE_CREATE_INTERFACE_NAME
                changed = True
                break
    return resolved if changed else None


def _list_processing_creator_names(*, project_id: int, environment_id: int) -> set[str]:
    """Initiators who currently have processing tickets are claim-locked."""
    rows = _list_tickets_from_api(
        project_id=project_id,
        environment_id=int(environment_id),
        query_path='/api/tickets?status=processing&page=1&pageSize=100',
    )
    names: set[str] = set()
    for row in rows:
        name = str(row.get('creatorName') or row.get('creator') or '').strip()
        if name:
            names.add(name)
    return names


def try_pick_claimable_ticket_from_pool(
    testcase: TestCase,
    *,
    environment_id: Optional[int],
) -> dict[str, Any]:
    """Pick a pending assigned ticket whose initiator has no processing chat.

    Do **not** soft-claim with the admin token to verify: admin claim itself
    locks the initiator with the admin handler and recreates INITIATOR_LOCKED
    for the assignee's UI claim.
    """
    env_id = environment_id or getattr(testcase, 'pre_data_environment_id', None)
    if not env_id:
        return {}
    locked_creators = _list_processing_creator_names(
        project_id=testcase.project_id,
        environment_id=int(env_id),
    )
    queries = (
        '/api/tickets?currentStatus=pending_process&page=1&pageSize=100',
        '/api/tickets?status=pending_process&page=1&pageSize=100',
    )
    rows: list[dict[str, Any]] = []
    for query in queries:
        rows = _list_tickets_from_api(
            project_id=testcase.project_id,
            environment_id=int(env_id),
            query_path=query,
        )
        if rows:
            break

    # Prefer creators that are not currently processing with anyone.
    ranked: list[dict[str, Any]] = []
    for row in rows:
        if not _ticket_row_supports_claim(row):
            continue
        ticket_id = row.get('id') or row.get('ticketId')
        ticket_no = row.get('ticketNo')
        if ticket_id in (None, '') or not ticket_no:
            continue
        creator = str(row.get('creatorName') or row.get('creator') or '').strip()
        if creator and creator in locked_creators:
            continue
        ranked.append(row)

    if not ranked:
        return {}

    # Prefer tickets whose creator differs from assignee (cross-user claim),
    # then fall back to any unlocked pending ticket.
    ranked.sort(
        key=lambda row: (
            0
            if str(row.get('creatorName') or '') != str(row.get('assigneeName') or '')
            else 1,
            -(row.get('id') or row.get('ticketId') or 0),
        )
    )
    candidate = ranked[0]
    ticket_id = candidate.get('id') or candidate.get('ticketId')
    ticket_no = candidate.get('ticketNo')
    logger.info(
        'Claimable pool: picked ticket_id=%s creator=%s assignee=%s locked_creators=%s',
        ticket_id,
        candidate.get('creatorName'),
        candidate.get('assigneeName'),
        sorted(locked_creators),
    )
    return {
        'ticketId': ticket_id,
        'ticketNo': str(ticket_no),
        'work_order_id': ticket_id,
        'ticketStatus': candidate.get('status') or 'pending_process',
        'autoAssignment': _assignment_snapshot_from_row(candidate),
        'source': 'claimable-pool',
    }


def validate_claimable_ticket_snapshot(
    snapshot: dict[str, Any],
    *,
    project_id: int,
    environment_id: Optional[int],
) -> Optional[str]:
    """Return warning text when auto data likely cannot show 领取工单 on UI."""
    ticket_id = snapshot.get('ticketId') or snapshot.get('work_order_id')
    if not ticket_id or not environment_id:
        return None
    rows = _list_tickets_from_api(
        project_id=project_id,
        environment_id=int(environment_id),
        query_path=f'/api/tickets?ticketNo={snapshot.get("ticketNo")}&page=1&pageSize=5',
    )
    row = next(
        (
            item
            for item in rows
            if str(item.get('ticketNo') or '') == str(snapshot.get('ticketNo') or '')
        ),
        None,
    )
    if row is None:
        row = next((item for item in rows if str(item.get('id')) == str(ticket_id)), None)
    if row is None:
        return None
    if _ticket_row_supports_claim(row):
        return None
    status = row.get('status') or 'unknown'
    actions = row.get('availableActions') or []
    return (
        f'造数工单 {snapshot.get("ticketNo")} 在环境中为 status={status}，'
        f'availableActions={actions}，尚未成为“待处理且已自动分配”的可领取数据。'
        '请等待自动分配完成，或检查自动分配规则与处理人测试账号配置。'
    )


def resolve_claimable_ticket_pre_data(testcase: TestCase) -> PreDataResolution:
    """待领取类：手工 fixture > 已解锁领取池 > 自动 TYPE_A 造数。"""
    params = testcase.pre_data_params if isinstance(testcase.pre_data_params, dict) else {}
    manual = extract_manual_ticket_fixture(params)
    env_id = getattr(testcase, 'pre_data_environment_id', None)
    if manual.get('ticketNo'):
        # Stale fixtures from a previous run often stick on the case after claim.
        # Re-check the business system; if no longer claimable, drop and refresh.
        still_ok = False
        if env_id:
            stale_reason = validate_claimable_ticket_snapshot(
                manual,
                project_id=testcase.project_id,
                environment_id=env_id,
            )
            rows = _list_tickets_from_api(
                project_id=testcase.project_id,
                environment_id=int(env_id),
                query_path=f'/api/tickets?ticketNo={manual.get("ticketNo")}&page=1&pageSize=5',
            )
            row = next(
                (
                    item
                    for item in rows
                    if str(item.get('ticketNo') or '') == str(manual.get('ticketNo') or '')
                ),
                None,
            )
            still_ok = bool(row and _ticket_row_supports_claim(row) and not stale_reason)
            if still_ok and isinstance(row, dict):
                # Refresh assignee facts so login matches the live ticket.
                manual = {
                    **manual,
                    'ticketId': row.get('id') or manual.get('ticketId'),
                    'work_order_id': row.get('id') or manual.get('work_order_id'),
                    'ticketStatus': row.get('status') or manual.get('ticketStatus'),
                    'autoAssignment': _assignment_snapshot_from_row(row),
                }
        else:
            # Cannot revalidate without an API environment; keep prior fixture.
            still_ok = True
            stale_reason = ''
        if still_ok:
            return PreDataResolution(
                plan=None,
                template_key=None,
                input_params=manual,
                default_environment_id=env_id,
                source=_CLAIMABLE_MANUAL_SOURCE,
                fail_fast=False,
                skip_reason='',
            )
        logger.info(
            'Claimable fixture stale for testcase_id=%s ticketNo=%s reason=%s; refreshing',
            testcase.id,
            manual.get('ticketNo'),
            stale_reason or 'not_claimable_or_missing',
        )
        try:
            cleared = {
                k: v
                for k, v in (params or {}).items()
                if k
                not in {
                    'ticketNo',
                    'ticketId',
                    'work_order_id',
                    'processingTicketId',
                    'autoAssignment',
                    'ticketStatus',
                    'source',
                }
            }
            testcase.pre_data_params = cleared
            testcase.save(update_fields=['pre_data_params'])
            params = cleared
        except Exception:
            logger.exception(
                'Claimable fixture: failed to clear stale params on testcase_id=%s',
                testcase.id,
            )

    pool = try_pick_claimable_ticket_from_pool(testcase, environment_id=env_id)
    if pool.get('ticketNo'):
        # Persist so helpers bind this ticket even without a DataGenerationRun.
        try:
            testcase.pre_data_params = {
                **(params or {}),
                'ticketId': pool.get('ticketId'),
                'ticketNo': pool.get('ticketNo'),
                'work_order_id': pool.get('work_order_id') or pool.get('ticketId'),
                'autoAssignment': pool.get('autoAssignment') or {},
            }
            testcase.save(update_fields=['pre_data_params'])
        except Exception:
            logger.exception('Claimable pool: failed to persist fixture on testcase_id=%s', testcase.id)
        return PreDataResolution(
            plan=None,
            template_key=None,
            input_params=pool,
            default_environment_id=env_id,
            source=_CLAIMABLE_MANUAL_SOURCE,
            fail_fast=False,
            skip_reason='',
        )

    text = collect_testcase_text(testcase)
    input_params = build_input_params(text, {'input_params': dict(params), 'steps': []})
    plan = DataGenerationPlan.objects.filter(
        project_id=testcase.project_id,
        template_key='biz_create_type_a',
        is_template=True,
        is_active=True,
    ).first()
    return PreDataResolution(
        plan=plan,
        template_key='biz_create_type_a',
        input_params=input_params,
        default_environment_id=env_id or (plan.default_environment_id if plan else None),
        source=_CLAIMABLE_AUTO_SOURCE,
        fail_fast=getattr(testcase, 'pre_data_fail_fast', True),
    )


def resolve_pre_data_for_testcase(testcase: TestCase) -> PreDataResolution:
    if getattr(testcase, 'skip_pre_data', False):
        return PreDataResolution(
            plan=None,
            template_key=None,
            input_params={},
            default_environment_id=None,
            source='skipped',
            fail_fast=False,
            skip_reason='用例已关闭自动造数',
        )

    if is_claimable_ticket_detail_case(testcase):
        return resolve_claimable_ticket_pre_data(testcase)

    if testcase.pre_data_plan_id:
        plan = testcase.pre_data_plan
        if plan.template_key == _LEGACY_CLAIMABLE_TEMPLATE_KEY:
            return resolve_claimable_ticket_pre_data(testcase)
        template_key = plan.template_key
        params = testcase.pre_data_params if isinstance(testcase.pre_data_params, dict) else {}
        env_id = testcase.pre_data_environment_id or (plan.default_environment_id if plan else None)
        return PreDataResolution(
            plan=plan,
            template_key=template_key,
            input_params=params,
            default_environment_id=env_id,
            source='testcase',
            fail_fast=getattr(testcase, 'pre_data_fail_fast', True),
        )

    module_plan, module = resolve_module_pre_data_plan(testcase.module)
    if module_plan is not None and module is not None:
        if module_plan.template_key == _LEGACY_CLAIMABLE_TEMPLATE_KEY:
            return resolve_claimable_ticket_pre_data(testcase)
        template_key = module_plan.template_key
        params = module.pre_data_params if isinstance(module.pre_data_params, dict) else {}
        env_id = module.pre_data_environment_id or (module_plan.default_environment_id if module_plan else None)
        return PreDataResolution(
            plan=module_plan,
            template_key=template_key,
            input_params=params,
            default_environment_id=env_id,
            source='module' if module_plan is not None else 'legacy-template-migrated',
            fail_fast=getattr(module, 'pre_data_fail_fast', True),
        )

    text = collect_testcase_text(testcase)
    if is_overview_dashboard_case(testcase) or is_overview_sla_detail_case(testcase):
        return PreDataResolution(
            plan=None,
            template_key=None,
            input_params={},
            default_environment_id=None,
            source='none',
            fail_fast=False,
            skip_reason='工单总览/SLA 类用例故意不走自动造数，依赖环境预置总览/预警数据',
        )

    if not needs_ticket_pre_data(text):
        return PreDataResolution(
            plan=None,
            template_key=None,
            input_params={},
            default_environment_id=None,
            source='none',
            fail_fast=False,
            skip_reason='未识别到工单类前置数据需求',
        )

    fixture_statuses = required_fixture_statuses(testcase.precondition or '')
    inferred_key = infer_business_template_key(text)
    status_filter_case = (
        inferred_key == 'biz_prepare_status_filter_data'
        and bool(re.search(r'筛选|过滤|查询|检索', testcase.name or ''))
    )
    if (
        not fixture_statuses
        and not status_filter_case
        and inferred_key not in {'biz_create_approval_processing', 'biz_create_and_transfer'}
    ):
        fixture_statuses = _required_statuses_from_navigation_steps(testcase)
    if set(fixture_statuses) == {'已关闭', '已解决'}:
        return PreDataResolution(
            plan=None,
            template_key=_TERMINAL_PAIR_KEY,
            input_params=build_input_params(text, {'input_params': {}, 'steps': []}),
            default_environment_id=None,
            source='terminal_pair',
            fail_fast=True,
        )
    if len(fixture_statuses) > 1:
        status_labels = '、'.join(fixture_statuses)
        return PreDataResolution(
            plan=None,
            template_key=None,
            input_params={},
            default_environment_id=None,
            source='unsupported_fixture',
            fail_fast=True,
            skip_reason=(
                f'用例前置条件需要「{status_labels}」工单；当前单工单模板无法安全准备该组合或已关闭状态，'
                '请配置能创建并验证全部目标状态的造数计划'
            ),
        )

    handover_flow = processing_handover_flow_kind(testcase)
    if handover_flow in _SUBMITTED_HANDOVER_FLOWS:
        template_key = 'biz_create_and_claim'
    elif inferred_key == 'biz_create_approval_processing':
        template_key = inferred_key
    elif fixture_statuses:
        template_key = _FIXTURE_STATUS_TO_TEMPLATE.get(fixture_statuses[0])
    else:
        template_key = inferred_key
    if not template_key:
        return PreDataResolution(
            plan=None,
            template_key=None,
            input_params={},
            default_environment_id=None,
            source='unmatched_ticket_fixture',
            fail_fast=getattr(testcase, 'pre_data_fail_fast', True),
            skip_reason='用例需要工单前置数据，但无法推断造数模板；请明确前置状态或绑定计划',
        )

    # 文本中可能仍保留了旧模板键；将它当作 TYPE_A 创建的别名处理，
    # 不能让显式指定旧键绕过上面对历史计划的兼容保护。
    if template_key == _LEGACY_CLAIMABLE_TEMPLATE_KEY:
        return resolve_claimable_ticket_pre_data(testcase)

    input_params = build_input_params(text, {'input_params': {}, 'steps': []})
    if handover_flow == 'approver_list':
        # The login account in this case is the approver, not the ticket owner.
        # Let the claim-plan schema supply the processing ticket's owner.
        input_params.pop('assigneeName', None)
        input_params.pop('assigneeUserId', None)
    plan = DataGenerationPlan.objects.filter(
        project_id=testcase.project_id,
        template_key=template_key,
        is_template=True,
        is_active=True,
    ).first()

    return PreDataResolution(
        plan=plan,
        template_key=template_key,
        input_params=input_params,
        default_environment_id=plan.default_environment_id if plan else None,
        source='inferred',
        fail_fast=getattr(testcase, 'pre_data_fail_fast', True),
    )


_MESSAGE_SEND_NEGATION_RE = re.compile(
    r'(?:不展示|不显示|未显示|没有|无|隐藏|不可见|不可用|禁用|禁止|不允许|不能|无法)'
    r'.{0,12}(?:发送|消息|按钮|入口|功能)|'
    r'(?:发送|消息|按钮|入口|功能).{0,12}'
    r'(?:不展示|不显示|未显示|不可见|不可用|禁用|禁止|不允许|不能|无法)',
    re.IGNORECASE,
)
_MESSAGE_SEND_ACTION_RE = re.compile(
    r'(?:点击|单击)\s*[「『“【"]?发送|'
    r'按\s*[「『“【"]?Enter.{0,12}发送|'
    r'输入.{0,80}(?:点击|单击|按\s*[「『“【"]?Enter).{0,12}发送|'
    r'(?:发送|输入并发送)(?:一条|文本|图片|文件|消息|内容)|'
    r'发送后.{0,24}(?:沟通|消息|记录|出现|显示|确认|验证)|'
    r'沟通记录.{0,24}(?:出现|显示).{0,24}(?:发送|消息)',
    re.IGNORECASE,
)


def ticket_message_send_steps(testcase: TestCase) -> list[TestCaseStep]:
    """Return only steps that positively instruct sending a message.

    Case titles and arbitrary mentions of message controls are not enough:
    negative visibility/read-only checks must not be routed through the send
    workflow or its post-execution content validator.
    """
    matched = []
    for step in testcase.steps.order_by('step_number'):
        description = (getattr(step, 'description', '') or '').strip()
        expected = (getattr(step, 'expected_result', '') or '').strip()
        text = f'{description}\n{expected}'
        if _MESSAGE_SEND_NEGATION_RE.search(text):
            continue
        if _MESSAGE_SEND_ACTION_RE.search(text):
            matched.append(step)
    return matched


def is_ticket_message_send_case(testcase: TestCase) -> bool:
    """Whether any testcase step explicitly requires a positive message send."""
    return bool(ticket_message_send_steps(testcase))


def is_ticket_detail_boundary_case(testcase: TestCase) -> bool:
    """沟通区只读/空态边界用例；发送消息类不得匹配。"""
    blob = collect_testcase_text(testcase)
    if is_ticket_message_send_case(testcase):
        return False
    return (
        '沟通记录' in blob
        and '处理' in blob
        and ('详情' in blob or '工单详情' in blob)
        and any(
            keyword in blob
            for keyword in ('只读', '不可发送', '无发送', '暂无沟通', '边界')
        )
    )


def is_transfer_ready_ticket_case(testcase: TestCase) -> bool:
    """Pending ticket for UI transfer probes (button/dialog), not already-transferred fixtures."""
    from .intent_router import _needs_transfer_ready_pending_ticket

    return _needs_transfer_ready_pending_ticket(collect_testcase_text(testcase))


def is_transfer_ui_helper_case(testcase: TestCase) -> bool:
    """Whether a case uses the deterministic transfer UI helper (button / dialog)."""
    name = (getattr(testcase, 'name', '') or '').strip()
    return any(
        marker in name
        for marker in (
            '转派按钮可见性',
            '转派弹窗',
        )
    )


def is_processing_handover_case(testcase: TestCase) -> bool:
    """Cases needing a processing ticket for an approval-handover scenario."""
    name = (getattr(testcase, 'name', '') or '').strip()
    blob = collect_testcase_text(testcase)
    if processing_handover_flow_kind(testcase):
        return True
    if '移交审批' in name:
        return True
    return '移交审批' in blob and '处理中' in blob


_SUBMITTED_HANDOVER_FLOWS = frozenset({
    'approval_detail', 'approval_summary', 'approval_button', 'approver_list',
})


def processing_handover_flow_kind(testcase: TestCase) -> str:
    """Classify the required approval state and UI actions from ordered steps."""
    steps = list(testcase.steps.order_by('step_number'))
    descriptions = [str(step.description or '') for step in steps]
    expectations = [str(step.expected_result or '') for step in steps]
    count = len(steps)
    if (
        count == 3
        and '待处理' in f'{testcase.name or ""} {testcase.precondition or ""}'
        and '详情' in descriptions[1]
        and '按钮' in descriptions[2]
        and '不展示' in expectations[2]
        and '移交审批' in expectations[2]
    ):
        return 'pending_no_handover'
    if count == 4 and '移交审批' in ' '.join(descriptions + expectations) and '按钮可见性' in (testcase.name or ''):
        return 'visibility'
    if count == 7 and any('选择审批人' in text for text in descriptions + expectations) and '确认移交' in descriptions[-1]:
        return 'submit_split'
    if count == 4 and '200' in descriptions[2] and '移交审批' in descriptions[2] and '确认移交' in descriptions[3]:
        return 'submit_reason_boundary'
    if count == 4 and '确认移交' in descriptions[2] and '审批详情' in expectations[3]:
        return 'submit_compact'
    if count == 3 and '审批详情' in descriptions[2] and '审批流程' in expectations[2]:
        return 'approval_detail'
    if count == 4 and '审批详情' in descriptions[2] and '工单摘要' in descriptions[3] and '待审批' in expectations[3]:
        return 'approval_summary'
    if count == 3 and '工单号' in descriptions[2] and '待审批' in expectations[2] and '移交审批' in descriptions[2]:
        return 'approver_list'
    if count == 3 and '审批详情' in expectations[2] and '不展示' in expectations[2] and '移交审批' in expectations[2]:
        return 'approval_button'
    if count == 5 and '不选择任何审批人' in descriptions[3] and '确认移交' in descriptions[4]:
        return 'selection_required'
    if count == 5 and '取消' in descriptions[3] and '移交审批' in expectations[4]:
        return 'cancel'
    return ''


def is_processing_handover_visibility_case(testcase: TestCase) -> bool:
    """Only the four-step button-visibility case uses the fixed helper."""
    return processing_handover_flow_kind(testcase) == 'visibility'


def is_processing_handover_submission_case(testcase: TestCase) -> bool:
    """Approval handover submission in either compact or fully split steps."""
    return processing_handover_flow_kind(testcase) in {
        'submit_split', 'submit_compact', 'submit_reason_boundary',
    }


def is_processing_handover_helper_case(testcase: TestCase) -> bool:
    return bool(processing_handover_flow_kind(testcase))


def _resolve_handover_approver_params(
    testcase: TestCase, params: Dict[str, Any], *, environment_id: int,
) -> Dict[str, Any]:
    """Resolve the actual business user ID before submitting approval."""
    import requests

    from api_environments.models import ApiEnvironment
    from api_environments.token_refresh import refresh_environment_tokens

    resolved = dict(params)
    text = collect_testcase_text(testcase)
    named = re.search(r'审批人[「“]?([\u4e00-\u9fff]{2,4})(?=[，。\s、”」]|$)', text)
    name = str(resolved.get('approverName') or (named.group(1) if named else '') or '黎沅欣').strip()
    environment = ApiEnvironment.objects.filter(id=environment_id, project_id=testcase.project_id).first()
    if environment is None:
        raise DataGenerationError('缺少审批造数 API 环境')
    variables = refresh_environment_tokens(
        base_url=environment.base_url,
        variables=environment.get_all_variables(),
        verify_ssl=environment.verify_ssl,
        environment_id=environment.id,
        persist=True,
    )
    token = variables.get('adminToken') or variables.get('accessToken')
    if not token:
        raise DataGenerationError('审批造数缺少管理员查询令牌')
    candidates = []
    for role in ('customer_service', 'product', 'test'):
        try:
            response = requests.get(
                environment.base_url.rstrip('/') + '/api/users/transfer-candidates',
                headers={'Authorization': f'Bearer {token}'},
                params={'keyword': name, 'role': role, 'page': 1, 'pageSize': 100},
                verify=environment.verify_ssl,
                timeout=20,
            )
            response.raise_for_status()
            rows = response.json().get('data')
        except (requests.RequestException, ValueError) as exc:
            logger.warning('Lookup approver %s role=%s failed: %s', name, role, exc)
            continue
        if isinstance(rows, list):
            candidates.extend(
                row for row in rows
                if isinstance(row, dict)
                and str(row.get('name') or '').strip() == name
                and row.get('id') not in (None, '')
                and row.get('isActive', True)
            )
    ids = {str(row['id']) for row in candidates}
    if len(ids) != 1:
        raise DataGenerationError(f'审批人“{name}”匹配到 {len(ids)} 个用户 ID，无法安全提交审批')
    found_id = next(iter(ids))
    if resolved.get('approverUserId') not in (None, '') and str(resolved['approverUserId']) != found_id:
        raise DataGenerationError(f'审批人“{name}”的指定用户 ID 与业务系统不一致')
    resolved['approverUserId'] = int(found_id)
    resolved['approverName'] = name
    resolved.setdefault('comment', '自动化测试准备待审批工单')
    return resolved


def _build_submitted_handover_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Append approval submission after the claim plan has produced its ticket."""
    from copy import deepcopy

    submission = {
        'type': 'api_call',
        'name': '提交审批',
        'interface_ref': 'submit_approval',
        'environment_ref': 'default',
        'variables': {
            'ticketId': '{{ticketId}}',
            'approverUserId': '{{approverUserId}}',
            'approverName': '{{approverName}}',
            'comment': '{{comment}}',
        },
    }
    result: list[dict[str, Any]] = []
    submitted = False
    for original in steps:
        step = deepcopy(original)
        if not submitted and step.get('type') in {'set_env_var', 'set_public_data'}:
            result.append(submission)
            submitted = True
        if step.get('type') == 'set_env_var':
            step.setdefault('variables', {}).update({
                'ticketStatus': 'pending_approval',
                'approvalStatus': 'pending_approval',
                'approverName': '{{approverName}}',
            })
        elif step.get('type') == 'set_public_data':
            step.setdefault('items', []).extend([
                {'key': 'approvalStatus', 'value': 'pending_approval', 'type': 0},
                {'key': 'approverName', 'value': '{{approverName}}', 'type': 0},
            ])
            for item in step['items']:
                if isinstance(item, dict) and item.get('key') == 'ticketStatus':
                    item['value'] = 'pending_approval'
        result.append(step)
    if not submitted:
        result.append(submission)
    return result


def _verify_submitted_handover_fixture(
    run: DataGenerationRun, *, environment_id: int, approver_name: str,
) -> None:
    """Fail before UI execution if the new ticket has no pending approval."""
    import requests

    from api_environments.models import ApiEnvironment
    from api_environments.token_refresh import refresh_environment_tokens

    snapshot = run.output_snapshot if isinstance(run.output_snapshot, dict) else {}
    ticket_no = str(snapshot.get('ticketNo') or '').strip()
    if not ticket_no:
        raise DataGenerationError('提交审批后缺少本次造数工单号')
    environment = ApiEnvironment.objects.filter(id=environment_id, project_id=run.project_id).first()
    if environment is None:
        raise DataGenerationError('提交审批后找不到 API 环境')
    variables = refresh_environment_tokens(
        base_url=environment.base_url,
        variables=environment.get_all_variables(),
        verify_ssl=environment.verify_ssl,
        environment_id=environment.id,
        persist=True,
    )
    token = variables.get('adminToken') or variables.get('accessToken')
    if not token:
        raise DataGenerationError('无法验证审批结果：缺少管理员查询令牌')
    for attempt in range(5):
        try:
            response = requests.get(
                environment.base_url.rstrip('/') + '/api/tickets',
                headers={'Authorization': f'Bearer {token}'},
                params={'ticketNo': ticket_no, 'page': 1, 'pageSize': 5},
                verify=environment.verify_ssl,
                timeout=20,
            )
            response.raise_for_status()
            rows = response.json().get('data')
        except (requests.RequestException, ValueError) as exc:
            raise DataGenerationError('无法查询已提交审批工单状态') from exc
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or str(row.get('ticketNo') or '') != ticket_no:
                continue
            status = str(row.get('status') or '').lower()
            approval = str(row.get('approvalStatus') or '').lower()
            if status == 'pending_approval' or approval in {'pending', 'pending_approval'}:
                snapshot.update({
                    'ticketStatus': row.get('status'),
                    'approvalStatus': row.get('approvalStatus') or 'pending_approval',
                    'approverName': approver_name,
                })
                run.output_snapshot = snapshot
                run.save(update_fields=['output_snapshot'])
                return
        if attempt < 4:
            time.sleep(0.8)
    raise DataGenerationError(f'工单 {ticket_no} 未进入待审批状态，已停止 UI 执行')


def _reusable_processing_handover_row(
    row: Dict[str, Any], *, ticket_no: str, assignee_id: Any, assignee_name: str,
) -> bool:
    """Reuse only this case's still-actionable processing ticket."""
    if str(row.get('ticketNo') or '') != ticket_no:
        return False
    if str(row.get('status') or '') not in {'processing', 'claimed'}:
        return False
    if str(row.get('approvalStatus') or 'none') != 'none':
        return False
    if assignee_id not in (None, '') and str(row.get('assigneeUserId')) != str(assignee_id):
        return False
    if assignee_name and str(row.get('assigneeName') or '') != assignee_name:
        return False
    actions = row.get('availableActions')
    return not isinstance(actions, list) or 'submit_approval' in actions


def _reuse_processing_handover_pre_data(
    testcase: TestCase, resolution: PreDataResolution,
) -> Optional[TestcasePreDataResult]:
    """Retry an unfinished handover on its own fixture instead of stacking creator locks."""
    from testcases.models import TestCaseRunRecord

    env_id = resolution.default_environment_id or (
        resolution.plan.default_environment_id if resolution.plan else None
    )
    if not env_id:
        return None
    params = apply_params_schema_defaults(
        resolution.input_params,
        resolution.plan.template_params_schema if resolution.plan else {},
    )
    desired_id = params.get('assigneeUserId')
    desired_name = str(params.get('assigneeName') or '').strip()
    records = (
        TestCaseRunRecord.objects.select_related('data_generation_run')
        .filter(
            testcase_id=testcase.id,
            data_generation_run__status=DataGenerationRun.STATUS_SUCCESS,
            data_generation_run__plan__template_key='biz_create_and_claim',
        )
        .order_by('-id')[:5]
    )
    for record in records:
        run = record.data_generation_run
        snapshot = run.output_snapshot if isinstance(run.output_snapshot, dict) else {}
        ticket_no = str(snapshot.get('ticketNo') or '').strip()
        if not ticket_no:
            continue
        rows = _list_tickets_from_api(
            project_id=testcase.project_id,
            environment_id=int(env_id),
            query_path=f'/api/tickets?ticketNo={ticket_no}&page=1&pageSize=5',
        )
        if not any(
            _reusable_processing_handover_row(
                row, ticket_no=ticket_no,
                assignee_id=desired_id, assignee_name=desired_name,
            )
            for row in rows
        ):
            continue
        resolution.source = 'reused-processing-handover'
        return TestcasePreDataResult(
            run=run,
            resolution=resolution,
            message_suffix=(
                '\n【复用本用例未提交审批的处理中测试工单】'
                f'\n- 本次工单号: {ticket_no}'
                '\n- 已校验工单仍由目标处理人处理，且尚未提交审批。'
            ),
        )
    return None


def is_complete_ticket_ui_case(testcase: TestCase) -> bool:
    """Processing ticket detail → click 工单完成 and assert confirm dialog."""
    from .intent_router import _needs_complete_button_ui_fixture

    # A whole ticket state-machine scenario is not a completion-dialog case.
    # Sending it to the focused helper would execute the wrong workflow.
    descriptions = ' '.join(
        str(step.description or '') for step in testcase.steps.all()
    )
    other_transitions = ('分配工单', '领取工单', '移交审批', '审批通过', '关闭工单')
    if sum(marker in descriptions for marker in other_transitions) >= 2:
        return False
    return _needs_complete_button_ui_fixture(collect_testcase_text(testcase))


def transfer_ui_helper_flow(testcase: TestCase) -> str:
    """visibility = button-only; dialog = select user + reason + double confirm."""
    name = (getattr(testcase, 'name', '') or '').strip()
    if '转派按钮可见性' in name:
        return 'visibility'
    return 'dialog'


def is_claimable_ticket_detail_case(testcase: TestCase) -> bool:
    """Whether a case belongs to the deterministic claim lifecycle helper.

    These five lifecycle cases share reliable navigation and generated data,
    but have explicit branches for opening the detail, checking dialog copy,
    cancelling, confirming, and checking list synchronisation.  Message and
    external-system cases deliberately do not use it.
    """
    name = (getattr(testcase, 'name', '') or '').strip()
    return any(
        marker in name
        for marker in (
            '待处理工单-列表点击处理进入详情页',
            '点击领取-弹出确认领取弹窗文案正确',
            '确认领取-点击取消',
            '确认领取-点击确定',
            '领取成功后-列表当前状态同步为处理中',
        )
    )


def _latest_pre_data_snapshot(testcase: TestCase) -> dict:
    manual = extract_manual_ticket_fixture(
        testcase.pre_data_params if isinstance(testcase.pre_data_params, dict) else {},
    )
    if manual.get('ticketNo'):
        return manual
    plan_id = getattr(testcase, 'pre_data_plan_id', None)
    queryset = DataGenerationRun.objects.filter(
        project_id=testcase.project_id,
        trigger_type=DataGenerationRun.TRIGGER_CASE_PRE,
        status=DataGenerationRun.STATUS_SUCCESS,
    )
    if plan_id:
        queryset = queryset.filter(plan_id=plan_id)
    run = queryset.order_by('-id').first()
    return run.output_snapshot if run and isinstance(run.output_snapshot, dict) else {}


def get_pre_data_snapshot_for_execution(
    testcase: TestCase,
    *,
    chat_session_id: Optional[str] = None,
) -> dict:
    """Resolve the ticket snapshot helpers should bind for this execution.

    Manual/pool claimable fixtures live on ``testcase.pre_data_params`` and do
    not create a ``DataGenerationRun``. Prefer that fixture over any older run
    attached to the chat session, otherwise helpers claim a locked initiator.
    """
    params = testcase.pre_data_params if isinstance(testcase.pre_data_params, dict) else {}
    manual = extract_manual_ticket_fixture(params)
    if manual.get('ticketNo'):
        return manual

    if chat_session_id:
        from testcases.models import TestCaseRunRecord

        record = (
            TestCaseRunRecord.objects.select_related('data_generation_run')
            .filter(session_id=str(chat_session_id).strip(), testcase_id=testcase.id)
            .order_by('-id')
            .first()
        )
        run = getattr(record, 'data_generation_run', None) if record else None
        if run and isinstance(run.output_snapshot, dict) and run.output_snapshot.get('ticketNo'):
            return run.output_snapshot

    return _latest_pre_data_snapshot(testcase)


def get_latest_pre_data_ticket_no(
    testcase: TestCase,
    *,
    chat_session_id: Optional[str] = None,
) -> str:
    snapshot = get_pre_data_snapshot_for_execution(
        testcase,
        chat_session_id=chat_session_id,
    )
    return str(snapshot.get('ticketNo') or '').strip()


def get_latest_pre_data_ticket_id(
    testcase: TestCase,
    *,
    chat_session_id: Optional[str] = None,
) -> str:
    snapshot = get_pre_data_snapshot_for_execution(
        testcase,
        chat_session_id=chat_session_id,
    )
    for key in ('ticketId', 'work_order_id', 'processingTicketId'):
        value = snapshot.get(key)
        if value not in (None, ''):
            return str(value).strip()
    return ''


def build_ticket_detail_case_hints(testcase: TestCase) -> str:
    if not is_ticket_detail_boundary_case(testcase):
        return ''
    return '\n'.join(
        [
            '',
            '【工单详情类用例 — 专用 helper】',
            '- 每步只调用 `await helpers.runTicketDetailCaseStep(page, <步骤号>);`',
            '- 该 helper 已包含：步骤3仅查询列表、步骤4点「处理」进详情、步骤5检查沟通区只读，并完成截图',
            '- ticketNo 由造数注入环境变量 WHARTTEST_TICKET_NO，禁止手写或编造工单号',
            '- 禁止在本用例中混用 fillFilterField + screenshotCaseStep 自行组合',
            '- 禁止 getByText(\'沟通记录\') 单点 waitFor；步骤5 由 helper 断言只读空态',
        ]
    )


def build_row_action_evidence_hints(steps) -> str:
    """Require a detail-page assertion before screenshotting a row-action step.

    A list screenshot after clicking an action is not evidence that the detail
    page loaded.  This stays data-driven so every case expecting the same
    action receives the same evidence rule.
    """
    claim_steps = [
        step for step in steps
        if '领取工单' in (getattr(step, 'expected_result', '') or '')
    ]
    if not claim_steps:
        return ''
    lines = ['', '【列表进入详情取证 — 必须执行】']
    for step in claim_steps:
        step_number = getattr(step, 'step_number', '?')
        lines.extend([
            f'- 步骤{step_number}：列表行操作成功后，先执行 '
            "`await helpers.assertPageShows(page, ['领取工单']);`，确认已进入详情页且按钮可见。",
            f'- 仅确认成功后，执行 `await helpers.screenshotCaseStep(page, {step_number});`。'
            '截图必须包含详情页和“领取工单”按钮；列表页截图不能作为该步骤证据。',
            '- 找不到该按钮时输出 RESULT=FAIL 并停止，禁止截列表页后报通过。',
        ])
    return '\n'.join(lines)


def build_testcase_step_script_hints(testcase: TestCase) -> str:
    """Generic execution hints. Do not hardcode a product or case script here."""
    if (
        is_claimable_ticket_detail_case(testcase)
        or is_ticket_detail_boundary_case(testcase)
        or is_overview_sla_detail_case(testcase)
    ):
        return '\n'.join([
            '',
            '【执行方式】本用例已匹配专用 helper；仅执行后续“固定 Playwright 脚本”中的单行调用。',
            '不要自行组合筛选、按编号查询、行操作或截图脚本。',
        ])
    lines = [
            '',
            '【执行脚本指南 — 通用】',
            '- 步骤1若是登录：该步只调用 `await helpers.loginStep1(page);`；它已包含步骤1截图，禁止再调 screenshotCaseStep；账号取自用例前置条件，禁止改用其他账号',
            '- 登录只有 stdout 出现 `RESULT=PASS` 才能判通过；出现 `RESULT=FAIL` 必须停止，禁止继续执行菜单步骤',
            '- 点菜单进入页面：`await helpers.navigateByMenu(page, \'步骤里的菜单名\');`；仅在明确知道目标路由时传唯一 URL 片段，禁止用 `/work-order` 这类父级公共前缀',
            '- 普通下拉：`await helpers.selectFormDropdownOption(page, \'字段标签\', \'选项\');`',
            '- 筛选项填值：`await helpers.fillFilterField(page, \'字段标签\', \'值\');`',
            '- 弹窗多选：`await helpers.selectDialogMultiSelect(page, \'字段标签\', \'选项名\');`',
            '- 点按钮：`await helpers.clickPageButton(page, \'按钮名\');`（会先关遮罩/侧栏）',
            '- 列表点操作列：`await helpers.clickRowAction(page, \'行内文本\', \'按钮名\');`',
            '- 每步截图：`await helpers.screenshotCaseStep(page, <步骤号>);` 系统自动上传',
            '- 断言用 .first() / getByRole，避免 getByText 命中多个节点被判失败',
            '- 禁止 expect()；loginStep1 输出 RESULT=FAIL 时该步骤必须判失败，不得继续',
            '- stdout 出现 RESULT=PASS: 步骤N 即该步通过，以 PASS 为准',
            '- 「xx标签」指页面上的徽章文案（如高/中/低），不要只搜标签名字本身',
            '- 执行结果由系统自动保存；禁止调用 whart_tools 或其他工具更新用例执行结果',
            '- 禁止手写路径、禁止 upload_screenshot、禁止 Python 风格 goto/fill/click',
    ]
    lines.extend(build_row_action_evidence_hints(testcase.steps.order_by('step_number')).splitlines())
    lines.extend(build_ticket_detail_case_hints(testcase).splitlines())
    lines.extend(build_message_send_case_hints(testcase).splitlines())
    lines.extend(build_overview_sla_detail_case_hints(testcase).splitlines())
    return '\n'.join(lines)


def extract_expected_send_message_texts_by_step(testcase: TestCase) -> dict[int, list[str]]:
    """Quoted payloads to assert, grouped by the step that sends them."""
    found: dict[int, list[str]] = {}
    for step in ticket_message_send_steps(testcase):
        blob = f"{getattr(step, 'description', '') or ''}\n{getattr(step, 'expected_result', '') or ''}"
        try:
            step_number = int(step.step_number)
        except (AttributeError, TypeError, ValueError):
            continue
        for match in re.findall(r'[「『“\"]([^」』”\"]+)[」』”\"]', blob):
            text = (match or '').strip()
            if len(text) < 4:
                continue
            if text in ('发送', '查询', '处理', '待处理', '未分配', '领取工单'):
                continue
            texts = found.setdefault(step_number, [])
            if text not in texts:
                texts.append(text)
    return found


def extract_expected_send_message_texts(testcase: TestCase) -> list[str]:
    """Quoted message texts that send-message steps must assert on page."""
    found = []
    for texts in extract_expected_send_message_texts_by_step(testcase).values():
        for text in texts:
            if text not in found:
                found.append(text)
    return found


def build_message_send_case_hints(testcase: TestCase) -> str:
    if not is_ticket_message_send_case(testcase):
        return ''
    messages = extract_expected_send_message_texts(testcase)
    sample = messages[0] if messages else '步骤中的测试文本'
    lines = [
        '',
        '【沟通消息发送用例 — 强制校验，禁止只进详情】',
        '- 禁止调用 `runTicketDetailCaseStep`（那是沟通区只读边界用例，不是发消息）。',
        '- 必须先进入可发消息的工单详情（已领取/处理中），再在输入框填入步骤要求的文本并点击「发送」或按 Enter。',
        f"- 发送后必须执行 `await helpers.assertPageShows(page, ['{sample}']);`，"
        '确认沟通记录出现该文本；否则 RESULT=FAIL，禁止报通过。',
        '- 截图必须包含沟通记录中刚发送的文本；仅详情页/空沟通区不能作为步骤通过证据。',
    ]
    return '\n'.join(lines)


def build_overview_sla_detail_case_hints(testcase: TestCase) -> str:
    if not is_overview_sla_detail_case(testcase):
        return ''
    return '\n'.join(
        [
            '',
            '【工单总览-SLA进详情类用例 — 专用 helper】',
            '- 每步只调用 `await helpers.runOverviewSlaDetailCaseStep(page, <步骤号>);`',
            '- 步骤3会点击 SLA 预警明细第一行蓝色工单号并等待详情页加载',
            '- 步骤4会校验详情页工单号与点击链接一致',
            '- 本用例故意不走自动造数；失败时报告写「环境缺少 SLA 预警预置数据」，禁止建议补充造数脚本',
            '- 禁止混用 runOverviewCaseStep / screenshotCaseStep 自行组合',
        ]
    )


def build_overview_sla_detail_navigation_hint(testcase: TestCase) -> str:
    if not is_overview_sla_detail_case(testcase):
        return ''
    step_count = testcase.steps.count()
    lines = [
        '',
        '【固定 Playwright 脚本 — 禁止改写】',
        '全程 session_id 不变；每步只执行下面一行 JavaScript：',
    ]
    for step in testcase.steps.order_by('step_number'):
        lines.append(
            f'- 步骤{step.step_number}: '
            f'`await helpers.runOverviewSlaDetailCaseStep(page, {step.step_number});`'
        )
    lines.extend(
        [
            '- 禁止自行组合 getByText / screenshotCaseStep / runOverviewCaseStep',
            f'- 本用例共 {step_count} 步，每步单独一次 execute_skill_script',
        ]
    )
    return '\n'.join(lines)


def build_testcase_navigation_hint(testcase: TestCase) -> str:
    """Inject fixed playwright scripts for specialized case patterns."""
    sla_hint = build_overview_sla_detail_navigation_hint(testcase)
    if sla_hint:
        return sla_hint
    if is_claimable_ticket_detail_case(testcase):
        lines = [
            '',
            '【待处理已分配工单→领取 — 固定 Playwright 脚本，禁止改写】',
            '全程 session_id 不变；每步只执行下面一行 JavaScript：',
        ]
        for step in testcase.steps.order_by('step_number'):
            lines.append(
                f'- 步骤{step.step_number}: `await helpers.runClaimableTicketCaseStep(page, {step.step_number});`'
            )
        lines.extend([
            '- 系统会先自动分配处理人；helper 会自动切换到该处理人的已配置账号，并从「我的工单」进入详情，步骤4须确认「领取工单」后才会截图。',
            '- helper 返回 null 或输出 RESULT=FAIL 时立即判不通过，禁止自行补截图或改报通过。',
        ])
        return '\n'.join(lines)
    if is_transfer_ui_helper_case(testcase):
        flow = transfer_ui_helper_flow(testcase)
        step_count = testcase.steps.count()
        lines = [
            '',
            '【转派 UI — 固定 Playwright 脚本，禁止改写】',
            '全程 session_id 不变；每步只执行下面一行 JavaScript：',
        ]
        for step in testcase.steps.order_by('step_number'):
            lines.append(
                f'- 步骤{step.step_number}: `await helpers.runTransferTicketCaseStep(page, {step.step_number});`'
            )
        if flow == 'visibility':
            lines.append(
                '- helper 以自动分配处理人登录，从「我的工单」进详情，校验橙色「转派工单」按钮可见。'
            )
        else:
            lines.append(
                '- helper 以自动分配处理人登录，打开转派弹窗，选择目标用户、填写原因并完成二次确认。'
            )
        lines.extend(
            [
                '- helper 返回 null 或输出 RESULT=FAIL 时立即判不通过，禁止自行补截图或改报通过。',
                f'- 本用例共 {step_count} 步，禁止改用 runClaimableTicketCaseStep 或自行拼装步骤。',
            ]
        )
        return '\n'.join(lines)
    if is_processing_handover_helper_case(testcase):
        step_count = testcase.steps.count()
        flow = processing_handover_flow_kind(testcase)
        fixture_state = (
            '待处理且未移交审批' if flow == 'pending_no_handover'
            else '待审批' if flow in _SUBMITTED_HANDOVER_FLOWS
            else '处理中且尚未提交审批'
        )
        lines = [
            '',
            f'【移交审批生命周期：{fixture_state} — 固定 Playwright 脚本，禁止改写】',
            '全程 session_id 不变；每步只执行下面一行 JavaScript：',
        ]
        for step in testcase.steps.order_by('step_number'):
            lines.append(
                f'- 步骤{step.step_number}: `await helpers.runProcessingHandoverCaseStep(page, {step.step_number});`'
            )
        if flow == 'pending_no_handover':
            lines.append('- 步骤2进入本次待处理工单详情；步骤3检查右上角不显示「移交审批」，并截取详情页。')
            lines.append('- 禁止改用 runClaimableTicketCaseStep；它的步骤3停留在列表筛选页。')
        else:
            lines.extend([
                '- helper 使用本次造数工单号定位；待审批用例的前置数据会先完成提交审批。',
                '- helper 根据实际步骤执行提交、审批详情、审批人列表、输入边界或取消/校验流程。',
                '- 禁止改用 runClaimableTicketCaseStep（那会按「待处理」筛选，必然找不到已领取工单）。',
            ])
        lines.extend([
            '- helper 返回 null 或输出 RESULT=FAIL 时立即判不通过，禁止自行补截图或改报通过。',
            f'- 本用例共 {step_count} 步。',
        ])
        return '\n'.join(lines)
    if is_complete_ticket_ui_case(testcase):
        step_count = testcase.steps.count()
        lines = [
            '',
            '【处理中工单→工单完成 — 按用例步骤分派 Playwright 动作】',
            '全程 session_id 不变；每步只执行下面一行 JavaScript：',
        ]
        for step in testcase.steps.order_by('step_number'):
            lines.append(
                f'- 步骤{step.step_number}: `await helpers.runCompleteTicketCaseStep(page, {step.step_number});`'
            )
        lines.extend(
            [
                '- helper 使用本次造数工单号，并依据每一步描述分别执行进入详情、弹窗、确定/取消及状态校验；不得跳过步骤。',
                '- 禁止改用 runClaimableTicketCaseStep（会按「待处理」筛选，截图停在列表页）。',
                '- helper 返回 null 或输出 RESULT=FAIL 时立即判不通过，禁止自行补截图或改报通过。',
                f'- 本用例共 {step_count} 步。',
            ]
        )
        return '\n'.join(lines)
    if not is_ticket_detail_boundary_case(testcase):
        return ''
    step_count = testcase.steps.count()
    lines = [
        '',
        '【固定 Playwright 脚本 — 禁止改写】',
        '全程 session_id 不变；每步只执行下面一行 JavaScript：',
    ]
    for step in testcase.steps.order_by('step_number'):
        lines.append(
            f'- 步骤{step.step_number}: `await helpers.runTicketDetailCaseStep(page, {step.step_number});`'
        )
    lines.extend(
        [
            '- 禁止自行组合 fillFilterField / clickRowAction / screenshotCaseStep',
            '- 禁止 getByText(\'沟通记录\')；步骤5 由 helper 检查只读空态',
            f'- 本用例共 {step_count} 步，每步单独一次 execute_skill_script',
        ]
    )
    return '\n'.join(lines)


def build_testcase_navigation_hint_by_id(testcase_id: int) -> str:
    testcase = TestCase.objects.filter(id=testcase_id).first()
    if testcase is None:
        return ''
    return build_testcase_navigation_hint(testcase)


def build_testcase_detail_suffix(testcase: TestCase) -> str:
    steps = [
        {
            'step_number': step.step_number,
            'description': step.description or '',
            'expected_result': step.expected_result or '',
        }
        for step in testcase.steps.order_by('step_number')
    ]
    lines = [
        '',
        '【已注入用例详情 — 禁止再调用 get_testcase_detail / get_testcases】',
        f'- project_id: {testcase.project_id}',
        f'- case_id: {testcase.id}',
        f'- 名称: {testcase.name or ""}',
        f'- 等级: {testcase.level or ""}',
        f'- 前置条件: {testcase.precondition or "无"}',
        '- 测试步骤:',
        json.dumps(steps, ensure_ascii=False, indent=2),
        build_testcase_step_script_hints(testcase),
        '',
        '请直接从步骤 1 开始用 playwright-skill 执行，不要先查项目/模块/用例列表。',
        '步骤1若含登录：只调用 await helpers.loginStep1(page);（已含截图，禁止再调用 screenshotCaseStep(page, 1)）',
        '步骤N截图：await helpers.screenshotCaseStep(page, <步骤号>); 系统自动上传，禁止 upload_screenshot。',
        'Playwright 必须是 JavaScript，禁止 Python 风格 goto/fill/click。',
    ]
    return '\n'.join(lines)


def build_testcase_detail_suffix_by_id(testcase_id: int) -> str:
    testcase = (
        TestCase.objects.prefetch_related('steps')
        .filter(id=testcase_id)
        .first()
    )
    if testcase is None:
        return ''
    return build_testcase_detail_suffix(testcase)


def _build_message_suffix(
    run: DataGenerationRun,
    resolution: PreDataResolution,
    *,
    case_text: str = '',
) -> str:
    snapshot = run.output_snapshot if isinstance(run.output_snapshot, dict) else {}
    continued = [
        entry.get('name') or f"步骤{entry.get('index')}"
        for entry in (run.step_logs or [])
        if isinstance(entry, dict) and entry.get('status') == 'failed_continued'
    ]
    lines = [
        '',
        '【系统自动准备的测试数据】',
        f'- 造数来源: {resolution.source}',
        f'- 造数计划: {run.plan.name if run.plan_id else "-"}',
        f'- 造数结果: {run.status}',
    ]
    if continued:
        lines.append(f'- 部分步骤失败（已忽略）: {"、".join(str(name) for name in continued)}')
    if run.error_message:
        lines.append(f'- 造数说明: {run.error_message}')
    if snapshot:
        lines.append('- 数据快照:')
        lines.append(json.dumps(snapshot, ensure_ascii=False, indent=2))
    automatic_assignment = snapshot.get('autoAssignment') if isinstance(snapshot, dict) else None
    if isinstance(automatic_assignment, dict):
        assignee_name = automatic_assignment.get('assigneeName') or '-'
        assignee_id = automatic_assignment.get('assigneeUserId') or '-'
        status = automatic_assignment.get('status') or '-'
        lines.append(
            f'- 自动分配结果: 状态={status}，处理人={assignee_name}（ID: {assignee_id}）。'
            '请按该实际分配结果验证自动分配规则；不要再按“未分配/领取工单”流程执行。'
        )
    ticket_no = snapshot.get('ticketNo')
    fixed_claimable_flow = (
        '待处理' in case_text and '领取工单' in case_text
    )
    if ticket_no and needs_ticket_row_action(case_text):
        if fixed_claimable_flow:
            lines.append(
                f'- 本次造数工单号: {ticket_no}。专用 helper 会以自动分配的处理人登录，'
                '按工单号在「我的工单 → 待处理」列表定位并进入领取详情。'
            )
            return '\n'.join(lines)
        lines.append(
            f'- 可用数据标识: {ticket_no}。'
            f'若步骤需要按编号定位列表行，用 `await helpers.fillFilterField(page, \'步骤里的筛选字段名\', \'{ticket_no}\');` 、'
            f'`await helpers.clickPageButton(page, \'步骤里的查询按钮名\');` 、'
            f'`await helpers.clickRowAction(page, \'{ticket_no}\', \'步骤里的操作按钮名\');`。'
            '列表状态列以页面为准；摘要/主题里的状态词不是状态列。'
            '不要先筛状态再找目标编号，以免把目标行筛掉。'
        )
        lines.append(
            '请优先使用上述数据满足前置条件；若按用例筛选后找不到目标数据，改用编号查询。'
        )
    elif ticket_no:
        lines.append(
            f'- 可用数据标识: {ticket_no}。仅在步骤需要定位该数据时使用；'
            '步骤未要求筛选或行操作时，不要用这条数据去点列表。'
        )
    return '\n'.join(lines)


def _build_manual_claimable_suffix(snapshot: Dict[str, Any]) -> str:
    lines = [
        '',
        '【手工/fixture 前置数据 — 待处理已分配领单用例】',
        '- 来源: 用例 pre_data_params（非 TYPE_A 自动造数）',
        '- 数据快照:',
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        '- 执行时 helper 会在「工单列表 + 待处理」筛选下使用该工单号。',
    ]
    ticket_no = snapshot.get('ticketNo')
    if ticket_no:
        lines.append(f'- 本次工单号: {ticket_no}')
    return '\n'.join(lines)


def _build_claimable_pool_suffix(snapshot: Dict[str, Any]) -> str:
    lines = [
        '',
        '【系统自动分配后的可领取工单前置数据】',
        '- 来源: 本次自动造数及自动分配',
        '- 数据快照:',
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        '- 执行时 helper 会在「工单列表 + 待处理」筛选下使用该工单号，并校验“领取工单”按钮。',
    ]
    if snapshot.get('ticketNo'):
        lines.append(f'- 本次工单号: {snapshot["ticketNo"]}')
    return '\n'.join(lines)


def _enrich_auto_assignment_snapshot(
    snapshot: Dict[str, Any],
    *,
    project_id: int,
    environment_id: Optional[int],
) -> Dict[str, Any]:
    """Record the target system's actual assignment result after creation."""
    ticket_no = snapshot.get('ticketNo')
    if not ticket_no or not environment_id:
        return snapshot
    rows = _list_tickets_from_api(
        project_id=project_id,
        environment_id=int(environment_id),
        query_path=f'/api/tickets?ticketNo={ticket_no}&page=1&pageSize=5',
    )
    row = next((item for item in rows if str(item.get('ticketNo') or '') == str(ticket_no)), None)
    if row is None:
        return snapshot
    return {
        **snapshot,
        'autoAssignment': _assignment_snapshot_from_row(row),
    }


def _resolve_transfer_assignee_params(
    input_params: Dict[str, Any],
    *,
    project_id: int,
    environment_id: Optional[int],
) -> Dict[str, Any]:
    """解析转派造数里的当前处理人与转派目标的 userId。"""
    from .assignee_resolver import AssigneeResolutionError, resolve_assignee_params
    from .intent_router import (
        _DEFAULT_TRANSFER_SOURCE_NAME,
        _pick_distinct_transfer_target,
    )

    params = dict(input_params or {})
    env_id = environment_id

    source_name = str(params.get('sourceAssigneeName') or '').strip()
    target_name = str(params.get('assigneeName') or '').strip()
    if not source_name:
        source_name = _DEFAULT_TRANSFER_SOURCE_NAME
        params['sourceAssigneeName'] = source_name
    if not target_name or target_name == source_name:
        target_name = _pick_distinct_transfer_target(source_name)
        params['assigneeName'] = target_name
        params.pop('assigneeUserId', None)

    if target_name:
        params = resolve_assignee_params(
            params,
            project_id=project_id,
            environment_id=env_id,
        )

    if source_name:
        source_seed = {
            'assigneeName': source_name,
            'assigneeRole': params.get('sourceAssigneeRole')
            or params.get('assigneeRole')
            or 'customer_service',
            'assigneeDepartment': params.get('sourceAssigneeDepartment') or '',
        }
        try:
            resolved = resolve_assignee_params(
                source_seed,
                project_id=project_id,
                environment_id=env_id,
            )
        except AssigneeResolutionError as exc:
            raise DataGenerationError(str(exc)) from exc
        params['sourceAssigneeUserId'] = resolved['assigneeUserId']
        params['sourceAssigneeName'] = resolved['assigneeName']
        params['sourceAssigneeDepartment'] = resolved.get('assigneeDepartment') or params.get(
            'sourceAssigneeDepartment',
        )
        params['sourceAssigneeRole'] = resolved.get('assigneeRole') or params.get('sourceAssigneeRole')

    source_id = params.get('sourceAssigneeUserId')
    target_id = params.get('assigneeUserId')
    if source_id not in (None, '') and target_id not in (None, '') and str(source_id) == str(target_id):
        raise DataGenerationError(
            f'转派造数当前处理人与目标相同（{params.get("sourceAssigneeName")}），'
            f'请在用例中指定不同的转派对象，避免 TRANSFER_TO_SELF'
        )

    # assigneeUserId is also used by the executor for the ticket's current
    # assignee and is refreshed after the assign/claim steps. Preserve the
    # transfer destination separately so the later transfer step cannot be
    # turned into a self-transfer.
    params['transferTargetUserId'] = params.get('assigneeUserId')
    params['transferTargetName'] = params.get('assigneeName')
    params['transferTargetRole'] = params.get('assigneeRole') or 'customer_service'

    return params


def _normalize_assignment_workflow_ticket_type(
    template_key: Optional[str],
    input_params: Dict[str, Any],
) -> Dict[str, Any]:
    """Normalize ticket types for assignment-related pre-data templates.

    - assign / transfer / resolve: force TYPE_A (assignment workflow).
    - claim / 处理中 / 移交审批: force TYPE_C so auto-assign lands on a holder
      whose API login works (e.g. 林业丰), then transfer+claim to the target.
      TYPE_A often lands on 张岩, whose UI password may not work on /api/auth/login.
      Transfer fixtures use an administrator transfer to place that new ticket
      with the source assignee, without logging in as 张岩.
    """
    params = dict(input_params or {})
    if template_key in {
        'biz_create_and_assign',
        'biz_create_and_transfer',
        'biz_create_assign_resolve',
        'biz_create_assign_close',
    }:
        params['ticketType'] = 'TYPE_A'
    elif template_key == 'biz_create_and_claim':
        # TYPE_A 常被自动分给张岩，而其 API 登录不可用；TYPE_C 会落到可登录的
        # 林业丰等处理人，再经转派+领取落到目标处理人（如李亮）。
        params['ticketType'] = 'TYPE_C'
    return params


def _run_terminal_pair_pre_data(testcase: TestCase, resolution: PreDataResolution, *, triggered_by=None) -> TestcasePreDataResult:
    """Compose two reusable single-ticket plans for closed + resolved cases."""
    resolved_plan = ensure_project_template_plan(
        project_id=testcase.project_id,
        template_key='biz_create_assign_resolve',
        created_by=triggered_by or testcase.creator,
    )
    if resolved_plan is None or not resolved_plan.default_environment_id:
        return TestcasePreDataResult(
            run=None, resolution=resolution, blocked=True,
            block_message='前置数据准备失败: 缺少已解决工单计划或其 API 环境',
        )
    env_id = resolved_plan.default_environment_id
    closed_plan = ensure_project_template_plan(
        project_id=testcase.project_id,
        template_key='biz_create_assign_close',
        created_by=triggered_by or testcase.creator,
        default_environment_id=env_id,
    )
    if closed_plan is None:
        return TestcasePreDataResult(
            run=None, resolution=resolution, blocked=True,
            block_message='前置数据准备失败: 缺少已关闭工单计划或关闭接口绑定',
        )
    base_params = dict(resolution.input_params or {})
    base_summary = str(base_params.get('summary') or '终态验证工单')[:14]
    closed_params = _normalize_assignment_workflow_ticket_type(
        closed_plan.template_key, {**base_params, 'summary': f'{base_summary}闭单'[:20]},
    )
    resolved_params = _normalize_assignment_workflow_ticket_type(
        resolved_plan.template_key, {**base_params, 'summary': f'{base_summary}解单'[:20]},
    )
    closed_run = execute_plan(
        closed_plan, trigger_type=DataGenerationRun.TRIGGER_CASE_PRE,
        input_params=closed_params, triggered_by=triggered_by,
        default_environment_id=env_id,
    )
    if closed_run.status != DataGenerationRun.STATUS_SUCCESS:
        return TestcasePreDataResult(
            run=closed_run, resolution=resolution, blocked=True,
            block_message=f'前置数据准备失败: 已关闭样本: {closed_run.error_message or "造数失败"}',
        )
    resolved_run = execute_plan(
        resolved_plan, trigger_type=DataGenerationRun.TRIGGER_CASE_PRE,
        input_params=resolved_params, triggered_by=triggered_by,
        default_environment_id=env_id, parent_run=closed_run,
    )
    if resolved_run.status != DataGenerationRun.STATUS_SUCCESS:
        return TestcasePreDataResult(
            run=resolved_run, resolution=resolution, blocked=True,
            block_message=f'前置数据准备失败: 已解决样本: {resolved_run.error_message or "造数失败"}',
        )
    closed_snapshot = closed_run.output_snapshot if isinstance(closed_run.output_snapshot, dict) else {}
    resolved_snapshot = resolved_run.output_snapshot if isinstance(resolved_run.output_snapshot, dict) else {}
    closed_no = str(closed_snapshot.get('ticketNo') or '').strip()
    resolved_no = str(resolved_snapshot.get('ticketNo') or '').strip()
    if not closed_no or not resolved_no or closed_no == resolved_no:
        return TestcasePreDataResult(
            run=resolved_run, resolution=resolution, blocked=True,
            block_message='前置数据准备失败: 双状态样本的工单号缺失或重复',
        )
    resolved_run.output_snapshot = {
        **resolved_snapshot,
        'closedTicketId': closed_snapshot.get('ticketId'),
        'closedTicketNo': closed_no,
        'resolvedTicketId': resolved_snapshot.get('ticketId'),
        'resolvedTicketNo': resolved_no,
    }
    resolved_run.save(update_fields=['output_snapshot'])
    resolution.plan = resolved_plan
    return TestcasePreDataResult(
        run=resolved_run, resolution=resolution,
        message_suffix=(
            '\n【系统自动准备的两张独立工单】'
            f'\n- 已关闭工单号: {closed_no}（只用于用例中“已关闭”步骤）'
            f'\n- 已解决工单号: {resolved_no}（只用于用例中“已解决”步骤）'
            '\n- 必须分别按上述工单号定位，禁止把同一工单用于两个状态断言。'
        ),
    )


def run_testcase_pre_data(
    testcase: TestCase,
    *,
    triggered_by=None,
) -> TestcasePreDataResult:
    resolution = resolve_pre_data_for_testcase(testcase)
    handover_flow = processing_handover_flow_kind(testcase)

    if resolution.source == _CLAIMABLE_MANUAL_SOURCE:
        snapshot = dict(resolution.input_params or {})
        return TestcasePreDataResult(
            run=None,
            resolution=resolution,
            message_suffix=_build_manual_claimable_suffix(snapshot),
        )

    if resolution.source == 'terminal_pair':
        return _run_terminal_pair_pre_data(testcase, resolution, triggered_by=triggered_by)

    if is_processing_handover_submission_case(testcase):
        reused = _reuse_processing_handover_pre_data(testcase, resolution)
        if reused is not None:
            return reused

    plan = resolution.plan
    if plan is None and resolution.template_key:
        plan = ensure_project_template_plan(
            project_id=testcase.project_id,
            template_key=resolution.template_key,
            created_by=triggered_by or testcase.creator,
            default_environment_id=resolution.default_environment_id,
        )
        resolution.plan = plan

    if plan is None:
        if resolution.source == 'unmatched_ticket_fixture':
            # An inferred fixture is a convenience, not a prerequisite for AI
            # exploration. If the case text does not specify a safe reusable
            # template, do not fail before the agent can inspect the target
            # system's existing data. Explicitly bound plans and recognized
            # templates still fail fast when their preparation fails.
            return TestcasePreDataResult(
                run=None,
                resolution=resolution,
                message_suffix=(
                    '\n【前置数据提示】未能安全推断通用造数方案，已跳过自动造数；'
                    '将继续使用目标环境现有数据执行。若目标工单不存在，'
                    '后续步骤会明确报告缺少目标数据。'
                ),
            )
        if resolution.fail_fast and (
            resolution.template_key
            or resolution.source in {'unmatched_ticket_fixture', 'unsupported_fixture'}
        ):
            message = resolution.skip_reason or f'无法加载造数模板: {resolution.template_key}'
            return TestcasePreDataResult(
                run=None,
                resolution=resolution,
                blocked=True,
                block_message=f'前置数据准备失败: {message}',
            )
        logger.info(
            'Skip testcase pre-data: testcase_id=%s reason=%s',
            testcase.id,
            resolution.skip_reason or resolution.source,
        )
        return TestcasePreDataResult(run=None, resolution=resolution)

    ensure_plan_has_environment(
        steps=plan.steps,
        cleanup_steps=plan.cleanup_steps,
        default_environment_id=resolution.default_environment_id or plan.default_environment_id,
    )

    exec_params = _normalize_assignment_workflow_ticket_type(
        resolution.template_key,
        resolution.input_params,
    )
    env_id = resolution.default_environment_id or plan.default_environment_id
    if (
        resolution.template_key == 'biz_create_and_assign'
        and str(exec_params.get('assigneeName') or '').strip()
    ):
        # A testcase can infer an assignee name from its login precondition while
        # the reusable template still contributes its default user ID. Resolve
        # the pair together so we never create a ticket for one user and then
        # search for it in another user's "My Tickets" page.
        from .assignee_resolver import AssigneeResolutionError, resolve_assignee_params

        try:
            exec_params = resolve_assignee_params(
                exec_params,
                project_id=testcase.project_id,
                environment_id=env_id,
            )
        except AssigneeResolutionError as exc:
            if resolution.fail_fast:
                return TestcasePreDataResult(
                    run=None,
                    resolution=resolution,
                    blocked=True,
                    block_message=f'前置数据准备失败: 处理人姓名与用户 ID 无法确认：{exc}',
                )
            logger.warning(
                'Could not resolve assigned-user identity for testcase_id=%s: %s',
                testcase.id,
                exc,
            )
    if exec_params.get('creatorUserId') not in (None, ''):
        try:
            exec_params = _inject_case_creator_token(
                exec_params,
                project_id=testcase.project_id,
                environment_id=int(env_id),
            )
        except (DataGenerationError, TypeError, ValueError) as exc:
            return TestcasePreDataResult(
                run=None,
                resolution=resolution,
                blocked=True,
                block_message=f'前置数据准备失败: {exc}',
            )
    if resolution.template_key == 'biz_create_and_transfer':
        try:
            exec_params = _resolve_transfer_assignee_params(
                exec_params,
                project_id=testcase.project_id,
                environment_id=env_id,
            )
        except DataGenerationError as exc:
            if resolution.fail_fast:
                return TestcasePreDataResult(
                    run=None,
                    resolution=resolution,
                    blocked=True,
                    block_message=f'前置数据准备失败: {exc}',
                )
            logger.warning(
                'Transfer assignee resolve failed: testcase_id=%s error=%s',
                testcase.id,
                exc,
            )

    if handover_flow in _SUBMITTED_HANDOVER_FLOWS:
        if resolution.template_key != 'biz_create_and_claim':
            return TestcasePreDataResult(
                run=None, resolution=resolution, blocked=True,
                block_message='前置数据准备失败: 已提交审批用例需要“创建并领取工单”造数计划',
            )
        try:
            exec_params = _resolve_handover_approver_params(
                testcase, exec_params, environment_id=int(env_id),
            )
        except (DataGenerationError, TypeError, ValueError) as exc:
            return TestcasePreDataResult(
                run=None, resolution=resolution, blocked=True,
                block_message=f'前置数据准备失败: {exc}',
            )

    steps_override = None
    transfer_ready = is_transfer_ready_ticket_case(testcase)
    claim_plan = resolution.template_key == 'biz_create_and_claim'
    if resolution.source == _CLAIMABLE_AUTO_SOURCE or transfer_ready or claim_plan:
        if resolution.source == _CLAIMABLE_AUTO_SOURCE or claim_plan:
            steps_override = _build_claimable_steps_without_initial_chat(plan)
            if steps_override:
                logger.info(
                    'Claimable pre-data: using %s to avoid INITIATOR_LOCKED',
                    _CLAIMABLE_CREATE_INTERFACE_NAME,
                )
        if claim_plan:
            exec_params = apply_params_schema_defaults(exec_params, plan.template_params_schema)
            if not steps_override:
                return TestcasePreDataResult(
                    run=None, resolution=resolution, blocked=True,
                    block_message=f'前置数据准备失败: 缺少“{_CLAIMABLE_CREATE_INTERFACE_NAME}”接口',
                )
            try:
                exec_params = _inject_claim_creator_token(
                    exec_params, project_id=testcase.project_id, environment_id=int(env_id),
                )
            except (DataGenerationError, TypeError, ValueError) as exc:
                return TestcasePreDataResult(
                    run=None, resolution=resolution, blocked=True,
                    block_message=f'前置数据准备失败: {exc}',
                )
            if handover_flow in _SUBMITTED_HANDOVER_FLOWS:
                steps_override = _build_submitted_handover_steps(steps_override)
        else:
            exec_params = _inject_unlocked_creator_token(
                exec_params, environment_id=env_id,
            )

    run = execute_plan(
        plan,
        trigger_type=DataGenerationRun.TRIGGER_CASE_PRE,
        input_params=exec_params,
        triggered_by=triggered_by,
        default_environment_id=resolution.default_environment_id or plan.default_environment_id,
        steps_override=steps_override,
    )

    if run.status != DataGenerationRun.STATUS_SUCCESS:
        message = run.error_message or '造数失败'
        if resolution.fail_fast:
            return TestcasePreDataResult(
                run=run,
                resolution=resolution,
                blocked=True,
                block_message=f'前置数据准备失败: {message}',
            )
        logger.warning(
            'Testcase pre-data failed but continue: testcase_id=%s error=%s',
            testcase.id,
            message,
        )
        return TestcasePreDataResult(run=run, resolution=resolution)

    if handover_flow in _SUBMITTED_HANDOVER_FLOWS:
        try:
            _verify_submitted_handover_fixture(
                run,
                environment_id=int(env_id),
                approver_name=str(exec_params['approverName']),
            )
        except (DataGenerationError, TypeError, ValueError) as exc:
            return TestcasePreDataResult(
                run=run, resolution=resolution, blocked=True,
                block_message=f'前置数据准备失败: {exc}',
            )

    env_id = resolution.default_environment_id or plan.default_environment_id
    case_text = collect_testcase_text(testcase)
    needs_assigned_claim = resolution.source == _CLAIMABLE_AUTO_SOURCE
    needs_auto_login = needs_assigned_claim or is_transfer_ready_ticket_case(testcase)
    if (uses_automatic_assignment(case_text) or needs_auto_login) and isinstance(run.output_snapshot, dict):
        enriched_snapshot = (
            _wait_for_assigned_claim_snapshot(
                run.output_snapshot,
                project_id=testcase.project_id,
                environment_id=env_id,
            )
            if needs_assigned_claim or is_transfer_ready_ticket_case(testcase)
            else _enrich_auto_assignment_snapshot(
                run.output_snapshot,
                project_id=testcase.project_id,
                environment_id=env_id,
            )
        )
        if enriched_snapshot != run.output_snapshot:
            run.output_snapshot = enriched_snapshot
            run.save(update_fields=['output_snapshot'])

    if needs_auto_login:
        snapshot = run.output_snapshot if isinstance(run.output_snapshot, dict) else {}
        assignment = snapshot.get('autoAssignment') if isinstance(snapshot, dict) else None
        if not isinstance(assignment, dict) or not assignment.get('assigneeUserId'):
            return TestcasePreDataResult(
                run=run,
                resolution=resolution,
                blocked=True,
                block_message=(
                    '前置数据准备失败: 新建工单尚未完成自动分配为“待处理”状态；'
                    '请检查自动分配规则后重试。'
                ),
            )
        _, credential_error = resolve_claim_login_credentials(testcase, snapshot)
        if credential_error:
            return TestcasePreDataResult(
                run=run,
                resolution=resolution,
                blocked=True,
                block_message=f'前置数据准备失败: {credential_error}',
            )

    suffix = _build_message_suffix(
        run,
        resolution,
        case_text=case_text,
    )
    return TestcasePreDataResult(
        run=run,
        resolution=resolution,
        message_suffix=suffix,
    )


def run_testcase_pre_data_by_id(
    testcase_id: int,
    *,
    user_id: Optional[int] = None,
) -> TestcasePreDataResult:
    testcase = (
        TestCase.objects.select_related('module', 'module__parent', 'pre_data_plan', 'pre_data_environment')
        .prefetch_related('steps')
        .filter(id=testcase_id)
        .first()
    )
    if testcase is None:
        raise DataGenerationError(f'测试用例不存在: {testcase_id}')

    triggered_by = None
    if user_id:
        user_model = get_user_model()
        triggered_by = user_model.objects.filter(id=user_id).first()

    return run_testcase_pre_data(testcase, triggered_by=triggered_by)
