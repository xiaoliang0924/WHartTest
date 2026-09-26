"""Persist UI test cases generated from an observed browser run."""

from __future__ import annotations

import json
import re
from typing import Any

from django.db import transaction

from .models import (
    UiCaseStepsDetailed,
    UiElement,
    UiModule,
    UiPage,
    UiPageSteps,
    UiTestCase,
)


MAX_PAYLOAD_BYTES = 200_000
MAX_STEPS = 80
MAX_ACTIONS = 500
SUPPORTED_ACTIONS = {
    "click", "fill", "check", "uncheck", "press", "goto", "wait", "assert"
}
LOCATOR_ACTIONS = {"click", "fill", "check", "uncheck", "press"}
VALID_ASSERTIONS = {
    "visible", "hidden", "enabled", "disabled", "checked", "text",
    "contain_text", "value", "count", "url", "title",
}


def _parse_payload(payload: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload, str):
        if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError("生成内容过大，请减少步骤或定位信息后重试。")
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("生成内容不是有效 JSON。") from exc
    if not isinstance(payload, dict):
        raise ValueError("生成内容必须是 JSON 对象。")
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("生成内容过大，请减少步骤或定位信息后重试。")
    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("没有可保存的浏览器步骤。")
    if len(steps) > MAX_STEPS:
        raise ValueError(f"步骤过多，最多支持 {MAX_STEPS} 步。")
    count = 0
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise ValueError(f"第 {index} 步格式不正确。")
        actions = step.get("actions")
        if not isinstance(actions, list) or not actions:
            raise ValueError(f"第 {index} 步没有可保存的浏览器操作。")
        for action in actions:
            count += 1
            if not isinstance(action, dict):
                raise ValueError(f"第 {index} 步包含格式不正确的操作。")
            action_type = str(action.get("type") or "")
            if action_type not in SUPPORTED_ACTIONS:
                raise ValueError(f"不支持的 UI 操作：{action_type or '空操作'}。")
            if action_type in LOCATOR_ACTIONS:
                selector = action.get("selector")
                if not isinstance(selector, dict) or not str(selector.get("locator_value") or "").strip():
                    raise ValueError(f"第 {index} 步的 {action_type} 操作缺少真实定位器。")
                if selector.get("locator_type") not in {kind for kind, _ in UiElement.LOCATOR_TYPE_CHOICES}:
                    raise ValueError(f"第 {index} 步的定位类型无效。")
            if action_type == "assert" and action.get("mode", "visible") not in VALID_ASSERTIONS:
                raise ValueError(f"第 {index} 步的断言类型无效。")
            if action_type == "assert" and action.get("mode", "visible") not in {"url", "title"}:
                selector = action.get("selector")
                if not isinstance(selector, dict) or not str(selector.get("locator_value") or "").strip():
                    raise ValueError(f"第 {index} 步的元素断言缺少真实定位器。")
            if action_type == "goto" and not str(action.get("url") or "").strip():
                raise ValueError(f"第 {index} 步的页面跳转缺少观察到的 URL。")
    if count > MAX_ACTIONS:
        raise ValueError(f"操作过多，最多支持 {MAX_ACTIONS} 个。")
    return payload


def _safe_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Never persist a literal password or token from browser fill operations."""
    sanitized = []
    for original in actions:
        action = dict(original)
        if action.get("type") == "fill":
            selector = action.get("selector") or {}
            label = " ".join(
                str(selector.get(key) or "")
                for key in ("name", "locator_value", "placeholder", "aria_label")
            ).lower()
            value = str(action.get("value") or "")
            if re.search(r"password|passwd|token|secret|密碼|密码|口令", label):
                action["value"] = "${{password}}"
            elif re.search(r"bearer\s+|access[_-]?token|refresh[_-]?token", value, re.I):
                action["value"] = "${{token}}"
        sanitized.append(action)
    return sanitized


@transaction.atomic
def persist_generated_ui_testcase(
    *,
    testcase_id: int,
    project_id: int,
    user_id: int,
    payload: str | dict[str, Any],
) -> dict[str, Any]:
    """Create a UI case from observed actions and bind it to the functional case."""
    from django.contrib.auth import get_user_model
    from testcases.models import TestCase
    from .recorder_apply import apply_recorded_actions
    from .models import UiCaseStepsDetailed

    data = _parse_payload(payload)
    testcase = TestCase.objects.select_for_update().select_related("ui_test_case").get(
        pk=testcase_id, project_id=project_id
    )
    if testcase.ui_test_case_id:
        return {
            "status": "success",
            "ui_test_case_id": testcase.ui_test_case_id,
            "message": "功能用例已绑定 UI 自动化用例，未重复创建。",
            "created": False,
        }
    user = get_user_model().objects.get(pk=user_id)
    module, _ = UiModule.objects.get_or_create(
        project_id=project_id,
        parent=None,
        name="AI 自动生成",
        defaults={"creator": user, "level": 1},
    )
    case_name = str(data.get("name") or testcase.name).strip()[:255]
    ui_case = UiTestCase.objects.create(
        project_id=project_id,
        module=module,
        name=f"{case_name}（AI生成）"[:255],
        description=(str(data.get("description") or "由本次成功的 AI 浏览器执行生成")[:4000]),
        level=testcase.level if testcase.level in {"P0", "P1", "P2", "P3"} else "P2",
        creator=user,
        case_flow="\n".join(
            str(step.get("name") or f"步骤 {index}")
            for index, step in enumerate(data["steps"], 1)
        ),
    )

    page_cache: dict[str, UiPage] = {}
    total_actions = 0
    for index, step in enumerate(data["steps"], 1):
        url = str(step.get("url") or data.get("url") or "").strip()
        actions = _safe_actions(step["actions"])
        action_urls = [str(a.get("url") or "").strip() for a in actions if a.get("type") == "goto"]
        if not url and action_urls:
            url = action_urls[-1]
        page_key = url or "(当前页面)"
        page = page_cache.get(page_key)
        if not page:
            page_name = str(step.get("page_name") or page_key.rsplit("/", 1)[-1] or "浏览器页面")[:64]
            page = UiPage.objects.create(
                project_id=project_id,
                module=module,
                name=page_name,
                url=url or None,
                description="由 AI 测试执行观察生成",
                creator=user,
            )
            page_cache[page_key] = page
        page_step = UiPageSteps.objects.create(
            project_id=project_id,
            page=page,
            module=module,
            name=str(step.get("name") or f"步骤 {index}")[:64],
            description=str(step.get("description") or "")[:2000],
            run_flow="浏览器观察动作自动生成",
            creator=user,
        )
        stats = apply_recorded_actions(
            page=page,
            page_step=page_step,
            user=user,
            actions=actions,
        )
        if not stats["steps_created"]:
            raise ValueError(f"第 {index} 步没有生成可执行的 UI 步骤。")
        total_actions += stats["steps_created"]
        UiCaseStepsDetailed.objects.create(
            test_case=ui_case,
            page_step=page_step,
            case_sort=index,
        )

    if not total_actions or not ui_case.case_steps.exists():
        raise ValueError("没有生成可执行的 UI 自动化步骤，已取消保存。")
    testcase.ui_test_case = ui_case
    testcase.save(update_fields=["ui_test_case", "updated_at"])
    return {
        "status": "success",
        "ui_test_case_id": ui_case.id,
        "message": "UI 自动化用例已保存并绑定到功能用例。",
        "created": True,
        "steps": ui_case.case_steps.count(),
        "actions": total_actions,
    }
