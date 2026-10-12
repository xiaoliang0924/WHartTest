"""Persist UI test cases generated from an observed browser run."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

from django.db import transaction

from .models import (
    UiCaseStepsDetailed,
    UiAuthState,
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
    "click", "fill", "check", "uncheck", "press", "goto", "wait",
    "wait_for_visible", "wait_for_ready", "assert",
}
LOCATOR_ACTIONS = {"click", "fill", "check", "uncheck", "press", "wait_for_visible"}
VALID_ASSERTIONS = {
    "visible", "hidden", "enabled", "disabled", "checked", "text",
    "contain_text", "value", "count", "url", "title",
}


def _auth_state_match_score(base_url: str, observed_url: str) -> int:
    """Score an environment URL against a URL observed by the AI browser.

    Environment configurations in existing projects commonly point to the
    login URL while the generated case records a post-login page.  Matching
    the parent path handles that layout, while an exact scheme/host match keeps
    an HTTP environment from accidentally winning over HTTPS.
    """
    try:
        base = urlsplit(str(base_url or '').strip())
        observed = urlsplit(str(observed_url or '').strip())
    except Exception:
        return -1
    if not base.netloc or not observed.netloc:
        return -1
    if base.netloc.lower() != observed.netloc.lower():
        return -1

    score = 100 if base.scheme.lower() == observed.scheme.lower() else 40
    base_path = (base.path or '/').rstrip('/') or '/'
    observed_path = (observed.path or '/').rstrip('/') or '/'
    if observed_path == base_path or observed_path.startswith(base_path + '/'):
        return score + 40

    # A configured login page such as /work-order/login owns post-login pages
    # such as /work-order/my-tickets and /work-order/users.
    parent_path = base_path.rsplit('/', 1)[0] or '/'
    if observed_path == parent_path or observed_path.startswith(parent_path + '/'):
        return score + 30
    return score


def _resolve_active_auth_state(
    *, project_id: int, observed_urls: list[str]
) -> UiAuthState | None:
    """Find the enabled login state belonging to the observed environment."""
    urls = [str(url or '').strip() for url in observed_urls if str(url or '').strip()]
    if not urls:
        return None

    candidates = list(
        UiAuthState.objects.filter(
            env_config__project_id=project_id,
            is_active=True,
        ).select_related('env_config').order_by('-updated_at', '-id')
    )
    best = None
    best_score = -1
    for auth_state in candidates:
        state_json = auth_state.state_json or {}
        if not isinstance(state_json, dict):
            continue
        if not (state_json.get('cookies') or state_json.get('origins')):
            continue
        scores = [
            _auth_state_match_score(auth_state.env_config.base_url, url)
            for url in urls
        ]
        valid_scores = [score for score in scores if score >= 0]
        if not valid_scores:
            continue
        # Prefer a state matching more observed pages, then exact HTTPS/host.
        score = sum(valid_scores) + (max(valid_scores) * 2)
        if score > best_score:
            best = auth_state
            best_score = score
    return best


def _resolve_generated_step_auth_state(
    *, step: dict[str, Any], project_id: int, default_auth_state: UiAuthState | None
) -> UiAuthState | None:
    """Resolve an optional per-step login state without allowing cross-project use.

    A generated workflow may compare different roles. In that case each page
    group can select the saved login state for that role; groups without an
    explicit selection retain the historical project/environment default.
    """
    if "auth_state_id" not in step:
        return default_auth_state
    raw_id = step.get("auth_state_id")
    if raw_id in (None, ""):
        return default_auth_state
    try:
        auth_state_id = int(raw_id)
    except (TypeError, ValueError):
        raise ValueError("UI 步骤的 auth_state_id 必须是有效的登录态编号。")
    auth_state = UiAuthState.objects.select_related("env_config").filter(
        id=auth_state_id,
        env_config__project_id=project_id,
        is_active=True,
    ).first()
    if auth_state is None:
        raise ValueError(f"UI 步骤指定的登录态 #{auth_state_id} 不存在、未启用或不属于当前项目。")
    state_json = auth_state.state_json or {}
    if not isinstance(state_json, dict) or not (state_json.get("cookies") or state_json.get("origins")):
        raise ValueError(f"UI 步骤指定的登录态「{auth_state.name}」没有可用的登录快照。")
    return auth_state


def _distinct_account_identities(functional_steps) -> set[str]:
    """Extract only explicit account identifiers from functional step text.

    This is used as a safety gate for multi-account cases. Passwords are never
    returned or persisted; matching stops at the slash before the password.
    """
    identities: set[str] = set()
    account_pattern = re.compile(r"(?:账号|账户)\s*[（(]?([^/（）()\s]+)\s*/")
    for step in functional_steps:
        text = " ".join((str(step.description or ""), str(step.expected_result or "")))
        identities.update(match.group(1).strip() for match in account_pattern.finditer(text))
    return identities


def bind_generated_ui_case_auth_state(
    *, ui_test_case_id: int, project_id: int | None = None
) -> dict[str, Any]:
    """Bind an enabled environment login state to unbound generated steps.

    This is intentionally limited to currently unbound page steps.  A
    manually selected auth state is explicit user configuration and must never
    be overwritten by automatic matching.
    """
    ui_case = UiTestCase.objects.filter(id=ui_test_case_id).first()
    if ui_case is None:
        return {'status': 'not_found', 'bound_count': 0}
    if project_id is not None and int(ui_case.project_id) != int(project_id):
        return {'status': 'project_mismatch', 'bound_count': 0}

    case_steps = list(
        UiCaseStepsDetailed.objects.filter(test_case=ui_case)
        .select_related('page_step', 'page_step__page')
        .order_by('case_sort')
    )
    observed_urls = [
        step.page_step.page.url
        for step in case_steps
        if step.page_step and step.page_step.page
    ]
    auth_state = _resolve_active_auth_state(
        project_id=ui_case.project_id,
        observed_urls=observed_urls,
    )
    if auth_state is None:
        return {'status': 'not_available', 'bound_count': 0}

    page_step_ids = [
        step.page_step_id for step in case_steps if step.page_step_id
    ]
    bound_count = UiPageSteps.objects.filter(
        id__in=page_step_ids,
        auth_state__isnull=True,
    ).update(auth_state=auth_state)
    return {
        'status': 'bound' if bound_count else 'already_bound',
        'bound_count': bound_count,
        'auth_state_id': auth_state.id,
        'env_config_id': auth_state.env_config_id,
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
        authentication_mode = step.get("authentication_mode", "auto")
        if authentication_mode not in {"auto", "environment", "script"}:
            raise ValueError(f"第 {index} 步的认证执行方式无效。")
        if authentication_mode == "environment" and (
            not isinstance(actions, list) or not actions
            or any(action.get("type") != "assert" for action in actions if isinstance(action, dict))
        ):
            raise ValueError(f"第 {index} 步由环境认证，只能保存登录成功的断言。")
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
            if action_type == "assert" and action.get("mode", "visible") in {"url", "title"}:
                match = str(action.get("match") or "exact")
                if match not in {"exact", "path", "contains"}:
                    raise ValueError(f"第 {index} 步的页面断言匹配方式无效。")
            if action_type == "goto" and not str(action.get("url") or "").strip():
                raise ValueError(f"第 {index} 步的页面跳转缺少观察到的 URL。")
            if action_type == "wait_for_visible":
                timeout = action.get("timeout", 10)
                try:
                    if not 1 <= int(float(timeout)) <= 60:
                        raise ValueError
                except (TypeError, ValueError):
                    raise ValueError(f"第 {index} 步的可见性等待超时时间无效。")
            if action_type == "wait_for_ready":
                timeout = action.get("timeout", 10)
                try:
                    if not 1 <= int(float(timeout)) <= 60:
                        raise ValueError
                except (TypeError, ValueError):
                    raise ValueError(f"第 {index} 步的页面就绪等待超时时间无效。")
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
    # Lock only the functional testcase row. ``ui_test_case`` is nullable, so
    # PostgreSQL rejects a plain FOR UPDATE over its LEFT OUTER JOIN.
    testcase = TestCase.objects.select_for_update(of=("self",)).select_related("ui_test_case").get(
        pk=testcase_id, project_id=project_id
    )
    if testcase.ui_test_case_id:
        return {
            "status": "success",
            "ui_test_case_id": testcase.ui_test_case_id,
            "message": "功能用例已绑定 UI 自动化用例，未重复创建。",
            "created": False,
        }
    functional_steps = list(testcase.steps.all().order_by("step_number"))
    if not functional_steps:
        raise ValueError("功能用例没有步骤，无法建立 UI 步骤映射。")

    valid_step_numbers = {int(step.step_number) for step in functional_steps}
    covered_step_numbers: set[int] = set()
    mapping_by_payload_step: list[list[int]] = []
    for index, step in enumerate(data["steps"], 1):
        raw_numbers = step.get("functional_step_numbers")
        if not isinstance(raw_numbers, list) or not raw_numbers:
            raise ValueError(
                f"UI 第 {index} 组缺少 functional_step_numbers，不能确认它覆盖的功能步骤。"
            )
        numbers: list[int] = []
        for raw_number in raw_numbers:
            if isinstance(raw_number, bool):
                raise ValueError(f"UI 第 {index} 组的功能步骤编号无效。")
            try:
                number = int(raw_number)
            except (TypeError, ValueError):
                raise ValueError(f"UI 第 {index} 组的功能步骤编号无效。")
            if number not in valid_step_numbers:
                raise ValueError(
                    f"UI 第 {index} 组引用了不存在的功能步骤 {number}。"
                )
            if number in numbers:
                raise ValueError(f"UI 第 {index} 组重复映射了功能步骤 {number}。")
            numbers.append(number)
            covered_step_numbers.add(number)
        mapping_by_payload_step.append(numbers)

    missing_step_numbers = sorted(valid_step_numbers - covered_step_numbers)
    if missing_step_numbers:
        missing = "、".join(str(number) for number in missing_step_numbers)
        raise ValueError(f"UI 自动化未覆盖功能步骤：{missing}。请补录或重新映射后保存。")

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
    observed_urls = [str(data.get('url') or '').strip()]
    for step in data['steps']:
        observed_urls.append(str(step.get('url') or '').strip())
        observed_urls.append(str(step.get('start_url') or '').strip())
        observed_urls.extend(
            str(action.get('url') or '').strip()
            for action in step.get('actions') or []
            if action.get('type') == 'goto'
        )
    generated_auth_state = _resolve_active_auth_state(
        project_id=project_id,
        observed_urls=observed_urls,
    )
    step_auth_states = [
        _resolve_generated_step_auth_state(
            step=step,
            project_id=project_id,
            default_auth_state=generated_auth_state,
        )
        for step in data["steps"]
    ]
    account_identities = _distinct_account_identities(functional_steps)
    bound_auth_state_ids = {state.id for state in step_auth_states if state is not None}
    if len(account_identities) > 1 and len(bound_auth_state_ids) < len(account_identities):
        raise ValueError(
            f"功能用例明确涉及 {len(account_identities)} 个账号，但 UI 步骤只绑定了 "
            f"{len(bound_auth_state_ids)} 个独立登录态。请先为每个账号分别录制并启用登录态，"
            "再按角色给 UI 步骤绑定对应 auth_state_id；未满足前不会保存无法回放的用例。"
        )
    total_actions = 0
    saved_step_mapping = []
    for index, step in enumerate(data["steps"], 1):
        step_auth_state = step_auth_states[index - 1]
        # Generated payload `url` records the observed end page. Replay starts
        # each page-step group from `start_url`, derived from the preceding
        # functional step's end URL. This keeps actions that navigate between
        # pages (such as filtering a row and opening its detail drawer) in
        # their original order.
        url = str(step.get("start_url") or step.get("url") or data.get("url") or "").strip()
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
            authentication_mode=step.get("authentication_mode", "auto"),
            creator=user,
            auth_state=step_auth_state,
        )
        stats = apply_recorded_actions(
            page=page,
            page_step=page_step,
            user=user,
            actions=actions,
            auth_state_id=step_auth_state.id if step_auth_state else None,
        )
        if not stats["steps_created"]:
            raise ValueError(f"第 {index} 步没有生成可执行的 UI 步骤。")
        total_actions += stats["steps_created"]
        ui_case_step = UiCaseStepsDetailed.objects.create(
            test_case=ui_case,
            page_step=page_step,
            case_sort=index,
        )
        saved_step_mapping.append({
            "ui_case_step_id": ui_case_step.id,
            "functional_step_numbers": mapping_by_payload_step[index - 1],
        })

    if not total_actions or not ui_case.case_steps.exists():
        raise ValueError("没有生成可执行的 UI 自动化步骤，已取消保存。")
    testcase.ui_test_case = ui_case
    testcase.ui_step_mapping = {
        "version": 1,
        "ui_test_case_id": ui_case.id,
        "groups": saved_step_mapping,
    }
    testcase.save(update_fields=["ui_test_case", "ui_step_mapping", "updated_at"])
    return {
        "status": "success",
        "ui_test_case_id": ui_case.id,
        "message": "UI 自动化用例已保存并绑定到功能用例。",
        "created": True,
        "steps": ui_case.case_steps.count(),
        "actions": total_actions,
        "auth_state_id": generated_auth_state.id if generated_auth_state else None,
        "auth_state_bound": bool(generated_auth_state),
        "auth_state_ids": [state.id if state else None for state in step_auth_states],
        "mapped_functional_steps": sorted(covered_step_numbers),
    }
