"""Refresh filter-step wording for 待处理 / 待分配 cases."""
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wharttest_django.settings")
django.setup()

from testcases.models import TestCaseStep

UPDATES = {
    (
        "在页面顶部「筛选条件」区域，点击「工单状态」下拉框，选择「待处理」，点击蓝色「查询」按钮",
        "列表刷新后，「当前状态」列均为「待处理」（橙色标签），且至少存在 1 条工单。",
    ): (
        "在页面顶部「筛选条件」区域，点击「工单状态」下拉框，选择「待处理」，点击蓝色「查询」按钮，等待列表刷新完成",
        "列表「当前状态」列仅显示橙色「待处理」标签，不得出现「处理中」「已完成」「已关闭」等其他状态；且至少存在 1 条工单。若仍为混合状态列表，判定本步失败，不得继续下一步。",
    ),
    (
        "在页面顶部「筛选条件」区域，点击「工单状态」下拉框，选择「待分配」，点击蓝色「查询」按钮",
        "列表刷新后，「当前状态」列均为「待分配」（黄色标签），且至少存在 1 条工单。",
    ): (
        "在页面顶部「筛选条件」区域，点击「工单状态」下拉框，选择「待分配」，点击蓝色「查询」按钮，等待列表刷新完成",
        "列表「当前状态」列仅显示「待分配」（黄色标签），不得出现「待处理」「处理中」等其他状态；且至少存在 1 条工单。若仍为混合状态列表，判定本步失败，不得继续下一步。",
    ),
}


def main() -> None:
    updated = 0
    for (old_desc, old_exp), (new_desc, new_exp) in UPDATES.items():
        for step in TestCaseStep.objects.filter(description=old_desc, expected_result=old_exp):
            step.description = new_desc
            step.expected_result = new_exp
            step.save(update_fields=["description", "expected_result"])
            updated += 1
            print(step.test_case_id, step.step_number, step.test_case.name[:40])
    print("updated_count", updated)


if __name__ == "__main__":
    main()
