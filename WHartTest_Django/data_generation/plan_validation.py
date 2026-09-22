"""Shared validation helpers for data generation plans."""

from __future__ import annotations

from typing import Any, Iterable, List, Optional

from rest_framework.exceptions import ValidationError

ENV_REQUIRED_STEP_TYPES = frozenset({'api_call', 'set_env_var'})


def _iter_steps(*step_lists: Optional[Iterable[Any]]) -> Iterable[Any]:
    for step_list in step_lists:
        if not isinstance(step_list, list):
            continue
        yield from step_list


def plan_requires_default_environment(
    steps: Optional[Iterable[Any]] = None,
    cleanup_steps: Optional[Iterable[Any]] = None,
) -> bool:
    """Return True when at least one step needs an environment but has none."""
    for step in _iter_steps(steps, cleanup_steps):
        if not isinstance(step, dict):
            continue
        if step.get('type') in ENV_REQUIRED_STEP_TYPES and not step.get('environment_id'):
            return True
    return False


def ensure_plan_has_environment(
    *,
    steps: Optional[Iterable[Any]] = None,
    cleanup_steps: Optional[Iterable[Any]] = None,
    default_environment_id: Optional[int] = None,
) -> None:
    if plan_requires_default_environment(steps, cleanup_steps) and not default_environment_id:
        raise ValidationError({
            'default_environment': [
                '请选择默认 API 环境（存在未指定环境的 API / 写入环境变量步骤）',
            ],
        })
