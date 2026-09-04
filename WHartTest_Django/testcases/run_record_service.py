"""Persist and finalize single test case runs from case management execution."""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from django.utils import timezone

from .models import TestCase, TestCaseRunRecord
from .tasks import _extract_test_result_json

logger = logging.getLogger(__name__)

_FAIL_HINTS = (
    "result=fail",
    "failed",
    "失败",
    "不通过",
    "未通过",
    "文件不存在",
    "命令执行失败",
    "syntaxerror",
    "timeout",
    "err_blocked_by_client",
    "already been declared",
    "strict mode violation",
)
_PASS_HINTS = ("result=pass", "passed", "测试执行结果: 通过", "测试执行结果：通过")
_REPORT_TITLE_RE = re.compile(r"测试执行结果[:：]\s*(通过|不通过)(?=\s*(?:\n|$|。))")
_REPORT_TEMPLATE_MARKERS = (
    "通过/不通过",
    "通过 / 失败：具体原因 / 未执行",
    "- 测试用例ID:\n",
    "- 测试用例ID：\n",
    "| 1 | … |",
)
_RESULT_FAIL_RE = re.compile(r"RESULT=FAIL[:：]?\s*(.+)", re.IGNORECASE)
_FILE_MISSING_RE = re.compile(r"文件不存在[:：]?\s*(.+)")
_COMMAND_FAIL_RE = re.compile(r"命令执行失败[^\n]*")
_TIMEOUT_RE = re.compile(r"(TimeoutError|Timeout \d+ms exceeded)[^\n]*", re.IGNORECASE)
_STEP_RE = re.compile(r"(?:步骤|step)\s*(\d+)", re.IGNORECASE)
_CASE_STEP_RE = re.compile(r"case_\d+_step(\d+)", re.IGNORECASE)


def has_execution_result_report(text: str) -> bool:
    return is_filled_execution_result_report(text)


def is_filled_execution_result_report(text: str) -> bool:
    """True only when the assistant produced a resolved pass/fail report, not the prompt template."""
    if not text or "测试执行结果" not in text:
        return False
    if any(marker in text for marker in _REPORT_TEMPLATE_MARKERS):
        return False
    title = _REPORT_TITLE_RE.search(text)
    return bool(title and title.group(1) in ("通过", "不通过"))


def collect_message_transcript(messages, *, exclude_human: bool = False) -> str:
    parts: list[str] = []
    for msg in messages or []:
        if exclude_human and type(msg).__name__ == "HumanMessage":
            continue
        content = getattr(msg, "content", None)
        if not content:
            continue
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and item.get("text"):
                    parts.append(str(item["text"]))
                else:
                    parts.append(str(item))
        else:
            parts.append(str(content))
    return "\n".join(parts)


def collect_assistant_transcript(messages) -> str:
    parts: list[str] = []
    for msg in messages or []:
        if type(msg).__name__ != "AIMessage":
            continue
        content = getattr(msg, "content", None)
        if not content:
            continue
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and item.get("text"):
                    parts.append(str(item["text"]))
                else:
                    parts.append(str(item))
        else:
            parts.append(str(content))
    return "\n".join(parts)


def _infer_status_from_text(final_content: str) -> tuple[str, str, list]:
    title = _REPORT_TITLE_RE.search(final_content or "")
    if title:
        status = "pass" if title.group(1) == "通过" else "fail"
        return status, (final_content or "")[:8000], []
    lowered = (final_content or "").lower()
    if any(token in lowered for token in _FAIL_HINTS):
        return "fail", final_content[:8000], []
    if any(token in lowered for token in _PASS_HINTS):
        return "pass", final_content[:8000], []
    return "fail", final_content[:8000], []


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


def _testcase_steps(testcase) -> list:
    steps = getattr(testcase, "steps", None)
    if steps is None:
        return []
    if hasattr(steps, "all"):
        return list(steps.all().order_by("step_number"))
    return list(steps)


def _extract_fail_context(transcript: str) -> dict[str, Any]:
    text = transcript or ""
    reason = ""
    fail_match = _RESULT_FAIL_RE.search(text)
    if fail_match:
        reason = fail_match.group(0).strip()
    elif _FILE_MISSING_RE.search(text):
        reason = _FILE_MISSING_RE.search(text).group(0).strip()
    elif _COMMAND_FAIL_RE.search(text):
        reason = _COMMAND_FAIL_RE.search(text).group(0).strip()
    elif _TIMEOUT_RE.search(text):
        reason = _TIMEOUT_RE.search(text).group(0).strip()
    elif "SyntaxError" in text:
        reason = "脚本 SyntaxError，执行中途结束"
    elif "already been declared" in text:
        reason = "脚本重复声明 chromium，执行中途结束"
    elif "ERR_BLOCKED_BY_CLIENT" in text:
        reason = "页面导航被拦截（ERR_BLOCKED_BY_CLIENT）"
    elif "登录失败" in text:
        login_match = re.search(r"登录失败[^\n\"']{0,120}", text)
        reason = login_match.group(0).strip() if login_match else "登录失败"
    elif "strict mode violation" in text.lower():
        sm = re.search(r"strict mode violation:[^\n]{0,200}", text, re.IGNORECASE)
        reason = (
            sm.group(0).strip()
            if sm
            else "定位不唯一（strict mode），请用 helpers.selectFormDropdownOption 选工单状态，禁止 getByText('处理中')"
        )
    elif re.search(r"步骤\s*1[^\n]{0,40}失败", text):
        reason = "第1步登录或前置操作失败"
    else:
        reason = "执行中途结束，未输出完整测试报告"

    failed_step = None
    window = text
    if fail_match:
        start = max(0, fail_match.start() - 400)
        window = text[start : fail_match.end() + 80]
    step_matches = _STEP_RE.findall(window) or _STEP_RE.findall(text)
    case_matches = _CASE_STEP_RE.findall(window) or _CASE_STEP_RE.findall(text)
    numbers = [int(n) for n in (step_matches + case_matches) if str(n).isdigit()]
    if numbers:
        failed_step = numbers[-1]
    return {"reason": reason, "failed_step": failed_step}


def _infer_run_status(*, transcript: str, stopped: bool, error_message: Optional[str]) -> str:
    if stopped:
        return "stopped"
    if error_message:
        return "error"
    title = _REPORT_TITLE_RE.search(transcript or "")
    if title:
        return "pass" if title.group(1) == "通过" else "fail"
    lowered = (transcript or "").lower()
    if any(token in lowered for token in _FAIL_HINTS):
        return "fail"
    if "RESULT=PASS" in (transcript or "") and "RESULT=FAIL" not in (transcript or ""):
        return "pass"
    if any(token in lowered for token in _PASS_HINTS) and not any(
        token in lowered for token in _FAIL_HINTS
    ):
        return "pass"
    return "fail"


def build_step_results(
    testcase,
    *,
    status: str,
    failed_step: Optional[int],
    fail_reason: str,
) -> list[dict[str, Any]]:
    steps = _testcase_steps(testcase)
    results = []
    passed = status == "pass"
    for step in steps:
        number = int(getattr(step, "step_number", 0) or 0)
        description = getattr(step, "description", "") or ""
        expected = getattr(step, "expected_result", "") or ""
        if passed:
            step_status, actual = "pass", "通过"
        elif failed_step and number < failed_step:
            step_status, actual = "pass", "通过"
        elif failed_step and number == failed_step:
            step_status, actual = "fail", fail_reason
        elif failed_step and number > failed_step:
            step_status, actual = "skip", "未执行"
        else:
            step_status = "fail" if number <= 1 else "skip"
            actual = fail_reason if number <= 1 else "未执行"
        results.append(
            {
                "step_number": number,
                "description": description,
                "expected_result": expected,
                "actual_result": actual,
                "status": step_status,
            }
        )
    return results


def build_execution_result_report(
    testcase,
    *,
    status: str,
    step_results: list[dict[str, Any]],
    fail_reason: str,
    extra_note: str = "",
) -> str:
    passed = status == "pass"
    title = "通过" if passed else "不通过"
    case_id = getattr(testcase, "id", "") or ""
    name = getattr(testcase, "name", "") or ""
    level = getattr(testcase, "level", "") or ""
    rows = []
    failed_desc = ""
    for item in step_results:
        number = item.get("step_number") or ""
        description = (item.get("description") or "").replace("|", "\\|").replace("\n", " ")
        step_status = item.get("status")
        if step_status == "pass":
            result = item.get("actual_result") or "符合预期"
            status_col = "✅ 通过"
        elif step_status == "fail":
            result = f"失败：{item.get('actual_result') or fail_reason}"
            status_col = "❌ 失败"
            failed_desc = description
        else:
            result = "—"
            status_col = "⏭ 未执行"
        rows.append(f"| {number} | {description} | {result} | {status_col} |")
    if not rows:
        rows.append("| - | （未读取到用例步骤） | — | ⏭ 未执行 |")

    analysis_reason = fail_reason if not passed else "各步骤均满足预期，功能符合需求。"
    suggestion = extra_note or (
        "请根据失败原因补充测试数据或修正页面操作后重跑。"
        if not passed
        else "无需处理。"
    )
    conclusion = (
        "测试通过。"
        if passed
        else "测试不通过。未执行步骤已标为「未执行」，请处理后重新执行。"
    )
    return (
        f"## 测试执行结果: {title}\n\n"
        f"### 基本信息\n"
        f"- 测试用例ID: {case_id}\n"
        f"- 名称: {name}\n"
        f"- 优先级: {level}\n\n"
        f"### 执行过程与结果\n"
        f"| 步骤 | 操作 | 结果 | 状态 |\n"
        f"|------|------|------|------|\n"
        + "\n".join(rows)
        + "\n\n"
        f"### 问题分析\n"
        f"- 失败步骤：{failed_desc or ('无' if passed else '执行中途结束')}\n"
        f"- 失败原因：{analysis_reason}\n"
        f"- 建议：{suggestion}\n\n"
        f"### 结论\n{conclusion}"
    )


def extract_first_execution_report(text: str) -> str:
    content = (text or "").strip()
    if not content:
        return ""
    match = re.search(r"##\s*测试执行结果[:：]\s*(通过|不通过)", content)
    if not match:
        return content[:8000]
    start = match.start()
    tail = content[start:]
    rest = tail[1:]
    next_match = re.search(r"##\s*测试执行结果[:：]\s*(通过|不通过)", rest)
    if next_match:
        return tail[: next_match.start() + 1].strip()
    return tail.strip()


def ensure_execution_result_report(
    testcase,
    *,
    transcript: str,
    assistant_transcript: str = "",
    stopped: bool = False,
    error_message: Optional[str] = None,
) -> dict[str, Any]:
    """Return a full 1436-style report even when the model ended silently."""
    combined = transcript or ""
    assistant_text = assistant_transcript or combined
    if error_message:
        combined = f"{combined}\n{error_message}".strip()
    if stopped and "用户停止" not in combined:
        combined = f"{combined}\n执行已被用户停止".strip()

    status = _infer_run_status(
        transcript=combined, stopped=stopped, error_message=error_message
    )
    fail_ctx = _extract_fail_context(combined)
    fail_reason = error_message or fail_ctx["reason"]
    if stopped:
        fail_reason = "执行已被用户停止"
    extra_note = ""
    if stopped:
        extra_note = "本次为用户手动停止，已执行步骤见上表。"
    elif error_message:
        extra_note = "模型或服务异常导致执行中断，请重试。"

    already_has_report = is_filled_execution_result_report(assistant_text)
    title = _REPORT_TITLE_RE.search(assistant_text)
    if title:
        status = "pass" if title.group(1) == "通过" else "fail"

    parsed = parse_execution_outcome(assistant_text) if already_has_report else None
    step_results = (parsed or {}).get("step_results") or []
    if not step_results:
        step_results = build_step_results(
            testcase,
            status=status if status in ("pass", "fail") else "fail",
            failed_step=fail_ctx["failed_step"],
            fail_reason=fail_reason,
        )

    if already_has_report:
        summary = assistant_text
        start = assistant_text.find("## 测试执行结果")
        if start < 0:
            start = assistant_text.find("测试执行结果")
        if start >= 0:
            summary = assistant_text[start:].strip()
        return {
            "status": status,
            "summary": extract_first_execution_report(summary)[:8000],
            "step_results": step_results,
            "injected": False,
            "report": extract_first_execution_report(summary),
        }

    report = build_execution_result_report(
        testcase,
        status=status if status in ("pass", "fail") else "fail",
        step_results=step_results,
        fail_reason=fail_reason,
        extra_note=extra_note,
    )
    return {
        "status": status,
        "summary": report[:8000],
        "step_results": step_results,
        "injected": True,
        "report": report,
    }


def start_testcase_run_record(
    *,
    testcase_id: int,
    user_id: int,
    session_id: str,
    generate_playwright_script: bool = False,
    data_generation_run_id: Optional[int] = None,
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
            "data_generation_run_id": data_generation_run_id,
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
            data_generation_run_id=data_generation_run_id,
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
    transcript: str = "",
    assistant_transcript: str = "",
) -> Optional[TestCaseRunRecord]:
    try:
        record = TestCaseRunRecord.objects.select_related("testcase").prefetch_related(
            "testcase__steps"
        ).get(session_id=session_id)
    except TestCaseRunRecord.DoesNotExist:
        return None

    if record.status != "running":
        return record

    combined = "\n".join(part for part in (transcript, final_content) if part).strip()
    assistant_text = "\n".join(
        part for part in (assistant_transcript, final_content) if part
    ).strip()
    outcome = ensure_execution_result_report(
        record.testcase,
        transcript=combined,
        assistant_transcript=assistant_text,
        stopped=stopped,
        error_message=error_message,
    )
    record.status = outcome["status"]
    record.summary = extract_first_execution_report(outcome["summary"])[:8000]
    record.step_results = outcome["step_results"]
    record.execution_log = (combined or error_message or "")[:8000]
    record.injected_report = outcome["injected"]

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
