"""Persist and finalize single test case runs from case management execution."""
from __future__ import annotations

import logging
from typing import Any, Optional

from django.utils import timezone

from .models import TestCase, TestCaseRunRecord
from .tasks import _extract_test_result_json

logger = logging.getLogger(__name__)


def _infer_status_from_text(final_content: str) -> tuple[str, str, list]:
    lowered = (final_content or "").lower()
    if any(token in lowered for token in ("fail", "failed", "失败", "不通过", "未通过")):
        return "fail", final_content[:2000], []
    if any(token in lowered for token in ("pass", "passed", "通过", "成功")):
        return "pass", final_content[:2000], []
    return "pass", final_content[:2000], []


def parse_execution_outcome(final_content: str) -> dict[str, Any]:
    parsed = _extract_test_result_json(final_content or "")
    if parsed:
        status = "pass" if parsed.get("status") == "pass" else "fail"
        return {
            "status": status,
            "summary": parsed.get("summary") or "",
            "step_results": parsed.get("steps") or [],
        }
    status, summary, step_results = _infer_status_from_text(final_content or "")
    return {
        "status": status,
        "summary": summary,
        "step_results": step_results,
    }


def start_testcase_run_record(
    *,
    testcase_id: int,
    user_id: int,
    session_id: str,
    generate_playwright_script: bool = False,
) -> Optional[TestCaseRunRecord]:
    try:
        testcase = TestCase.objects.get(id=testcase_id)
    except TestCase.DoesNotExist:
        logger.warning("Skip run record: testcase %s not found", testcase_id)
        return None

    record, created = TestCaseRunRecord.objects.get_or_create(
        session_id=session_id,
        defaults={
            "testcase": testcase,
            "executor_id": user_id,
            "status": "running",
            "generate_playwright_script": generate_playwright_script,
        },
    )
    if not created and record.status == "running":
        return record
    if not created:
        TestCaseRunRecord.objects.filter(pk=record.pk).update(
            testcase=testcase,
            executor_id=user_id,
            status="running",
            summary="",
            step_results=[],
            execution_log="",
            completed_at=None,
            generate_playwright_script=generate_playwright_script,
            started_at=timezone.now(),
        )
        record.refresh_from_db()
    return record


def finalize_testcase_run_record(
    *,
    session_id: str,
    final_content: str = "",
    stopped: bool = False,
    error_message: Optional[str] = None,
) -> Optional[TestCaseRunRecord]:
    try:
        record = TestCaseRunRecord.objects.get(session_id=session_id)
    except TestCaseRunRecord.DoesNotExist:
        return None

    if record.status != "running":
        return record

    if stopped:
        record.status = "stopped"
        record.summary = "执行已被用户停止"
    elif error_message:
        record.status = "error"
        record.summary = error_message[:2000]
        record.execution_log = error_message[:5000]
    else:
        outcome = parse_execution_outcome(final_content)
        record.status = outcome["status"]
        record.summary = outcome["summary"]
        record.step_results = outcome["step_results"]
        if final_content and not record.summary:
            record.execution_log = final_content[:8000]

    record.completed_at = timezone.now()
    record.save(
        update_fields=[
            "status",
            "summary",
            "step_results",
            "execution_log",
            "completed_at",
        ]
    )
    return record
