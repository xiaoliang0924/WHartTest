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

    if testcase.pre_data_plan_id:
        plan = testcase.pre_data_plan
        params = testcase.pre_data_params if isinstance(testcase.pre_data_params, dict) else {}
        env_id = testcase.pre_data_environment_id or plan.default_environment_id
        return PreDataResolution(
            plan=plan,
            template_key=plan.template_key,
            input_params=params,
            default_environment_id=env_id,
            source='testcase',
            fail_fast=getattr(testcase, 'pre_data_fail_fast', True),
        )

    module_plan, module = resolve_module_pre_data_plan(testcase.module)
    if module_plan is not None and module is not None:
        params = module.pre_data_params if isinstance(module.pre_data_params, dict) else {}
        env_id = module.pre_data_environment_id or module_plan.default_environment_id
        return PreDataResolution(
            plan=module_plan,
            template_key=module_plan.template_key,
            input_params=params,
            default_environment_id=env_id,
            source='module',
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
            skip_reason='工单总览/数据总览类用例依赖环境已有统计数据，跳过自动推断造数',
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


def is_ticket_detail_boundary_case(testcase: TestCase) -> bool:
    blob = collect_testcase_text(testcase)
    return (
        '沟通记录' in blob
        and '处理' in blob
        and ('详情' in blob or '工单详情' in blob)
    )


def _latest_pre_data_snapshot(testcase: TestCase) -> dict:
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


def get_latest_pre_data_ticket_no(testcase: TestCase) -> str:
    snapshot = _latest_pre_data_snapshot(testcase)
    return str(snapshot.get('ticketNo') or '').strip()


def get_latest_pre_data_ticket_id(testcase: TestCase) -> str:
    snapshot = _latest_pre_data_snapshot(testcase)
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


def build_testcase_step_script_hints(testcase: TestCase) -> str:
    """Generic execution hints. Do not hardcode a product or case script here."""
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
    lines.extend(build_ticket_detail_case_hints(testcase).splitlines())
    lines.extend(build_overview_sla_detail_case_hints(testcase).splitlines())
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
    if ticket_no and needs_ticket_row_action(case_text):
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


def run_testcase_pre_data(
    testcase: TestCase,
    *,
    triggered_by=None,
) -> TestcasePreDataResult:
    resolution = resolve_pre_data_for_testcase(testcase)

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

    return TestcasePreDataResult(
        run=run,
        resolution=resolution,
        message_suffix=_build_message_suffix(
            run,
            resolution,
            case_text=collect_testcase_text(testcase),
        ),
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
