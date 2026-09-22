"""Split combined 待处理 locate+click steps in 领取工单 module cases."""
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wharttest_django.settings")
django.setup()

from django.db import transaction

from testcases.models import TestCase, TestCaseModule, TestCaseStep

CLAIM_MODULE_ROOT_ID = 253

FILTER_STEP = (
    "在页面顶部「筛选条件」区域，点击「工单状态」下拉框，选择「待处理」，点击蓝色「查询」按钮，等待列表刷新完成",
    "列表「当前状态」列仅显示橙色「待处理」标签，不得出现「处理中」「已完成」「已关闭」等其他状态；且至少存在 1 条工单。若仍为混合状态列表，判定本步失败，不得继续下一步。",
)

OLD_PATTERNS = (
    "找到当前状态为「待处理」（橙色标签）的工单行，点击操作列的「处理」按钮",
    "找到当前状态为「待处理」（橙色标签）的工单行，点击操作列的「处理」按钮进入详情页",
    "找到目标待处理工单，点击「处理」进入详情页并完成领取（点击领取→确定）",
    "找到目标待处理工单，点击「处理」进入详情页完成领取",
    "找到带锁的待处理工单，点击「处理」进入详情页",
)


def get_descendant_ids(root_id: int) -> list[int]:
    ids = [root_id]
    for cid in TestCaseModule.objects.filter(parent_id=root_id).values_list("id", flat=True):
        ids.extend(get_descendant_ids(cid))
    return ids


def click_description(original: str) -> str:
    if "并完成领取（点击领取→确定）" in original:
        return (
            "点击列表中任意「待处理」工单操作列的「处理」按钮进入详情页，"
            "点击「领取」→「确定」完成领取"
        )
    if "完成领取" in original:
        return (
            "点击列表中任意「待处理」工单操作列的「处理」按钮进入详情页，完成领取"
        )
    if "带锁" in original:
        return (
            "在筛选结果中找到带锁标识的待处理工单，"
            "点击操作列的「处理」按钮进入详情页"
        )
    if "进入详情页" in original:
        return "点击列表中任意「待处理」工单操作列的「处理」按钮进入详情页"
    return "点击列表中任意「待处理」工单操作列的「处理」按钮"


def should_update_step(description: str) -> bool:
    if "筛选条件" in description and "工单状态" in description:
        return False
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
    new_click_expected = old_step.expected_result

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
    module_ids = get_descendant_ids(CLAIM_MODULE_ROOT_ID)
    updated = []
    skipped = []

    for tc in TestCase.objects.filter(module_id__in=module_ids).order_by("id"):
        if update_case(tc):
            updated.append((tc.id, tc.name))
        else:
            skipped.append(tc.id)

    print("updated_count", len(updated))
    for case_id, name in updated:
        print(f"  {case_id}: {name}")
    print("skipped", skipped)


if __name__ == "__main__":
    main()
