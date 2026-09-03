"""造数计划执行引擎。"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional

import jmespath
from django.db import transaction
from django.utils import timezone

from api_environments.models import ApiEnvironment, ApiEnvironmentVariable
from api_functions.models import ApiCustomFunction
from api_interfaces.models import ApiInterface
from api_interfaces.runner import InterfaceRunner
from api_interfaces.logging_utils import new_trace_id
from ui_automation.models import UiPublicData

from .exceptions import DataGenerationError
from .models import DataGenerationPlan, DataGenerationRun
from .template_resolver import resolve_template_steps
from .templates import get_template_by_key
from .sql_utils import execute_sql_step, extract_sql_result

logger = logging.getLogger(__name__)

_TEMPLATE_PATTERN = re.compile(r'\{\{([^}]+)\}\}')
_DYNAMIC_GENERATORS = {
    'uuid': lambda _ctx: str(uuid.uuid4()),
    'timestamp': lambda _ctx: str(int(timezone.now().timestamp())),
    'random_int': lambda ctx: str(int(time.time() * 1000) % 100000),
}


def apply_params_schema_defaults(
    input_params: Optional[Dict[str, Any]],
    params_schema: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """将 template params_schema 中的 default 合并进 input_params（用户传参优先）。"""
    merged: Dict[str, Any] = {}
    if isinstance(params_schema, dict):
        for key, spec in params_schema.items():
            if isinstance(spec, dict) and 'default' in spec:
                merged[key] = spec['default']
    if isinstance(input_params, dict):
        merged.update(input_params)
    return merged


def substitute_templates(value: Any, context: Dict[str, Any]) -> Any:
    """递归替换 {{var}} 模板变量，支持 uuid/timestamp 等动态变量。"""

    def _resolve_key(key: str) -> Any:
        key = key.strip()
        if key in context:
            return context[key]
        if key in _DYNAMIC_GENERATORS:
            generated = _DYNAMIC_GENERATORS[key](context)
            context[key] = generated
            return generated
        if key.startswith('faker.'):
            suffix = key.split('.', 1)[1]
            generated = f'{suffix}_{uuid.uuid4().hex[:8]}'
            context[key] = generated
            return generated
        return None

    if isinstance(value, str):
        full_match = _TEMPLATE_PATTERN.fullmatch(value.strip())
        if full_match:
            resolved = _resolve_key(full_match.group(1))
            if resolved is not None:
                return resolved

        def _replace(match: re.Match) -> str:
            key = match.group(1).strip()
            resolved = _resolve_key(key)
            if resolved is None:
                return match.group(0)
            return str(resolved)

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


def _collect_force_refresh_token_vars(interface_data: Dict[str, Any]) -> List[str]:
    """接口若使用 assigneeToken 等变量，强制重新登录以避免 TOKEN_SCOPE_STALE。"""
    forced: List[str] = []
    headers = interface_data.get('headers') or []
    if isinstance(headers, list):
        for item in headers:
            if not isinstance(item, dict):
                continue
            value = str(item.get('value') or '')
            if 'assigneeToken' in value:
                forced.append('assigneeToken')
            elif 'adminToken' in value:
                forced.append('adminToken')
            elif 'accessToken' in value:
                forced.append('accessToken')
    return list(dict.fromkeys(forced))


class PlanExecutor:
    """执行造数计划。"""

    SUPPORTED_STEP_TYPES = {
        'api_call',
        'set_env_var',
        'set_public_data',
        'sql',
        'custom_function',
        'delay',
    }

    def __init__(
        self,
        plan: DataGenerationPlan,
        *,
        trigger_type: str = DataGenerationRun.TRIGGER_MANUAL,
        input_params: Optional[Dict[str, Any]] = None,
        triggered_by=None,
        test_execution=None,
        default_environment_id: Optional[int] = None,
        steps_override: Optional[List[Dict[str, Any]]] = None,
        parent_run: Optional[DataGenerationRun] = None,
    ):
        self.plan = plan
        self.trigger_type = trigger_type
        schema = plan.template_params_schema if isinstance(plan.template_params_schema, dict) else {}
        if not schema and plan.template_key:
            template = get_template_by_key(plan.template_key, project_id=plan.project_id)
            if template:
                schema = template.get('params_schema') or {}
        if not schema and plan.is_template:
            schema = plan.template_params_schema if isinstance(plan.template_params_schema, dict) else {}
        self.input_params = apply_params_schema_defaults(input_params, schema)
        self.triggered_by = triggered_by
        self.test_execution = test_execution
        self.parent_run = parent_run
        self.default_environment_id = (
            default_environment_id
            or (plan.default_environment_id if plan.default_environment_id else None)
        )
        self.steps_override = steps_override
        self.context: Dict[str, Any] = dict(self.input_params)
        if 'summary' not in self.context:
            self.context['summary'] = f"造数{int(timezone.now().timestamp()) % 100000}"[:20]
        self.step_logs: list[Dict[str, Any]] = []

    def _snapshot_context(self) -> Dict[str, Any]:
        return {
            k: v
            for k, v in self.context.items()
            if not isinstance(v, (dict, list))
        }

    def execute(self) -> DataGenerationRun:
        run = DataGenerationRun.objects.create(
            plan=self.plan,
            project=self.plan.project,
            status=DataGenerationRun.STATUS_RUNNING,
            trigger_type=self.trigger_type,
            test_execution=self.test_execution,
            input_params=self.input_params,
            triggered_by=self.triggered_by,
            parent_run=self.parent_run,
            started_at=timezone.now(),
        )

        try:
            if not self.plan.is_active and self.trigger_type != DataGenerationRun.TRIGGER_CLEANUP:
                raise DataGenerationError('造数计划未启用')

            steps = self._resolve_steps()
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

    def _resolve_steps(self) -> List[Dict[str, Any]]:
        plan_bindings = (
            self.plan.template_bindings if isinstance(self.plan.template_bindings, dict) else None
        )
        template_key = self.plan.template_key or None
        if self.steps_override is not None:
            return resolve_template_steps(
                self.steps_override,
                project_id=self.plan.project_id,
                plan_bindings=plan_bindings,
                default_environment_id=self.default_environment_id,
                template_key=template_key,
            )
        steps = self.plan.steps if isinstance(self.plan.steps, list) else []
        return resolve_template_steps(
            steps,
            project_id=self.plan.project_id,
            plan_bindings=plan_bindings,
            default_environment_id=self.default_environment_id,
            template_key=template_key,
        )

    def _execute_step(self, index: int, step: Dict[str, Any]) -> None:
        step_type = (step.get('type') or '').strip()
        step_name = step.get('name') or f'步骤{index}'
        log_entry: Dict[str, Any] = {
            'index': index,
            'type': step_type,
            'name': step_name,
            'status': 'success',
            'context_before': self._snapshot_context(),
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
            elif step_type == 'sql':
                result = self._run_sql(step)
                log_entry.update(result)
            elif step_type == 'custom_function':
                result = self._run_custom_function(step)
                log_entry.update(result)
            elif step_type == 'delay':
                result = self._run_delay(step)
                log_entry.update(result)
        except DataGenerationError as exc:
            log_entry['status'] = 'failed'
            log_entry['error'] = str(exc)
            log_entry['context_after'] = self._snapshot_context()
            self.step_logs.append(log_entry)
            raise
        except Exception as exc:
            log_entry['status'] = 'failed'
            log_entry['error'] = str(exc)
            log_entry['context_after'] = self._snapshot_context()
            self.step_logs.append(log_entry)
            raise DataGenerationError(f'{step_name} 执行失败: {exc}') from exc

        log_entry['context_after'] = self._snapshot_context()
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
        runner.variables = {}

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
                force_token_vars=_collect_force_refresh_token_vars(interface_data),
            )
            if isinstance(refreshed_variables, dict):
                runner.variables.update(refreshed_variables)

        runner.variables.update(
            {
                k: v
                for k, v in self.context.items()
                if not isinstance(v, (dict, list))
            }
        )

        step_variables = substitute_templates(step.get('variables') or {}, self.context)
        if isinstance(step_variables, dict):
            runner.variables.update(step_variables)

        runner.run_interface({})
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

    def _run_sql(self, step: Dict[str, Any]) -> Dict[str, Any]:
        database_config_id = substitute_templates(
            step.get('database_config_id'),
            self.context,
        )
        if not database_config_id:
            raise DataGenerationError('sql 步骤缺少 database_config_id')

        sql = substitute_templates(step.get('sql') or '', self.context)
        method = substitute_templates(step.get('method') or 'fetchall', self.context)
        result = execute_sql_step(
            database_config_id=int(database_config_id),
            project_id=self.plan.project_id,
            sql=str(sql),
            method=str(method),
        )

        custom_extract = step.get('extract') or {}
        extracted: Dict[str, Any] = {}
        if custom_extract:
            extracted = extract_sql_result(result, custom_extract)
            self.context.update(extracted)
        elif step.get('output_var'):
            output_var = str(step.get('output_var'))
            self.context[output_var] = result
            extracted[output_var] = result

        return {
            'database_config_id': database_config_id,
            'method': method,
            'result': result,
            'extracted': extracted,
        }

    def _run_custom_function(self, step: Dict[str, Any]) -> Dict[str, Any]:
        function_id = step.get('function_id')
        if not function_id:
            raise DataGenerationError('custom_function 步骤缺少 function_id')

        try:
            func_obj = ApiCustomFunction.objects.get(
                id=function_id,
                project_id=self.plan.project_id,
                is_active=True,
            )
        except ApiCustomFunction.DoesNotExist as exc:
            raise DataGenerationError(f'自定义函数不存在: {function_id}') from exc

        args = substitute_templates(step.get('args') or {}, self.context)
        if not isinstance(args, dict):
            args = {}

        temp_file = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as handle:
                handle.write(func_obj.code)
                temp_file = handle.name

            spec = importlib.util.spec_from_file_location('dg_custom_function', temp_file)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            func_name = func_obj.name
            if not hasattr(module, func_name):
                func_name = func_obj.code.split('def ')[1].split('(')[0].strip()
            func = getattr(module, func_name)
            result = func(**args) if args else func()
        finally:
            if temp_file:
                import os
                try:
                    os.unlink(temp_file)
                except OSError:
                    pass

        output_var = step.get('output_var') or func_obj.name
        self.context[str(output_var)] = result

        return {
            'function_id': func_obj.id,
            'function_name': func_obj.name,
            'output_var': output_var,
            'result': result,
        }

    def _run_delay(self, step: Dict[str, Any]) -> Dict[str, Any]:
        seconds_raw = substitute_templates(step.get('seconds', 1), self.context)
        try:
            seconds = float(seconds_raw)
        except (TypeError, ValueError) as exc:
            raise DataGenerationError('delay 步骤 seconds 无效') from exc
        if seconds < 0:
            raise DataGenerationError('delay 步骤 seconds 不能为负数')
        if seconds > 300:
            raise DataGenerationError('delay 步骤 seconds 不能超过 300 秒')
        time.sleep(seconds)
        return {'seconds': seconds}


def execute_plan(
    plan: DataGenerationPlan,
    *,
    trigger_type: str = DataGenerationRun.TRIGGER_MANUAL,
    input_params: Optional[Dict[str, Any]] = None,
    triggered_by=None,
    test_execution=None,
    default_environment_id: Optional[int] = None,
    steps_override: Optional[List[Dict[str, Any]]] = None,
    parent_run: Optional[DataGenerationRun] = None,
) -> DataGenerationRun:
    executor = PlanExecutor(
        plan,
        trigger_type=trigger_type,
        input_params=input_params,
        triggered_by=triggered_by,
        test_execution=test_execution,
        default_environment_id=default_environment_id,
        steps_override=steps_override,
        parent_run=parent_run,
    )
    return executor.execute()


def execute_cleanup_steps(
    run: DataGenerationRun,
    *,
    triggered_by=None,
) -> DataGenerationRun:
    """执行造数计划的 cleanup_steps，使用原 run 的 output_snapshot 作为上下文。"""
    plan = run.plan
    cleanup_steps = plan.cleanup_steps if isinstance(plan.cleanup_steps, list) else []
    if not cleanup_steps:
        run.cleanup_status = DataGenerationRun.CLEANUP_SKIPPED
        run.save(update_fields=['cleanup_status'])
        return run

    context = dict(run.output_snapshot or {})
    context.update(run.input_params or {})

    cleanup_run = execute_plan(
        plan,
        trigger_type=DataGenerationRun.TRIGGER_CLEANUP,
        input_params=context,
        triggered_by=triggered_by,
        default_environment_id=plan.default_environment_id,
        steps_override=cleanup_steps,
        parent_run=run,
    )

    run.is_cleaned = cleanup_run.status == DataGenerationRun.STATUS_SUCCESS
    run.cleanup_status = (
        DataGenerationRun.CLEANUP_SUCCESS
        if cleanup_run.status == DataGenerationRun.STATUS_SUCCESS
        else DataGenerationRun.CLEANUP_FAILED
    )
    run.cleanup_logs = cleanup_run.step_logs
    run.cleanup_error_message = cleanup_run.error_message or ''
    run.save(
        update_fields=[
            'is_cleaned',
            'cleanup_status',
            'cleanup_logs',
            'cleanup_error_message',
        ]
    )
    return run


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


def run_suite_post_data_cleanup(suite, test_execution, triggered_by=None) -> Optional[DataGenerationRun]:
    """测试套件执行后的造数清理钩子。"""
    if not getattr(suite, 'post_data_cleanup', False):
        return None

    run = getattr(test_execution, 'data_generation_run', None)
    if run is None:
        return None

    return execute_cleanup_steps(run, triggered_by=triggered_by)
