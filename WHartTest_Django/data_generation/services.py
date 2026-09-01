"""造数计划执行引擎。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

import jmespath
from django.db import transaction
from django.utils import timezone

from api_environments.models import ApiEnvironment, ApiEnvironmentVariable
from api_interfaces.models import ApiInterface
from api_interfaces.runner import InterfaceRunner
from api_interfaces.logging_utils import new_trace_id
from ui_automation.models import UiPublicData

from .models import DataGenerationPlan, DataGenerationRun

logger = logging.getLogger(__name__)

_TEMPLATE_PATTERN = re.compile(r'\{\{([^}]+)\}\}')


class DataGenerationError(Exception):
    """造数执行失败。"""


def substitute_templates(value: Any, context: Dict[str, Any]) -> Any:
    """递归替换 {{var}} 模板变量。"""
    if isinstance(value, str):
        def _replace(match: re.Match) -> str:
            key = match.group(1).strip()
            if key not in context:
                return match.group(0)
            return str(context[key])
        return _TEMPLATE_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {k: substitute_templates(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_templates(item, context) for item in value]
    return value


def _parse_response_body(content: Any) -> Any:
    if content is None:
        return None
    if isinstance(content, (dict, list)):
        return content
    if isinstance(content, str):
        text = content.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return content
    return content


def _extract_values(source: Any, mapping: Dict[str, str]) -> Dict[str, Any]:
    extracted: Dict[str, Any] = {}
    for var_name, expr in mapping.items():
        if not expr:
            continue
        jmes_expr = expr.strip()
        if jmes_expr.startswith('$.'):
            jmes_expr = jmes_expr[2:]
        try:
            value = jmespath.search(jmes_expr, source)
        except Exception as exc:
            raise DataGenerationError(f'变量提取失败 {var_name}: {exc}') from exc
        if value is None:
            raise DataGenerationError(f'变量提取为空: {var_name} (expr={expr})')
        extracted[var_name] = value
    return extracted


class PlanExecutor:
    """执行造数计划。"""

    SUPPORTED_STEP_TYPES = {'api_call', 'set_env_var', 'set_public_data'}

    def __init__(
        self,
        plan: DataGenerationPlan,
        *,
        trigger_type: str = DataGenerationRun.TRIGGER_MANUAL,
        input_params: Optional[Dict[str, Any]] = None,
        triggered_by=None,
        test_execution=None,
        default_environment_id: Optional[int] = None,
    ):
        self.plan = plan
        self.trigger_type = trigger_type
        self.input_params = dict(input_params or {})
        self.triggered_by = triggered_by
        self.test_execution = test_execution
        self.default_environment_id = (
            default_environment_id
            or (plan.default_environment_id if plan.default_environment_id else None)
        )
        self.context: Dict[str, Any] = dict(self.input_params)
        if 'summary' not in self.context:
            self.context['summary'] = f"造数{int(timezone.now().timestamp()) % 100000}"[:20]
        self.step_logs: list[Dict[str, Any]] = []

    def execute(self) -> DataGenerationRun:
        run = DataGenerationRun.objects.create(
            plan=self.plan,
            project=self.plan.project,
            status=DataGenerationRun.STATUS_RUNNING,
            trigger_type=self.trigger_type,
            test_execution=self.test_execution,
            input_params=self.input_params,
            triggered_by=self.triggered_by,
            started_at=timezone.now(),
        )

        try:
            if not self.plan.is_active:
                raise DataGenerationError('造数计划未启用')

            steps = self.plan.steps if isinstance(self.plan.steps, list) else []
            if not steps:
                raise DataGenerationError('造数计划没有配置步骤')

            for index, raw_step in enumerate(steps, start=1):
                if not isinstance(raw_step, dict):
                    raise DataGenerationError(f'步骤 #{index} 格式无效')
                self._execute_step(index, raw_step)

            run.status = DataGenerationRun.STATUS_SUCCESS
            run.output_snapshot = dict(self.context)
            run.step_logs = self.step_logs
            run.finished_at = timezone.now()
            run.save(
                update_fields=[
                    'status',
                    'output_snapshot',
                    'step_logs',
                    'finished_at',
                ]
            )
            return run
        except Exception as exc:
            message = str(exc)
            logger.error(
                'Data generation failed: plan_id=%s run_id=%s error=%s',
                self.plan.id,
                run.id,
                message,
                exc_info=True,
            )
            run.status = DataGenerationRun.STATUS_FAILED
            run.error_message = message
            run.output_snapshot = dict(self.context)
            run.step_logs = self.step_logs
            run.finished_at = timezone.now()
            run.save(
                update_fields=[
                    'status',
                    'error_message',
                    'output_snapshot',
                    'step_logs',
                    'finished_at',
                ]
            )
            return run

    def _execute_step(self, index: int, step: Dict[str, Any]) -> None:
        step_type = (step.get('type') or '').strip()
        step_name = step.get('name') or f'步骤{index}'
        log_entry: Dict[str, Any] = {
            'index': index,
            'type': step_type,
            'name': step_name,
            'status': 'success',
        }

        if step_type not in self.SUPPORTED_STEP_TYPES:
            raise DataGenerationError(f'不支持的步骤类型: {step_type}')

        try:
            if step_type == 'api_call':
                result = self._run_api_call(step)
                log_entry.update(result)
            elif step_type == 'set_env_var':
                result = self._set_env_vars(step)
                log_entry.update(result)
            elif step_type == 'set_public_data':
                result = self._set_public_data(step)
                log_entry.update(result)
        except DataGenerationError:
            log_entry['status'] = 'failed'
            self.step_logs.append(log_entry)
            raise
        except Exception as exc:
            log_entry['status'] = 'failed'
            log_entry['error'] = str(exc)
            self.step_logs.append(log_entry)
            raise DataGenerationError(f'{step_name} 执行失败: {exc}') from exc

        self.step_logs.append(log_entry)

    def _resolve_environment(self, step: Dict[str, Any]) -> Optional[ApiEnvironment]:
        env_id = step.get('environment_id') or self.default_environment_id
        if not env_id:
            return None
        try:
            return ApiEnvironment.objects.get(id=env_id, project_id=self.plan.project_id)
        except ApiEnvironment.DoesNotExist as exc:
            raise DataGenerationError(f'API 环境不存在: {env_id}') from exc

    def _run_api_call(self, step: Dict[str, Any]) -> Dict[str, Any]:
        interface_id = step.get('interface_id')
        if not interface_id:
            raise DataGenerationError('api_call 步骤缺少 interface_id')

        try:
            interface = ApiInterface.objects.get(
                id=interface_id,
                project_id=self.plan.project_id,
            )
        except ApiInterface.DoesNotExist as exc:
            raise DataGenerationError(f'接口不存在: {interface_id}') from exc

        environment = self._resolve_environment(step)
        interface_data = interface.get_interface_data()
        interface_data['project_id'] = interface.project_id
        interface_data['trace_id'] = new_trace_id('data-gen')
        interface_data['body_source'] = 'data_generation_plan'

        if environment:
            interface_data['base_url'] = environment.base_url or ''
            interface_data['verify'] = environment.verify_ssl

        runner = InterfaceRunner(interface_data)
        runner.variables = dict(runner.variables or {})

        step_variables = substitute_templates(step.get('variables') or {}, self.context)
        if isinstance(step_variables, dict):
            runner.variables.update(step_variables)

        env_config: Dict[str, Any] = {}
        if environment:
            from api_environments.token_refresh import refresh_environment_tokens

            env_variables = environment.get_all_variables()
            if not isinstance(env_variables, dict):
                env_variables = {}
            refreshed_variables = refresh_environment_tokens(
                base_url=environment.base_url,
                variables=env_variables,
                verify_ssl=environment.verify_ssl,
                environment_id=environment.id,
                persist=True,
            )
            env_config['variables'] = refreshed_variables
            if isinstance(refreshed_variables, dict):
                runner.variables.update(refreshed_variables)

        runner.run_interface(env_config)
        response_data = runner.get_response()

        status_code = response_data.get('status_code') or 0
        if status_code >= 400 or not response_data.get('success'):
            error_detail = (
                (response_data.get('response') or {}).get('content')
                or (response_data.get('response') or {}).get('error')
                or response_data.get('error')
                or f'HTTP {status_code}'
            )
            raise DataGenerationError(f'接口 [{interface.name}] 调用失败: {error_detail}')

        extracted = dict(response_data.get('extracted_variables') or {})
        custom_extract = step.get('extract') or {}
        if custom_extract:
            response_body = _parse_response_body(
                (response_data.get('response') or {}).get('content')
            )
            if response_body is None:
                raise DataGenerationError(
                    f'接口 [{interface.name}] 响应为空，无法提取变量'
                )
            extracted.update(_extract_values(response_body, custom_extract))

        self.context.update(extracted)

        return {
            'interface_id': interface.id,
            'interface_name': interface.name,
            'status_code': response_data.get('status_code'),
            'extracted': extracted,
        }

    def _set_env_vars(self, step: Dict[str, Any]) -> Dict[str, Any]:
        environment_id = step.get('environment_id') or self.default_environment_id
        if not environment_id:
            raise DataGenerationError('set_env_var 步骤缺少 environment_id')

        try:
            environment = ApiEnvironment.objects.get(
                id=environment_id,
                project_id=self.plan.project_id,
            )
        except ApiEnvironment.DoesNotExist as exc:
            raise DataGenerationError(f'API 环境不存在: {environment_id}') from exc

        variables = substitute_templates(step.get('variables') or {}, self.context)
        if not isinstance(variables, dict) or not variables:
            raise DataGenerationError('set_env_var 步骤 variables 不能为空')

        saved = []
        with transaction.atomic():
            for name, value in variables.items():
                typed_value = value if value is not None else ''
                obj, _created = ApiEnvironmentVariable.objects.update_or_create(
                    environment=environment,
                    name=str(name),
                    defaults={
                        'value': str(typed_value),
                        'type': 'string',
                    },
                )
                saved.append({'name': obj.name, 'value': obj.value})
                self.context[str(name)] = typed_value

        return {'environment_id': environment.id, 'variables': saved}

    def _set_public_data(self, step: Dict[str, Any]) -> Dict[str, Any]:
        items = step.get('items') or []
        if not isinstance(items, list) or not items:
            raise DataGenerationError('set_public_data 步骤 items 不能为空')

        saved = []
        with transaction.atomic():
            for raw_item in items:
                item = substitute_templates(raw_item, self.context)
                if not isinstance(item, dict):
                    continue
                key = item.get('key')
                value = item.get('value')
                if not key:
                    raise DataGenerationError('set_public_data 条目缺少 key')
                data_type = int(item.get('type', 0))
                obj, _created = UiPublicData.objects.update_or_create(
                    project=self.plan.project,
                    key=str(key),
                    defaults={
                        'value': '' if value is None else str(value),
                        'type': data_type,
                        'is_enabled': True,
                        'creator': self.triggered_by,
                    },
                )
                saved.append({'key': obj.key, 'value': obj.value})
                self.context[str(key)] = value

        return {'items': saved}


def execute_plan(
    plan: DataGenerationPlan,
    *,
    trigger_type: str = DataGenerationRun.TRIGGER_MANUAL,
    input_params: Optional[Dict[str, Any]] = None,
    triggered_by=None,
    test_execution=None,
    default_environment_id: Optional[int] = None,
) -> DataGenerationRun:
    executor = PlanExecutor(
        plan,
        trigger_type=trigger_type,
        input_params=input_params,
        triggered_by=triggered_by,
        test_execution=test_execution,
        default_environment_id=default_environment_id,
    )
    return executor.execute()


def run_suite_pre_data_generation(suite, test_execution, triggered_by=None) -> Optional[DataGenerationRun]:
    """测试套件执行前的造数钩子。"""
    plan_id = getattr(suite, 'pre_data_plan_id', None)
    if not plan_id:
        return None

    plan = DataGenerationPlan.objects.filter(
        id=plan_id,
        project_id=suite.project_id,
        is_active=True,
    ).first()
    if plan is None:
        raise DataGenerationError(f'造数计划不存在或未启用: {plan_id}')

    params = getattr(suite, 'pre_data_params', None) or {}
    default_env_id = getattr(suite, 'pre_data_environment_id', None)

    run = execute_plan(
        plan,
        trigger_type=DataGenerationRun.TRIGGER_SUITE_PRE,
        input_params=params if isinstance(params, dict) else {},
        triggered_by=triggered_by,
        test_execution=test_execution,
        default_environment_id=default_env_id,
    )

    test_execution.data_generation_run = run
    test_execution.save(update_fields=['data_generation_run', 'updated_at'])

    if run.status != DataGenerationRun.STATUS_SUCCESS:
        if getattr(suite, 'pre_data_fail_fast', True):
            raise DataGenerationError(run.error_message or '造数失败，已阻断套件执行')
    return run
