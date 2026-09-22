"""造数计划执行引擎。"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

import jmespath
from django.db import close_old_connections, transaction
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
from .templates import REF_ASSIGN_TICKET, REF_CLAIM_TICKET, get_template_by_key
from .sql_utils import execute_sql_step, extract_sql_result

logger = logging.getLogger(__name__)

_TEMPLATE_PATTERN = re.compile(r'\{\{([^}]+)\}\}')
_DYNAMIC_GENERATORS = {
    'uuid': lambda _ctx: str(uuid.uuid4()),
    'timestamp': lambda _ctx: str(int(timezone.now().timestamp())),
    'random_int': lambda ctx: str(int(time.time() * 1000) % 100000),
}


def _normalize_legacy_auth_header_placeholders(interface_data: Dict[str, Any]) -> None:
    """Normalize auth headers to HttpRunner ``$var`` placeholders.

    InterfaceRunner/HttpRunner substitutes ``$accessToken`` / ``${accessToken}``,
    but not Django-style ``{{accessToken}}``. Some interfaces also still store
    Postman-style ``$accessToken`` already; leave those alone.
    """
    headers = interface_data.get('headers')
    if not isinstance(headers, list):
        return
    replacements = (
        ('{{accessToken}}', '$accessToken'),
        ('{{adminToken}}', '$adminToken'),
        ('{{assigneeToken}}', '$assigneeToken'),
        ('{{noCommonToken}}', '$noCommonToken'),
    )
    for item in headers:
        if not isinstance(item, dict):
            continue
        value = str(item.get('value') or '')
        if not value:
            continue
        updated = value
        for old, new in replacements:
            updated = updated.replace(old, new)
        if updated != value:
            item['value'] = updated


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


def _should_persist_variable_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return False
        if _TEMPLATE_PATTERN.search(stripped):
            return False
    return True


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


def _ticket_row_is_already_assigned(row: Dict[str, Any]) -> bool:
    """Target system already moved the ticket past manual assign."""
    status = str(row.get('status') or '').strip()
    assignee = row.get('assigneeUserId')
    assignee_obj = row.get('assignee') if isinstance(row.get('assignee'), dict) else {}
    if assignee in (None, '', 0) and assignee_obj.get('id') not in (None, '', 0):
        assignee = assignee_obj.get('id')
    if assignee in (None, '', 0):
        return False
    return status in {'pending_process', 'processing', 'claimed'}


def _ticket_row_assignee_id(row: Dict[str, Any]) -> str:
    assignee_obj = row.get('assignee') if isinstance(row.get('assignee'), dict) else {}
    value = row.get('assigneeUserId')
    if value in (None, '', 0):
        value = assignee_obj.get('id')
    return str(value).strip() if value not in (None, '', 0) else ''


def _ticket_row_assignee_name(row: Dict[str, Any]) -> str:
    assignee_obj = row.get('assignee') if isinstance(row.get('assignee'), dict) else {}
    value = (
        row.get('assigneeName')
        or assignee_obj.get('name')
        or assignee_obj.get('username')
        or ''
    )
    return str(value).strip()


def _assignee_matches_desired(
    row: Dict[str, Any],
    *,
    desired_user_id: Any = None,
    desired_name: Any = None,
) -> bool:
    """Whether the ticket is already assigned to the intended person."""
    current_id = _ticket_row_assignee_id(row)
    current_name = _ticket_row_assignee_name(row)
    wanted_id = str(desired_user_id).strip() if desired_user_id not in (None, '', 0) else ''
    wanted_name = str(desired_name or '').strip()
    if wanted_id and current_id:
        return wanted_id == current_id
    if wanted_name and current_name:
        return wanted_name == current_name or wanted_name in current_name or current_name in wanted_name
    return False


def _query_ticket_rows_for_plan(
    *,
    project_id: int,
    environment_id: int,
    query_path: str,
) -> List[Dict[str, Any]]:
    """Best-effort ticket list lookup during plan execution."""
    try:
        from api_environments.token_refresh import refresh_environment_tokens

        iface = ApiInterface.objects.filter(
            project_id=project_id,
            name='工单列表查询',
        ).first()
        if iface is None:
            return []
        environment = ApiEnvironment.objects.filter(id=environment_id).first()
        if environment is None:
            return []

        interface_data = iface.get_interface_data()
        interface_data['project_id'] = project_id
        interface_data['base_url'] = environment.base_url or ''
        interface_data['verify'] = environment.verify_ssl
        interface_data['method'] = 'GET'
        interface_data['url'] = query_path

        runner = InterfaceRunner(interface_data)
        runner.variables = refresh_environment_tokens(
            base_url=environment.base_url,
            variables=environment.get_all_variables(),
            verify_ssl=environment.verify_ssl,
            environment_id=environment.id,
            persist=True,
            force_token_vars=_collect_force_refresh_token_vars(interface_data),
        )
        runner.run_interface({})
        response_data = runner.get_response()
        body = _parse_response_body((response_data.get('response') or {}).get('content'))
        if isinstance(body, dict):
            block = body.get('data')
            if isinstance(block, list):
                return [row for row in block if isinstance(row, dict)]
        if isinstance(body, list):
            return [row for row in body if isinstance(row, dict)]
    except Exception as exc:
        logger.debug('plan executor ticket lookup skipped: %s', exc)
    return []


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
        self._active_run: Optional[DataGenerationRun] = None

    def _snapshot_context(self) -> Dict[str, Any]:
        return {
            k: v
            for k, v in self.context.items()
            if not isinstance(v, (dict, list))
        }

    def execute(self, run: Optional[DataGenerationRun] = None) -> DataGenerationRun:
        if run is None:
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
        self._active_run = run

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

            continued_failures = [
                entry
                for entry in self.step_logs
                if isinstance(entry, dict) and entry.get('status') == 'failed_continued'
            ]
            run.status = DataGenerationRun.STATUS_SUCCESS
            run.output_snapshot = dict(self.context)
            run.step_logs = self.step_logs
            run.finished_at = timezone.now()
            if continued_failures:
                names = [
                    str(entry.get('name') or f"步骤{entry.get('index')}")
                    for entry in continued_failures
                ]
                run.error_message = f"部分步骤失败（已忽略）: {'、'.join(names)}"
            run.save(
                update_fields=[
                    'status',
                    'output_snapshot',
                    'step_logs',
                    'finished_at',
                    'error_message',
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
        finally:
            self._active_run = None

    def _persist_run_progress(self) -> None:
        if self._active_run is None:
            return
        self._active_run.step_logs = list(self.step_logs)
        self._active_run.output_snapshot = self._snapshot_context()
        self._active_run.save(update_fields=['step_logs', 'output_snapshot'])

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
            if step.get('continue_on_error'):
                log_entry['status'] = 'failed_continued'
                log_entry['error'] = str(exc)
                log_entry['continued'] = True
                log_entry['context_after'] = self._snapshot_context()
                self.step_logs.append(log_entry)
                self._persist_run_progress()
                return
            log_entry['status'] = 'failed'
            log_entry['error'] = str(exc)
            log_entry['context_after'] = self._snapshot_context()
            self.step_logs.append(log_entry)
            self._persist_run_progress()
            raise
        except Exception as exc:
            wrapped = DataGenerationError(f'{step_name} 执行失败: {exc}')
            if step.get('continue_on_error'):
                log_entry['status'] = 'failed_continued'
                log_entry['error'] = str(wrapped)
                log_entry['continued'] = True
                log_entry['context_after'] = self._snapshot_context()
                self.step_logs.append(log_entry)
                self._persist_run_progress()
                return
            log_entry['status'] = 'failed'
            log_entry['error'] = str(wrapped)
            log_entry['context_after'] = self._snapshot_context()
            self.step_logs.append(log_entry)
            self._persist_run_progress()
            raise wrapped from exc

        log_entry['context_after'] = self._snapshot_context()
        self.step_logs.append(log_entry)
        self._persist_run_progress()

    def _resolve_environment(self, step: Dict[str, Any]) -> Optional[ApiEnvironment]:
        env_id = step.get('environment_id') or self.default_environment_id
        if not env_id:
            return None
        try:
            return ApiEnvironment.objects.get(id=env_id, project_id=self.plan.project_id)
        except ApiEnvironment.DoesNotExist as exc:
            raise DataGenerationError(f'API 环境不存在: {env_id}') from exc

    def _is_manual_assign_api_step(self, step: Dict[str, Any]) -> bool:
        step_name = str(step.get('name') or '').strip()
        if step_name == '分配工单':
            return True
        if step.get('interface_ref') == REF_ASSIGN_TICKET:
            return True
        interface_id = step.get('interface_id')
        if not interface_id:
            return False
        interface = ApiInterface.objects.filter(
            id=interface_id,
            project_id=self.plan.project_id,
        ).first()
        if interface is None:
            return False
        name = str(interface.name or '')
        return '分配工单' in name or (name.startswith('分配') and '取消' not in name)

    def _find_ticket_row_for_context(self) -> Optional[Dict[str, Any]]:
        env_id = self.default_environment_id
        if not env_id:
            return None
        ticket_no = self.context.get('ticketNo')
        ticket_id = self.context.get('ticketId') or self.context.get('processingTicketId')
        if ticket_no:
            rows = _query_ticket_rows_for_plan(
                project_id=self.plan.project_id,
                environment_id=int(env_id),
                query_path=f'/api/tickets?ticketNo={ticket_no}&page=1&pageSize=5',
            )
            for row in rows:
                if str(row.get('ticketNo') or '') == str(ticket_no):
                    return row
        if ticket_id:
            rows = _query_ticket_rows_for_plan(
                project_id=self.plan.project_id,
                environment_id=int(env_id),
                query_path=f'/api/tickets/{ticket_id}',
            )
            if rows:
                return rows[0]
            rows = _query_ticket_rows_for_plan(
                project_id=self.plan.project_id,
                environment_id=int(env_id),
                query_path=f'/api/tickets?id={ticket_id}&page=1&pageSize=5',
            )
            for row in rows:
                if str(row.get('id') or row.get('ticketId') or '') == str(ticket_id):
                    return row
        return None

    def _merge_assign_context_from_ticket_row(self, row: Dict[str, Any]) -> None:
        assignee = row.get('assignee') if isinstance(row.get('assignee'), dict) else {}
        ticket_id = row.get('id') or row.get('ticketId') or self.context.get('ticketId')
        if ticket_id:
            self.context['processingTicketId'] = ticket_id
            self.context.setdefault('ticketId', ticket_id)
        assignee_id = row.get('assigneeUserId') or assignee.get('id')
        assignee_name = row.get('assigneeName') or assignee.get('name') or assignee.get('username')
        if assignee_id not in (None, '', 0):
            self.context['assigneeUserId'] = assignee_id
        if assignee_name:
            self.context['assigneeName'] = assignee_name
        self.context['ticketStatus'] = row.get('status') or 'pending_process'

    def _desired_assign_target(self, step: Dict[str, Any]) -> Dict[str, Any]:
        step_variables = substitute_templates(step.get('variables') or {}, self.context)
        if not isinstance(step_variables, dict):
            step_variables = {}
        return {
            'assigneeUserId': step_variables.get('assigneeUserId', self.context.get('assigneeUserId')),
            'assigneeName': step_variables.get('assigneeName', self.context.get('assigneeName')),
        }

    def _maybe_skip_manual_assign_api_call(self, step: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self._is_manual_assign_api_step(step):
            return None
        row = self._find_ticket_row_for_context()
        if row is None or not _ticket_row_is_already_assigned(row):
            return None
        desired = self._desired_assign_target(step)
        # TYPE_A 创建后常被自动分给其他人；只有目标处理人一致时才能跳过手动分配，
        # 否则登录账号与造数工单会对不上（如李亮登录却分给了张岩）。
        if not _assignee_matches_desired(
            row,
            desired_user_id=desired.get('assigneeUserId'),
            desired_name=desired.get('assigneeName'),
        ):
            logger.info(
                'Keep manual assign API: ticket assigned to someone else '
                'ticketNo=%s current=%s desired=%s/%s',
                row.get('ticketNo') or self.context.get('ticketNo'),
                _ticket_row_assignee_name(row) or _ticket_row_assignee_id(row),
                desired.get('assigneeName'),
                desired.get('assigneeUserId'),
            )
            return None
        self._merge_assign_context_from_ticket_row(row)
        logger.info(
            'Skip manual assign API: ticket already assigned ticketNo=%s assignee=%s status=%s',
            row.get('ticketNo') or self.context.get('ticketNo'),
            row.get('assigneeName') or row.get('assigneeUserId'),
            row.get('status'),
        )
        return {
            'skipped': True,
            'reason': 'ticket_already_assigned',
            'ticketNo': row.get('ticketNo'),
            'assigneeUserId': self.context.get('assigneeUserId'),
        }

    def _lookup_user_login_username(
        self,
        *,
        display_name: str,
        environment: ApiEnvironment,
        access_token: str,
        preferred_role: Optional[str] = None,
    ) -> str:
        import requests

        from projects.models import ProjectCredential

        name = str(display_name or '').strip()
        if not name:
            raise DataGenerationError('缺少处理人姓名，无法查询登录账号')

        # 项目凭据里 user_role 常填姓名（如张岩→802576），优先走本地映射。
        credential = (
            ProjectCredential.objects.filter(project_id=self.plan.project_id)
            .exclude(username='')
            .filter(user_role__iexact=name)
            .first()
        )
        if credential is not None and str(credential.username or '').strip():
            return str(credential.username).strip()

        # transfer-candidates 的 role 必填且枚举有限；sales/空 role 会直接 400。
        # TYPE_A 自动分配常见 assigneeRole=test（张岩）。
        roles: list[str] = []
        for role in (
            preferred_role,
            self.context.get('assigneeRole'),
            'customer_service',
            'product',
            'test',
        ):
            normalized = str(role or '').strip()
            if normalized and normalized not in roles:
                roles.append(normalized)

        last_error = None
        for role in roles:
            try:
                response = requests.get(
                    environment.base_url.rstrip('/') + '/api/users/transfer-candidates',
                    headers={'Authorization': f'Bearer {access_token}'},
                    params={
                        'keyword': name,
                        'role': role,
                        'page': 1,
                        'pageSize': 100,
                    },
                    verify=environment.verify_ssl,
                    timeout=30,
                )
                if response.status_code >= 400:
                    last_error = f'HTTP {response.status_code} role={role}'
                    continue
                payload = response.json()
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                continue
            candidates = payload.get('data') if isinstance(payload, dict) else None
            if not isinstance(candidates, list):
                last_error = '返回格式无效'
                continue
            exact = [
                row
                for row in candidates
                if isinstance(row, dict)
                and (
                    str(row.get('name') or '').strip() == name
                    or str(row.get('username') or '').strip() == name
                )
            ]
            if not exact:
                continue
            username = (
                str(exact[0].get('username') or '').strip()
                or str(exact[0].get('wecomUserId') or '').strip()
            )
            if username:
                return username
            last_error = f'处理人“{name}”缺少登录用户名'
        detail = f'；详情: {last_error}' if last_error else ''
        raise DataGenerationError(f'未找到处理人“{name}”的登录账号{detail}')

    def _resolve_login_password_for_username(
        self,
        *,
        username: str,
        environment: ApiEnvironment,
        fallback_password: str,
        display_name: str = '',
    ) -> List[str]:
        """Return candidate passwords for a username (credential / self / fallback)."""
        from projects.models import ProjectCredential

        passwords: list[str] = []
        user = str(username or '').strip()
        name = str(display_name or '').strip()

        def _add(value: Any) -> None:
            text = str(value or '').strip()
            if text and text not in passwords:
                passwords.append(text)

        if user:
            for credential in ProjectCredential.objects.filter(
                project_id=self.plan.project_id,
            ).exclude(password='').filter(username__iexact=user):
                _add(credential.password)
        if name:
            for credential in ProjectCredential.objects.filter(
                project_id=self.plan.project_id,
            ).exclude(password='').filter(user_role__iexact=name):
                _add(credential.password)
        # 业务侧常见「账号=密码」账号（如张岩 802576/802576）
        _add(user)
        env_variables = environment.get_all_variables()
        if isinstance(env_variables, dict):
            for key in ('assigneePassword', 'noCommonPassword', 'adminPassword'):
                _add(env_variables.get(key))
        _add(fallback_password)
        return passwords

    def _login_any_username(
        self,
        *,
        usernames: List[str],
        environment: ApiEnvironment,
        fallback_password: str,
        display_name: str = '',
    ) -> tuple[str, str]:
        errors: list[str] = []
        for username in usernames:
            passwords = self._resolve_login_password_for_username(
                username=username,
                environment=environment,
                fallback_password=fallback_password,
                display_name=display_name,
            )
            for password in passwords:
                try:
                    token = self._login_user_token(
                        environment=environment,
                        username=username,
                        password=password,
                    )
                    return username, token
                except DataGenerationError as exc:
                    errors.append(f'{username}/{password[:2]}***: {exc}')
        detail = '；'.join(errors[:4]) if errors else '无可用账号'
        raise DataGenerationError(f'处理人登录失败（{detail}）')

    def _lookup_user_login_candidates(
        self,
        *,
        display_name: str,
        environment: ApiEnvironment,
        access_token: str,
        preferred_role: Optional[str] = None,
    ) -> List[str]:
        """Return possible login usernames for a display name (credential + API)."""
        import requests

        from projects.models import ProjectCredential

        name = str(display_name or '').strip()
        usernames: list[str] = []
        if not name:
            return usernames

        for credential in ProjectCredential.objects.filter(
            project_id=self.plan.project_id,
        ).exclude(username='').filter(user_role__iexact=name):
            username = str(credential.username or '').strip()
            if username and username not in usernames:
                usernames.append(username)

        # 环境变量里常见「通用处理人」账号（林业丰 / noCommonUsername）
        env_vars = environment.get_all_variables() if environment is not None else {}
        if isinstance(env_vars, dict):
            common_user = str(env_vars.get('noCommonUsername') or '').strip()
            common_name = str(
                env_vars.get('noCommonName')
                or env_vars.get('noCommonDisplayName')
                or ''
            ).strip()
            if common_user and (
                not common_name or common_name == name or name in ('林业丰', '林业务')
            ):
                if common_user not in usernames:
                    usernames.append(common_user)

        roles: list[str] = []
        for role in (
            preferred_role,
            self.context.get('assigneeRole'),
            'customer_service',
            'product',
            'test',
        ):
            normalized = str(role or '').strip()
            if normalized and normalized not in roles:
                roles.append(normalized)

        for role in roles:
            try:
                response = requests.get(
                    environment.base_url.rstrip('/') + '/api/users/transfer-candidates',
                    headers={'Authorization': f'Bearer {access_token}'},
                    params={
                        'keyword': name,
                        'role': role,
                        'page': 1,
                        'pageSize': 100,
                    },
                    verify=environment.verify_ssl,
                    timeout=30,
                )
                if response.status_code >= 400:
                    continue
                payload = response.json()
            except Exception:  # noqa: BLE001
                continue
            candidates = payload.get('data') if isinstance(payload, dict) else None
            if not isinstance(candidates, list):
                continue
            for row in candidates:
                if not isinstance(row, dict):
                    continue
                if (
                    str(row.get('name') or '').strip() != name
                    and str(row.get('username') or '').strip() != name
                ):
                    continue
                username = (
                    str(row.get('username') or '').strip()
                    or str(row.get('wecomUserId') or '').strip()
                )
                if username and username not in usernames:
                    usernames.append(username)
        return usernames

    def _adopt_existing_ticket_for_desired_assignee(
        self,
        desired: Dict[str, Any],
        *,
        statuses: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Reuse a ticket already owned by the desired person when reassign is impossible."""
        env_id = self.default_environment_id
        if not env_id:
            return None
        desired_name = str(desired.get('assigneeName') or '').strip()
        desired_user_id = desired.get('assigneeUserId')
        if not desired_name and desired_user_id in (None, '', 0):
            return None

        status_list = statuses or ['processing', 'pending_process']
        for status in status_list:
            query = f'/api/tickets?status={status}&page=1&pageSize=50'
            if desired_user_id not in (None, '', 0):
                query += f'&assigneeUserId={desired_user_id}'
            rows = _query_ticket_rows_for_plan(
                project_id=self.plan.project_id,
                environment_id=int(env_id),
                query_path=query,
            )
            for row in rows:
                if not _assignee_matches_desired(
                    row,
                    desired_user_id=desired_user_id,
                    desired_name=desired_name,
                ):
                    continue
                # 移交审批按钮要求未提交审批。
                approval = str(row.get('approvalStatus') or 'none').strip().lower()
                if approval not in ('', 'none', 'null', 'nil'):
                    continue
                self._merge_assign_context_from_ticket_row(row)
                ticket_no = row.get('ticketNo')
                ticket_id = row.get('id') or row.get('ticketId')
                if ticket_no:
                    self.context['ticketNo'] = ticket_no
                if ticket_id:
                    self.context['ticketId'] = ticket_id
                    self.context['processingTicketId'] = ticket_id
                self.context['ticketStatus'] = row.get('status') or status
                logger.info(
                    'Adopt existing ticket for desired assignee ticketNo=%s status=%s assignee=%s',
                    ticket_no,
                    row.get('status'),
                    desired_name or desired_user_id,
                )
                return {
                    'skipped': True,
                    'reason': 'adopted_existing_desired_assignee_ticket',
                    'ticketNo': ticket_no,
                    'assigneeUserId': self.context.get('assigneeUserId'),
                    'ticketStatus': self.context.get('ticketStatus'),
                }
        return None

    def _is_claim_api_step(self, step: Dict[str, Any]) -> bool:
        step_name = str(step.get('name') or '').strip()
        if step_name == '领取工单':
            return True
        if step.get('interface_ref') == REF_CLAIM_TICKET:
            return True
        interface_id = step.get('interface_id')
        if not interface_id:
            return False
        interface = ApiInterface.objects.filter(
            id=interface_id,
            project_id=self.plan.project_id,
        ).first()
        return bool(interface and '领取' in str(interface.name or ''))

    def _maybe_skip_claim_api_call(self, step: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self._is_claim_api_step(step):
            return None
        row = self._find_ticket_row_for_context()
        if row is None:
            return None
        status = str(row.get('status') or '').strip()
        if status in {'processing', 'claimed'}:
            self._merge_assign_context_from_ticket_row(row)
            self.context['ticketStatus'] = status
            logger.info(
                'Skip claim API: ticket already %s ticketNo=%s',
                status,
                row.get('ticketNo') or self.context.get('ticketNo'),
            )
            return {
                'skipped': True,
                'reason': 'ticket_already_claimed_or_processing',
                'ticketNo': row.get('ticketNo'),
                'ticketStatus': status,
            }
        return None

    def _login_user_token(
        self,
        *,
        environment: ApiEnvironment,
        username: str,
        password: str,
    ) -> str:
        from api_environments.token_refresh import _login_for_token

        try:
            return _login_for_token(
                environment.base_url,
                username,
                password,
                verify_ssl=environment.verify_ssl,
            )
        except Exception as exc:
            raise DataGenerationError(f'登录账号 {username} 失败: {exc}') from exc

    def _reassign_if_wrong_assignee(self, step: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """When TYPE_A auto-assign lands on the wrong person, move it to the desired assignee.

        1) cancel-assign + continue normal assign
        2) else transfer from current holder -> desired, then accept as desired
           (so the ticket becomes a normal pending ticket owned by the target)
        """
        if not self._is_manual_assign_api_step(step):
            return None
        row = self._find_ticket_row_for_context()
        if row is None or not _ticket_row_is_already_assigned(row):
            return None
        desired = self._desired_assign_target(step)
        if _assignee_matches_desired(
            row,
            desired_user_id=desired.get('assigneeUserId'),
            desired_name=desired.get('assigneeName'),
        ):
            return None

        ticket_id = row.get('id') or row.get('ticketId') or self.context.get('ticketId')
        current_name = _ticket_row_assignee_name(row) or _ticket_row_assignee_id(row)
        desired_label = desired.get('assigneeName') or desired.get('assigneeUserId')
        desired_user_id = desired.get('assigneeUserId')
        environment = self._resolve_environment(step)
        if environment is None:
            raise DataGenerationError('缺少 API 环境，无法将自动分配工单改派给目标处理人')
        if not ticket_id:
            raise DataGenerationError('工单已被其他人领取/分配，但缺少 ticketId，无法改派')
        if desired_user_id in (None, '', 0):
            raise DataGenerationError(f'缺少目标处理人 ID，无法从 {current_name} 改派给 {desired_label}')

        # 1) Try cancel assign.
        unassign_error = None
        iface = ApiInterface.objects.filter(
            project_id=self.plan.project_id,
            name='取消分配工单',
        ).first()
        if iface is not None:
            try:
                self._run_named_ticket_mutation(
                    interface=iface,
                    environment=environment,
                    ticket_id=ticket_id,
                    step=step,
                    trace_prefix='data-gen-unassign',
                    force_empty_json_body=True,
                )
                logger.info(
                    'Unassigned auto-assignee before reassign ticketId=%s from=%s to=%s',
                    ticket_id,
                    current_name,
                    desired_label,
                )
                return None
            except DataGenerationError as exc:
                unassign_error = str(exc)
                logger.warning('Cancel assign failed, will try transfer+accept: %s', exc)

        # 2) Transfer current holder -> desired, then accept as desired.
        from api_environments.token_refresh import refresh_environment_tokens

        env_variables = environment.get_all_variables()
        if not isinstance(env_variables, dict):
            env_variables = {}
        refreshed = refresh_environment_tokens(
            base_url=environment.base_url,
            variables=env_variables,
            verify_ssl=environment.verify_ssl,
            environment_id=environment.id,
            persist=True,
        )
        if isinstance(refreshed, dict):
            env_variables.update(refreshed)
        access_token = str(
            env_variables.get('accessToken') or env_variables.get('adminToken') or ''
        ).strip()
        if not access_token:
            raise DataGenerationError('API 环境缺少管理员令牌，无法查询处理人登录账号')

        default_password = str(
            env_variables.get('assigneePassword')
            or env_variables.get('noCommonPassword')
            or '000000'
        )
        holder_role = str(row.get('assigneeRole') or '').strip() or None
        try:
            holder_usernames = self._lookup_user_login_candidates(
                display_name=str(current_name),
                environment=environment,
                access_token=access_token,
                preferred_role=holder_role,
            )
            if not holder_usernames:
                raise DataGenerationError(f'未找到处理人“{current_name}”的登录账号')
            _holder_username, holder_token = self._login_any_username(
                usernames=holder_usernames,
                environment=environment,
                fallback_password=default_password,
                display_name=str(current_name),
            )
        except DataGenerationError as exc:
            adopted = self._adopt_existing_ticket_for_desired_assignee(desired)
            if adopted is not None:
                return adopted
            raise DataGenerationError(
                f'工单已被自动分配给 {current_name}，无法改派给 {desired_label}：{exc}'
                + (f'；取消分配错误: {unassign_error}' if unassign_error else '')
            ) from exc

        transfer_iface = ApiInterface.objects.filter(
            project_id=self.plan.project_id,
            name='转派工单',
        ).first()
        if transfer_iface is None:
            adopted = self._adopt_existing_ticket_for_desired_assignee(desired)
            if adopted is not None:
                return adopted
            raise DataGenerationError(
                f'工单已被自动分配给 {current_name}，项目未配置「转派工单」接口，无法改派给 {desired_label}'
                + (f'；取消分配错误: {unassign_error}' if unassign_error else '')
            )
        try:
            self._run_named_ticket_mutation(
                interface=transfer_iface,
                environment=environment,
                ticket_id=ticket_id,
                step=step,
                trace_prefix='data-gen-reassign-transfer',
                extra_variables={
                    'assigneeToken': holder_token,
                    'targetUserId': desired_user_id,
                    'targetRole': self.context.get('assigneeRole') or 'customer_service',
                    'reason': self.context.get('transferReason') or '自动化测试改派到目标处理人',
                },
            )
        except DataGenerationError:
            adopted = self._adopt_existing_ticket_for_desired_assignee(desired)
            if adopted is not None:
                return adopted
            raise

        # 业务侧转派可直接落处理人，无需再 accept；accept 常 404。
        post_transfer = self._find_ticket_row_for_context()
        if post_transfer is not None and _assignee_matches_desired(
            post_transfer,
            desired_user_id=desired_user_id,
            desired_name=desired.get('assigneeName'),
        ):
            self.context['assigneeUserId'] = desired_user_id
            if desired.get('assigneeName'):
                self.context['assigneeName'] = desired.get('assigneeName')
            self.context['processingTicketId'] = ticket_id
            self.context.setdefault('ticketId', ticket_id)
            self.context['ticketStatus'] = (
                post_transfer.get('status') or 'pending_process'
            )
            logger.info(
                'Reassigned via transfer (accept skipped) ticketId=%s from=%s to=%s',
                ticket_id,
                current_name,
                desired_label,
            )
            return {
                'skipped': True,
                'reason': 'reassigned_via_transfer',
                'ticketNo': post_transfer.get('ticketNo') or self.context.get('ticketNo'),
                'assigneeUserId': desired_user_id,
            }

        desired_name = str(desired.get('assigneeName') or desired_label).strip()
        try:
            desired_usernames = self._lookup_user_login_candidates(
                display_name=desired_name,
                environment=environment,
                access_token=access_token,
                preferred_role=str(self.context.get('assigneeRole') or 'customer_service'),
            )
            if not desired_usernames:
                raise DataGenerationError(f'未找到处理人“{desired_name}”的登录账号')
            _desired_username, desired_token = self._login_any_username(
                usernames=desired_usernames,
                environment=environment,
                fallback_password=default_password,
                display_name=desired_name,
            )
        except DataGenerationError:
            adopted = self._adopt_existing_ticket_for_desired_assignee(desired)
            if adopted is not None:
                return adopted
            raise

        accept_iface = ApiInterface.objects.filter(
            project_id=self.plan.project_id,
            name='接受转派',
        ).first()
        if accept_iface is None:
            # 转派已成功且无 accept 接口时，仍可继续领取。
            self.context['assigneeUserId'] = desired_user_id
            if desired.get('assigneeName'):
                self.context['assigneeName'] = desired.get('assigneeName')
            self.context['processingTicketId'] = ticket_id
            self.context.setdefault('ticketId', ticket_id)
            self.context['ticketStatus'] = 'pending_process'
            logger.info(
                'Reassigned via transfer (no accept iface) ticketId=%s from=%s to=%s',
                ticket_id,
                current_name,
                desired_label,
            )
            return {
                'skipped': True,
                'reason': 'reassigned_via_transfer_no_accept',
                'ticketNo': row.get('ticketNo') or self.context.get('ticketNo'),
                'assigneeUserId': desired_user_id,
            }
        try:
            self._run_named_ticket_mutation(
                interface=accept_iface,
                environment=environment,
                ticket_id=ticket_id,
                step=step,
                trace_prefix='data-gen-reassign-accept',
                force_empty_json_body=True,
                url_override=f'/api/tickets/{ticket_id}/transfer/accept',
                extra_variables={'assigneeToken': desired_token, 'accessToken': desired_token},
            )
        except DataGenerationError as exc:
            # accept 404/已处理：若工单已在目标人名下则视为成功。
            verify_row = self._find_ticket_row_for_context()
            if verify_row is not None and _assignee_matches_desired(
                verify_row,
                desired_user_id=desired_user_id,
                desired_name=desired.get('assigneeName'),
            ):
                logger.warning(
                    'Accept transfer failed but assignee already desired, continue: %s',
                    exc,
                )
            else:
                adopted = self._adopt_existing_ticket_for_desired_assignee(desired)
                if adopted is not None:
                    return adopted
                raise

        self.context['assigneeUserId'] = desired_user_id
        if desired.get('assigneeName'):
            self.context['assigneeName'] = desired.get('assigneeName')
        self.context['processingTicketId'] = ticket_id
        self.context.setdefault('ticketId', ticket_id)
        self.context['ticketStatus'] = 'pending_process'
        logger.info(
            'Reassigned via transfer+accept ticketId=%s from=%s to=%s',
            ticket_id,
            current_name,
            desired_label,
        )
        return {
            'skipped': True,
            'reason': 'reassigned_via_transfer_accept',
            'ticketNo': row.get('ticketNo') or self.context.get('ticketNo'),
            'assigneeUserId': desired_user_id,
        }

    def _run_named_ticket_mutation(
        self,
        *,
        interface: ApiInterface,
        environment: ApiEnvironment,
        ticket_id: Any,
        step: Dict[str, Any],
        trace_prefix: str,
        extra_variables: Optional[Dict[str, Any]] = None,
        force_empty_json_body: bool = False,
        url_override: Optional[str] = None,
    ) -> None:
        interface_data = interface.get_interface_data()
        interface_data['project_id'] = self.plan.project_id
        interface_data['trace_id'] = new_trace_id(trace_prefix)
        interface_data['body_source'] = 'data_generation_plan'
        _normalize_legacy_auth_header_placeholders(interface_data)
        interface_data['base_url'] = environment.base_url or ''
        interface_data['verify'] = environment.verify_ssl
        if url_override:
            interface_data['url'] = url_override
            interface_data['path'] = url_override
        if force_empty_json_body:
            interface_data['body'] = {'type': 'none', 'content': ''}
        # 接受转派等接口可能存了硬编码 JWT；改派时强制走变量令牌。
        if extra_variables and (
            extra_variables.get('assigneeToken') or extra_variables.get('accessToken')
        ):
            token_expr = (
                '$assigneeToken'
                if extra_variables.get('assigneeToken')
                else '$accessToken'
            )
            headers = interface_data.get('headers')
            if not isinstance(headers, list):
                headers = []
            replaced = False
            new_headers = []
            for item in headers:
                if not isinstance(item, dict):
                    continue
                row = dict(item)
                if str(row.get('key') or '').lower() == 'authorization':
                    row['value'] = f'Bearer {token_expr}'
                    row['enabled'] = True
                    replaced = True
                new_headers.append(row)
            if not replaced:
                new_headers.append(
                    {
                        'key': 'Authorization',
                        'value': f'Bearer {token_expr}',
                        'enabled': True,
                    }
                )
            interface_data['headers'] = new_headers

        runner = InterfaceRunner(interface_data)
        runner.variables = {}
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
        if extra_variables:
            runner.variables.update(extra_variables)
        runner.variables['ticketId'] = ticket_id

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

    def _unassign_ticket_before_reassign(self, step: Dict[str, Any]) -> None:
        # Kept for compatibility; reassignment is handled by _reassign_if_wrong_assignee.
        return

    def _run_api_call(self, step: Dict[str, Any]) -> Dict[str, Any]:
        skipped = self._maybe_skip_manual_assign_api_call(step)
        if skipped is not None:
            return skipped
        reassigned = self._reassign_if_wrong_assignee(step)
        if reassigned is not None:
            return reassigned
        claim_skipped = self._maybe_skip_claim_api_call(step)
        if claim_skipped is not None:
            return claim_skipped

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
        _normalize_legacy_auth_header_placeholders(interface_data)

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
            error_text = str(error_detail)
            if self._is_manual_assign_api_step(step) and 'TICKET_STATUS_INVALID' in error_text:
                row = self._find_ticket_row_for_context()
                desired = self._desired_assign_target(step)
                if (
                    row is not None
                    and _ticket_row_is_already_assigned(row)
                    and _assignee_matches_desired(
                        row,
                        desired_user_id=desired.get('assigneeUserId'),
                        desired_name=desired.get('assigneeName'),
                    )
                ):
                    self._merge_assign_context_from_ticket_row(row)
                    logger.info(
                        'Treat assign API failure as success: ticket already assigned ticketNo=%s',
                        row.get('ticketNo') or self.context.get('ticketNo'),
                    )
                    return {
                        'skipped': True,
                        'reason': 'assign_rejected_ticket_already_assigned',
                        'interface_name': interface.name,
                        'status_code': status_code,
                    }
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
        if not isinstance(variables, dict):
            raise DataGenerationError('set_env_var 步骤 variables 不能为空')

        variables = {
            name: value
            for name, value in variables.items()
            if _should_persist_variable_value(value)
        }
        if not variables:
            return {'environment_id': environment.id, 'variables': [], 'skipped': True}

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
                if not _should_persist_variable_value(value):
                    continue
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

        if not saved:
            return {'items': [], 'skipped': True}

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


def _build_plan_executor(
    plan: DataGenerationPlan,
    *,
    trigger_type: str = DataGenerationRun.TRIGGER_MANUAL,
    input_params: Optional[Dict[str, Any]] = None,
    triggered_by=None,
    test_execution=None,
    default_environment_id: Optional[int] = None,
    steps_override: Optional[List[Dict[str, Any]]] = None,
    parent_run: Optional[DataGenerationRun] = None,
) -> PlanExecutor:
    return PlanExecutor(
        plan,
        trigger_type=trigger_type,
        input_params=input_params,
        triggered_by=triggered_by,
        test_execution=test_execution,
        default_environment_id=default_environment_id,
        steps_override=steps_override,
        parent_run=parent_run,
    )


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
    executor = _build_plan_executor(
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


def _fail_stale_running_run(run_id: int, message: str) -> None:
    """后台线程异常退出时，将仍为 running 的记录标记为 failed。"""
    close_old_connections()
    DataGenerationRun.objects.filter(
        id=run_id,
        status=DataGenerationRun.STATUS_RUNNING,
    ).update(
        status=DataGenerationRun.STATUS_FAILED,
        error_message=message,
        finished_at=timezone.now(),
    )


def execute_plan_async(
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
    """创建执行记录并在后台线程中运行，便于前端轮询进度。"""
    run = DataGenerationRun.objects.create(
        plan=plan,
        project=plan.project,
        status=DataGenerationRun.STATUS_RUNNING,
        trigger_type=trigger_type,
        test_execution=test_execution,
        input_params=input_params or {},
        triggered_by=triggered_by,
        parent_run=parent_run,
        started_at=timezone.now(),
    )
    executor = _build_plan_executor(
        plan,
        trigger_type=trigger_type,
        input_params=input_params,
        triggered_by=triggered_by,
        test_execution=test_execution,
        default_environment_id=default_environment_id,
        steps_override=steps_override,
        parent_run=parent_run,
    )

    def _run_in_background() -> None:
        close_old_connections()
        try:
            executor.execute(run=run)
        except Exception as exc:
            logger.exception(
                'Background data generation failed: plan_id=%s run_id=%s',
                plan.id,
                run.id,
            )
            _fail_stale_running_run(run.id, f'后台执行异常: {exc}')
        finally:
            close_old_connections()

    thread = threading.Thread(
        target=_run_in_background,
        name=f'data-gen-run-{run.id}',
        daemon=True,
    )
    thread.start()
    return run


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


def bind_suite_pre_data_plan(
    suite,
    *,
    created_by,
    default_environment_id: Optional[int] = None,
    use_llm: bool = True,
    enable_post_cleanup: bool = True,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """分析套件变量缺口 → AI/规则生成计划 → 创建并绑定为 pre_data_plan。"""
    from .analysis import analyze_suite_variable_gaps, build_generation_description_from_gap
    from .llm_plan_generator import (
        _resolve_plan_steps_for_project,
        build_plan_from_template_key,
        generate_plan_from_description_with_llm,
    )
    from .serializers import DataGenerationPlanSerializer

    env_id = default_environment_id or getattr(suite, 'pre_data_environment_id', None)
    gap = analyze_suite_variable_gaps(suite, environment_id=env_id)
    generation_description = (description or '').strip() or gap.get('recommended_description') or build_generation_description_from_gap(
        suite.name,
        gap.get('missing_variables') or [],
        gap.get('recommended_template_key'),
    )

    generated = None
    recommended_key = gap.get('recommended_template_key')
    if recommended_key:
        try:
            generated = build_plan_from_template_key(
                str(recommended_key),
                generation_description,
                project_id=suite.project_id,
                default_environment_id=env_id,
                generation_method='rule_match',
            )
        except Exception as exc:
            logger.warning(
                'Recommended template %s expand failed for suite %s: %s',
                recommended_key,
                suite.id,
                exc,
            )

    if generated is None:
        generated = generate_plan_from_description_with_llm(
            generation_description,
            project_id=suite.project_id,
            default_environment_id=env_id,
            suite_id=suite.id,
            use_llm=use_llm,
        )
    generated = _resolve_plan_steps_for_project(
        generated,
        project_id=suite.project_id,
        default_environment_id=env_id,
    )

    plan_payload = {
        'name': f'套件「{suite.name}」前置造数',
        'description': generated.get('description') or generation_description,
        'target_type': generated.get('target_type') or 'both',
        'steps': generated.get('steps') or [],
        'cleanup_steps': generated.get('cleanup_steps') or [],
        'default_environment': env_id or generated.get('default_environment'),
        'is_active': True,
        'is_template': False,
        'template_key': generated.get('template_key') or '',
        'template_params_schema': generated.get('template_params_schema') or {},
    }
    plan_name = plan_payload['name']
    existing_plan = DataGenerationPlan.objects.filter(
        project_id=suite.project_id,
        name=plan_name,
        is_template=False,
    ).first()
    serializer = DataGenerationPlanSerializer(
        existing_plan,
        data=plan_payload,
        context={'project_id': suite.project_id},
    )
    serializer.is_valid(raise_exception=True)
    plan = serializer.save(
        project=suite.project,
        created_by=existing_plan.created_by if existing_plan else created_by,
    )

    suite.pre_data_plan = plan
    if env_id:
        suite.pre_data_environment_id = env_id
    cleanup_steps = plan.cleanup_steps if isinstance(plan.cleanup_steps, list) else []
    if enable_post_cleanup and cleanup_steps:
        suite.post_data_cleanup = True
    suite.save(
        update_fields=[
            'pre_data_plan',
            'pre_data_environment',
            'post_data_cleanup',
            'updated_at',
        ]
    )

    return {
        'gap_analysis': gap,
        'plan': plan,
        'generation': generated,
        'bound': True,
        'created': existing_plan is None,
        'post_data_cleanup_enabled': bool(suite.post_data_cleanup),
    }
