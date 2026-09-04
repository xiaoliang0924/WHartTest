"""单条用例（用例管理）执行前的自动造数。"""

from __future__ import annotations

import json
import logging
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


def needs_ticket_pre_data(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in text or keyword in lowered for keyword in _TICKET_CONTEXT_KEYWORDS)


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


def _build_message_suffix(run: DataGenerationRun, resolution: PreDataResolution) -> str:
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
    if ticket_no:
        lines.append(f'- 目标工单号: {ticket_no}（筛选后请优先定位该行验证操作列）')
    lines.append(
        '请优先使用上述数据满足前置条件；若 UI 列表需按状态筛选，仍按用例步骤操作并验证筛选结果。'
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
        message_suffix=_build_message_suffix(run, resolution),
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
