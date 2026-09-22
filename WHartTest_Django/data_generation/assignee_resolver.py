"""Resolve work-order assignee names to consistent business user metadata."""

from __future__ import annotations

from typing import Any, Dict, Optional

import requests

from api_environments.models import ApiEnvironment
from api_environments.token_refresh import refresh_environment_tokens


class AssigneeResolutionError(ValueError):
    """Raised when an assignee cannot be resolved safely."""


def resolve_assignee_params(
    input_params: Dict[str, Any],
    *,
    project_id: int,
    environment_id: Optional[int],
) -> Dict[str, Any]:
    """Resolve assigneeName through the business transfer-candidate API."""
    params = dict(input_params or {})
    assignee_name = str(params.get('assigneeName') or '').strip()
    if not assignee_name:
        raise AssigneeResolutionError('请在描述中明确填写要分配给谁')

    environments = ApiEnvironment.objects.filter(project_id=project_id)
    if environment_id:
        environment = environments.filter(id=environment_id).first()
    else:
        environment = environments.filter(is_active=True).order_by('id').first()
    if environment is None:
        raise AssigneeResolutionError('未找到可用于查询处理人的 API 环境')

    variables = environment.get_all_variables()
    if not isinstance(variables, dict):
        variables = {}
    variables = refresh_environment_tokens(
        base_url=environment.base_url,
        variables=variables,
        verify_ssl=environment.verify_ssl,
        environment_id=environment.id,
        persist=True,
    )
    token = variables.get('accessToken') or variables.get('adminToken')
    if not token:
        raise AssigneeResolutionError('API 环境缺少可用的管理员登录令牌')

    try:
        response = requests.get(
            environment.base_url.rstrip('/') + '/api/users/transfer-candidates',
            headers={'Authorization': f'Bearer {token}'},
            params={
                'keyword': assignee_name,
                'role': str(params.get('assigneeRole') or 'customer_service'),
                'page': 1,
                'pageSize': 100,
            },
            verify=environment.verify_ssl,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise AssigneeResolutionError(f'查询处理人“{assignee_name}”失败') from exc

    candidates = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(candidates, list):
        raise AssigneeResolutionError('处理人查询接口返回格式无效')

    exact_matches = [
        candidate
        for candidate in candidates
        if isinstance(candidate, dict)
        and (
            str(candidate.get('name') or '').strip() == assignee_name
            or str(candidate.get('username') or '').strip() == assignee_name
        )
        and candidate.get('isActive', True)
    ]
    if not exact_matches:
        raise AssigneeResolutionError(f'未找到可分配的处理人“{assignee_name}”')
    if len(exact_matches) > 1:
        raise AssigneeResolutionError(f'处理人“{assignee_name}”存在多个匹配，请使用账号指定')

    candidate = exact_matches[0]
    candidate_id = candidate.get('id')
    if not candidate_id:
        raise AssigneeResolutionError(f'处理人“{assignee_name}”缺少用户 ID')

    departments = candidate.get('departmentNames')
    department = (
        str(departments[0]).strip()
        if isinstance(departments, list) and departments
        else str(params.get('assigneeDepartment') or '').strip()
    )
    params.update({
        'assigneeUserId': candidate_id,
        'assigneeName': str(candidate.get('name') or assignee_name).strip(),
        'assigneeDepartment': department,
        'assigneeRole': str(params.get('assigneeRole') or 'customer_service'),
    })
    return params
