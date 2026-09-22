"""Normalize the 领取工单 test suite for repeatable automated execution.

The command is deliberately restricted to the existing suite IDs.  It does
not create cases or change business expectations; it removes machine- and
person-specific assumptions which made otherwise valid cases flaky.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from testcases.models import TestCase, TestCaseStep


CASE_IDS = (1398, *range(1401, 1419))

AUTO_LOGIN = "使用系统根据本次造数自动匹配的处理人账号登录系统（执行时自动切换）"
PENDING_LOOKUP = "在「我的工单」筛选区域选择「工单状态：待处理」，查询本次造数生成的工单"
PENDING_LOOKUP_RESULT = "列表唯一定位到本次造数工单；工单状态为「待处理」，处理人为当前登录处理人"
OPEN_PENDING = "在筛选结果中定位本次造数工单，点击其操作列「处理」按钮进入详情页"
OPEN_PENDING_RESULT = "成功进入工单处理详情页，右上角可见蓝色「领取工单」按钮"


def _steps(*rows):
    return {number: (description, expected) for number, description, expected in rows}


# These are the lifecycle cases most frequently executed together with 1400.
# Their setup intentionally mirrors 1400, while their final assertion remains
# distinct (open detail, cancel, confirm, or list-status synchronisation).
LIFECYCLE_STEPS = {
    1398: _steps(
        (1, AUTO_LOGIN, "登录成功，进入我的工单页面"),
        (2, "点击左侧菜单「工单中心」→「我的工单」进入列表页", "成功加载我的工单列表"),
        (3, PENDING_LOOKUP, PENDING_LOOKUP_RESULT),
        (4, OPEN_PENDING, OPEN_PENDING_RESULT),
    ),
    1401: _steps(
        (1, AUTO_LOGIN, "登录成功，进入我的工单页面"),
        (2, "点击左侧菜单「工单中心」→「我的工单」进入列表页", "成功加载我的工单列表"),
        (3, PENDING_LOOKUP, PENDING_LOOKUP_RESULT),
        (4, OPEN_PENDING, OPEN_PENDING_RESULT),
        (5, "点击右上角蓝色「领取工单」按钮", "弹出确认领取弹窗，包含「取消」和「确定」按钮"),
        (6, "点击确认领取弹窗中的「取消」按钮", "弹窗关闭；仍可见「领取工单」按钮，工单未被领取"),
        (7, "返回「我的工单」列表并重新查询本次造数工单", "工单仍为「待处理」，处理人仍为当前登录处理人"),
    ),
    1402: _steps(
        (1, AUTO_LOGIN, "登录成功，进入我的工单页面"),
        (2, "点击左侧菜单「工单中心」→「我的工单」进入列表页", "成功加载我的工单列表"),
        (3, PENDING_LOOKUP, PENDING_LOOKUP_RESULT),
        (4, OPEN_PENDING, OPEN_PENDING_RESULT),
        (5, "点击右上角蓝色「领取工单」按钮", "弹出确认领取弹窗，包含「取消」和「确定」按钮"),
        (6, "点击确认领取弹窗中的「确定」按钮", "弹窗关闭；详情页状态更新为「处理中」，处理人为当前登录处理人"),
        (7, "返回「我的工单」列表并重新查询本次造数工单", "工单状态同步为「处理中」，与详情页一致"),
    ),
    1403: _steps(
        (1, AUTO_LOGIN, "登录成功，进入我的工单页面"),
        (2, "点击左侧菜单「工单中心」→「我的工单」进入列表页", "成功加载我的工单列表"),
        (3, PENDING_LOOKUP, PENDING_LOOKUP_RESULT),
        (4, OPEN_PENDING, OPEN_PENDING_RESULT),
        (5, "点击「领取工单」，在确认弹窗中点击「确定」", "领取成功；详情页状态更新为「处理中」"),
        (6, "返回「我的工单」列表并重新查询本次造数工单", "目标工单当前状态为「处理中」，与详情页状态一致"),
    ),
}


def _precondition(case_id: int) -> str:
    common = (
        "1. 执行前由系统为本次运行创建隔离工单，并注入工单号；"
        "2. 执行时自动切换到该工单对应的处理人账号；"
        "3. 禁止依赖固定账号、历史工单或其他用例留下的状态。"
    )
    if case_id in (1404, 1405, 1409, 1410, 1411):
        return common + " 4. 企业微信「工单助手-测试 BOT」连接已配置且可用；外部会话校验只读取本次工单关联消息。"
    if case_id in (1415, 1416):
        return common + " 4. 自动化环境已具备可控的网络故障模拟能力，且恢复网络后可刷新详情页。"
    return common


def _normalize_remaining_steps(case: TestCase) -> int:
    changed = 0
    for step in case.steps.order_by("step_number"):
        before = (step.description, step.expected_result)
        description = step.description or ""
        expected = step.expected_result or ""
        description = description.replace("使用李亮账号(802714/000000)登录系统(https://test.bot.by56.com/work-order/login)", AUTO_LOGIN)
        description = description.replace("使用李亮账号(802714/000000)登录系统", AUTO_LOGIN)
        expected = expected.replace("李亮", "当前登录处理人")
        description = description.replace("李亮", "当前登录处理人")
        if "页面顶部「筛选条件」区域" in description and "待处理" in description:
            description, expected = PENDING_LOOKUP, PENDING_LOOKUP_RESULT
        if "处理人」列为「未分配」" in description and "处理」按钮" in description:
            description, expected = OPEN_PENDING, OPEN_PENDING_RESULT
        if before != (description, expected):
            step.description, step.expected_result = description, expected
            step.save(update_fields=["description", "expected_result", "updated_at"])
            changed += 1
    return changed


class Command(BaseCommand):
    help = "将领取工单模块的历史固定账号/工单前置条件规范化为可重复执行的动态数据约束"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="实际写入；省略时只预览改动数量")

    def handle(self, *args, **options):
        cases = list(TestCase.objects.filter(id__in=CASE_IDS).prefetch_related("steps").order_by("id"))
        found_ids = {case.id for case in cases}
        missing = set(CASE_IDS) - found_ids
        if missing:
            raise CommandError(f"目标用例不存在，未执行任何修改：{sorted(missing)}")
        if not options["apply"]:
            self.stdout.write(self.style.WARNING(f"预览：将规范化 {len(cases)} 条用例（{min(CASE_IDS)}–{max(CASE_IDS)}，不含 1400）。"))
            return

        case_updates = step_updates = 0
        with transaction.atomic():
            for case in cases:
                case.precondition = _precondition(case.id)
                notes = case.notes or ""
                marker = "【自动化执行约束】使用本次造数的隔离工单和动态处理人；不依赖固定账号、历史工单或上一次执行状态。"
                if marker not in notes:
                    case.notes = f"{notes} {marker}".strip()
                case.save(update_fields=["precondition", "notes", "updated_at"])
                case_updates += 1
                if case.id in LIFECYCLE_STEPS:
                    for number, (description, expected) in LIFECYCLE_STEPS[case.id].items():
                        step, created = TestCaseStep.objects.get_or_create(
                            test_case=case,
                            step_number=number,
                            defaults={
                                "description": description,
                                "expected_result": expected,
                                "creator": case.creator,
                            },
                        )
                        if not created:
                            step.description, step.expected_result = description, expected
                            step.save(update_fields=["description", "expected_result", "updated_at"])
                        step_updates += 1
                else:
                    step_updates += _normalize_remaining_steps(case)
        self.stdout.write(self.style.SUCCESS(f"已规范化 {case_updates} 条用例、更新 {step_updates} 个步骤；1400 未改动。"))
