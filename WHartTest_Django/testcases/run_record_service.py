"""Persist and finalize single test case runs from case management execution."""
from __future__ import annotations

import logging
import os
import re
import shutil
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
_DATA_USE_ACTION_RE = re.compile(
    r"fillFilterField|clickRowAction|queryTicketInList|runTicketDetailCaseStep|"
    r"工单号.*(?:查询|筛选)|(?:查询|筛选).*工单号|定位.*工单",
    re.IGNORECASE,
)
# 报告正文之后模型额外贴出的内容（通常是「测试用例执行」提示词要求的 JSON 结果块）
_REPORT_APPENDIX_FENCE_RE = re.compile(r"\n+```[^\n`]*\n[\s\S]*?```[ \t]*$")
_REPORT_APPENDIX_JSON_RE = re.compile(r"\n+[ \t]*[\[{][\s\S]*$")
# 全部通过时用于规范化的固定措辞（与 build_execution_result_report 保持一致）
_PASS_ANALYSIS_BULLETS = "- 失败步骤：无\n- 失败原因：各步骤均满足预期，功能符合需求。\n- 建议：无需处理。"
_PASS_TITLE_RE = re.compile(r"##\s*测试执行结果[:：]\s*通过")
_PASS_ANALYSIS_SECTION_RE = re.compile(r"###\s*问题分析[^\n]*\n([\s\S]*?)(?=\n#{3}\s|\Z)")
_PASS_CONCLUSION_SECTION_RE = re.compile(r"(###\s*结论[^\n]*\n)[\s\S]*\Z")


def has_execution_result_report(text: str) -> bool:
    return is_filled_execution_result_report(text)


def build_pre_data_usage(data_run, assistant_transcript: str) -> dict[str, Any]:
    """Build auditable evidence that generated data was actually used by the agent.

    The human prompt contains the generated snapshot, so it must never be used as
    evidence.  Only the assistant/tool transcript is examined here.
    """
    if not data_run:
        return {"status": "not_applicable", "message": "本次执行未自动造数"}

    snapshot = getattr(data_run, "output_snapshot", None)
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    identifiers = {
        key: str(value).strip()
        for key, value in snapshot.items()
        if key in {"ticketNo", "ticketId", "work_order_id", "processingTicketId"}
        and value not in (None, "")
    }
    if not identifiers:
        return {
            "status": "not_confirmed",
            "message": "本次造数未返回可追踪的工单标识",
            "identifiers": {},
        }

    text = assistant_transcript or ""
    matched = [(key, value) for key, value in identifiers.items() if value in text]
    if not matched:
        return {
            "status": "not_confirmed",
            "message": "执行日志未发现本次造数的工单标识",
            "identifiers": identifiers,
        }

    key, value = matched[0]
    position = text.find(value)
    evidence = text[max(0, position - 220): position + len(value) + 320].strip()
    action_nearby = _DATA_USE_ACTION_RE.search(evidence)
    if not action_nearby:
        # A tool call and the identifier can be separated by formatting, but a
        # transcript that only mentions the identifier remains an unverified reference.
        action_nearby = _DATA_USE_ACTION_RE.search(text)
    if action_nearby:
        return {
            "status": "verified_used",
            "message": "已在执行脚本或工具日志中确认使用本次造数数据",
            "identifiers": identifiers,
            "matched_identifier": {"key": key, "value": value},
            "evidence": evidence[:800],
        }
    return {
        "status": "referenced",
        "message": "执行日志引用了造数标识，但缺少查询、定位或行操作证据",
        "identifiers": identifiers,
        "matched_identifier": {"key": key, "value": value},
        "evidence": evidence[:800],
    }


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
    failed_number: Any = ""
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
            if not failed_number:
                failed_number = number
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
        f"本次测试执行全部 {len(step_results) or '-'} 个步骤均通过，测试通过。"
        if passed
        else f"本次测试执行在步骤 {failed_number or '?'} 失败，未完成全部步骤，测试不通过。"
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


def strip_execution_report_appendix(report: str) -> str:
    """裁掉报告正文之后的内容，避免报告卡片里拖出一段原始 JSON。

    对话内容里保留 JSON 是必要的（``_extract_test_result_json`` 依赖它填充
    ``step_results``），但展示用的 summary 只应该有 markdown 报告本身。
    """
    text = (report or "").rstrip()
    # 1) 结尾的围栏代码块：```json … ``` / ``` … ```
    text = _REPORT_APPENDIX_FENCE_RE.sub("", text).rstrip()
    # 2) 结尾的裸 JSON（模型偶尔忘记加围栏）
    match = _REPORT_APPENDIX_JSON_RE.search(text)
    if match and len(text) - match.start() > 20:
        text = text[: match.start()].rstrip()
    return text


def normalize_passed_execution_report(report: str) -> str:
    """全部通过时把「问题分析 / 结论」写成固定措辞。

    模型自己写报告和后台兜底各有一套说法（「无失败步骤」vs「失败步骤：无」、
    「所有测试步骤执行完成…」vs「测试通过。」），同一场景每次都不一样。
    通过场景不需要自由发挥，统一按 ``build_execution_result_report`` 的措辞输出。
    """
    text = report or ""
    if not _PASS_TITLE_RE.search(text):
        return text

    section = _PASS_ANALYSIS_SECTION_RE.search(text)
    if section:
        body = section.group(1)
        # 用带冒号的标签判断（「无失败步骤」这种压缩写法不含冒号，需要补齐）
        if not re.search(r"失败步骤\s*[:：]", body):
            text = (
                text[: section.start(1)]
                + _PASS_ANALYSIS_BULLETS
                + "\n"
                + text[section.end(1) :]
            )

    conclusion = _PASS_CONCLUSION_SECTION_RE.search(text)
    if conclusion:
        step_count = len(re.findall(r"^\|\s*\d+\s*\|", text, re.MULTILINE))
        text = (
            text[: conclusion.start(1)]
            + conclusion.group(1)
            + f"本次测试执行全部 {step_count or '-'} 个步骤均通过，测试通过。"
        )
    return text.rstrip()


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
        report = tail[: next_match.start() + 1].strip()
    else:
        report = tail.strip()
    return normalize_passed_execution_report(strip_execution_report_appendix(report))


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


def _cleanup_testcase_screenshots(testcase_id: int) -> None:
    """Delete previous screenshots so each run starts with a clean set."""
    try:
        from django.conf import settings

        from testcases.models import TestCase, TestCaseScreenshot

        screenshots = list(TestCaseScreenshot.objects.filter(test_case_id=testcase_id))
        for screenshot in screenshots:
            if screenshot.screenshot and os.path.isfile(screenshot.screenshot.path):
                try:
                    os.remove(screenshot.screenshot.path)
                except OSError:
                    pass
            screenshot.delete()

        testcase = TestCase.objects.filter(id=testcase_id).only("project_id").first()
        if testcase and testcase.project_id:
            runtime_dir = os.path.join(
                settings.MEDIA_ROOT,
                "skill_runtime",
                "screenshots",
                str(testcase.project_id),
                str(testcase_id),
            )
            if os.path.isdir(runtime_dir):
                shutil.rmtree(runtime_dir, ignore_errors=True)
    except Exception as exc:
        logger.warning("Failed to cleanup screenshots for testcase %s: %s", testcase_id, exc)


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

    _cleanup_testcase_screenshots(testcase_id)

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
        record = TestCaseRunRecord.objects.select_related("testcase", "data_generation_run").prefetch_related(
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
    record.data_usage = build_pre_data_usage(record.data_generation_run, assistant_text)

    record.completed_at = timezone.now()
    record.save(
        update_fields=[
            "status",
            "summary",
            "step_results",
            "execution_log",
            "data_usage",
            "completed_at",
        ]
    )
    return record
