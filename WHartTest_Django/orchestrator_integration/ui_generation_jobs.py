"""Durable post-run jobs. Queue messages contain IDs, never scripts or credentials."""
import logging
import os
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from testcases.models import TestCase, TestCaseRunRecord

logger = logging.getLogger(__name__)
ACTIVE_STATUSES = {"queued", "generating", "validating", "stopping"}
REPORT_HEADING = "### UI 自动化用例生成"


def job_timeout_seconds():
    try:
        value = int(os.getenv("UI_GENERATION_JOB_TIMEOUT_SECONDS", "900"))
    except ValueError:
        value = 900
    return max(60, min(value, 1500))


def write_state(record, state):
    record.ui_generation = state
    base_summary = (record.summary or "").split(REPORT_HEADING, 1)[0].rstrip()
    text = state.get("message", "")
    if state.get("reason"):
        text += f"\n原因：{state['reason']}"
    record.summary = f"{base_summary}\n\n{REPORT_HEADING}\n{text}"
    record.save(update_fields=["ui_generation", "summary"])


def update_job(record_id, task_id, **changes):
    with transaction.atomic():
        record = TestCaseRunRecord.objects.select_for_update().get(pk=record_id)
        state = dict(record.ui_generation or {})
        if state.get("task_id") != task_id or state.get("status") not in ACTIVE_STATUSES:
            return False
        if state.get("status") == "stopping" and changes.get("status") not in {"stopped", "failed"}:
            return False
        state.update(changes, updated_at=timezone.now().isoformat())
        write_state(record, state)
        return True


def reconcile_expired_job(record):
    if (record.ui_generation or {}).get("status") not in ACTIVE_STATUSES:
        return record
    with transaction.atomic():
        current = TestCaseRunRecord.objects.select_for_update().get(pk=record.pk)
        state = dict(current.ui_generation or {})
        deadline = parse_datetime(state.get("deadline_at", "")) if state.get("deadline_at") else None
        if state.get("status") in ACTIVE_STATUSES and deadline and deadline <= timezone.now():
            state.update(status="failed",
                         message="功能测试已通过，但 UI 用例生成任务超时。可使用已有执行记录重试。",
                         reason="后台任务未在规定时间内完成。", completed_at=timezone.now().isoformat(),
                         updated_at=timezone.now().isoformat())
            write_state(current, state)
    record.refresh_from_db()
    return record


def enqueue_generation(record_id, *, llm_config_id=None, retry=False):
    """Serialize submissions for a case; duplicate requests reuse the existing job."""
    with transaction.atomic():
        initial = TestCaseRunRecord.objects.get(pk=record_id)
        TestCase.objects.select_for_update().get(pk=initial.testcase_id)
        record = TestCaseRunRecord.objects.select_for_update().get(pk=record_id)
        if record.status != "pass" or not record.generate_playwright_script:
            raise ValueError("只有已通过且启用自动生成的功能执行记录可以生成 UI 用例。")
        if not record.executor_id:
            raise ValueError("执行用户不存在，无法生成 UI 用例。")
        state = dict(record.ui_generation or {})
        if state.get("status") in ACTIVE_STATUSES or state.get("status") == "success":
            return state
        if state and not retry:
            return state
        if TestCaseRunRecord.objects.filter(
            testcase_id=record.testcase_id, ui_generation__status__in=list(ACTIVE_STATUSES),
        ).exclude(pk=record.pk).exists():
            raise ValueError("该用例已有生成任务，请等待完成后再重试。")
        now = timezone.now()
        state = {
            "enabled": True, "status": "queued", "task_id": uuid.uuid4().hex,
            "llm_config_id": llm_config_id or state.get("llm_config_id"),
            "attempt": int(state.get("attempt", 0)) + 1,
            "queued_at": now.isoformat(), "updated_at": now.isoformat(),
            "deadline_at": (now + timedelta(seconds=job_timeout_seconds() + 300)).isoformat(),
            "message": "功能测试已通过，UI 用例生成已排队。切页或刷新后后台会继续执行。",
        }
        write_state(record, state)

        def publish():
            from .tasks import generate_ui_case_from_run
            try:
                generate_ui_case_from_run.apply_async(
                    args=[record.pk, state["task_id"]], task_id=state["task_id"], retry=False,
                )
            except Exception:
                logger.exception("Could not enqueue UI generation; run=%s", record.pk)
                update_job(record.pk, state["task_id"], status="failed",
                           message="UI 用例生成任务提交失败，可重试。",
                           reason="后台任务队列暂不可用。", completed_at=timezone.now().isoformat())
        transaction.on_commit(publish)
    record.refresh_from_db()
    return record.ui_generation


def request_job_stop(record):
    with transaction.atomic():
        current = TestCaseRunRecord.objects.select_for_update().get(pk=record.pk)
        state = dict(current.ui_generation or {})
        status = state.get("status")
        if status in ACTIVE_STATUSES:
            # A running task acknowledges stop after recorder cleanup, before allowing retry.
            state.update(status="stopped" if status == "queued" else "stopping",
                         message="UI 用例生成已停止。" if status == "queued" else "正在停止 UI 用例生成并清理浏览器。",
                         updated_at=timezone.now().isoformat())
            if status == "queued":
                state["completed_at"] = timezone.now().isoformat()
            write_state(current, state)
    record.refresh_from_db()
    return record.ui_generation
