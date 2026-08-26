"""Split combined 待分配 locate+click steps in 工单处理 module cases."""
import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wharttest_django.settings")
django.setup()

from django.db import transaction

from testcases.models import TestCase, TestCaseModule, TestCaseStep

FILTER_STEP = (
    "在页面顶部「筛选条件」区域，点击「工单状态」下拉框，选择「待分配」，点击蓝色「查询」按钮",
    "列表刷新后，「当前状态」列均为「待分配」（黄色标签），且至少存在 1 条工单。",
)
CLICK_STEP = (
    "点击列表中任意「待分配」工单操作列的「处理」按钮",
    "弹出「选择处理人」弹窗，页面不跳转详情",
)

OLD_PATTERNS = (
    '找到状态为"待分配"的工单，点击该行操作列的「处理」按钮',
    "找到状态为\"待分配\"的工单，点击该行操作列的「处理」按钮",
    '找到状态为"待分配"的工单，记录原处理人信息，点击该行操作列的「处理」按钮',
    '找到状态为"待分配"的工单，记录原处理人和状态，点击该行操作列的「处理」按钮',
)


def get_descendant_ids(root_id: int) -> list[int]:
    ids = [root_id]
    for cid in TestCaseModule.objects.filter(parent_id=root_id).values_list("id", flat=True):
        ids.extend(get_descendant_ids(cid))
    return ids


def click_description(original: str) -> str:
    if "记录原处理人和状态" in original:
        return "记录原处理人和状态，" + CLICK_STEP[0]
    if "记录原处理人信息" in original:
        return "记录原处理人信息，" + CLICK_STEP[0]
    return CLICK_STEP[0]


def should_update_step(description: str) -> bool:
    return any(pattern in description for pattern in OLD_PATTERNS)


@transaction.atomic
def update_case(tc: TestCase) -> bool:
    steps = list(tc.steps.order_by("step_number"))
    target_idx = None
    for idx, step in enumerate(steps):
        if should_update_step(step.description):
            target_idx = idx
            break
    if target_idx is None:
        return False

    old_step = steps[target_idx]
    new_click_desc = click_description(old_step.description)
    new_click_expected = CLICK_STEP[1]

    # Remove old combined step and insert filter + click before remaining steps.
    before = steps[:target_idx]
    after = steps[target_idx + 1 :]

    tc.steps.all().delete()

    new_steps = []
    for step in before:
        new_steps.append((step.description, step.expected_result))

    new_steps.append(FILTER_STEP)
    new_steps.append((new_click_desc, new_click_expected))

    for step in after:
        new_steps.append((step.description, step.expected_result))

    creator_id = tc.creator_id or 1
    for number, (desc, expected) in enumerate(new_steps, start=1):
        TestCaseStep.objects.create(
            test_case=tc,
            step_number=number,
            description=desc,
            expected_result=expected,
            creator_id=creator_id,
        )

    return True


def main() -> None:
    module_ids = get_descendant_ids(224)
    updated = []
    skipped = []

    for tc in TestCase.objects.filter(module_id__in=module_ids).order_by("id"):
        if update_case(tc):
            updated.append(tc.id)
        else:
            skipped.append(tc.id)

    print("updated", updated)
    print("skipped", skipped)
    print("updated_count", len(updated))


if __name__ == "__main__":
    main()
