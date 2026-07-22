import logging
from typing import Iterable
from urllib.parse import urljoin

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


STATUS_LABELS = {
    "success": "成功",
    "failure": "失败",
    "failed": "失败",
    "error": "错误",
    "completed": "完成",
    "canceled": "已取消",
    "cancelled": "已取消",
    "pending": "待执行",
    "running": "执行中",
    "pass": "通过",
    "fail": "失败",
    "skip": "跳过",
    "0": "待执行",
    "1": "执行中",
    "2": "成功",
    "3": "失败",
    "4": "取消",
}


def _get_webhook_urls() -> list[str]:
    urls: list[str] = []
    for value in (
        getattr(settings, "WECHAT_WORK_BOT_WEBHOOK", ""),
        getattr(settings, "WEIXIN_WORK_BOT_WEBHOOK", ""),
        getattr(settings, "WECHAT_WORK_BOT_WEBHOOKS", ""),
        getattr(settings, "WEIXIN_WORK_BOT_WEBHOOKS", ""),
    ):
        if not value:
            continue
        urls.extend(item.strip() for item in str(value).split(",") if item.strip())
    return list(dict.fromkeys(urls))


def is_wechat_work_notification_enabled() -> bool:
    return bool(_get_webhook_urls())


def _frontend_url(path: str) -> str:
    base_url = (
        getattr(settings, "FRONTEND_BASE_URL", "")
        or getattr(settings, "WHARTTEST_FRONTEND_BASE_URL", "")
        or getattr(settings, "DJANGO_BASE_URL", "")
        or getattr(settings, "BASE_URL", "")
    )
    if not base_url:
        return ""
    return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def _status_label(status: object) -> str:
    value = str(status or "").strip()
    return STATUS_LABELS.get(value.lower(), value or "-")


def _ui_batch_status_label(status: object) -> str:
    return {
        0: "待执行",
        1: "执行中",
        2: "全部成功",
        3: "部分失败",
        4: "全部失败",
        "0": "待执行",
        "1": "执行中",
        "2": "全部成功",
        "3": "部分失败",
        "4": "全部失败",
    }.get(status, _status_label(status))


def _format_duration(seconds: object) -> str:
    try:
        value = float(seconds or 0)
    except (TypeError, ValueError):
        return "-"

    if value <= 0:
        return "-"
    if value < 60:
        return f"{value:.2f} 秒"
    minutes, sec = divmod(int(value), 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分 {sec} 秒"


def _safe_name(obj: object, attr: str = "name", default: str = "-") -> str:
    return str(getattr(obj, attr, "") or default)


def _send_markdown(title: str, lines: Iterable[str]) -> None:
    webhook_urls = _get_webhook_urls()
    if not webhook_urls:
        return

    content = "\n".join([f"## {title}", *[line for line in lines if line]])
    timeout = getattr(settings, "WECHAT_WORK_BOT_TIMEOUT_SECONDS", 5)
    payload = {"msgtype": "markdown", "markdown": {"content": content[:4096]}}

    for url in webhook_urls:
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()
            if data.get("errcode") not in (0, None):
                logger.warning(
                    "WeChat Work notification returned error url=%s errcode=%s errmsg=%s",
                    url,
                    data.get("errcode"),
                    data.get("errmsg"),
                )
        except Exception as exc:
            logger.warning("WeChat Work notification failed url=%s error=%s", url, exc)


def notify_api_test_report(report) -> None:
    testcase = getattr(report, "testcase", None)
    project = getattr(testcase, "project", None)
    user = getattr(report, "executed_by", None)
    detail_url = _frontend_url(f"/api-testing/reports/{report.id}")

    lines = [
        f"> 项目：{_safe_name(project)}",
        f"> 用例：{_safe_name(testcase)}",
        f"> 状态：{_status_label(report.status)}",
        f"> 统计：成功 {report.success_count} / 失败 {report.fail_count} / 错误 {report.error_count}",
        f"> 耗时：{_format_duration(report.duration)}",
        f"> 执行人：{getattr(user, 'username', '-') or '-'}",
    ]
    if detail_url:
        lines.append(f"> 报告：[{report.name}]({detail_url})")
    else:
        lines.append(f"> 报告：{report.name}")
    _send_markdown("接口自动化用例执行结果", lines)


def notify_api_task_execution(execution) -> None:
    suite = getattr(execution, "task_suite", None)
    project = getattr(suite, "project", None)
    user = getattr(execution, "executed_by", None)
    detail_url = _frontend_url(f"/api-testing/tasks/executions/{execution.id}")

    lines = [
        f"> 项目：{_safe_name(project)}",
        f"> 任务：{_safe_name(suite)}",
        f"> 状态：{_status_label(execution.status)}",
        f"> 统计：总数 {execution.total_count} / 成功 {execution.success_count} / 失败 {execution.fail_count} / 错误 {execution.error_count}",
        f"> 通过率：{round(float(execution.success_rate or 0) * 100, 2)}%",
        f"> 耗时：{_format_duration(execution.duration)}",
        f"> 执行人：{getattr(user, 'username', '-') or '-'}",
    ]
    if detail_url:
        lines.append(f"> 详情：[查看执行记录]({detail_url})")
    _send_markdown("接口自动化测试任务执行结果", lines)


def notify_ui_execution_record(record) -> None:
    test_case = getattr(record, "test_case", None)
    project = getattr(test_case, "project", None)
    user = getattr(record, "executor", None)
    detail_url = _frontend_url("/ui-automation")

    lines = [
        f"> 项目：{_safe_name(project)}",
        f"> UI 用例：{_safe_name(test_case)}",
        f"> 状态：{_status_label(record.status)}",
        f"> 耗时：{_format_duration(record.duration)}",
        f"> 触发方式：{record.trigger_type or '-'}",
        f"> 执行人：{getattr(user, 'username', '-') or '-'}",
    ]
    if getattr(record, "error_message", None):
        lines.append(f"> 错误：{str(record.error_message)[:300]}")
    if detail_url:
        lines.append(f"> 详情：[查看执行记录]({detail_url})")
    _send_markdown("UI 自动化用例执行结果", lines)


def notify_ui_batch_execution(batch) -> None:
    user = getattr(batch, "executor", None)
    first_record = (
        batch.execution_records.select_related("test_case__project").first()
        if getattr(batch, "id", None)
        else None
    )
    project = getattr(getattr(first_record, "test_case", None), "project", None)
    detail_url = _frontend_url("/ui-automation")
    lines = [
        f"> 项目：{_safe_name(project)}",
        f"> 批次：{batch.name}",
        f"> 状态：{_ui_batch_status_label(batch.status)}",
        f"> 统计：总数 {batch.total_cases} / 成功 {batch.passed_cases} / 失败 {batch.failed_cases}",
        f"> 耗时：{_format_duration(batch.duration)}",
        f"> 触发方式：{batch.trigger_type or '-'}",
        f"> 执行人：{getattr(user, 'username', '-') or '-'}",
    ]
    if detail_url:
        lines.append(f"> 详情：[查看执行记录]({detail_url})")
    _send_markdown("UI 自动化批量执行结果", lines)


def notify_test_suite_execution(execution) -> None:
    suite = getattr(execution, "suite", None)
    project = getattr(suite, "project", None)
    user = getattr(execution, "executor", None)
    detail_url = _frontend_url("/test-executions")

    lines = [
        f"> 项目：{_safe_name(project)}",
        f"> 测试套件：{_safe_name(suite)}",
        f"> 状态：{_status_label(execution.status)}",
        f"> 统计：总数 {execution.total_count} / 通过 {execution.passed_count} / 失败 {execution.failed_count} / 跳过 {execution.skipped_count} / 错误 {execution.error_count}",
        f"> 通过率：{execution.pass_rate}%",
        f"> 耗时：{_format_duration(execution.duration)}",
        f"> 执行人：{getattr(user, 'username', '-') or '-'}",
    ]
    if detail_url:
        lines.append(f"> 详情：[查看执行历史]({detail_url})")
    _send_markdown("测试任务执行结果", lines)


def notify_manual_test_assignment(run, assignee, creator) -> None:
    project = getattr(run, "project", None)
    detail_url = _frontend_url("/manual-test-executions")
    lines = [
        f"> 项目：{_safe_name(project)}",
        f"> 执行批次：{run.name}",
        f"> 分派人员：{getattr(creator, 'username', '-') or '-'}",
        f"> 测试人员：{getattr(assignee, 'username', '-') or '-'}",
        f"> 用例数量：{run.total_count}",
        f"> 状态：{_status_label(run.status)}",
    ]
    if detail_url:
        lines.append(f"> 详情：[查看用例执行]({detail_url})")
    _send_markdown("人工用例执行任务分派", lines)


def notify_manual_test_completion(run) -> None:
    project = getattr(run, "project", None)
    detail_url = _frontend_url("/manual-test-executions")
    executed = run.passed_count + run.failed_count
    pass_rate = round(run.passed_count / run.total_count * 100, 1) if run.total_count else 0
    lines = [
        f"> 项目：{_safe_name(project)}",
        f"> 执行批次：{run.name}",
        f"> 状态：已完成",
        f"> 统计：总数 {run.total_count} / 通过 {run.passed_count} / 不通过 {run.failed_count} / 待执行 {run.pending_count}",
        f"> 通过率：{pass_rate}%",
        f"> 已执行：{executed}/{run.total_count}",
    ]
    if detail_url:
        lines.append(f"> 详情：[查看执行结果]({detail_url})")
    _send_markdown("人工用例执行任务完成", lines)
