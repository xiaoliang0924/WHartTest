"""Persist and finalize single test case runs from case management execution."""
from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import timedelta
from typing import Any, Optional

from django.db import transaction
from django.utils import timezone

from .models import TestCase, TestCaseRunRecord
from .tasks import _extract_test_result_json

logger = logging.getLogger(__name__)


def testcase_bound_ui_timeout_seconds() -> float:
    """Single source of truth for bound UI script execution timeout."""
    try:
        return max(30.0, float(os.environ.get("TESTCASE_BOUND_UI_TIMEOUT_SECONDS", "600")))
    except (TypeError, ValueError):
        return 600.0


def reconcile_stale_bound_ui_run_records() -> int:
    """Close orphaned run records left behind by a timed-out/interrupted UI script."""
    try:
        grace_seconds = max(
            0.0,
            float(os.environ.get("TESTCASE_BOUND_UI_TIMEOUT_GRACE_SECONDS", "120")),
        )
    except (TypeError, ValueError):
        grace_seconds = 120.0

    now = timezone.now()
    cutoff = now - timedelta(seconds=testcase_bound_ui_timeout_seconds() + grace_seconds)
    ui_script_paths = ("bound_ui_script", "hybrid_script_pending")
    stale_records = TestCaseRunRecord.objects.select_for_update().filter(
        status="running",
        actual_execution_path__in=ui_script_paths,
        started_at__lte=cutoff,
    )

    closed_count = 0
    with transaction.atomic():
        for record in stale_records:
            # Re-check the state when writing so a concurrent successful finalizer
            # can never be overwritten by the periodic reconciler.
            note = (
                "绑定 UI 自动化脚本执行超时或执行进程中断；"
                f"超过配置时限 {testcase_bound_ui_timeout_seconds():g} 秒及收尾宽限期 "
                f"{grace_seconds:g} 秒，系统已将遗留运行记录标记为错误。"
            )
            updated = TestCaseRunRecord.objects.filter(
                pk=record.pk,
                status="running",
                actual_execution_path__in=ui_script_paths,
            ).update(
                status="error",
                summary=note,
                execution_log=(record.execution_log + "\n" + note).strip(),
                completed_at=now,
            )
            closed_count += updated

    if closed_count:
        logger.warning("Reconciled %s stale bound UI testcase run records", closed_count)
    return closed_count

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
    "playwright exec timed out",
    "浏览器已重置",
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
_HELPER_FAIL_LINE_RE = re.compile(
    r"^RESULT=FAIL[:：]\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)
_HELPER_STEP_RESULT_RE = re.compile(
    r"RESULT=(PASS|FAIL)\b[:：]?\s*步骤\s*(\d+)",
    re.IGNORECASE,
)
_AUTO_RESUME_STEP_RE = re.compile(
    r"\[AUTO_RESUME\]\s*强制执行步骤\s*(\d+)\s*/",
    re.IGNORECASE,
)
_INSTRUCTIONAL_FAIL_RE = re.compile(
    r"console\.log|process\.exit|禁止\s*`?throw|通过则|不要杀进程|\.\.\.\)",
    re.IGNORECASE,
)
_NON_EXECUTION_RESULT_FAIL_RE = re.compile(
    r"RESULT=FAIL\s*(?:"
    r"时禁止继续[^\n]*|"
    r"时立即判不通过，禁止自行补截图或改报通过|"
    r"必须停止，禁止改报通过|"
    r"[:：][^\n]*?(?:禁止\s*`?throw|通过则\s*`?RESULT=PASS|process\.exit|console\.log)"
    r")[^\n]*",
    re.IGNORECASE,
)
# Skill 文档 / 提示词里的 RESULT=FAIL 示例（非真实 helper 输出）
_SKILL_DOC_RESULT_FAIL_RE = re.compile(
    r"(?m)^[^\n]*RESULT=FAIL[^\n]*(?:"
    r"禁止\s*(?:throw|`throw`)|"
    r"通过则|"
    r"process\.exit|"
    r"console\.log|"
    r"\.\.\.\)[`']?\s*\+\s*截图"
    r")[^\n]*\n?",
    re.IGNORECASE,
)
_FILE_MISSING_RE = re.compile(r"文件不存在[:：]?\s*(.+)")
_COMMAND_FAIL_RE = re.compile(r"命令执行失败[^\n]*")
_TIMEOUT_RE = re.compile(
    r"(TimeoutError|Timeout \d+ms exceeded|Playwright exec timed out after \d+ms)[^\n]*",
    re.IGNORECASE,
)
_STEP_RE = re.compile(r"(?:步骤|step)\s*(\d+)", re.IGNORECASE)
_CASE_STEP_RE = re.compile(r"case_\d+_step(\d+)", re.IGNORECASE)
_CASE_SCREENSHOT_PATH_RE = re.compile(
    r"\[(?:CASE_SCREENSHOT|SCREENSHOT_SAVED|AUTO_UPLOAD)\][^\n]*?"
    r"((?:[A-Za-z]:)?[^\s<>\"'`|]*case_\d+_step\d+\.png)",
    re.IGNORECASE,
)
_DATA_USE_ACTION_RE = re.compile(
    r"fillFilterField|clickRowAction|queryTicketInList|runTicketDetailCaseStep|"
    r"runClaimableTicketCaseStep|WHARTTEST_TICKET_NO|\[PRE_DATA\]\s*ticketNo=|"
    r"enterClaimableTicketDetailFromList|ticketId\s*直达|已通过 ticketId 进入工单详情|"
    r"工单号.*(?:查询|筛选)|(?:查询|筛选).*工单号|定位.*工单|"
    r"已进入.{0,20}(?:工单)?详情|"
    r"(?:工单|ticket).{0,12}(?:详情页|detail page)",
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


def build_pre_data_usage(
    data_run,
    assistant_transcript: str,
    *,
    execution_transcript: str = "",
) -> dict[str, Any]:
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

    text = "\n".join(
        part for part in (assistant_transcript or "", execution_transcript or "") if part
    )
    matched = [(key, value) for key, value in identifiers.items() if value in text]
    if not matched:
        return {
            "status": "not_confirmed",
            "message": "执行日志未发现本次造数的工单标识",
            "identifiers": identifiers,
        }

    key, value = matched[0]
    position = text.find(value)
    evidence = text[max(0, position - 500): position + len(value) + 700].strip()
    # Do not let an unrelated action elsewhere in a long run transcript confirm
    # this identifier. Tool/action evidence must be close to the exact ID.
    identifier_context = text[
        max(0, position - 180): position + len(value) + 180
    ]
    action_nearby = _DATA_USE_ACTION_RE.search(identifier_context)
    action_negated = bool(
        re.search(
            r"(?:但|并|且)?\s*(?:无|未|没有|缺少|不含|不具备).{0,40}"
            r"(?:查询|筛选|定位|行操作|点击|进入)",
            identifier_context,
            re.IGNORECASE,
        )
    )
    # ROW_VISIBLE is only auditable when the same evidence window ties it to
    # the generated identifier in a ticket-detail/list URL. A generic visible
    # row elsewhere in the run must not confirm this generated record.
    escaped_identifier = re.escape(value)
    matching_ticket_url = re.search(
        rf"/(?:my-tickets|tickets)/{escaped_identifier}(?=[/?#\s]|$)",
        evidence,
        re.IGNORECASE,
    )
    visible_matching_row = bool(
        matching_ticket_url
        and re.search(r"ROW_VISIBLE\s*=\s*true", evidence, re.IGNORECASE)
    )
    if (action_nearby and not action_negated) or visible_matching_row:
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


def collect_tool_execution_transcript(messages) -> str:
    """Collect executable browser helper outputs, excluding assistant claims."""
    parts: list[str] = []
    for msg in messages or []:
        if type(msg).__name__ not in ("ToolMessage", "ToolMessageChunk"):
            continue
        if getattr(msg, "name", "") != "execute_skill_script":
            continue
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and item.get("text"):
                    parts.append(str(item["text"]))
        elif content:
            parts.append(str(content))
    from orchestrator_integration.testcase_receipts import normalize_testcase_receipts
    return "\n".join(normalize_testcase_receipts(part) for part in parts)


def append_testcase_tool_execution_output(
    *, session_id: str, testcase_id: int, output: str
) -> bool:
    """Persist browser tool output before it is returned to LangGraph.

    LangGraph checkpoints can lag a completed browser command during stream
    shutdown. Keep a small durable copy on the run record so finalization can
    still verify RESULT markers when the corresponding ToolMessage is absent.
    """
    from django.db import transaction
    from testcases.models import TestCaseRunRecord

    text = str(output or "").strip()
    if not text:
        return False
    with transaction.atomic():
        record = (
            TestCaseRunRecord.objects.select_for_update()
            .filter(
                session_id=str(session_id),
                testcase_id=int(testcase_id),
                status=TestCaseRunRecord.STATUS_CHOICES[0][0],
            )
            .first()
        )
        if not record:
            return False
        record.execution_log = _append_bounded_tool_output(
            record.execution_log or "", text
        )
        record.save(update_fields=["execution_log"])
    return True


def _append_bounded_tool_output(
    existing_log: str, output: str, *, max_chars: int = 8000
) -> str:
    """Keep tool-output delimiters intact when retaining the latest run evidence."""
    marker = "\n[TESTCASE_TOOL_OUTPUT]\n"
    existing = str(existing_log or "")
    if marker in existing:
        prefix, *chunks = existing.split(marker)
    else:
        prefix, chunks = existing, []
    chunks.append(str(output or "").strip())

    def size() -> int:
        return len(prefix) + sum(len(marker) + len(chunk) for chunk in chunks)

    while len(chunks) > 1 and size() > max_chars:
        chunks.pop(0)
    if size() > max_chars:
        # Prefer the newest tool output over an old report or a partial marker.
        prefix = ""
        available = max(0, max_chars - len(marker))
        current = chunks[-1]
        if len(current) > available:
            head_size = max(0, (available - 22) * 3 // 4)
            tail_size = max(0, available - head_size - 22)
            current = current[:head_size] + "\n[OUTPUT_TRUNCATED]\n" + (
                current[-tail_size:] if tail_size else ""
            )
        chunks = [current]
    return prefix + "".join(marker + chunk for chunk in chunks)


def _truncate_execution_log(text: str, *, max_chars: int = 8000) -> str:
    """Keep the run context and latest browser error within the database field limit."""
    value = str(text or "")
    if len(value) <= max_chars:
        return value
    marker = "\n[OUTPUT_TRUNCATED: middle of execution transcript omitted]\n"
    head_size = min(1800, max(0, (max_chars - len(marker)) // 3))
    tail_size = max(0, max_chars - head_size - len(marker))
    return value[:head_size] + marker + (value[-tail_size:] if tail_size else "")


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
    effective_content = _NON_EXECUTION_RESULT_FAIL_RE.sub("", final_content or "")
    title = _REPORT_TITLE_RE.search(effective_content)
    if title:
        status = "pass" if title.group(1) == "通过" else "fail"
        return status, (final_content or "")[:8000], []
    lowered = effective_content.lower()
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


def _strip_instructional_result_fail(text: str) -> str:
    """Remove RESULT=FAIL examples from skill docs / prompts, keep real helper fails."""
    cleaned = _NON_EXECUTION_RESULT_FAIL_RE.sub("", text or "")
    cleaned = _SKILL_DOC_RESULT_FAIL_RE.sub("", cleaned)
    return cleaned


def _extract_fail_context(transcript: str) -> dict[str, Any]:
    text = _strip_instructional_result_fail(_normalize_execution_transcript(transcript or ""))
    reason = ""
    fail_match = None
    for match in _HELPER_FAIL_LINE_RE.finditer(text):
        if _INSTRUCTIONAL_FAIL_RE.search(match.group(0)):
            continue
        fail_match = match
    if fail_match:
        reason = (fail_match.group(1) or "").strip().strip("`| ").strip()
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
    effective_transcript = _NON_EXECUTION_RESULT_FAIL_RE.sub("", transcript or "")
    title = _REPORT_TITLE_RE.search(effective_transcript)
    if title:
        return "pass" if title.group(1) == "通过" else "fail"
    lowered = effective_transcript.lower()
    if any(token in lowered for token in _FAIL_HINTS):
        return "fail"
    if "RESULT=PASS" in effective_transcript and "RESULT=FAIL" not in effective_transcript:
        return "pass"
    if any(token in lowered for token in _PASS_HINTS) and not any(
        token in lowered for token in _FAIL_HINTS
    ):
        return "pass"
    return "fail"


def collect_checkpoint_execution_transcript(
    *,
    user_id: int,
    project_id: int | str,
    session_id: str,
    tools_only: bool = False,
) -> str:
    """Load tool/assistant execution text from the latest LangGraph checkpoint."""
    thread_id = f"{user_id}_{project_id}_{session_id}"
    try:
        from wharttest_django.checkpointer import get_sync_checkpointer

        with get_sync_checkpointer() as memory:
            checkpoint_tuples = list(
                memory.list(config={"configurable": {"thread_id": thread_id}})
            )
        if not checkpoint_tuples:
            return ""
        checkpoint_data = checkpoint_tuples[0].checkpoint
        if not isinstance(checkpoint_data, dict):
            return ""
        messages = checkpoint_data.get("channel_values", {}).get("messages") or []
        if not messages:
            return ""
        if tools_only:
            return collect_tool_execution_transcript(messages)
        return collect_message_transcript(messages, exclude_human=True)
    except Exception as exc:
        logger.warning(
            "collect_checkpoint_execution_transcript failed session=%s: %s",
            session_id,
            exc,
        )
        return ""


def _normalize_execution_transcript(text: str) -> str:
    """Turn JSON-escaped helper output back into line-oriented evidence."""
    if not text:
        return ""
    return (
        str(text)
        .replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\t", "\t")
    )


def build_step_results_from_execution_evidence(
    testcase,
    transcript: str,
    *,
    fail_reason: str,
) -> list[dict[str, Any]]:
    """Derive per-step status from Playwright helper output (RESULT=PASS/FAIL + step screenshots)."""
    text = _strip_instructional_result_fail(_normalize_execution_transcript(transcript or ""))
    if not text.strip():
        return []

    case_id = int(getattr(testcase, "id", 0) or 0)
    step_outcomes: dict[int, str] = {}
    step_actuals: dict[int, str] = {}
    step_execution_blocks: dict[int, list[str]] = {}

    # The browser tool emits a boundary marker for each single-step command.
    # Use that boundary instead of relying on screenshots being printed before
    # RESULT (or on a model-generated progress line).
    execution_markers = list(re.finditer(
        r"\[CASE_STEP_EXECUTION\]\s*case=(\d+)\s+step=(\d+)",
        text,
        flags=re.IGNORECASE,
    ))
    for index, marker in enumerate(execution_markers):
        if int(marker.group(1)) != case_id:
            continue
        step_no = int(marker.group(2))
        end = (
            execution_markers[index + 1].start()
            if index + 1 < len(execution_markers)
            else len(text)
        )
        block = text[marker.end():end]
        step_execution_blocks.setdefault(step_no, []).append(block)
        result_matches = list(re.finditer(
            r"^[ \t]*RESULT[ \t]*=[ \t]*(PASS|FAIL)\b[:：]?([^\n]*)",
            block,
            flags=re.IGNORECASE | re.MULTILINE,
        ))
        if result_matches:
            last_result = result_matches[-1]
            step_outcomes[step_no] = last_result.group(1).lower()
            if last_result.group(1).lower() == "fail":
                step_actuals[step_no] = (last_result.group(2) or "").strip()
            continue

        # A screenshot is useful evidence, but it cannot stand in for a browser
        # assertion. Preserve an uncaught tool error when available so the report
        # explains what interrupted the step instead of showing a generic receipt
        # warning.
        step_outcomes[step_no] = "fail"
        timeout = _TIMEOUT_RE.search(block)
        error_line = re.search(
            r"(?m)^\s*((?:Error|TypeError|ReferenceError|SyntaxError|locator\.[\w]+):[^\n]{1,500})",
            block,
        )
        if timeout:
            step_actuals[step_no] = timeout.group(0).strip()
        elif error_line:
            step_actuals[step_no] = error_line.group(1).strip()
        else:
            step_actuals[step_no] = "步骤已执行，但工具没有返回浏览器断言回执，无法确认预期结果"

    for match in _HELPER_STEP_RESULT_RE.finditer(text):
        step_no = int(match.group(2))
        if step_no not in step_execution_blocks:
            step_outcomes[step_no] = match.group(1).lower()

    auto_parts = re.split(r"\[AUTO_RESUME\]\s*强制执行步骤\s*(\d+)\s*/\d+", text)
    auto_idx = 1
    while auto_idx + 1 < len(auto_parts):
        try:
            step_no = int(auto_parts[auto_idx])
        except (TypeError, ValueError):
            auto_idx += 2
            continue
        block = auto_parts[auto_idx + 1]
        last = None
        for result_match in re.finditer(
            r"^RESULT=(PASS|FAIL)\b",
            block,
            flags=re.IGNORECASE | re.MULTILINE,
        ):
            last = result_match.group(1).lower()
        if last:
            step_outcomes[step_no] = last
        auto_idx += 2

    # 截图行 + 随后 RESULT，补上未写成「步骤N」的 helper 输出
    try:
        matches = list(
            re.finditer(
                rf"(?:\[(?:CASE_SCREENSHOT|SCREENSHOT_SAVED)\].*?case[_-]{case_id}_step(\d+)|\b正在执行步骤\s*(\d+))"
                rf"(?:(?!\[CASE_STEP_EXECUTION\])[\s\S]){{0,2500}}?RESULT=(PASS|FAIL)\b[:：]?\s*([^\n\"}}]*)",
                text,
                flags=re.IGNORECASE | re.MULTILINE,
            )
        )
    except re.error:
        logger.exception(
            "build_step_results_from_execution_evidence regex failed case=%s",
            case_id,
        )
        matches = []
    progress_markers = list(
        re.finditer(
            r"正在执行步骤\s*(\d+)|\[AUTO_RESUME\]\s*强制执行步骤\s*(\d+)\s*/\d+",
            text,
            flags=re.IGNORECASE,
        )
    )
    progress_blocks: dict[int, str] = {}
    for idx, match in enumerate(progress_markers):
        step_no = int(match.group(1) or match.group(2))
        block_start = match.start()
        block_end = (
            progress_markers[idx + 1].start()
            if idx + 1 < len(progress_markers)
            else len(text)
        )
        progress_blocks[step_no] = text[block_start:block_end]

    for match in matches:
        step_no = int(match.group(1) or match.group(2))
        block = progress_blocks.get(step_no) or ""
        if block:
            last_in_block = None
            last_reason = ""
            for result_match in re.finditer(
                r"RESULT=(PASS|FAIL)\b[:：]?\s*([^\n\"}]*)",
                block,
                flags=re.IGNORECASE | re.MULTILINE,
            ):
                last_in_block = result_match.group(1).lower()
                last_reason = (result_match.group(2) or "").strip().rstrip(",")
            if last_in_block:
                step_outcomes[step_no] = last_in_block
                if last_in_block == "fail" and last_reason:
                    step_actuals[step_no] = last_reason
            continue
        step_outcomes[step_no] = match.group(3).lower()
        if match.group(4) and match.group(3).lower() == "fail":
            step_actuals[step_no] = match.group(4).strip().rstrip(',"')

    # Authoritative per-step outcome: compare the final result marker with the final
    # Playwright timeout in that step. A helper may print RESULT=PASS and a later
    # assertion/locator in the same script can still time out; a trailing timeout
    # must invalidate that earlier pass. Conversely, a later explicit PASS after a
    # recovered timeout remains valid.
    for step_no, window in progress_blocks.items():
        result_matches = list(re.finditer(
            r"^RESULT=(PASS|FAIL)\b",
            window,
            flags=re.IGNORECASE | re.MULTILINE,
        ))
        last_result = result_matches[-1] if result_matches else None
        if last_result:
            step_outcomes[step_no] = last_result.group(1).lower()

        timeout_matches = list(re.finditer(
            r"(?:Timeout\s+\d+ms\s+exceeded|Playwright exec timed out|"
            r"locator\.[\w]+:\s*Timeout|浏览器已重置)",
            window,
            flags=re.IGNORECASE,
        ))
        last_timeout = timeout_matches[-1] if timeout_matches else None
        if last_timeout and (not last_result or last_timeout.start() > last_result.start()):
            step_outcomes[step_no] = "fail"
            timeout_detail = _TIMEOUT_RE.search(window)
            if timeout_detail:
                step_actuals[step_no] = timeout_detail.group(0).strip()

    # A tool-call boundary is the strongest attribution available. Re-evaluate
    # it last so a trailing timeout in the same call cannot be hidden by a PASS,
    # and a PASS printed before its screenshot remains associated with the step.
    for step_no, blocks in step_execution_blocks.items():
        # A later screenshot-only call must not erase an earlier explicit
        # PASS/FAIL from the same step. Use the latest explicit receipt across
        # calls; only use a missing-receipt fallback if none exists anywhere.
        explicit_receipts = []
        trailing_timeouts = []
        for block in blocks:
            explicit_receipts.extend(re.finditer(
                r"RESULT=(PASS|FAIL)\b[:：]?([^\n\"}]*)",
                block,
                flags=re.IGNORECASE | re.MULTILINE,
            ))
            trailing_timeouts.extend(
                (block, match)
                for match in re.finditer(
                    r"(?:Timeout\s+\d+ms\s+exceeded|Playwright exec timed out|"
                    r"locator\.[\w]+:\s*Timeout|浏览器已重置)",
                    block,
                    flags=re.IGNORECASE,
                )
            )
        if explicit_receipts:
            last_result = explicit_receipts[-1]
            step_outcomes[step_no] = last_result.group(1).lower()
            if last_result.group(1).lower() == "fail":
                reason = (last_result.group(2) or "").strip().rstrip(',"')
                if reason:
                    step_actuals[step_no] = reason
        elif trailing_timeouts:
            timeout_block, last_timeout = trailing_timeouts[-1]
            step_outcomes[step_no] = "fail"
            timeout_detail = _TIMEOUT_RE.search(timeout_block)
            step_actuals[step_no] = (
                timeout_detail.group(0).strip()
                if timeout_detail
                else last_timeout.group(0).strip()
            )
        elif step_no not in step_outcomes:
            step_outcomes[step_no] = "fail"
            step_actuals[step_no] = "步骤已有浏览器执行记录，但没有返回明确断言回执"

    if not step_outcomes:
        return []

    steps = _testcase_steps(testcase)
    screenshot_steps: set[int] = set()
    try:
        from testcases.models import TestCaseScreenshot

        screenshot_steps = {
            int(number)
            for number in TestCaseScreenshot.objects.filter(
                test_case_id=case_id,
                step_number__isnull=False,
            ).values_list("step_number", flat=True)
        }
    except Exception:
        logger.debug(
            "Could not load saved screenshot evidence for testcase %s",
            case_id,
            exc_info=True,
        )

    results: list[dict[str, Any]] = []
    for step in steps:
        number = int(getattr(step, "step_number", 0) or 0)
        description = getattr(step, "description", "") or ""
        expected = getattr(step, "expected_result", "") or ""
        outcome = step_outcomes.get(number)
        actual = ""
        # Long numeric identifiers in a step (for example an order number) are
        # exact test data, not values the model may synthesize from today's date.
        # Bind them to the output attached to this step's screenshot so that a
        # plausible RESULT=PASS for a different identifier cannot pass the case.
        required_identifiers = re.findall(r"(?<!\d)\d{8,}(?!\d)", description)
        screenshot_markers = list(re.finditer(
            rf"\[(?:CASE_SCREENSHOT|SCREENSHOT_SAVED)\][^\n]*case[_-]{case_id}_step\d+\.png",
            text,
            flags=re.IGNORECASE,
        ))
        step_output = "\n".join(step_execution_blocks.get(number, []))
        for marker_index in range(len(screenshot_markers) - 1, -1, -1):
            if step_output:
                break
            marker = screenshot_markers[marker_index]
            marker_step = re.search(
                r"case[_-]" + str(case_id) + r"_step(\d+)\.png",
                marker.group(0),
                re.IGNORECASE,
            )
            if not marker_step or int(marker_step.group(1)) != number:
                continue
            end = (
                screenshot_markers[marker_index + 1].start()
                if marker_index + 1 < len(screenshot_markers)
                else len(text)
            )
            step_output = text[marker.end():end]
            break
        missing_identifiers = [value for value in required_identifiers if value not in step_output]
        if outcome == "pass" and missing_identifiers:
            outcome = "fail"
            actual = (
                "执行证据中的数据与用例步骤不一致，缺少原始值："
                + ", ".join(missing_identifiers)
            )
        if outcome == "pass":
            step_status, actual = "pass", "通过"
        elif outcome == "fail":
            step_status, actual = (
                "fail",
                actual or step_actuals.get(number) or fail_reason or "步骤执行失败",
            )
        elif number in screenshot_steps:
            step_status = "fail"
            step_status = "fail"
            actual = step_actuals.get(
                number,
                "该步骤已有截图，但缺少对应的浏览器断言回执，无法确认通过",
            )
        else:
            step_status, actual = "skip", "未执行"
        results.append(
            {
                "step_number": number,
                "description": description,
                "expected_result": expected,
                "actual_result": actual,
                "status": step_status,
            }
        )

    # Legacy/browser calls can return a screenshot and then a Playwright
    # exception without a CASE_STEP_EXECUTION marker. Keep the concrete error
    # visible instead of reducing that failure to a generic receipt warning.
    unexplained = [
        item for item in results
        if item.get("status") == "fail"
        and any(token in str(item.get("actual_result") or "") for token in (
            "没有返回浏览器断言回执",
            "没有返回明确断言回执",
            "缺少对应的浏览器断言回执",
        ))
    ]
    timeout_matches = list(_TIMEOUT_RE.finditer(text))
    if unexplained and timeout_matches:
        if len(unexplained) == 1:
            unexplained[0]["actual_result"] = timeout_matches[-1].group(0).strip()
        elif len(timeout_matches) >= len(unexplained):
            for item, match in zip(unexplained, timeout_matches[-len(unexplained):]):
                item["actual_result"] = match.group(0).strip()
    return results


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
            result = item.get("actual_result") or "未执行"
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
        else (
            "本次 UI 步骤映射未对齐，未完成全部功能步骤验证，测试不通过。"
            if not failed_number and "映射" in str(fail_reason or "")
            else f"本次测试执行在步骤 {failed_number or '?'} 失败，未完成全部步骤，测试不通过。"
        )
    )
    failed_step_label = failed_desc or (
        "无" if passed else (
            "UI 步骤映射未对齐"
            if "映射" in str(fail_reason or "")
            else "执行中途结束"
        )
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
        f"- 失败步骤：{failed_step_label}\n"
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
    execution_mode: str = "",
    tool_transcript: str = "",
) -> dict[str, Any]:
    """Return a full 1436-style report even when the model ended silently."""
    combined = transcript or ""
    assistant_text = assistant_transcript or combined
    if error_message:
        combined = f"{combined}\n{error_message}".strip()
    if stopped and "用户停止" not in combined:
        combined = f"{combined}\n执行已被用户停止".strip()

    diagnostic_transcript = (
        tool_transcript if execution_mode == "ai_only" else combined
    )
    status = _infer_run_status(
        transcript=diagnostic_transcript, stopped=stopped, error_message=error_message
    )
    fail_ctx = _extract_fail_context(diagnostic_transcript)
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
    has_authoritative_step_evidence = False
    if execution_mode == "ai_only":
        # A model-authored table is a claim, not proof that a browser action ran.
        # In AI-only mode derive every step exclusively from tool output.
        evidence_results = build_step_results_from_execution_evidence(
            testcase,
            tool_transcript,
            fail_reason=fail_reason,
        )
        if evidence_results:
            step_results = evidence_results
            has_authoritative_step_evidence = True
        else:
            step_results = [
                {
                    "step_number": int(getattr(step, "step_number", 0) or 0),
                    "description": getattr(step, "description", "") or "",
                    "expected_result": getattr(step, "expected_result", "") or "",
                    "actual_result": "没有浏览器工具执行证据",
                    "status": "skip",
                }
                for step in _testcase_steps(testcase)
            ]
            fail_reason = error_message or "纯 AI 执行没有可核对的浏览器工具步骤证据"
            status = "fail"
    elif not step_results:
        step_results = build_step_results_from_execution_evidence(
            testcase,
            combined,
            fail_reason=fail_reason,
        )
    expected_steps = _testcase_steps(testcase)
    all_steps_passed = bool(expected_steps) and len(step_results) == len(expected_steps) and all(
        item.get("status") == "pass" for item in step_results
    )
    if step_results:
        # Browser helper evidence is authoritative once every configured step
        # passed.  A later LLM timeout or an instructional example containing
        # ``RESULT=FAIL`` must not overwrite a fully evidenced successful run.
        if all_steps_passed:
            status = "pass"
            fail_reason = ""
            extra_note = ""
        elif any(item.get("status") == "fail" for item in step_results):
            status = "fail"
            if not fail_reason or fail_reason in (
                "执行中途结束，未输出完整测试报告",
                "仍有步骤未执行完成",
            ):
                first_failed = next(
                    (item for item in step_results if item.get("status") == "fail"),
                    None,
                )
                fail_reason = (
                    (first_failed or {}).get("actual_result")
                    or "步骤执行失败"
                )
        elif any(item.get("status") == "skip" for item in step_results):
            status = "fail"
            if not fail_reason or fail_reason == "执行中途结束，未输出完整测试报告":
                fail_reason = "仍有步骤未执行完成"
    if not step_results:
        step_results = build_step_results(
            testcase,
            status=status if status in ("pass", "fail") else "fail",
            failed_step=fail_ctx["failed_step"],
            fail_reason=fail_reason,
        )

    # A model-authored report is not authoritative when browser-tool evidence
    # exists. Rebuild it from the same per-step evidence used for record.status;
    # otherwise the chat summary can say "pass" while the testcase run record
    # correctly says "fail" (for example, a screenshot without an assertion).
    if already_has_report and not all_steps_passed and not has_authoritative_step_evidence:
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


def _append_missing_message_note(summary: str, missing_texts: list[str]) -> str:
    if not missing_texts:
        return summary
    joined = "、".join(f"「{t}」" for t in missing_texts)
    note = (
        f"\n\n### 消息内容校验\n"
        f"- 执行日志未出现用例要求发送的文本 {joined}。"
        f"通常表示未真正发送消息，或误用了沟通区只读 helper；"
        f"对话中的「通过」不可信。\n"
    )
    return (summary + note).strip()


def list_missing_step_screenshots(testcase: TestCase) -> list[int]:
    """Steps that have no uploaded screenshot on the testcase detail page."""
    expected = [
        int(number)
        for number in testcase.steps.values_list("step_number", flat=True)
        if number
    ]
    if not expected:
        return []
    from testcases.models import TestCaseScreenshot

    present = {
        int(number)
        for number in TestCaseScreenshot.objects.filter(
            test_case_id=testcase.id,
            step_number__isnull=False,
        ).values_list("step_number", flat=True)
        if number
    }
    return [number for number in sorted(set(expected)) if number not in present]


def runtime_screenshot_dir(testcase: TestCase) -> str:
    from django.conf import settings

    return os.path.join(
        settings.MEDIA_ROOT,
        "skill_runtime",
        "screenshots",
        str(testcase.project_id),
        str(testcase.id),
    )


def runtime_step_screenshot_path(testcase: TestCase, step_number: int) -> str:
    return os.path.join(
        runtime_screenshot_dir(testcase),
        f"case_{testcase.id}_step{step_number}.png",
    )


def runtime_last_screenshot_path(testcase: TestCase) -> str:
    return os.path.join(runtime_screenshot_dir(testcase), "last.png")


def attach_testcase_step_screenshot(
    *,
    testcase_id: int,
    step_number: int,
    filepath: str,
    title: str = "",
    uploader_id: Optional[int] = None,
) -> bool:
    """Persist a local step screenshot onto the testcase detail page."""
    if int(step_number or 0) <= 0 or not filepath or not os.path.isfile(filepath):
        return False
    try:
        if os.path.getsize(filepath) <= 0:
            return False
    except OSError:
        return False

    from django.core.files import File

    from testcases.models import TestCaseScreenshot
    import time

    try:
        existing = TestCaseScreenshot.objects.filter(
            test_case_id=testcase_id,
            step_number=int(step_number),
        )
        for old in existing:
            if old.screenshot and os.path.isfile(old.screenshot.path):
                try:
                    os.remove(old.screenshot.path)
                except OSError:
                    pass
            old.delete()

        shot = TestCaseScreenshot(
            test_case_id=testcase_id,
            step_number=int(step_number),
            title=title or f"步骤{step_number}",
            uploader_id=uploader_id,
        )
        # 文件名带时间戳，避免浏览器/CDN 缓存旧图导致用例详情里「截图和步骤对不上」。
        base = os.path.basename(filepath) or f"case_{testcase_id}_step{step_number}.png"
        stem, ext = os.path.splitext(base)
        if not ext:
            ext = ".png"
        unique_name = f"{stem}_{int(time.time())}{ext}"
        with open(filepath, "rb") as handle:
            shot.screenshot.save(unique_name, File(handle), save=True)
        return True
    except Exception as exc:
        logger.warning(
            "Failed to attach step %s screenshot for testcase %s from %s: %s",
            step_number,
            testcase_id,
            filepath,
            exc,
        )
        return False


def backfill_missing_step_screenshots(
    testcase: TestCase,
    *,
    transcript: str = "",
    uploader_id: Optional[int] = None,
) -> list[int]:
    """Attach any missing step screenshots that already exist on disk."""
    missing = list_missing_step_screenshots(testcase)
    if not missing:
        return []

    declared: dict[int, str] = {}
    for match in _CASE_SCREENSHOT_PATH_RE.findall(transcript or ""):
        step_match = re.search(r"step(\d+)", os.path.basename(match), flags=re.IGNORECASE)
        if not step_match:
            continue
        declared[int(step_match.group(1))] = match

    still_missing: list[int] = []
    last_missing = missing[-1]
    last_png = runtime_last_screenshot_path(testcase)
    for step_number in missing:
        candidates = []
        declared_path = declared.get(step_number)
        if declared_path:
            candidates.append(declared_path)
        named_path = runtime_step_screenshot_path(testcase, step_number)
        if (
            step_number == last_missing
            and os.path.isfile(last_png)
            and not os.path.isfile(named_path)
        ):
            try:
                os.makedirs(os.path.dirname(named_path), exist_ok=True)
                shutil.copyfile(last_png, named_path)
            except OSError:
                candidates.append(last_png)
        candidates.append(named_path)
        attached = False
        for candidate in candidates:
            if attach_testcase_step_screenshot(
                testcase_id=testcase.id,
                step_number=step_number,
                filepath=candidate,
                title=f"步骤{step_number}",
                uploader_id=uploader_id,
            ):
                attached = True
                break
        if not attached:
            still_missing.append(step_number)
    return still_missing


def _append_screenshot_gap_note(summary: str, missing_steps: list[int]) -> str:
    if not missing_steps:
        return summary
    labels = "、".join(f"步骤{n}" for n in missing_steps)
    note = (
        f"\n\n### 截图校验\n"
        f"- 用例详情缺少 {labels} 的自动上传截图。"
        f"通常表示该步未执行固定 helper / 未产生 `[CASE_SCREENSHOT]`，"
        f"对话中的「通过」不可信，请重新逐步执行。\n"
    )
    if "测试执行结果: 通过" in summary or "测试执行结果：通过" in summary:
        summary = re.sub(
            r"测试执行结果[:：]\s*通过",
            "测试执行结果: 不通过",
            summary,
            count=1,
        )
        summary = summary.replace(
            "本次测试执行全部",
            "缺少步骤截图，本次测试执行未完整取证，",
            1,
        )
    return (summary + note).strip()


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
    execution_mode: str = "hybrid",
    actual_execution_path: Optional[str] = None,
    ui_test_case_id: Optional[int] = None,
    data_generation_run_id: Optional[int] = None,
) -> Optional[TestCaseRunRecord]:
    # Keep the selected strategy separate from the route that actually ran.
    if not actual_execution_path:
        if execution_mode == "script_only":
            actual_execution_path = "bound_ui_script"
        elif execution_mode == "hybrid":
            actual_execution_path = (
                "hybrid_script_pending" if ui_test_case_id else "agent_no_bound_script"
            )
        else:
            actual_execution_path = "agent_browser"
    try:
        testcase = TestCase.objects.get(id=testcase_id)
    except TestCase.DoesNotExist:
        logger.warning("Skip run record: testcase %s not found", testcase_id)
        return None

    existing = (
        TestCaseRunRecord.objects.filter(session_id=session_id)
        .only("id", "status")
        .first()
    )
    if existing and existing.status == "running":
        # Do not wipe step screenshots that this running session already uploaded.
        return TestCaseRunRecord.objects.get(pk=existing.pk)

    _cleanup_testcase_screenshots(testcase_id)

    record, created = TestCaseRunRecord.objects.get_or_create(
        session_id=session_id,
        defaults={
            "testcase": testcase,
            "executor_id": user_id,
            "status": "running",
            "generate_playwright_script": generate_playwright_script,
            "execution_mode": execution_mode,
            "actual_execution_path": actual_execution_path,
            "ui_test_case_id_snapshot": ui_test_case_id,
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
            execution_mode=execution_mode,
            actual_execution_path=actual_execution_path,
            ui_test_case_id_snapshot=ui_test_case_id,
            data_generation_run_id=data_generation_run_id,
            started_at=timezone.now(),
        )
        record.refresh_from_db()
    return record


def block_testcase_run_record(*, session_id: str, reason: str) -> Optional[TestCaseRunRecord]:
    """Record a prerequisite failure without attributing it to a test step."""
    try:
        record = TestCaseRunRecord.objects.get(session_id=session_id)
    except TestCaseRunRecord.DoesNotExist:
        return None

    record.status = "error"
    record.summary = f"测试数据准备失败，已中止执行。\n失败原因：{reason}\n尚未执行任何测试步骤。"
    record.step_results = []
    record.execution_log = record.summary
    record.completed_at = timezone.now()
    record.save(update_fields=["status", "summary", "step_results", "execution_log", "completed_at"])
    return record


def finalize_testcase_run_record(
    *,
    session_id: str,
    final_content: str = "",
    stopped: bool = False,
    error_message: Optional[str] = None,
    transcript: str = "",
    assistant_transcript: str = "",
    tool_transcript: str = "",
) -> Optional[TestCaseRunRecord]:
    try:
        record = TestCaseRunRecord.objects.select_related("testcase", "data_generation_run").prefetch_related(
            "testcase__steps"
        ).get(session_id=session_id)
    except TestCaseRunRecord.DoesNotExist:
        return None

    if record.status != "running":
        return record

    # A fixed helper can finalize immediately after the last PASS marker, before
    # the streaming loop has loaded ``all_messages``.  Include its checkpoint so
    # the successful final step does not make earlier steps look unexecuted.
    checkpoint_transcript = collect_checkpoint_execution_transcript(
        user_id=int(record.executor_id),
        project_id=record.testcase.project_id,
        session_id=session_id,
    )
    checkpoint_tool_transcript = collect_checkpoint_execution_transcript(
        user_id=int(record.executor_id),
        project_id=record.testcase.project_id,
        session_id=session_id,
        tools_only=True,
    )
    persisted_tool_transcript = "\n".join(
        part.strip()
        for part in str(record.execution_log or "").split(
            "\n[TESTCASE_TOOL_OUTPUT]\n"
        )[1:]
        if part.strip()
    )
    tool_transcript = "\n".join(
        part
        for part in (
            checkpoint_tool_transcript,
            tool_transcript,
            persisted_tool_transcript,
        )
        if part
    )
    if checkpoint_transcript and checkpoint_transcript not in (transcript or ""):
        transcript = "\n".join(
            part for part in (checkpoint_transcript, transcript) if part
        )

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
        execution_mode=record.execution_mode,
        tool_transcript=tool_transcript,
    )
    # A generated report may claim PASS after the wrong fixed helper completed
    # a different step. For this flow, only the detail-page button assertion is
    # evidence that the final step actually ran.
    if outcome.get("status") == "pass":
        from data_generation.testcase_pre_data import processing_handover_flow_kind

        if processing_handover_flow_kind(record.testcase) == "pending_no_handover" and (
            "RESULT=PASS: 步骤3已确认待处理工单详情未显示移交审批按钮" not in combined
        ):
            reason = "步骤3缺少待处理工单详情页的移交审批按钮断言，执行结果与用例步骤不一致"
            step_results = outcome.get("step_results") or []
            for item in step_results:
                if item.get("step_number") == 3:
                    item["status"] = "fail"
                    item["actual_result"] = reason
            outcome["status"] = "fail"
            outcome["summary"] = build_execution_result_report(
                record.testcase,
                status="fail",
                step_results=step_results,
                fail_reason=reason,
            )
            outcome["step_results"] = step_results
    missing_shots = backfill_missing_step_screenshots(
        record.testcase,
        transcript=combined,
        uploader_id=int(record.executor_id) if record.executor_id else None,
    )
    if missing_shots and outcome.get("status") == "pass":
        outcome["status"] = "fail"
        outcome["summary"] = _append_screenshot_gap_note(
            outcome.get("summary") or "",
            missing_shots,
        )

    try:
        from data_generation.testcase_pre_data import (
            extract_expected_send_message_texts,
            extract_expected_send_message_texts_by_step,
            is_ticket_message_send_case,
            ticket_message_send_steps,
        )

        if is_ticket_message_send_case(record.testcase) and outcome.get("status") == "pass":
            expected_msgs = extract_expected_send_message_texts(record.testcase)
            expected_msgs_by_step = extract_expected_send_message_texts_by_step(record.testcase)
            missing_msgs: list[str] = []
            missing_by_step: dict[int, list[str]] = {}
            used_wrong_helper = bool(
                re.search(r"runTicketDetailCaseStep\s*\(", combined, flags=re.IGNORECASE)
            )
            for step_number, texts in expected_msgs_by_step.items():
                for text in texts:
                    asserted = bool(
                        re.search(
                            rf"assertPageShows\s*\([^)]*{re.escape(text)}",
                            combined,
                            flags=re.IGNORECASE,
                        )
                    ) or bool(
                        re.search(
                            rf"RESULT=PASS:[^\n]*{re.escape(text)}",
                            combined,
                            flags=re.IGNORECASE,
                        )
                    )
                    if not asserted:
                        missing_msgs.append(text)
                        missing_by_step.setdefault(step_number, []).append(text)
            # 步骤结果写成「进详情」而未写出发送内容，也视为虚报通过。
            for item in outcome.get("step_results") or []:
                if not isinstance(item, dict):
                    continue
                desc = str(item.get("description") or "")
                actual = str(item.get("actual_result") or "")
                if "发送" not in desc or item.get("status") != "pass":
                    continue
                if any(token in actual for token in ("进入工单详情", "沟通区检查", "只读")):
                    for text in expected_msgs:
                        if text not in actual and text not in missing_msgs:
                            missing_msgs.append(text)
            if used_wrong_helper or missing_msgs:
                outcome["status"] = "fail"
                note_msgs = missing_msgs or expected_msgs[:1] or ["步骤要求的发送文本"]
                if used_wrong_helper:
                    note_msgs = list(note_msgs) + ["(误用 runTicketDetailCaseStep)"]
                message_fail_reason = (
                    "真正的消息发送步骤缺少沟通记录内容断言："
                    + "、".join(note_msgs)
                )
                send_step_numbers = {
                    int(step.step_number)
                    for step in ticket_message_send_steps(record.testcase)
                    if getattr(step, "step_number", None) is not None
                }
                failed_step_numbers = (
                    send_step_numbers
                    if used_wrong_helper
                    else set(missing_by_step) or send_step_numbers
                )
                step_results = [dict(item) for item in outcome.get("step_results") or []]
                marked_failed = False
                for item in step_results:
                    try:
                        step_number = int(item.get("step_number"))
                    except (TypeError, ValueError):
                        continue
                    if step_number in failed_step_numbers and item.get("status") == "pass":
                        item["status"] = "fail"
                        item["actual_result"] = message_fail_reason
                        marked_failed = True
                if not marked_failed:
                    for item in step_results:
                        if item.get("status") == "pass" and "发送" in str(item.get("description") or ""):
                            item["status"] = "fail"
                            item["actual_result"] = message_fail_reason
                            marked_failed = True
                            break
                if not marked_failed:
                    for item in reversed(step_results):
                        if item.get("status") == "pass":
                            item["status"] = "fail"
                            item["actual_result"] = message_fail_reason
                            marked_failed = True
                            break
                if marked_failed:
                    outcome["step_results"] = step_results
                    outcome["summary"] = build_execution_result_report(
                        record.testcase,
                        status="fail",
                        step_results=step_results,
                        fail_reason=message_fail_reason,
                        extra_note="请确认消息确实发送，并在沟通记录中断言发送内容。",
                    )
                    outcome["injected"] = True
                outcome["summary"] = _append_missing_message_note(
                    outcome.get("summary") or "",
                    note_msgs,
                )
    except Exception as exc:
        logger.debug("message send content check skipped: %s", exc)

    prior_execution_log = str(record.execution_log or "")
    hybrid_script_trace = (
        prior_execution_log
        if record.actual_execution_path == "agent_fallback"
        and prior_execution_log.startswith("[HYBRID_SCRIPT_ATTEMPT]")
        else ""
    )
    record.status = outcome["status"]
    record.summary = extract_first_execution_report(outcome["summary"])[:8000]
    record.step_results = outcome["step_results"]
    final_execution_log = "\n\n[HYBRID_AGENT_FALLBACK]\n".join(
        part for part in (hybrid_script_trace, combined or error_message or "") if part
    )
    record.execution_log = _truncate_execution_log(final_execution_log)
    record.injected_report = outcome["injected"]
    record.data_usage = build_pre_data_usage(
        record.data_generation_run,
        assistant_text,
        execution_transcript=final_execution_log,
    )

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
