"""方式 A：LLM 结构化造数计划生成。"""

from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from typing import Any, Dict, Optional

from .analysis import generate_plan_from_description
from .assignee_resolver import AssigneeResolutionError, resolve_assignee_params
from .intent_router import route_llm_payload
from .llm_context import build_llm_generation_context
from .serializers import DataGenerationPlanSerializer
from .templates import get_template_by_key

logger = logging.getLogger(__name__)

_JSON_BLOCK_PATTERN = re.compile(r'```(?:json)?\s*([\s\S]*?)```', re.IGNORECASE)
_ASSIGNMENT_TEMPLATE_KEYS = {
    'biz_create_and_assign',
    'biz_create_and_transfer',
    'biz_create_and_claim',
    'biz_create_assign_resolve',
}

SYSTEM_PROMPT = """你是 WHartTest 平台的造数计划生成器。根据用户需求与【可用资源】生成造数计划。

你必须只输出一个 JSON 对象，不要 markdown 说明。

支持两种 generation_mode：
1. template — 需求与已有模板高度匹配时优先使用
   字段：generation_mode, template_key, name(可选), description(可选), target_type(可选), input_params(可选)
2. custom — 仅当没有任何模板可匹配时使用
   字段：generation_mode, name, description, target_type, steps, cleanup_steps(可选)

规则：
- template_key 只能来自 templates 列表
- custom.steps 中 api_call 的 interface_id 只能来自 interfaces
- custom.steps 的 api_call 参数只能放在 variables，提取规则只能使用 extract；禁止使用 input_params/extracts/url_params
- set_env_var 必须含 environment_id（来自 environments）
- sql 必须含 database_config_id（来自 database_configs）
- custom_function 必须含 function_id（来自 custom_functions）
- 工单转派：优先 template_key=biz_create_and_transfer
- 工单待处理/分配：优先 template_key=biz_create_and_assign（创建后分配才会进入待处理）
- 工单领取：优先 template_key=biz_create_and_claim
- 工单完成闭环：优先 template_key=biz_create_assign_resolve
- 仅创建待分配工单：biz_create_type_a 或 biz_create_type_b
- 业务状态与模板：待分配=仅创建；待处理=创建+分配；已领取=创建+分配+领取
- 若提供了 suite_gap_analysis.missing_variables，计划应覆盖这些变量

【可用资源】
{context}
"""


def _extract_json_object(text: str) -> Dict[str, Any]:
    raw = (text or '').strip()
    if not raw:
        raise ValueError('LLM 返回为空')

    block_match = _JSON_BLOCK_PATTERN.search(raw)
    if block_match:
        raw = block_match.group(1).strip()

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = raw.find('{')
    end = raw.rfind('}')
    if start >= 0 and end > start:
        parsed = json.loads(raw[start : end + 1])
        if isinstance(parsed, dict):
            return parsed

    raise ValueError('无法从 LLM 响应中解析 JSON 对象')


def _get_active_llm(temperature: float = 0.2):
    from langgraph_integration.models import LLMConfig
    from langgraph_integration.views import create_llm_instance

    config = LLMConfig.objects.filter(is_active=True).first()
    if config is None:
        return None, None
    return create_llm_instance(config, temperature=temperature), config


def _validate_plan_dict(plan: Dict[str, Any], project_id: int) -> None:
    payload = {
        'name': plan.get('name') or 'LLM生成计划',
        'target_type': plan.get('target_type') or 'both',
        'steps': plan.get('steps') or [],
        'cleanup_steps': plan.get('cleanup_steps') or [],
        'default_environment': plan.get('default_environment'),
        'is_active': True,
    }
    serializer = DataGenerationPlanSerializer(
        data=payload,
        context={'project_id': project_id},
    )
    serializer.is_valid(raise_exception=True)


def _build_generation_summary(
    plan: Dict[str, Any],
    *,
    input_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    source = str(plan.get('source') or '')
    template_key = plan.get('template_key')
    mode = 'template' if template_key or source.startswith('llm:template:') else 'custom'
    if mode == 'template' and not template_key and source.startswith('llm:template:'):
        template_key = source.split(':', 2)[-1]

    summary: Dict[str, Any] = {
        'mode': mode,
        'template_key': template_key,
        'template_name': plan.get('name') if mode == 'template' else None,
        'step_count': len(plan.get('steps') or []),
        'input_params': input_params or plan.get('suggested_input_params') or {},
    }
    if template_key:
        template = get_template_by_key(str(template_key))
        if template:
            summary['template_name'] = template.get('name')
    return summary


def _expand_template_plan(
    llm_payload: Dict[str, Any],
    *,
    description: str,
    default_environment_id: Optional[int],
) -> Dict[str, Any]:
    template_key = llm_payload.get('template_key')
    if not template_key:
        raise ValueError('template 模式缺少 template_key')

    template = get_template_by_key(str(template_key))
    if template is None:
        raise ValueError(f'未知模板: {template_key}')

    input_params = llm_payload.get('input_params') or {}
    if not isinstance(input_params, dict):
        input_params = {}

    params_schema = deepcopy(template.get('params_schema') or {})
    for key, value in input_params.items():
        if key in params_schema and isinstance(params_schema[key], dict):
            params_schema[key]['default'] = value

    return {
        'name': llm_payload.get('name') or template.get('name') or '造数计划',
        'description': llm_payload.get('description') or description or template.get('description', ''),
        'target_type': llm_payload.get('target_type') or template.get('target_type') or 'both',
        'default_environment': default_environment_id,
        'steps': deepcopy(template.get('steps') or []),
        'cleanup_steps': deepcopy(template.get('cleanup_steps') or []),
        'template_key': template.get('template_key') or template_key,
        'template_params_schema': params_schema,
        'suggested_input_params': input_params,
        'source': f'llm:template:{template_key}',
        'hint': f'已匹配模板「{template.get("name")}」，AI 参数已保存为试跑默认值',
        'generation_summary': _build_generation_summary(
            {
                'source': f'llm:template:{template_key}',
                'template_key': template.get('template_key') or template_key,
                'name': template.get('name'),
                'steps': template.get('steps') or [],
            },
            input_params=input_params,
        ),
    }


def _expand_custom_plan(
    llm_payload: Dict[str, Any],
    *,
    description: str,
    default_environment_id: Optional[int],
) -> Dict[str, Any]:
    plan = {
        'name': llm_payload.get('name') or 'LLM 造数计划',
        'description': llm_payload.get('description') or description,
        'target_type': llm_payload.get('target_type') or 'both',
        'default_environment': default_environment_id,
        'steps': deepcopy(llm_payload.get('steps') or []),
        'cleanup_steps': deepcopy(llm_payload.get('cleanup_steps') or []),
        'source': 'llm:custom',
        'hint': llm_payload.get('hint') or '已生成自定义步骤，请确认 interface_id 与环境 ID 后试跑',
    }
    plan['generation_summary'] = _build_generation_summary(plan)
    return plan


def _invoke_llm_for_plan(
    description: str,
    context: Dict[str, Any],
    *,
    retry_hint: Optional[str] = None,
) -> Dict[str, Any]:
    from langchain_core.messages import HumanMessage, SystemMessage

    llm, _config = _get_active_llm()
    if llm is None:
        raise RuntimeError('未配置可用的 LLM（请在 LLM 配置中启用一个模型）')

    system_text = SYSTEM_PROMPT.format(
        context=json.dumps(context, ensure_ascii=False, indent=2),
    )
    user_parts = [description.strip()]
    if retry_hint:
        user_parts.append(f'\n\n上次生成无效，请修正：{retry_hint}')

    response = llm.invoke([
        SystemMessage(content=system_text),
        HumanMessage(content='\n'.join(user_parts)),
    ])
    content = getattr(response, 'content', response)
    if isinstance(content, list):
        content = ''.join(
            part.get('text', '') if isinstance(part, dict) else str(part)
            for part in content
        )
    return _extract_json_object(str(content))


def generate_plan_from_description_with_llm(
    description: str,
    *,
    project_id: int,
    default_environment_id: Optional[int] = None,
    suite_id: Optional[int] = None,
    use_llm: bool = True,
) -> Dict[str, Any]:
    """方式 A 入口：优先 LLM，失败回退规则引擎。"""
    text = (description or '').strip()
    if not text:
        raise ValueError('描述不能为空')

    suite_gap = None
    if suite_id:
        from testcases.models import TestSuite

        from .analysis import analyze_suite_variable_gaps

        suite = TestSuite.objects.filter(pk=suite_id, project_id=project_id).first()
        if suite is not None:
            suite_gap = analyze_suite_variable_gaps(
                suite,
                environment_id=default_environment_id,
            )

    if not use_llm:
        plan = generate_plan_from_description(
            text,
            default_environment_id=default_environment_id,
        )
        plan['source'] = plan.get('source') or 'rules'
        plan['llm_used'] = False
        return plan

    context = build_llm_generation_context(
        project_id,
        default_environment_id=default_environment_id,
        suite_gap=suite_gap,
    )

    last_error = ''
    for attempt in range(2):
        try:
            llm_payload = _invoke_llm_for_plan(
                text,
                context,
                retry_hint=last_error if attempt else None,
            )
            llm_payload = route_llm_payload(text, llm_payload)
            mode = (llm_payload.get('generation_mode') or 'template').strip().lower()
            if mode == 'template':
                template_key = str(llm_payload.get('template_key') or '')
                input_params = llm_payload.get('input_params') or {}
                if (
                    template_key in _ASSIGNMENT_TEMPLATE_KEYS
                    and str(input_params.get('assigneeName') or '').strip()
                ):
                    resolved_params = resolve_assignee_params(
                        input_params,
                        project_id=project_id,
                        environment_id=default_environment_id,
                    )
                    llm_payload = dict(llm_payload)
                    llm_payload['input_params'] = resolved_params
                plan = _expand_template_plan(
                    llm_payload,
                    description=text,
                    default_environment_id=default_environment_id,
                )
            else:
                plan = _expand_custom_plan(
                    llm_payload,
                    description=text,
                    default_environment_id=default_environment_id,
                )
                _validate_plan_dict(plan, project_id)

            plan['llm_used'] = True
            return plan
        except AssigneeResolutionError:
            raise
        except Exception as exc:
            last_error = str(exc)
            logger.warning(
                'LLM plan generation attempt %s failed: %s',
                attempt + 1,
                last_error,
                exc_info=True,
            )

    logger.info('LLM plan generation failed, fallback to rules: %s', last_error)
    plan = generate_plan_from_description(
        text,
        default_environment_id=default_environment_id,
    )
    plan['source'] = plan.get('source') or 'rules:fallback'
    plan['llm_used'] = False
    plan['hint'] = (
        f'LLM 生成失败（{last_error[:120]}），已回退为规则模板。'
        + (f' {plan["hint"]}' if plan.get('hint') else '')
    )
    return plan
