"""单条用例（用例管理）执行前的自动造数。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from django.contrib.auth import get_user_model

from testcases.models import TestCase, TestCaseModule

from .exceptions import DataGenerationError
from .intent_router import build_input_params, infer_business_template_key
from .models import DataGenerationPlan, DataGenerationRun
from .plan_validation import ensure_plan_has_environment
from .services import execute_plan
from .templates import get_template_by_key

logger = logging.getLogger(__name__)

# 兼容早期保存的模板。待领取类用例默认自动创建 TYPE_A，由 helper 在「待处理」列表完成领单流程。
_LEGACY_CLAIMABLE_TEMPLATE_KEY = 'biz_create_claimable_pending'
_CLAIMABLE_AUTO_SOURCE = 'claimable-auto'
_CLAIMABLE_MANUAL_SOURCE = 'manual-fixture'

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


def _ticket_row_supports_claim(row: dict[str, Any]) -> bool:
    actions = row.get('availableActions') or []
    if isinstance(actions, list) and 'claim' in actions:
        return True
    status = str(row.get('status') or '')
    assignee = row.get('assigneeUserId')
    unassigned = assignee in (None, '', 0)
    return unassigned and status == 'pending_process'


def try_pick_claimable_ticket_from_pool(
    testcase: TestCase,
    *,
    environment_id: Optional[int],
) -> dict[str, Any]:
    env_id = environment_id or getattr(testcase, 'pre_data_environment_id', None)
    if not env_id:
        return {}
    queries = (
        '/api/tickets?currentStatus=pending_process&page=1&pageSize=50',
        '/api/tickets?currentStatus=pending_assign&page=1&pageSize=50',
    )
    for query in queries:
        for row in _list_tickets_from_api(
            project_id=testcase.project_id,
            environment_id=int(env_id),
            query_path=query,
        ):
            if not _ticket_row_supports_claim(row):
                continue
            ticket_id = row.get('id') or row.get('ticketId')
            ticket_no = row.get('ticketNo')
            if ticket_id in (None, '') or not ticket_no:
                continue
            return {
                'ticketId': ticket_id,
                'ticketNo': str(ticket_no),
                'work_order_id': ticket_id,
                'ticketStatus': row.get('status') or 'pending_process',
                'source': 'claimable-pool',
            }
    return {}


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
        f'availableActions={actions}，不具备 claim/领取 能力。'
        'TYPE_A 新建多为 pending_assign（仅 assign）；取消分配接口当前 500，无法转为可领取态。'
        '请后端修复 unassign 或提供可领取种子工单，或在 pre_data_params 配置手工 ticketNo。'
    )


def resolve_claimable_ticket_pre_data(testcase: TestCase) -> PreDataResolution:
    """待领取类：默认自动 TYPE_A 造数；若用例配置了 pre_data_params 则优先手工工单。"""
    params = testcase.pre_data_params if isinstance(testcase.pre_data_params, dict) else {}
    manual = extract_manual_ticket_fixture(params)
    env_id = getattr(testcase, 'pre_data_environment_id', None)
    if manual.get('ticketNo'):
        return PreDataResolution(
            plan=None,
            template_key=None,
            input_params=manual,
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

    template_key = infer_business_template_key(text)
    if not template_key:
        return PreDataResolution(
            plan=None,
            template_key=None,
            input_params={},
            default_environment_id=None,
            source='none',
            fail_fast=False,
            skip_reason='无法推断造数模板',
        )

    # 文本中可能仍保留了旧模板键；将它当作 TYPE_A 创建的别名处理，
    # 不能让显式指定旧键绕过上面对历史计划的兼容保护。
    if template_key == _LEGACY_CLAIMABLE_TEMPLATE_KEY:
        return resolve_claimable_ticket_pre_data(testcase)

    input_params = build_input_params(text, {'input_params': {}, 'steps': []})
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


def is_ticket_message_send_case(testcase: TestCase) -> bool:
    """Cases that must type and send a message, then assert it in 沟通记录."""
    blob = collect_testcase_text(testcase)
    return any(
        keyword in blob
        for keyword in (
            '发送消息',
            '点击发送',
            '按Enter',
            '按 Enter',
            '沟通记录出现',
            '输入文本点击发送',
            '输入文本按Enter',
        )
    )


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


def is_claimable_ticket_detail_case(testcase: TestCase) -> bool:
    """Cases that must open an unassigned pending ticket and expose claim."""
    blob = collect_testcase_text(testcase)
    return (
        '待处理' in blob
        and '未分配' in blob
        and '处理' in blob
        and '领取工单' in blob
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
    """Prefer the data run tied to the current case-management execution."""
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


def extract_expected_send_message_texts(testcase: TestCase) -> list[str]:
    """Quoted message texts that send-message steps must assert on page."""
    found: list[str] = []
    for step in testcase.steps.order_by('step_number'):
        blob = f"{getattr(step, 'description', '') or ''}\n{getattr(step, 'expected_result', '') or ''}"
        if not any(k in blob for k in ('发送', '沟通记录', 'Enter')):
            continue
        for match in re.findall(r'[「『“\"]([^」』”\"]+)[」』”\"]', blob):
            text = (match or '').strip()
            if len(text) < 4:
                continue
            if text in ('发送', '查询', '处理', '待处理', '未分配', '领取工单'):
                continue
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
            '【待处理未分配工单 — 固定 Playwright 脚本，禁止改写】',
            '全程 session_id 不变；每步只执行下面一行 JavaScript：',
        ]
        for step in testcase.steps.order_by('step_number'):
            lines.append(
                f'- 步骤{step.step_number}: `await helpers.runClaimableTicketCaseStep(page, {step.step_number});`'
            )
        lines.extend([
            '- 步骤4须进入详情并确认「领取工单」后才会截图；TYPE_A 自动造数多为待分配，helper 会优先待处理+「处理」，否则用工单号链接进详情。',
            '- helper 返回 null 或输出 RESULT=FAIL 时立即判不通过，禁止自行补截图或改报通过。',
        ])
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
    ticket_no = snapshot.get('ticketNo')
    fixed_claimable_flow = (
        '待处理' in case_text and '未分配' in case_text and '领取工单' in case_text
    )
    if ticket_no and needs_ticket_row_action(case_text):
        if fixed_claimable_flow:
            lines.append(
                f'- 本次造数工单号: {ticket_no}。专用 helper 会按工单号定位行并进入领取详情：'
                '优先「待处理」+「处理」，若为 TYPE_A 待分配则改点工单号链接（勿用 ticketId 直达）。'
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
        '【手工/fixture 前置数据 — 待处理未分配领单用例】',
        '- 来源: 用例 pre_data_params（非 TYPE_A 自动造数）',
        '- 数据快照:',
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        '- 执行时 helper 会在「工单列表 + 待处理」筛选下使用该工单号。',
    ]
    ticket_no = snapshot.get('ticketNo')
    if ticket_no:
        lines.append(f'- 本次工单号: {ticket_no}')
    return '\n'.join(lines)


def run_testcase_pre_data(
    testcase: TestCase,
    *,
    triggered_by=None,
) -> TestcasePreDataResult:
    resolution = resolve_pre_data_for_testcase(testcase)

    if resolution.source == _CLAIMABLE_MANUAL_SOURCE:
        snapshot = dict(resolution.input_params or {})
        return TestcasePreDataResult(
            run=None,
            resolution=resolution,
            message_suffix=_build_manual_claimable_suffix(snapshot),
        )

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
        if resolution.template_key and resolution.fail_fast:
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

    run = execute_plan(
        plan,
        trigger_type=DataGenerationRun.TRIGGER_CASE_PRE,
        input_params=resolution.input_params,
        triggered_by=triggered_by,
        default_environment_id=resolution.default_environment_id or plan.default_environment_id,
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

    env_id = resolution.default_environment_id or plan.default_environment_id
    claimable_warn = ''
    if resolution.source == _CLAIMABLE_AUTO_SOURCE and isinstance(run.output_snapshot, dict):
        pool = try_pick_claimable_ticket_from_pool(testcase, environment_id=env_id)
        if pool.get('ticketNo'):
            run.output_snapshot = {**run.output_snapshot, **pool}
            run.save(update_fields=['output_snapshot'])
        else:
            # 暂不阻断：TYPE_A 常无 claim 能力，仍继续跑 UI；步骤4可能因无「领取工单」失败。
            warn = validate_claimable_ticket_snapshot(
                run.output_snapshot,
                project_id=testcase.project_id,
                environment_id=env_id,
            )
            if warn:
                claimable_warn = f'\n- 造数能力警告（不阻断执行）: {warn}'
                logger.warning(
                    'Claimable pre-data warning (continue): testcase_id=%s %s',
                    testcase.id,
                    warn,
                )

    suffix = _build_message_suffix(
        run,
        resolution,
        case_text=collect_testcase_text(testcase),
    )
    if claimable_warn:
        suffix = (suffix or '') + claimable_warn

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
