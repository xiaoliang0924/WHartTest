"""
Agent Loop API 视图 (LangChain v1 重构版)

使用 LangChain v1 的 create_agent + middleware 模式，
替代原有的手动 AgentOrchestrator 循环。

核心变更：
- 使用 create_agent() 统一创建 Agent
- 使用 SummarizationMiddleware 自动处理上下文压缩
- 使用 HumanInTheLoopMiddleware 处理 HITL 审批
- 在流处理层检测工具调用，生成 step_start/step_complete 事件
- 支持 stream 参数控制流式/非流式输出
- SSE 事件格式与旧版保持兼容，前端无需修改
"""

import asyncio
import base64
import json
import logging
import os
import re
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlsplit

import httpx
from django.http import StreamingHttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from channels.db import database_sync_to_async as sync_to_async

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain.agents import create_agent
from wharttest_django.checkpointer import get_async_checkpointer

from .middleware_config import (
    extract_llm_failure_message_from_text,
    get_middleware_from_config,
    get_user_tool_approvals,
    get_user_friendly_llm_error,
)
from .playwright_instructions import (
    CLAIMABLE_PENDING_EXECUTION_HINT,
    COMPLETE_TICKET_UI_EXECUTION_HINT,
    MANUAL_TESTCASE_EXECUTION_HINT,
    OVERVIEW_SLA_DETAIL_EXECUTION_HINT,
    PENDING_NO_HANDOVER_EXECUTION_HINT,
    PLAYWRIGHT_SCRIPT_INSTRUCTION,
    PROCESSING_HANDOVER_EXECUTION_HINT,
    TRANSFER_UI_EXECUTION_HINT,
)
from .stop_signal import should_stop, clear_stop_signal
from langgraph_integration.models import ChatSession, LLMConfig
from langgraph_integration.views import (
    _extract_requirement_doc_images_for_message,
    create_llm_instance,
    create_sse_data,
    get_effective_system_prompt_async,
    check_project_permission,
    schedule_auto_summarize_session_title,
)
from projects.models import Project
from prompts.models import PromptType, UserPrompt
from mcp_tools.models import RemoteMCPConfig
from mcp_tools.persistent_client import mcp_session_manager
from file_management.services import validate_file_ids, build_llm_attachment_context, sync_file_references
from file_management.models import FileReference
from requirements.context_limits import (
    MODEL_CONTEXT_LIMITS,
    context_checker,
    get_context_limit_from_llm,
)

logger = logging.getLogger(__name__)


def _is_final_testcase_step_pass(
    *, tool_name: str, content: str, helper_step: Optional[int], total_steps: Optional[int]
) -> bool:
    """Return whether a fixed helper conclusively completed the final case step."""
    if tool_name != "execute_skill_script" or not helper_step or not total_steps:
        return False
    if helper_step != total_steps:
        return False
    markers = re.findall(
        r"RESULT\s*=\s*(PASS|FAIL)\b", str(content), flags=re.IGNORECASE
    )
    return bool(markers) and markers[-1].upper() == "PASS"


def _build_sse_error_event(exc: Exception) -> Dict[str, Any]:
    friendly_error = get_user_friendly_llm_error(exc)
    if friendly_error:
        event = {
            "type": "error",
            "message": friendly_error["message"],
            "code": friendly_error["status_code"],
            "error_code": friendly_error["error_code"],
            "errors": friendly_error["errors"],
        }
        if friendly_error.get("model"):
            event["model"] = friendly_error["model"]
        if friendly_error.get("reset_time"):
            event["retry_after"] = friendly_error["reset_time"]
        if friendly_error.get("reset_seconds") is not None:
            event["retry_after_seconds"] = friendly_error["reset_seconds"]
        return event

    return {"type": "error", "message": f"执行错误: {str(exc)}", "code": 500}


def _stream_content_to_text(content: Any) -> str:
    """Normalize string and Anthropic-style content blocks for SSE text output."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    text_parts = []
    for block in content:
        if isinstance(block, str):
            text_parts.append(block)
        elif isinstance(block, dict) and block.get("type") in {"text", "output_text"}:
            text = block.get("text")
            if isinstance(text, str):
                text_parts.append(text)
    return "".join(text_parts)


def create_flushed_sse(data_dict: Dict[str, Any]) -> str:
    """SSE event plus padding so nginx/proxies flush before the next blocking helper."""
    return create_sse_data(data_dict) + (":" + (" " * 4096) + "\n\n")


def _decode_sse_payload(event: str) -> Optional[Dict[str, Any]]:
    """Decode one JSON data event produced by ``create_sse_data``."""
    if not isinstance(event, str):
        return None
    for line in event.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            payload = json.loads(line[5:].strip())
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None
    return None


def _testcase_bound_ui_timeout_seconds() -> float:
    """Total wall-clock limit for a bound UI case (individual browser calls also have limits)."""
    from testcases.run_record_service import testcase_bound_ui_timeout_seconds

    return testcase_bound_ui_timeout_seconds()


def _mcp_tool_load_timeout_seconds() -> float:
    """Maximum time to wait for remote MCP tool discovery before continuing without it."""
    raw_value = os.getenv("MCP_TOOL_LOAD_TIMEOUT_SECONDS", "30")
    try:
        configured_value = float(raw_value)
    except (TypeError, ValueError):
        configured_value = 30.0
    return max(5.0, min(configured_value, 300.0))


async def _persist_helper_step_to_checkpoint(
    *,
    agent,
    invoke_config,
    commentary: str,
    tool_output: str,
    step: int,
    command: str,
) -> None:
    """Write auto-run step cards into LangGraph history so refresh keeps them."""
    if agent is None or invoke_config is None:
        return
    call_id = f"auto_step_{int(step)}_{uuid.uuid4().hex[:10]}"
    try:
        await agent.aupdate_state(
            invoke_config,
            {
                "messages": [
                    AIMessage(
                        content=(commentary or "").strip() or f"步骤 {step}",
                        tool_calls=[
                            {
                                "id": call_id,
                                "name": "execute_skill_script",
                                "args": {"command": command},
                            }
                        ],
                    ),
                    ToolMessage(
                        content=tool_output or "",
                        name="execute_skill_script",
                        tool_call_id=call_id,
                    ),
                ]
            },
        )
    except Exception as persist_err:
        logger.warning(
            "AgentLoopStreamAPI: persist helper step %s failed: %s",
            step,
            persist_err,
        )


async def _complete_testcase_run_with_report(
    *,
    session_id: str,
    final_content: str = "",
    messages=None,
    stopped: bool = False,
    error_message: Optional[str] = None,
    agent=None,
    invoke_config=None,
):
    """Finalize the case-management run and inject a 1436-style report if the model omitted one."""
    from testcases.run_record_service import (
        collect_assistant_transcript,
        collect_message_transcript,
        collect_tool_execution_transcript,
        finalize_testcase_run_record,
        is_filled_execution_result_report,
    )

    transcript = collect_message_transcript(messages, exclude_human=True)
    assistant_transcript = collect_assistant_transcript(messages)
    tool_transcript = collect_tool_execution_transcript(messages)
    record = await sync_to_async(finalize_testcase_run_record)(
        session_id=session_id,
        final_content=final_content or "",
        stopped=stopped,
        error_message=error_message,
        transcript=transcript,
        assistant_transcript=assistant_transcript,
        tool_transcript=tool_transcript,
    )
    injected = ""
    if (
        record
        and getattr(record, "injected_report", False)
        and record.summary
        and not is_filled_execution_result_report(final_content or "")
    ):
        injected = record.summary
        if agent is not None and invoke_config is not None:
            try:
                await agent.aupdate_state(
                    invoke_config,
                    {"messages": [AIMessage(content=injected)]},
                )
            except Exception as persist_err:
                logger.warning(
                    "AgentLoopStreamAPI: Failed to persist fallback execution report: %s",
                    persist_err,
                )
    return record, injected


def _map_ui_script_results_to_testcase_steps(
    case_steps,
    raw_results,
    *,
    ui_status: int,
    ui_error: str = "",
    ui_case_groups=None,
    ui_step_mapping=None,
    ui_test_case_id=None,
):
    """Map recorder actions to functional steps using binding-specific metadata.

    New bindings carry explicit UI-case-step IDs -> functional step numbers.
    Legacy bindings may use positional mapping only when the saved group count
    exactly matches the functional step count. A mismatch is reported as an
    alignment issue and never assigned as a failure to unrelated steps.
    """
    groups: dict[int, list[dict]] = {}
    for index, item in enumerate(raw_results or [], start=1):
        if not isinstance(item, dict):
            continue
        group_value = item.get("group")
        if group_value is None:
            step_id = str(item.get("step_id") or "")
            match = re.match(r"^group(\d+)_", step_id)
            if match:
                group_value = match.group(1)
        try:
            group_no = int(group_value or index)
        except (TypeError, ValueError):
            group_no = index
        # Recorder emits group0_0 for the shared login/authentication pre-step.
        # It is not one of the generated UI case's page-step groups and must not
        # be counted against the functional testcase's business steps.
        if group_no <= 0:
            continue
        groups.setdefault(group_no, []).append(item)

    ordered_steps = list(case_steps)
    expected_numbers = {int(step.step_number) for step in ordered_steps}
    ui_case_groups = list(ui_case_groups or [])
    # Saved case-step metadata is authoritative for group IDs and case_sort;
    # runtime groupN identifiers use the same case_sort numbering.
    group_number_by_id = {}
    group_ids = set()
    for index, item in enumerate(ui_case_groups, start=1):
        if not isinstance(item, dict):
            continue
        try:
            case_step_id = int(item.get("id"))
            group_no = int(item.get("case_sort") or index)
        except (TypeError, ValueError):
            continue
        group_number_by_id[case_step_id] = group_no
        group_ids.add(case_step_id)

    step_groups: dict[int, list[int]] = {number: [] for number in expected_numbers}
    alignment_error = ""
    if ui_step_mapping:
        try:
            mapping_case_id = int(ui_step_mapping.get("ui_test_case_id"))
        except (AttributeError, TypeError, ValueError):
            mapping_case_id = None
        if mapping_case_id is None or (
            ui_test_case_id is not None and mapping_case_id != int(ui_test_case_id)
        ):
            alignment_error = "UI 步骤映射与当前绑定的 UI 用例不一致。"
        elif not group_number_by_id:
            alignment_error = "缺少 UI 步骤组信息，无法校验已保存的步骤映射。"
        else:
            mapped_group_ids = set()
            try:
                entries = ui_step_mapping.get("groups")
                if not isinstance(entries, list):
                    raise ValueError
                for entry in entries:
                    group_id = int(entry.get("ui_case_step_id"))
                    step_numbers = entry.get("functional_step_numbers")
                    if group_id not in group_ids or group_id in mapped_group_ids:
                        raise ValueError
                    if not isinstance(step_numbers, list) or not step_numbers:
                        raise ValueError
                    mapped_group_ids.add(group_id)
                    group_no = group_number_by_id[group_id]
                    for raw_number in step_numbers:
                        if isinstance(raw_number, bool):
                            raise ValueError
                        step_number = int(raw_number)
                        if step_number not in expected_numbers:
                            raise ValueError
                        step_groups[step_number].append(group_no)
                if mapped_group_ids != group_ids or any(not values for values in step_groups.values()):
                    raise ValueError
            except (AttributeError, TypeError, ValueError):
                alignment_error = "UI 步骤映射不完整或引用了已变更的功能/UI步骤，请重新对齐。"
    elif len(ui_case_groups) == len(ordered_steps) and len(ui_case_groups) > 0:
        # Backward-compatible behavior for legacy bindings with a one-to-one
        # structure. New generated bindings always persist explicit mappings.
        for index, step in enumerate(ordered_steps, start=1):
            group = ui_case_groups[index - 1]
            try:
                group_no = int(group.get("case_sort") or index)
            except (AttributeError, TypeError, ValueError):
                group_no = index
            step_groups[int(step.step_number)].append(group_no)
    elif ui_status != 2 and groups and len(groups) <= len(ordered_steps):
        # For legacy failed runs without saved group metadata, retain the
        # executor's actual ordered failure evidence; the unexecuted tail is
        # reported as skipped. Successful mismatched runs still fail closed.
        for step, group_no in zip(ordered_steps, sorted(groups)):
            step_groups[int(step.step_number)].append(group_no)
    elif len(ordered_steps) == len(groups) and len(ordered_steps) > 0:
        # Compatibility for isolated callers that provide recorder evidence
        # but no persisted case-group metadata.
        for index, step in enumerate(ordered_steps, start=1):
            step_groups[int(step.step_number)].append(index)
    else:
        alignment_error = (
            f"旧绑定缺少明确步骤映射：UI 执行记录包含 {len(groups)} 组页面步骤，"
            f"功能用例有 {len(ordered_steps)} 步。请补充映射或重新生成绑定。"
        )

    if alignment_error:
        reason = alignment_error
        if ui_status != 2 and ui_error:
            reason = f"{ui_error}；同时，{alignment_error}"
        step_results = [{
            "step_number": step.step_number,
            "description": step.description or "",
            "expected_result": step.expected_result or "",
            "actual_result": reason,
            "status": "skip",
        } for step in ordered_steps]
        return step_results, "fail", reason

    last_executed_group = max(groups, default=0)
    step_results = []
    for step in ordered_steps:
        mapped_group_numbers = step_groups[int(step.step_number)]
        mapped_items_by_group = [groups.get(group_no, []) for group_no in mapped_group_numbers]
        group_items = [item for items in mapped_items_by_group for item in items]
        missing_group_numbers = [
            group_no
            for group_no, items in zip(mapped_group_numbers, mapped_items_by_group)
            if not items
        ]
        failed = any(
            str(item.get("status") or "").lower() not in {"success", "pass", "passed"}
            for item in group_items
        )
        if ui_status != 2 and last_executed_group in mapped_group_numbers and group_items and not failed:
            # The executor can fail after recording successful actions (for
            # example during screenshot/finalization); fail only the mapped
            # functional step instead of guessing by its ordinal position.
            failed = True
        if not mapped_group_numbers:
            status, actual = "skip", "该功能步骤没有对应的 UI 步骤组，未执行"
        elif failed:
            status, actual = "fail", ui_error or "对应 UI 自动化步骤未全部通过"
        elif ui_status != 2 and last_executed_group in mapped_group_numbers and group_items:
            status, actual = "fail", ui_error or "UI 自动化步骤组未完整执行"
        elif missing_group_numbers:
            if ui_status == 2:
                status, actual = "fail", "映射到的 UI 步骤组没有完整执行记录"
            else:
                status, actual = "skip", "对应 UI 步骤组尚未执行"
        else:
            status, actual = "pass", f"映射的 {len(mapped_group_numbers)} 组 UI 步骤通过"
        step_results.append({
            "step_number": step.step_number,
            "description": step.description or "",
            "expected_result": step.expected_result or "",
            "actual_result": actual,
            "status": status,
        })

    passed = (
        ui_status == 2
        and bool(ordered_steps)
        and len(step_results) == len(ordered_steps)
        and all(item["status"] == "pass" for item in step_results)
    )
    failure_reason = ui_error or "UI 自动化脚本存在失败或未执行步骤"
    return step_results, ("pass" if passed else "fail"), ("" if passed else failure_reason)


async def _run_testcase_with_bound_ui_script(
    *, request, project, session_id: str, thread_id: str, chat_session,
    testcase_id: int, ui_test_case_id: int, display_message: str,
    data_generation_run=None,
    execution_mode: str = "script_only",
    actual_execution_path: str = "bound_ui_script",
    stream_mode: str = "ui_script_only",
    suppress_failure_report: bool = False,
    emit_start: bool = True,
):
    """Run the bound UI case directly through the recorder, without invoking an LLM."""
    from django.utils import timezone
    from langgraph_integration.models import ChatMessage
    from testcases.models import TestCase
    from testcases.run_record_service import build_execution_result_report, build_pre_data_usage
    from testcases.models import TestCaseRunRecord
    from ui_automation.consumers import SocketUserManager
    from ui_automation.models import UiCaseStepsDetailed, UiExecutionRecord, UiTestCase

    user = request.user
    ui_case = await sync_to_async(
        lambda: UiTestCase.objects.filter(id=ui_test_case_id, project=project).first()
    )()
    if ui_case is None:
        raise ValueError("绑定的 UI 自动化用例不存在或不属于当前项目")

    consumer = SocketUserManager.get_web_user(user.username)
    if consumer is None:
        raise RuntimeError("当前用户的 UI 自动化连接不可用，请刷新页面后重试")
    active_task = getattr(consumer, "_recorder_exec_task", None)
    if active_task is not None and not active_task.done():
        raise RuntimeError("当前用户已有 UI 自动化任务正在执行，请稍后重试")

    started_at = timezone.now()
    current_task = asyncio.current_task()
    consumer._recorder_exec_task = current_task
    if emit_start:
        yield create_sse_data({
            "type": "start",
            "session_id": session_id,
            "thread_id": thread_id,
            "project_id": str(project.id),
            "display_message": display_message,
            "mode": stream_mode,
            "created_at": chat_session.created_at.isoformat() if chat_session and chat_session.created_at else None,
        })
    yield create_sse_data({"type": "message", "data": f"前置数据已准备，开始执行绑定的 UI 自动化用例：{ui_case.name}"})

    try:
        await consumer._execute_via_recorder(
            "case",
            {
                "case_id": int(ui_test_case_id),
                "execution_request_id": session_id,
                "actuator_id": "recorder-browser",
                "executor_id": int(user.id),
                "executor_name": user.username,
                # 失败时在同一个新浏览器上下文中采集真实页面现场，供
                # AI 诊断定位器/等待问题；该采集不执行额外业务动作。
                "explore_failure": True,
            },
            user.username,
        )

        def load_result():
            return (
                UiExecutionRecord.objects.filter(
                    test_case_id=ui_test_case_id,
                    executor_id=user.id,
                    created_at__gte=started_at,
                ).order_by("-created_at").first()
            )

        ui_record = await sync_to_async(load_result)()
        if ui_record is None:
            raise RuntimeError("UI 脚本执行结束，但未生成 UI 执行记录")

        def build_case_report():
            case = TestCase.objects.prefetch_related("steps").get(id=testcase_id)
            case_steps = list(case.steps.all().order_by("step_number"))
            ui_case_groups = []
            for case_group in (
                UiCaseStepsDetailed.objects.filter(test_case_id=ui_test_case_id)
                .select_related("page_step")
                .prefetch_related("page_step__step_details__element")
                .order_by("case_sort")
            ):
                page_step = case_group.page_step
                details = list(page_step.step_details.all())
                ui_case_groups.append({
                    "id": case_group.id,
                    "case_sort": case_group.case_sort,
                    "name": page_step.name,
                    "description": page_step.description or "",
                    "operations": [detail.ope_key or "" for detail in details],
                    "elements": [detail.element.name for detail in details if detail.element],
                })
            raw_results = ui_record.step_results if isinstance(ui_record.step_results, list) else []
            step_results, final_status, failure_reason = _map_ui_script_results_to_testcase_steps(
                case_steps,
                raw_results,
                ui_status=ui_record.status,
                ui_error=ui_record.error_message or "",
                ui_case_groups=ui_case_groups,
                ui_step_mapping=case.ui_step_mapping,
                ui_test_case_id=ui_test_case_id,
            )
            summary = build_execution_result_report(
                case,
                status=final_status,
                step_results=step_results,
                fail_reason=failure_reason,
                extra_note=(
                    "结果由绑定的 UI 自动化脚本逐组执行产生。"
                    if final_status == "pass"
                    else "请检查 UI 脚本步骤与功能用例步骤是否对应，并查看 UI 执行记录。"
                ),
            )
            return case, step_results, summary, final_status, failure_reason

        case, step_results, summary, final_status, failure_reason = await sync_to_async(build_case_report)()

        def persist_result():
            run_record = TestCaseRunRecord.objects.filter(session_id=session_id).first()
            if run_record is None:
                from testcases.run_record_service import start_testcase_run_record
                run_record = start_testcase_run_record(
                    testcase_id=testcase_id,
                    user_id=user.id,
                    session_id=session_id,
                    execution_mode=execution_mode,
                    actual_execution_path=actual_execution_path,
                    ui_test_case_id=ui_test_case_id,
                    data_generation_run_id=getattr(data_generation_run, "id", None),
                )
            if run_record is None:
                return None
            run_record.status = final_status
            run_record.summary = summary[:8000]
            run_record.step_results = step_results
            run_record.actual_execution_path = actual_execution_path
            run_record.execution_log = (
                f"执行策略：{execution_mode}\n"
                f"实际执行路径：{actual_execution_path}\n"
                f"UI 用例 ID：{ui_test_case_id}\n"
                f"UI 执行记录 ID：{ui_record.id}\n"
                f"{ui_record.log or ''}\n"
                f"失败原因：{failure_reason if final_status != 'pass' else '无'}"
            )[:8000]
            run_record.data_usage = build_pre_data_usage(
                run_record.data_generation_run,
                "",
                execution_transcript=run_record.execution_log,
            )
            run_record.completed_at = timezone.now()
            run_record.save(update_fields=[
                "status", "summary", "step_results", "execution_log", "data_usage",
                "actual_execution_path", "completed_at",
            ])
            return run_record

        run_record = await sync_to_async(persist_result)()
        report_message_id = f"ui-script-result-{session_id}"

        def persist_chat_messages():
            ChatMessage.objects.get_or_create(
                session=chat_session,
                user=user,
                message_id=f"ui-script-human-{session_id}",
                defaults={
                    "role": "human",
                    "content": display_message,
                    "metadata": {"source": "testcase_ui_script"},
                },
            )
            ChatMessage.objects.update_or_create(
                session=chat_session,
                user=user,
                message_id=report_message_id,
                defaults={
                    "role": "ai",
                    "content": summary,
                    "metadata": {
                        "source": "testcase_ui_script",
                        "run_record_id": getattr(run_record, "id", None),
                        "ui_execution_record_id": ui_record.id,
                    },
                },
            )

        if not (suppress_failure_report and final_status != "pass"):
            await sync_to_async(persist_chat_messages)()
        yield create_sse_data({"type": "stream", "data": summary})
        yield create_sse_data({
            "type": "complete",
            "total_steps": len(step_results),
            "test_case_run": {
                "status": final_status,
                "summary": summary,
                "run_record_id": getattr(run_record, "id", None),
                "ui_execution_record_id": ui_record.id,
            },
        })
        yield "data: [DONE]\n\n"
    finally:
        if getattr(consumer, "_recorder_exec_task", None) is current_task:
            consumer._recorder_exec_task = None


def _build_ui_generation_observations(messages, *, max_chars: int = 80_000) -> str:
    """Keep only this run's browser execution evidence for the UI-case save pass."""
    chunks = []
    for message in messages or []:
        message_type = type(message).__name__
        if message_type == "HumanMessage":
            content = getattr(message, "content", "")
            if isinstance(content, str) and content.strip():
                chunks.append(f"[测试用例与执行要求]\n{content}")
        elif message_type == "AIMessage":
            for call in getattr(message, "tool_calls", []) or []:
                if call.get("name") != "execute_skill_script":
                    continue
                args = call.get("args") or {}
                command = args.get("command") or args.get("script") or ""
                if command:
                    chunks.append(f"[实际执行的浏览器操作脚本]\n{command}")
        elif message_type == "ToolMessage" and getattr(message, "name", "") == "execute_skill_script":
            content = getattr(message, "content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, default=str)
            if content.strip():
                chunks.append(f"[浏览器操作结果与页面观察]\n{content[-12_000:]}")
    transcript = "\n\n".join(chunks)
    return transcript[-max_chars:]


def _has_browser_execution_evidence(messages) -> bool:
    for message in messages or []:
        if type(message).__name__ == "AIMessage" and any(
            call.get("name") == "execute_skill_script"
            for call in (getattr(message, "tool_calls", []) or [])
        ):
            return True
        if (
            type(message).__name__ == "ToolMessage"
            and getattr(message, "name", "") == "execute_skill_script"
            and str(getattr(message, "content", "")).strip()
        ):
            return True
    return False


async def _force_save_generated_ui_case(*, llm, tools, messages, provider="openai_compatible") -> Dict[str, Any]:
    """Run a constrained second pass so generation is not left to the test agent's discretion."""
    save_tool = next(
        (tool for tool in (tools or []) if getattr(tool, "name", "") == "save_generated_ui_testcase"),
        None,
    )
    if save_tool is None:
        return {"status": "failed", "message": "UI 用例保存工具未注册。"}

    if not _has_browser_execution_evidence(messages):
        return {"status": "failed", "message": "本次执行没有 Playwright 浏览器操作证据，不能安全生成 UI 用例。"}
    observations = _build_ui_generation_observations(messages)
    if not observations:
        return {"status": "failed", "message": "本次执行没有可用的浏览器操作记录，无法安全生成 UI 用例。"}

    system = SystemMessage(content=(
        "你负责把已成功的浏览器测试执行记录转成可复用 UI 自动化用例。"
        "只能根据提供的实际浏览器脚本和工具输出构造步骤，不得猜测定位器、页面或动作；"
        "忽略登录密码、令牌等秘密，不要复述秘密。"
        "现在必须调用唯一可用工具 save_generated_ui_testcase 一次，"
        "传入 payload_json JSON 字符串，包含 name、description、url、steps。"
        "每个功能测试步骤对应一个 UI step，动作按实际观察顺序记录；"
        "每个 step 可指定 authentication_mode：auto（默认协调环境认证）、environment（环境已负责认证，该组只能保存真实登录成功断言）、script（必须由脚本执行登录或角色切换）。"
        "不要把登录表单填写动作放到登录后的业务页面；多账号切换不得省略或用同一环境账号替代。"
        "不要把页面结果行中观察到的长数字编号、UUID 等动态业务数据保存为 wait_for_visible 或普通存在性断言的定位器；"
        "只有当功能步骤明确要求验证该精确编号时才可使用。应等待稳定页面结构，并由后续文本/行断言验证当前查询结果。"
        "导航步骤必须检查当前页面状态：如果该步骤开始时 URL 的 origin+pathname 已与目标页面一致，不要生成冗余菜单点击，改为保存基于本次实际 URL 的 assert mode=url、match=path；"
        "若必须点击导航，只能保存本次观察到的可见菜单项。定位到隐藏菜单节点时不得使用 force click 或单纯替换 XPath，应限定可见导航区域；没有可见目标时保留失败，不要伪造通过。"
        "对于 Element Plus 普通下拉，若实际脚本通过 selectFormDropdownOption 选择，保存动作时点击可见的 .el-select__wrapper，再点击可见下拉层中的精确选项；禁止把只读 input[role=combobox] 作为 click 目标。选择后必须保存对筛选框已显示目标值的断言。"
        "对于弹窗多选，必须把打开弹窗、点击真实选项行、确认作为独立且有序的实际动作；只有观察到弹窗已选数量大于 0 或选中标记后才保存确认动作。关闭后断言筛选字段已显示所选项：限定到页面实际观察到的字段容器及其标签/控件结构，不能假设标签是原生 label，也不能猜 .el-tag 或 following-sibling DOM 层级；自定义筛选面板应依据真实 .filter-field 与 .filter-field-label 结构定位。查询后不要再定位已关闭弹窗中的选项；若没有结果行，依据页面实际显示的空结果状态和仍保留的筛选值验收。对于本系统工单列表的工单类型弹窗筛选，触发器是真实 .filter-field 容器内的 .add-icon；若需保存为元素定位，使用 XPath //*[contains(@class,'filter-field')][.//*[contains(@class,'filter-field-label') and normalize-space()='工单类型']]//*[contains(@class,'add-icon')]，禁止推测 .el-icon-plus 或点击只读 input[role=combobox]；其他字段也必须按当前 DOM 观察确定对应的真实触发器。"
        "对于日期范围，必须保存选择实际目标起止日期的动作，并分别断言起始与结束输入值；只打开日历不算完成选择。若实际执行使用 selectFormDateRange，应保存真实观察到的输入值校验；若用日历选择，则定位日期面板中的完整年月与日期。日期格式与定位器必须来自本次页面观察，不得猜测。"
        "任何控件操作如果没有可验证的回填值/选中状态证据，不得保存为成功步骤；应保留失败或说明证据不足。"
        "没有足够证据的步骤不要编造，工具若因步骤不完整拒绝保存，应如实报告。"
        "禁止再次操作浏览器或调用其他工具。"
    ))
    human = HumanMessage(content="以下是本次已通过执行的原始证据，请据此保存 UI 自动化用例：\n\n" + observations)
    try:
        # Anthropic Messages API calls this forced-tool mode "any"; OpenAI
        # compatible APIs use "required". Both force the sole save tool.
        tool_choice = "any" if provider == "anthropic" else "required"
        bound_model = llm.bind_tools([save_tool], tool_choice=tool_choice)
        response = await asyncio.wait_for(bound_model.ainvoke([system, human]), timeout=180)
    except asyncio.TimeoutError:
        return {"status": "failed", "message": "生成模型在 180 秒内未返回，可使用本次执行记录重试生成。"}
    except Exception as exc:
        logger.exception("UI testcase generation follow-up model call failed")
        return {"status": "failed", "message": f"生成模型调用失败：{str(exc)[:500]}"}

    calls = getattr(response, "tool_calls", []) or []
    call = next((item for item in calls if item.get("name") == "save_generated_ui_testcase"), None)
    if call is None:
        return {"status": "failed", "message": "生成模型未调用 UI 用例保存工具。"}
    args = call.get("args") or {}
    if not isinstance(args, dict) or not args.get("payload_json"):
        return {"status": "failed", "message": "生成模型未提供有效的 UI 用例内容。"}
    try:
        raw_result = await save_tool.ainvoke(args)
        result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
        if not isinstance(result, dict):
            return {"status": "failed", "message": "UI 用例保存工具返回格式无效。"}
        return result
    except Exception as exc:
        logger.exception("UI testcase generation follow-up save failed")
        return {"status": "failed", "message": f"UI 用例保存失败：{str(exc)[:500]}"}


async def _validate_generated_ui_case(
    *, user, project_id: int, ui_test_case_id: int, validation_session_id: str, background=False
) -> Dict[str, Any]:
    """Replay a freshly generated UI case before reporting it as usable.

    The replay deliberately goes through the existing recorder execution path so
    validation has the same step results, screenshots, and Trace data as a
    normal UI-case execution.  This phase only validates; it does not mutate
    generated steps or attempt AI repair.
    """
    if user is None:
        # Kept for callers/tests that only exercise persistence. Production
        # generation always supplies the authenticated request user.
        return {
            "status": "skipped",
            "message": "已生成并绑定，但缺少执行用户，未进行回放验证。",
        }

    from django.utils import timezone
    from ui_automation.consumers import SocketUserManager
    from ui_automation.generated_case_service import bind_generated_ui_case_auth_state
    from ui_automation.models import UiExecutionRecord, UiTestCase

    if background:
        from ui_automation.background_runner import create_background_runner
        consumer = create_background_runner(user)
    else:
        consumer = SocketUserManager.get_web_user(user.username)
    if consumer is None:
        message = "UI 自动化连接不可用，无法自动回放验证。请刷新页面后重试。"
        await sync_to_async(
            lambda: UiTestCase.objects.filter(id=ui_test_case_id).update(
                status=3,
                error_message=message,
                result_data={
                    "generation_validation": {
                        "status": "unavailable",
                        "message": message,
                    }
                },
            )
        )()
        return {"status": "failed", "validation_status": "unavailable", "message": message}

    active_task = getattr(consumer, "_recorder_exec_task", None)
    if active_task is not None and not active_task.done():
        message = "当前用户已有 UI 自动化任务正在执行，无法进行生成用例回放验证。"
        await sync_to_async(
            lambda: UiTestCase.objects.filter(id=ui_test_case_id).update(
                status=3,
                error_message=message,
                result_data={
                    "generation_validation": {
                        "status": "unavailable",
                        "message": message,
                    }
                },
            )
        )()
        return {"status": "failed", "validation_status": "unavailable", "message": message}

    try:
        auth_binding = await sync_to_async(bind_generated_ui_case_auth_state)(
            ui_test_case_id=int(ui_test_case_id),
            project_id=int(project_id),
        )
    except Exception as exc:
        logger.exception(
            "Generated UI testcase auth state auto-binding failed: ui_case=%s",
            ui_test_case_id,
        )
        auth_binding = {"status": "failed", "bound_count": 0, "message": str(exc)[:300]}

    await sync_to_async(
        lambda: UiTestCase.objects.filter(id=ui_test_case_id).update(
            status=1,
            error_message=None,
            result_data={
                "generation_validation": {
                    "status": "running",
                    "message": "正在回放验证生成的 UI 自动化用例。",
                    "auth_binding": auth_binding,
                }
            },
        )
    )()

    started_at = timezone.now()
    # Reuse the consumer's stop/cancel mechanism while the validation replay
    # is running, just like a normal recorder execution started by the UI.
    validation_task = asyncio.current_task()
    consumer._recorder_exec_task = validation_task
    try:
        await consumer._execute_via_recorder(
            "case",
            {
                "case_id": int(ui_test_case_id),
                "execution_request_id": validation_session_id,
                "actuator_id": "recorder-browser",
                "executor_id": int(user.id),
                "executor_name": user.username,
                # 失败时在同一个新浏览器上下文中采集真实页面现场，供
                # AI 诊断定位器/等待问题；该采集不执行额外业务动作。
                "explore_failure": True,
            },
            user.username,
        )
    except Exception as exc:
        logger.exception(
            "Generated UI testcase replay raised an exception: project=%s ui_case=%s",
            project_id,
            ui_test_case_id,
        )
        message = f"生成 UI 用例回放异常：{str(exc)[:700]}"
        await sync_to_async(
            lambda: UiTestCase.objects.filter(id=ui_test_case_id).update(
                status=3,
                error_message=message,
                result_data={
                    "generation_validation": {
                        "status": "failed",
                        "message": message,
                    }
                },
            )
        )()
        return {"status": "failed", "validation_status": "failed", "message": message}
    finally:
        if getattr(consumer, "_recorder_exec_task", None) is validation_task:
            consumer._recorder_exec_task = None

    def load_record():
        return (
            UiExecutionRecord.objects.filter(
                test_case_id=ui_test_case_id,
                executor_id=user.id,
                created_at__gte=started_at,
            )
            .order_by("-created_at")
            .first()
        )

    execution_record = await sync_to_async(load_record)()
    if execution_record is None:
        message = "生成 UI 用例回放结束，但没有生成执行记录。"
        await sync_to_async(
            lambda: UiTestCase.objects.filter(id=ui_test_case_id).update(
                status=3,
                error_message=message,
                result_data={
                    "generation_validation": {
                        "status": "failed",
                        "message": message,
                    }
                },
            )
        )()
        return {"status": "failed", "validation_status": "failed", "message": message}

    passed = execution_record.status == 2
    message = (
        "生成 UI 用例回放验证通过。"
        if passed
        else (execution_record.error_message or execution_record.log or "生成 UI 用例回放失败。")
    )[:1000]

    def persist_validation_metadata():
        case = UiTestCase.objects.get(id=ui_test_case_id)
        result_data = dict(case.result_data or {})
        result_data["generation_validation"] = {
            "status": "passed" if passed else "failed",
            "execution_record_id": execution_record.id,
            "message": message,
            "auth_binding": auth_binding,
        }
        exploration = (result_data.get("exploration") or {}) if not passed else {}
        if exploration:
            result_data["generation_validation"]["exploration"] = {
                "status": exploration.get("status"),
                "page_url": exploration.get("page_url", ""),
                "title": exploration.get("title", ""),
                "captured_at": exploration.get("captured_at", ""),
            }
        case.status = 2 if passed else 3
        case.error_message = None if passed else message
        case.result_data = result_data
        case.save(update_fields=["status", "error_message", "result_data", "updated_at"])

    await sync_to_async(persist_validation_metadata)()
    return {
        "status": "success" if passed else "failed",
        "validation_status": "passed" if passed else "failed",
        "execution_record_id": execution_record.id,
        "message": message,
    }


MAX_GENERATED_UI_REPAIR_ATTEMPTS = 2


async def _apply_safe_generated_ui_repair(
    *, project_id: int, ui_test_case_id: int, diagnosis: Dict[str, Any], attempt: int
) -> Dict[str, Any]:
    """Apply only an evidence-backed locator change to the generated case.

    Generated cases can share page elements with manually maintained cases.  A
    repair must therefore clone the element and rewire only details belonging
    to this UI case; changing the shared element in-place could silently alter
    unrelated tests.
    """
    suggestion = diagnosis.get("healing_suggestion") or {}
    repair_type = str(suggestion.get("repair_type") or "locator").strip()
    if diagnosis.get("root_cause_type") != "SCRIPT_DEFECT":
        return {"status": "skipped", "reason": "AI 判断为业务或环境问题，未自动修改用例。"}
    if suggestion.get("can_self_heal") is not True:
        return {"status": "skipped", "reason": "AI 未确认存在安全的自动修复方案。"}

    # Only machine-resolved failure evidence may select a repair target.
    # Membership in the case alone does not prove that an element failed.
    evidence = diagnosis.get("evidence") or {}
    try:
        failed_detail_id = int(evidence.get("detail_id"))
    except (TypeError, ValueError):
        return {"status": "skipped", "reason": "无法准确定位真实失败步骤，未自动修改用例。"}
    from ui_automation.models import UiPageStepsDetailed

    failed_detail = await sync_to_async(lambda: UiPageStepsDetailed.objects.select_related("element").filter(
        id=failed_detail_id,
        page_step__case_usages__test_case_id=ui_test_case_id,
        page_step__project_id=project_id,
    ).first())()
    if (
        evidence.get("mapping_status") != "resolved"
        or failed_detail is None
        or failed_detail.ope_key != evidence.get("operation_type")
        or failed_detail.element_id != evidence.get("element_id")
    ):
        return {"status": "skipped", "reason": "失败步骤证据不完整或已变化，未自动修改用例。"}
    failure_info = {
        "operation_type": evidence.get("operation_type"),
        "element_id": evidence.get("element_id"),
        "current_locator_value": evidence.get("locator_value") or "",
        "error_message": evidence.get("error_message") or "",
    }

    # The work-order type filter is a custom popup trigger, not an Element Plus
    # el-icon-plus or readonly combobox. Prefer the selector used by the
    # verified Playwright helper when a generated locator for this trigger
    # times out; never accept an LLM-invented selector for this known control.
    step_info = diagnosis.get("failure_step_info") or {}
    element_name = str(step_info.get("element_name") or "")
    label_match = re.match(r"^\s*([\w\u4e00-\u9fff /-]+?)\s*筛选字段添加按钮", element_name)
    page_url = str(evidence.get("actual_url") or "")
    try:
        page_path = urlsplit(page_url).path.rstrip("/") or "/"
    except Exception:
        page_path = ""
    error_lower = failure_info["error_message"].lower()
    if (
        failure_info["operation_type"] == "click"
        and "timeout" in error_lower
        and page_path == "/work-order/tickets"
        and label_match
        and label_match.group(1) == "工单类型"
    ):
        suggestion = {
            **suggestion,
            "can_self_heal": True,
            "repair_type": "locator",
            "element_id": evidence.get("element_id"),
            "suggested_locator_type": "xpath",
            "suggested_locator_value": (
                "//*[contains(@class,'filter-field')]"
                "[.//*[contains(@class,'filter-field-label') and normalize-space()='工单类型']]"
                "//*[contains(@class,'add-icon')]"
            ),
            "explanation": "当前页面是自定义弹窗筛选字段；按已验证的页面助手，点击 .filter-field 中工单类型标签对应的 .add-icon 触发器。",
        }
        diagnosis["root_cause_type"] = "SCRIPT_DEFECT"
        diagnosis["healing_suggestion"] = suggestion
        repair_type = "locator"

    if repair_type == "navigation_assertion":
        from ui_automation.models import UiCaseStepsDetailed, UiPageStepsDetailed

        evidence = diagnosis.get("evidence") or {}
        actual_url = str(evidence.get("actual_url") or "").strip()
        target_url = str(evidence.get("page_url") or "").strip()
        error_text = str(failure_info.get("error_message") or "").lower()
        locator_evidence = " ".join((
            str(failure_info.get("current_locator_value") or ""),
            str(suggestion.get("current_locator_value") or ""),
            error_text,
        )).lower()
        if (
            failure_info.get("operation_type") != "click"
            or "not visible" not in error_text
            or "menuitem" not in locator_evidence
        ):
            return {"status": "skipped", "reason": "失败证据不能确认是隐藏菜单点击，未修改用例。"}
        try:
            actual = urlsplit(actual_url)
            target = urlsplit(target_url)
        except Exception:
            return {"status": "skipped", "reason": "失败现场或目标页面 URL 无效，未修改用例。"}
        if not (
            actual.scheme and actual.netloc and actual.path
            and target.scheme and target.netloc and target.path
            and actual.scheme.lower() == target.scheme.lower()
            and actual.netloc.lower() == target.netloc.lower()
            and (actual.path.rstrip("/") or "/") == (target.path.rstrip("/") or "/")
        ):
            return {"status": "skipped", "reason": "失败时路由与目标页面不一致，保留导航点击失败。"}
        try:
            element_id = int(failure_info.get("element_id") or suggestion.get("element_id"))
        except (TypeError, ValueError):
            return {"status": "skipped", "reason": "失败记录没有可定位的菜单元素，未修改用例。"}

        def replace_redundant_navigation():
            from django.db import transaction

            with transaction.atomic():
                matching_ids = list(UiPageStepsDetailed.objects.filter(
                    id=failed_detail_id,
                    element_id=element_id,
                    ope_key="click",
                    page_step__case_usages__test_case_id=ui_test_case_id,
                    page_step__project_id=project_id,
                ).order_by().values_list("id", flat=True).distinct())
                details = list(UiPageStepsDetailed.objects.select_for_update().filter(
                    id__in=matching_ids,
                ).order_by("id"))
                if len(details) != 1:
                    return {"status": "skipped", "reason": "无法唯一定位本用例中的失败菜单点击。"}
                detail = details[0]
                usages = UiCaseStepsDetailed.objects.filter(page_step_id=detail.page_step_id)
                if usages.exclude(test_case_id=ui_test_case_id).exists():
                    return {"status": "skipped", "reason": "页面步骤被其他用例复用，未修改共享步骤。"}
                detail.ope_key = "assert_url"
                detail.step_type = 1
                detail.element = None
                detail.ope_value = {"expected": target_url, "match": "path", "timeout": 10}
                detail.description = "验证当前页面已到达目标路由"
                detail.save(update_fields=[
                    "ope_key", "step_type", "element", "ope_value", "description", "updated_at",
                ])
                return {
                    "status": "applied",
                    "repair_type": "navigation_assertion",
                    "old_locator": f"click element #{element_id}",
                    "new_locator": f"assert URL path {target.path}",
                    "reason": "失败时实际 URL 已与目标页面路由一致，仅把隐藏的冗余导航点击改为路由断言。",
                }

        return await sync_to_async(replace_redundant_navigation)()

    if repair_type == "url_path":
        # A URL-path repair is safe only when machine evidence proves that the
        # route is identical and only query/hash parameters changed. It never
        # turns a redirect to login or another route into a pass.
        from ui_automation.models import UiCaseStepsDetailed, UiPageStepsDetailed

        evidence = diagnosis.get("evidence") or {}
        actual_url = str(evidence.get("actual_url") or "").strip()

        def apply_url_path_repair():
            from django.db import transaction

            try:
                actual = urlsplit(actual_url)
            except Exception:
                return {"status": "skipped", "reason": "失败现场没有有效的实际 URL，未放宽 URL 断言。"}
            if not actual.netloc or not actual.path:
                return {"status": "skipped", "reason": "失败现场 URL 不完整，未放宽 URL 断言。"}

            details = list(UiPageStepsDetailed.objects.filter(
                id=failed_detail_id,
                page_step__case_usages__test_case_id=ui_test_case_id,
                ope_key="assert_url",
            ).distinct().order_by("page_step_id", "step_sort"))
            if len(details) != 1:
                return {"status": "skipped", "reason": "无法唯一定位 URL 断言，未自动修改。"}
            detail = details[0]
            value = dict(detail.ope_value or {})
            expected_raw = str(value.get("expected") or value.get("value") or "").strip()
            if not expected_raw:
                return {"status": "skipped", "reason": "URL 断言缺少期望值，未自动修改。"}
            try:
                expected = urlsplit(expected_raw)
            except Exception:
                return {"status": "skipped", "reason": "URL 断言期望值无效，未自动修改。"}
            expected_path = expected.path.rstrip("/") or "/"
            actual_path = actual.path.rstrip("/") or "/"
            if (
                expected.scheme.lower() != actual.scheme.lower()
                or expected.netloc.lower() != actual.netloc.lower()
                or expected_path != actual_path
            ):
                return {"status": "skipped", "reason": "实际页面不是同一路由，未通过放宽 URL 条件掩盖页面跳转问题。"}
            if value.get("match") == "path":
                return {"status": "skipped", "reason": "URL 已按路由匹配，无需重复修改。"}
            if UiCaseStepsDetailed.objects.filter(
                page_step_id=detail.page_step_id,
            ).exclude(test_case_id=ui_test_case_id).exists():
                return {"status": "skipped", "reason": "URL 断言所在页面步骤被其他用例复用，未自动修改。"}

            with transaction.atomic():
                value["match"] = "path"
                detail.ope_value = value
                detail.save(update_fields=["ope_value", "updated_at"])
            return {
                "status": "applied",
                "repair_type": "url_path",
                "old_wait_mode": "exact",
                "new_wait_mode": "path",
                "reason": "实际 URL 与期望 URL 的 origin 和 pathname 一致，仅忽略动态 query/hash。",
            }

        return await sync_to_async(apply_url_path_repair)()

    # URL/title assertions keep their expected value immutable.  For a real
    # page transition that is merely slower than the default, allow one small,
    # bounded wait increase.  The recorder honours this timeout below; it does
    # not turn a failed URL/title assertion into a pass.
    operation_type = str(failure_info.get("operation_type") or "").strip()
    error_text = str(failure_info.get("error_message") or "")
    if (
        repair_type == "wait_condition"
        and suggestion.get("can_self_heal") is True
        and "timeout" in error_text.lower()
        and operation_type in {"assert_url", "assert_title"}
    ):
        from ui_automation.models import UiCaseStepsDetailed, UiPageStepsDetailed

        def extend_assertion_wait():
            from django.db import transaction

            with transaction.atomic():
                candidate_keys = (
                    [operation_type]
                    if operation_type in {"assert_url", "assert_title"}
                    else ["assert_url", "assert_title"]
                )
                candidates = list(UiPageStepsDetailed.objects.filter(
                    id=failed_detail_id,
                    page_step__case_usages__test_case_id=ui_test_case_id,
                    ope_key__in=candidate_keys,
                ).distinct().order_by("page_step_id", "step_sort"))
                # Multiple identical assertions cannot be mapped safely from a
                # generic failure message; leave them for a human instead.
                if len(candidates) != 1:
                    return {"status": "skipped", "reason": "无法唯一定位超时断言，未自动调整等待时间。"}
                detail = candidates[0]
                if UiCaseStepsDetailed.objects.filter(
                    page_step_id=detail.page_step_id,
                ).exclude(test_case_id=ui_test_case_id).exists():
                    return {"status": "skipped", "reason": "超时断言所在页面步骤被其他用例复用，未自动修改。"}
                value = dict(detail.ope_value or {})
                expected = value.get("expected") if "expected" in value else value.get("value")
                if not expected:
                    return {"status": "skipped", "reason": "断言缺少期望值，未自动调整等待时间。"}
                try:
                    old_timeout = int(value.get("timeout") or 10)
                except (TypeError, ValueError):
                    old_timeout = 10
                new_timeout = min(max(old_timeout + 5, 10), 60)
                if new_timeout <= old_timeout:
                    return {"status": "skipped", "reason": "断言等待时间已达到安全上限。"}
                value["timeout"] = new_timeout
                detail.ope_value = value
                detail.save(update_fields=["ope_value", "updated_at"])
                return {
                    "status": "applied",
                    "repair_type": "wait",
                    "old_wait_seconds": old_timeout,
                    "new_wait_seconds": new_timeout,
                    "reason": "仅增加断言等待时间，未修改 URL/标题期望值。",
                }

        return await sync_to_async(extend_assertion_wait)()

    try:
        element_id = int(suggestion.get("element_id"))
    except (TypeError, ValueError):
        return {"status": "skipped", "reason": "AI 未返回当前用例可定位的元素。"}

    if element_id != failed_detail.element_id:
        return {"status": "skipped", "reason": "AI 建议指向其他步骤元素，未自动修改用例。"}

    locator_type = str(suggestion.get("suggested_locator_type") or "").strip()
    locator_value = str(suggestion.get("suggested_locator_value") or "").strip()

    if failure_info.get("operation_type") == "click":
        compact_locator = re.sub(r"\\s+", "", locator_value).lower()
        if "input" in compact_locator and "combobox" in compact_locator and "role" in compact_locator:
            return {
                "status": "skipped",
                "reason": "自动修复不能把只读 input[role=combobox] 设为点击目标；应选择 .el-select__wrapper 或 select_filter_option。",
            }

    # Generated post-confirm assertions for the custom filter panel must scope
    # the selected value to the actual `.filter-field` container. This app uses
    # a div-based label/control layout, so LLM repairs that assume a native
    # <label>, guessed siblings, or Element Plus `.el-tag` keep failing even
    # when the selected chip is visibly present. Derive the field and value
    # strictly from the failed asset and use the observed DOM contract.
    element_name = str(getattr(failed_detail.element, "name", "") or "")
    current_locator = str(failure_info.get("current_locator_value") or "")
    if (
        failure_info.get("operation_type") == "assert_visible"
        and "Timeout" in str(failure_info.get("error_message") or "")
        and "筛选字段显示" in element_name
        and ("label" in current_locator or "filter-field" in current_locator)
    ):
        label_match = re.search(
            r"contains\s*\(\s*text\(\)\s*,\s*['\"]([^'\"]+)['\"]",
            current_locator,
        )
        expected_value = element_name.split("筛选字段显示", 1)[1].split("（AI修复", 1)[0].strip()
        if label_match and expected_value:
            field_label = label_match.group(1).strip()

            def xpath_literal(value: str) -> str:
                if "'" not in value:
                    return f"'{value}'"
                if '"' not in value:
                    return f'"{value}"'
                return "concat(" + ", \"'\", ".join(
                    f"'{part}'" for part in value.split("'")
                ) + ")"

            locator_type = "xpath"
            locator_value = (
                "//*[contains(concat(' ', normalize-space(@class), ' '), ' filter-field ')]"
                "[.//*[contains(concat(' ', normalize-space(@class), ' '), ' filter-field-label ') "
                f"and normalize-space()={xpath_literal(field_label)}]]"
                f"[contains(., {xpath_literal(expected_value)})]"
            )
    from ui_automation.models import UiCaseStepsDetailed, UiElement, UiPageStepsDetailed

    valid_locator_types = {value for value, _label in UiElement.LOCATOR_TYPE_CHOICES}
    if locator_type not in valid_locator_types or not locator_value:
        return {"status": "skipped", "reason": "AI 返回的定位器格式不安全，未自动修改用例。"}
    from ui_automation.generated_case_service import _locator_dialect_error
    dialect_error = _locator_dialect_error(locator_type, locator_value)
    if dialect_error:
        return {"status": "skipped", "reason": f"{dialect_error} 未自动写回，避免回放语法错误。"}
    if len(locator_value) > 4000:
        return {"status": "skipped", "reason": "AI 返回的定位器过长，未自动修改用例。"}

    def apply_repair():
        from django.db import transaction

        with transaction.atomic():
            element = UiElement.objects.select_for_update().filter(
                id=element_id, page__project_id=project_id
            ).first()
            if element is None:
                return {"status": "skipped", "reason": "待修复元素不存在或不属于当前项目。"}

            detail = UiPageStepsDetailed.objects.select_for_update().filter(
                id=failed_detail_id,
                page_step__case_usages__test_case_id=ui_test_case_id,
                page_step__project_id=project_id,
                element_id=element.id,
                ope_key=evidence.get("operation_type"),
            ).first()
            if detail is None:
                return {"status": "skipped", "reason": "失败步骤已变化或不属于当前生成用例。"}
            if UiCaseStepsDetailed.objects.filter(
                page_step_id=detail.page_step_id,
            ).exclude(test_case_id=ui_test_case_id).exists():
                return {"status": "skipped", "reason": "目标页面步骤被其他用例复用，未自动修改。"}
            if (
                element.locator_type == locator_type
                and element.locator_value == locator_value
            ):
                return {"status": "skipped", "reason": "AI 建议与当前定位器相同，未重复修改。"}

            # The current click already tried every configured locator and
            # timed out, so do not retain the failed primary or its fallbacks.
            # A repaired clone must start with only the evidence-backed locator.
            clone = UiElement.objects.create(
                page=element.page,
                name=f"{element.name}（AI修复{attempt}）"[:64],
                locator_type=locator_type,
                locator_value=locator_value,
                locator_index=element.locator_index,
                locator_type_2=None,
                locator_value_2=None,
                locator_index_2=None,
                locator_type_3=None,
                locator_value_3=None,
                locator_index_3=None,
                wait_time=element.wait_time,
                is_iframe=element.is_iframe,
                iframe_locator=element.iframe_locator,
                description=element.description,
                creator=element.creator,
            )
            detail.element = clone
            detail.save(update_fields=["element", "updated_at"])
            return {
                "status": "applied",
                "element_id": clone.id,
                "old_locator": f"[{element.locator_type}] {element.locator_value}",
                "new_locator": f"[{locator_type}] {locator_value}",
                "reason": str(suggestion.get("explanation") or diagnosis.get("summary") or "AI 建议替换定位器")[:500],
            }

    return await sync_to_async(apply_repair)()


async def _repair_generated_ui_case_after_failure(
    *, test_case_id: int, project_id: int, ui_test_case_id: int, user, validation: Dict[str, Any], background=False
) -> Dict[str, Any]:
    """Diagnose and safely repair a failed generated case, then replay it.

    This deliberately repairs locator configuration or a bounded assertion
    wait. URL/title expectations, action values, step count, and step order are
    immutable in this automatic path so a real product defect cannot be hidden.
    """
    if not user or validation.get("status") != "failed":
        return {"validation": validation, "attempts": []}

    from testcases.ai_diagnosis_service import diagnose_execution_failure
    from testcases.models import TestCase

    attempts: list[Dict[str, Any]] = []
    current_validation = validation
    for attempt in range(1, MAX_GENERATED_UI_REPAIR_ATTEMPTS + 1):
        execution_record_id = current_validation.get("execution_record_id")
        if not execution_record_id:
            attempts.append({"attempt": attempt, "status": "skipped", "reason": "没有失败执行记录，无法分析。"})
            break

        def diagnose():
            testcase = TestCase.objects.select_related("ui_test_case").filter(
                id=int(test_case_id), project_id=int(project_id), ui_test_case_id=int(ui_test_case_id)
            ).first()
            if testcase is None:
                raise ValueError("功能用例与生成的 UI 用例绑定关系不存在。")
            return diagnose_execution_failure(
                testcase, ui_record_id=int(execution_record_id), user=user
            )

        try:
            diagnosis = await sync_to_async(diagnose)()
        except Exception as exc:
            logger.exception("Generated UI testcase AI repair diagnosis failed")
            attempts.append({"attempt": attempt, "status": "failed", "reason": f"AI 分析失败：{str(exc)[:500]}"})
            break

        try:
            repair = await _apply_safe_generated_ui_repair(
                project_id=int(project_id),
                ui_test_case_id=int(ui_test_case_id),
                diagnosis=diagnosis,
                attempt=attempt,
            )
        except Exception as exc:
            logger.exception("Generated UI testcase safe repair failed")
            repair = {"status": "failed", "reason": f"安全修复未应用：{str(exc)[:500]}"}

        attempt_result = {
            "attempt": attempt,
            "status": repair.get("status"),
            "reason": repair.get("reason") or diagnosis.get("summary") or "",
            "root_cause_type": diagnosis.get("root_cause_type"),
            "diagnosed_by": diagnosis.get("diagnosed_by"),
            "evidence": diagnosis.get("evidence") or {},
        }
        if repair.get("status") == "applied":
            attempt_result.update({
                "old_locator": repair.get("old_locator"),
                "new_locator": repair.get("new_locator"),
                "repair_type": repair.get("repair_type") or (diagnosis.get("healing_suggestion") or {}).get("repair_type"),
            })
            attempts.append(attempt_result)
            current_validation = await _validate_generated_ui_case(
                user=user,
                project_id=int(project_id),
                ui_test_case_id=int(ui_test_case_id),
                validation_session_id=f"ui-generation-repair-{uuid.uuid4().hex}",
                background=background,
            )
            if current_validation.get("status") == "success":
                break
            continue

        attempts.append(attempt_result)
        break

    def persist_repair_metadata():
        from ui_automation.models import UiTestCase

        case = UiTestCase.objects.get(id=ui_test_case_id)
        result_data = dict(case.result_data or {})
        result_data["generation_repair"] = {
            "status": "passed" if current_validation.get("status") == "success" else "not_repaired",
            "max_attempts": MAX_GENERATED_UI_REPAIR_ATTEMPTS,
            "attempts": attempts,
        }
        case.result_data = result_data
        case.save(update_fields=["result_data", "updated_at"])

    await sync_to_async(persist_repair_metadata)()
    current_validation["repair_attempts"] = attempts
    return {"validation": current_validation, "attempts": attempts}


async def _queue_generated_ui_case(*, record, llm_config_id):
    from .ui_generation_jobs import enqueue_generation

    if not record or record.status != "pass":
        return {"enabled": True, "status": "skipped", "message": "功能测试未通过，未自动生成 UI 用例。"}
    # Once functional execution passes, handing off must finish even if the
    # client disconnects during the database/broker transaction.
    try:
        result = await asyncio.shield(sync_to_async(enqueue_generation)(
            record.pk, llm_config_id=llm_config_id,
        ))
        await sync_to_async(record.refresh_from_db)()
        return result
    except ValueError as exc:
        from .ui_generation_jobs import write_state
        result = {"enabled": True, "status": "failed", "message": str(exc)}
        await sync_to_async(write_state)(record, result)
        return result


async def _finalize_generated_ui_case(
    *, llm, tools, messages, test_case_id, project_id, record, user=None, background=False, on_stage=None,
    provider="openai_compatible",
) -> Dict[str, Any]:
    """Generate, replay-validate, bind, and report a UI case after success."""
    if not test_case_id:
        return {
            "enabled": True,
            "status": "failed",
            "message": "缺少测试用例 ID，无法绑定 UI 自动化用例。",
        }
    from testcases.models import TestCase

    ui_case_id = None
    generation_error = ""
    try:
        ui_case_id = await sync_to_async(
            TestCase.objects.filter(
                id=int(test_case_id), project_id=int(project_id)
            ).values_list("ui_test_case_id", flat=True).first
        )()
    except Exception:
        logger.exception(
            "AgentLoopStreamAPI: failed to verify generated UI case binding; testcase=%s",
            test_case_id,
        )

    if not ui_case_id and record and record.status == "pass":
        generated = await _force_save_generated_ui_case(
            llm=llm,
            tools=tools,
            messages=messages,
            provider=provider,
        )
        reported_ui_case_id = None
        if generated.get("status") == "success":
            reported_ui_case_id = generated.get("ui_test_case_id")
            if not reported_ui_case_id:
                generation_error = "保存工具标记成功，但没有返回 UI 用例 ID。"
            logger.info(
                "AgentLoopStreamAPI: post-run UI case save completed; testcase=%s ui_case=%s",
                test_case_id,
                reported_ui_case_id,
            )
        else:
            generation_error = str(
                generated.get("message") or "保存工具未返回成功结果。"
            )[:700]
            logger.warning(
                "AgentLoopStreamAPI: post-run UI case generation failed; testcase=%s reason=%s",
                test_case_id,
                generation_error,
            )
        try:
            ui_case_id = await sync_to_async(
                TestCase.objects.filter(
                    id=int(test_case_id), project_id=int(project_id)
                ).values_list("ui_test_case_id", flat=True).first
            )()
        except Exception:
            logger.exception(
                "AgentLoopStreamAPI: failed to verify post-run UI binding; testcase=%s",
                test_case_id,
            )
        if reported_ui_case_id and not ui_case_id and not generation_error:
            generation_error = "保存工具报告成功，但数据库中未查到绑定记录。"

    validation = {"status": "skipped", "message": "未进行回放验证。"}
    if ui_case_id and user is not None:
        if on_stage:
            await on_stage("validating")
        validation = await _validate_generated_ui_case(
            user=user,
            project_id=int(project_id),
            ui_test_case_id=int(ui_case_id),
            validation_session_id=f"ui-generation-validation-{uuid.uuid4().hex}",
            background=background,
        )
        if validation.get("status") == "failed":
            repair_result = await _repair_generated_ui_case_after_failure(
                test_case_id=int(test_case_id),
                project_id=int(project_id),
                ui_test_case_id=int(ui_case_id),
                user=user,
                validation=validation,
                background=background,
            )
            validation = repair_result.get("validation") or validation

    if ui_case_id and validation.get("status") in {"success", "skipped"}:
        if validation.get("status") == "success":
            repair_attempts = validation.get("repair_attempts") or []
            repaired = any(item.get("status") == "applied" for item in repair_attempts)
            if repaired:
                generation_note = f"成功：UI 自动化用例 #{ui_case_id} 已生成、绑定，AI 修复后通过回放验证。"
                result_message = f"UI 自动化用例 #{ui_case_id} 已生成，AI 修复后通过回放验证。"
            else:
                generation_note = f"成功：UI 自动化用例 #{ui_case_id} 已生成、绑定并通过回放验证。"
                result_message = f"UI 自动化用例 #{ui_case_id} 已生成、绑定并通过回放验证。"
        else:
            generation_note = f"已生成：UI 自动化用例 #{ui_case_id} 已生成并绑定，但未进行回放验证。"
            result_message = f"UI 自动化用例 #{ui_case_id} 已生成并绑定，但未进行回放验证。"
        note = f"\n\n### UI 自动化用例生成\n{generation_note}"
        if record and "### UI 自动化用例生成" not in (record.summary or ""):
            record.summary = (record.summary or "") + note
            await sync_to_async(record.save)(update_fields=["summary"])
        return {
            "enabled": True,
            "status": "success",
            "ui_test_case_id": ui_case_id,
            "validation_status": validation.get("validation_status", "skipped"),
            "execution_record_id": validation.get("execution_record_id"),
            "repair_attempts": validation.get("repair_attempts", []),
            "message": result_message,
        }

    if record and record.status == "pass":
        validation_message = validation.get("message") or "回放验证失败。"
        if ui_case_id:
            repair_attempts = validation.get("repair_attempts") or []
            applied_attempts = [item for item in repair_attempts if item.get("status") == "applied"]
            failed_attempts = [item for item in repair_attempts if item.get("status") == "failed"]
            if failed_attempts:
                repair_summary = f"AI 自动修复流程失败：{failed_attempts[-1].get('reason') or '失败分析未完成'}。"
            elif applied_attempts:
                repair_summary = f"AI 已尝试安全修复 {len(applied_attempts)} 次，但再次回放仍未通过。"
            elif repair_attempts:
                repair_summary = "AI 已完成失败分析，但未找到不会改变业务期望的安全修复方案。"
            else:
                repair_summary = "未执行自动修复。"
            if repair_attempts:
                evidence = repair_attempts[-1].get("evidence") or {}
                actual_url = str(evidence.get("actual_url") or "").strip()
                expected = str(evidence.get("expected") or "").strip()
                if actual_url or expected:
                    repair_summary += (
                        f"现场对比：预期={expected or '未记录'}，实际URL={actual_url or '未记录'}。"
                    )
            note = (
                "\n\n### UI 自动化用例生成\n"
                f"已生成但需要修正：UI 自动化用例 #{ui_case_id} 已保存并绑定，但回放验证失败。"
                f"{repair_summary}原因：{validation_message}"
            )
            result_message = f"UI 自动化用例 #{ui_case_id} 已生成，但回放验证失败，需要修正。"
            result_reason = validation_message
        else:
            result_reason = generation_error or "本次执行没有成功保存并绑定 UI 自动化用例。"
            note = (
                "\n\n### UI 自动化用例生成\n"
                "失败：功能用例已通过，但本次执行没有成功保存并绑定 UI 自动化用例。"
                f"原因：{result_reason}"
            )
            result_message = "功能用例通过，但未检测到已保存并绑定的 UI 自动化用例。"
        if "### UI 自动化用例生成" not in (record.summary or ""):
            record.summary = (record.summary or "") + note
            await sync_to_async(record.save)(update_fields=["summary"])
        return {
            "enabled": True,
            "status": "failed",
            "ui_test_case_id": ui_case_id,
            "validation_status": validation.get("validation_status", "failed"),
            "execution_record_id": validation.get("execution_record_id"),
            "repair_attempts": validation.get("repair_attempts", []),
            "message": result_message,
            "reason": result_reason or None,
        }

    return {
        "enabled": True,
        "status": "skipped",
        "message": "功能用例未通过，未生成 UI 自动化用例。",
    }


async def _get_agent_state_messages(agent, invoke_config):
    """Read the final browser tool evidence for fast-finish branches."""
    if agent is None or invoke_config is None:
        return []
    try:
        state = await agent.aget_state(invoke_config)
        return state.values.get("messages", []) if state.values else []
    except Exception:
        logger.exception("AgentLoopStreamAPI: failed to read messages for UI case generation")
        return []


# ============== 统一响应辅助函数 ==============


def api_success_response(
    message: str, data: Any = None, code: int = 200
) -> JsonResponse:
    """构建统一格式的成功响应"""
    return JsonResponse(
        {
            "status": "success",
            "code": code,
            "message": message,
            "data": data,
            "errors": None,
        },
        json_dumps_params={"ensure_ascii": False},
    )


def api_error_response(
    message: str, code: int = 400, errors: Any = None
) -> JsonResponse:
    """构建统一格式的错误响应"""
    if errors is None:
        errors = {"detail": [message]}
    return JsonResponse(
        {
            "status": "error",
            "code": code,
            "message": message,
            "data": None,
            "errors": errors,
        },
        status=code,
        json_dumps_params={"ensure_ascii": False},
    )


_MARKDOWN_IMAGE_URL_RE = re.compile(
    r"!\[[^\]]*?\]\((?P<url>https?://[^)\s]+)\)", re.IGNORECASE
)
_PLAIN_HTTP_URL_RE = re.compile(r'(?P<url>https?://[^\s<>"\']+)', re.IGNORECASE)
_URL_LEADING_WRAP_CHARS = "([<{\"'“‘（【《「『"
_URL_TRAILING_WRAP_CHARS = ")]}>\"'”’）】》」』.,;!?，。；！？、"
_URL_HARD_STOP_CHARS = "\r\n\t ,;)}]>\"'，。；！？、：”’）】》」』"


def _get_env_int(name: str, default: int, min_value: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return max(min_value, int(raw))
    except ValueError:
        logger.warning(
            "AgentLoopStreamAPI: Invalid int env %s=%s, fallback=%s", name, raw, default
        )
        return default


def _get_env_float(name: str, default: float, min_value: float = 0.1) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return max(min_value, float(raw))
    except ValueError:
        logger.warning(
            "AgentLoopStreamAPI: Invalid float env %s=%s, fallback=%s",
            name,
            raw,
            default,
        )
        return default


def _normalize_uploaded_image_base64_list(
    raw_images: Any, raw_image: Any = None
) -> List[str]:
    """兼容旧 image 字段与新 images 数组，返回去重后的上传图片列表。"""

    normalized: List[str] = []

    def _append(candidate: Any) -> None:
        if not isinstance(candidate, str):
            return
        value = candidate.strip()
        if not value or value in normalized:
            return
        normalized.append(value)

    if isinstance(raw_images, list):
        for item in raw_images:
            _append(item)
    else:
        _append(raw_images)

    _append(raw_image)
    return normalized


_LINKED_IMAGE_URL_ALLOWLIST = {
    host.strip().lower()
    for host in os.getenv("AGENT_LOOP_IMAGE_URL_ALLOWLIST", "*").split(",")
    if host.strip()
}
_MAX_LINKED_IMAGES_PER_REQUEST = _get_env_int(
    "AGENT_LOOP_MAX_LINKED_IMAGES", 3, min_value=1
)
_MAX_LINKED_IMAGE_BYTES = _get_env_int(
    "AGENT_LOOP_MAX_LINKED_IMAGE_BYTES", 5 * 1024 * 1024, min_value=1024
)
_LINKED_IMAGE_FETCH_TIMEOUT = _get_env_float(
    "AGENT_LOOP_LINKED_IMAGE_FETCH_TIMEOUT", 8.0, min_value=1.0
)
_MAX_SAFE_TOOL_MESSAGE_CHARS = _get_env_int(
    "AGENT_LOOP_MAX_SAFE_TOOL_MESSAGE_CHARS", 20000, min_value=1000
)


def _extract_linked_image_urls(text: str) -> List[str]:
    """从用户消息中提取图片 URL（支持 Markdown 图片语法和纯 URL）。"""
    if not text:
        return []

    urls: List[str] = []
    seen: set[str] = set()

    def _normalize_url(candidate: str) -> str:
        url = (candidate or "").strip()
        while url and url[0] in _URL_LEADING_WRAP_CHARS:
            url = url[1:]
        hard_stop_indexes = [
            url.find(char) for char in _URL_HARD_STOP_CHARS if char in url
        ]
        if hard_stop_indexes:
            url = url[: min(index for index in hard_stop_indexes if index >= 0)]
        while url and url[-1] in _URL_TRAILING_WRAP_CHARS:
            url = url[:-1]
        return url

    for pattern in (_MARKDOWN_IMAGE_URL_RE, _PLAIN_HTTP_URL_RE):
        for match in pattern.finditer(text):
            url = _normalize_url(match.group("url") or "")
            if not url or url in seen:
                continue

            try:
                parsed = urlparse(url)
            except ValueError:
                continue
            if parsed.scheme.lower() not in ("http", "https"):
                continue
            if not parsed.netloc:
                continue

            seen.add(url)
            urls.append(url)

    return urls


def _is_linked_image_url_allowed(url: str) -> bool:
    """校验图片 URL 是否在允许的 host 白名单中（支持 * 全放开）。"""
    if "*" in _LINKED_IMAGE_URL_ALLOWLIST or "all" in _LINKED_IMAGE_URL_ALLOWLIST:
        return True

    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").strip().lower()
    return bool(host and host in _LINKED_IMAGE_URL_ALLOWLIST)


async def _download_linked_image_as_data_url(url: str) -> Optional[str]:
    """下载图片 URL 并转为 data URL，供视觉模型消费。"""
    if not _is_linked_image_url_allowed(url):
        logger.warning(
            "AgentLoopStreamAPI: Skip linked image URL not in allowlist. url=%s, allowlist=%s",
            url,
            sorted(_LINKED_IMAGE_URL_ALLOWLIST),
        )
        return None

    timeout = httpx.Timeout(
        _LINKED_IMAGE_FETCH_TIMEOUT, connect=_LINKED_IMAGE_FETCH_TIMEOUT
    )
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream(
                "GET", url, headers={"Accept": "image/*"}
            ) as response:
                response.raise_for_status()
                content_type = (
                    (response.headers.get("Content-Type") or "")
                    .split(";")[0]
                    .strip()
                    .lower()
                )
                if not content_type.startswith("image/"):
                    logger.warning(
                        "AgentLoopStreamAPI: Skip linked URL with non-image content-type. url=%s, content_type=%s",
                        url,
                        content_type or "unknown",
                    )
                    return None

                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    if len(data) + len(chunk) > _MAX_LINKED_IMAGE_BYTES:
                        logger.warning(
                            "AgentLoopStreamAPI: Skip linked image over size limit. url=%s, max_bytes=%s",
                            url,
                            _MAX_LINKED_IMAGE_BYTES,
                        )
                        return None
                    data.extend(chunk)

                if not data:
                    logger.warning(
                        "AgentLoopStreamAPI: Skip empty linked image response. url=%s",
                        url,
                    )
                    return None

                encoded = base64.b64encode(bytes(data)).decode("utf-8")
                return f"data:{content_type};base64,{encoded}"
    except Exception as e:
        logger.warning(
            "AgentLoopStreamAPI: Failed to fetch linked image url=%s, error=%s", url, e
        )
        return None


async def _collect_linked_image_data_urls(
    user_message: str,
    linked_urls: Optional[List[str]] = None,
) -> List[str]:
    """从用户消息 URL 中收集可用图片，并转换为 data URL 列表。"""
    if linked_urls is None:
        linked_urls = _extract_linked_image_urls(user_message)
    if not linked_urls:
        return []

    limited_urls = linked_urls[:_MAX_LINKED_IMAGES_PER_REQUEST]
    if len(linked_urls) > len(limited_urls):
        logger.info(
            "AgentLoopStreamAPI: Truncated linked image URLs from %s to %s",
            len(linked_urls),
            len(limited_urls),
        )

    download_tasks = [_download_linked_image_as_data_url(url) for url in limited_urls]
    results = await asyncio.gather(*download_tasks, return_exceptions=True)

    data_urls: List[str] = []
    for url, result in zip(limited_urls, results):
        if isinstance(result, Exception):
            logger.warning(
                "AgentLoopStreamAPI: Linked image download task failed. url=%s, error=%s",
                url,
                result,
            )
            continue
        if result:
            data_urls.append(result)

    return data_urls


async def _prepare_agent_loop_human_message(
    user_message: str,
    *,
    project: Project,
    supports_vision: bool,
    uploaded_images_base64: Optional[List[str]] = None,
) -> tuple[Any, Dict[str, Any], str]:
    """
    规范化 Agent Loop 的用户消息。

    - 将需求文档中的 `docimg://` 占位符替换为可访问的 `/api/.../images/...` URL
    - 当模型支持视觉时，把需求文档图片与普通 HTTP(S) 图片一并转成多模态输入
    - 返回：LangChain HumanMessage content、additional_kwargs、展示用文本
    """
    display_message = (user_message or "").strip()
    uploaded_images_base64 = uploaded_images_base64 or []

    (
        display_message,
        requirement_doc_image_data_urls,
        requirement_document_id,
    ) = await _extract_requirement_doc_images_for_message(display_message, project)

    if requirement_doc_image_data_urls and not supports_vision:
        logger.warning(
            "AgentLoopStreamAPI: Requirement document images detected but current model does not support vision"
        )

    linked_image_data_urls: List[str] = []
    linked_image_urls = _extract_linked_image_urls(display_message)
    if linked_image_urls:
        logger.info(
            "AgentLoopStreamAPI: Extracted %s candidate linked image URLs from message",
            len(linked_image_urls),
        )
        if supports_vision:
            linked_image_data_urls = await _collect_linked_image_data_urls(
                display_message,
                linked_urls=linked_image_urls,
            )
            if linked_image_data_urls:
                logger.info(
                    "AgentLoopStreamAPI: Loaded %s linked images for multimodal input",
                    len(linked_image_data_urls),
                )
        else:
            logger.warning(
                "AgentLoopStreamAPI: Found %s linked image URLs but current model does not support vision",
                len(linked_image_urls),
            )
    elif "http://" in display_message.lower() or "https://" in display_message.lower():
        logger.warning(
            "AgentLoopStreamAPI: Message contains URL text but extractor found 0 valid URLs"
        )

    multimodal_image_data_urls = requirement_doc_image_data_urls + linked_image_data_urls

    additional_kwargs: Dict[str, Any] = {}
    if requirement_document_id:
        additional_kwargs["requirement_document_id"] = requirement_document_id
        if requirement_doc_image_data_urls:
            additional_kwargs["image_source"] = "requirement_document"

    if supports_vision and (uploaded_images_base64 or multimodal_image_data_urls):
        human_message_content: Any = [{"type": "text", "text": display_message}]
        for data_url in multimodal_image_data_urls:
            human_message_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": data_url},
                }
            )
        for image_base64 in uploaded_images_base64:
            human_message_content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{image_base64}"
                    },
                }
            )
    else:
        human_message_content = display_message

    return human_message_content, additional_kwargs, display_message


def _build_testcase_execution_display_message(testcase_id: int, pre_data_run: Any = None) -> str:
    """Build the short, user-facing status for a manual testcase execution.

    The complete testcase, generated-data snapshot and helper instructions belong
    to the agent's internal system context. Keeping the HumanMessage concise also
    prevents those implementation details from appearing when history is reopened.
    """
    from testcases.models import TestCase

    testcase = TestCase.objects.filter(id=testcase_id).first()
    if not testcase:
        return f"正在执行用例 {testcase_id}。执行结果和截图会自动保存。"

    lines = [f"正在执行用例 {testcase.id}：{testcase.name}"]
    snapshot = getattr(pre_data_run, "output_snapshot", None)
    if isinstance(snapshot, dict):
        identifier = str(
            snapshot.get("ticketNo")
            or snapshot.get("orderNo")
            or snapshot.get("id")
            or ""
        ).strip()
        if identifier:
            lines.append(f"测试数据已准备：{identifier}")
        elif pre_data_run is not None:
            lines.append("已完成测试数据准备。")
    elif pre_data_run is not None:
        lines.append("已完成测试数据准备。")

    lines.append(f"共 {testcase.steps.count()} 个步骤，执行结果和截图会自动保存。")
    return "\n".join(lines)


def _get_testcase_step_count(testcase_id: int) -> int:
    """Return the number of configured steps for a manual testcase."""
    from testcases.models import TestCase

    testcase = TestCase.objects.filter(id=testcase_id).first()
    return testcase.steps.count() if testcase else 0


def _get_testcase_step_details(testcase_id: int) -> dict[int, dict[str, str]]:
    """Return canonical descriptions and expectations for AI step continuation."""
    from testcases.models import TestCaseStep

    rows = TestCaseStep.objects.filter(test_case_id=int(testcase_id)).values_list(
        "step_number", "description", "expected_result"
    )
    details: dict[int, dict[str, str]] = {}
    for number, description, expected_result in rows:
        try:
            step_no = int(number)
        except (TypeError, ValueError):
            continue
        details[step_no] = {
            "description": (description or "").strip() or f"步骤{step_no}",
            "expected_result": (expected_result or "").strip(),
        }
    return details


def _get_testcase_step_names(testcase_id: int) -> dict[int, str]:
    """Map step_number -> description for helper-runner progress messages."""
    return {
        number: detail["description"]
        for number, detail in _get_testcase_step_details(testcase_id).items()
    }


def _extract_helper_result_reason(content: Any) -> tuple[str, str]:
    """Return (PASS|FAIL|'', reason) from helper stdout, ignoring skill-doc samples."""
    if isinstance(content, (dict, list)):
        text = json.dumps(content, ensure_ascii=False)
    else:
        text = content if isinstance(content, str) else str(content or "")
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, TypeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            text = "\n".join(
                str(item.get("text") or "") if isinstance(item, dict) else str(item)
                for item in parsed
            )
        elif isinstance(parsed, dict):
            text = str(parsed.get("text") or text)
    text = text.replace("\\n", "\n")
    matches = list(
        re.finditer(
            r"^RESULT=(PASS|FAIL)[:：]?\s*(.*)$",
            text,
            flags=re.MULTILINE | re.IGNORECASE,
        )
    )
    if re.search(
        r"Playwright exec timed out|浏览器已重置",
        text,
        flags=re.IGNORECASE,
    ) and not any((m.group(1) or "").upper() == "PASS" for m in matches):
        return "FAIL", "执行超时，未完成截图"
    if not matches:
        return "", ""
    last = matches[-1]
    status = (last.group(1) or "").upper()
    reason = (last.group(2) or "").strip().strip("'\"")
    if reason.lower().startswith("result="):
        reason = ""
    return status, reason


def _build_helper_step_commentary(
    *,
    prev_step: Optional[int] = None,
    prev_status: str = "",
    prev_reason: str = "",
    next_step: Optional[int] = None,
    next_action: str = "",
) -> str:
    """Build the image-3 style AI lines between helper cards."""
    lines: list[str] = []
    if prev_step:
        passed = (prev_status or "PASS").upper() != "FAIL"
        label = "通过" if passed else "失败"
        reason = prev_reason or ("已完成" if passed else "未通过")
        lines.append(f"步骤 {prev_step} {label}: {reason}")
    if next_step:
        action = (next_action or "").strip() or f"步骤{next_step}"
        lines.append(f"步骤 {next_step}: {action}")
    return "\n".join(lines)


def _tool_content_as_text(content: Any) -> str:
    """Flatten MCP text blocks, including JSON-serialized block arrays, for evidence parsing."""
    if isinstance(content, str):
        stripped = content.strip()
        if stripped.startswith(("[", "{")):
            try:
                decoded = json.loads(stripped)
            except (TypeError, ValueError):
                return content
            if decoded != content:
                return _tool_content_as_text(decoded)
        return content
    if isinstance(content, (list, tuple)):
        return "\n".join(
            text for text in (_tool_content_as_text(item) for item in content) if text
        )
    if isinstance(content, dict):
        for key in ("text", "content", "output", "plain", "display", "result"):
            value = content.get(key)
            if value is not None:
                return _tool_content_as_text(value)
        return json.dumps(content, ensure_ascii=False)
    return str(content or "")


def _extract_helper_testcase_progress(content: Any) -> tuple[Optional[int], Optional[int]]:
    """Extract testcase and step ids from a fixed Playwright helper result."""
    text = _tool_content_as_text(content)
    execution = re.findall(
        r"\[CASE_STEP_EXECUTION\]\s*case=(\d+)\s+step=(\d+)",
        text,
        flags=re.IGNORECASE,
    )
    if execution:
        case_id, step = execution[-1]
        return int(case_id), int(step)
    declared = re.findall(
        r"\[(?:CASE_SCREENSHOT|SCREENSHOT_SAVED)\]\s+\S*case[_-](\d+)_step(\d+)",
        text,
        flags=re.IGNORECASE,
    )
    if declared:
        case_id, step = declared[-1]
        return int(case_id), int(step)
    matches = list(re.finditer(r"case[_-](\d+)_step(\d+)", text, flags=re.IGNORECASE))
    if not matches:
        return None, None
    last = matches[-1]
    return int(last.group(1)), int(last.group(2))


def _latest_passed_helper_step_from_messages(messages, *, test_case_id=None) -> Optional[int]:
    """Return the contiguous PASS prefix; screenshots alone do not prove success."""
    outcomes = {}
    for msg in messages or []:
        if type(msg).__name__ not in ("ToolMessage", "ToolMessageChunk"):
            continue
        if getattr(msg, "name", "") != "execute_skill_script":
            continue
        from .testcase_receipts import normalize_testcase_receipts
        text = normalize_testcase_receipts(
            _tool_content_as_text(getattr(msg, "content", "") or ""), test_case_id
        )
        execution_markers = list(re.finditer(
            r"\[CASE_STEP_EXECUTION\]\s*case=(\d+)\s+step=(\d+)",
            text,
            flags=re.IGNORECASE,
        ))
        if execution_markers:
            for index, marker in enumerate(execution_markers):
                case_id = int(marker.group(1))
                step = int(marker.group(2))
                if test_case_id is not None and case_id != int(test_case_id):
                    continue
                end = (
                    execution_markers[index + 1].start()
                    if index + 1 < len(execution_markers)
                    else len(text)
                )
                block = text[marker.end():end]
                markers = re.findall(
                    r"(?m)^\s*RESULT\s*=\s*(PASS|FAIL)\b",
                    block,
                    flags=re.IGNORECASE,
                )
                outcomes[step] = bool(markers) and markers[-1].upper() == "PASS"
            continue
        case_id, step = _extract_helper_testcase_progress(text)
        if not step or (test_case_id is not None and case_id != int(test_case_id)):
            continue
        markers = re.findall(
            r"(?m)^\s*RESULT\s*=\s*(PASS|FAIL)\b",
            text,
            flags=re.IGNORECASE,
        )
        outcomes[step] = bool(markers) and markers[-1].upper() == "PASS"
    if not outcomes:
        return None
    done = 0
    while outcomes.get(done + 1, False):
        done += 1
    return done


def _explicitly_failed_helper_step_from_messages(messages, *, test_case_id=None) -> Optional[int]:
    """Return the earliest step with an explicit browser FAIL receipt."""
    failed = []
    for msg in messages or []:
        if type(msg).__name__ not in ("ToolMessage", "ToolMessageChunk"):
            continue
        if getattr(msg, "name", "") != "execute_skill_script":
            continue
        from .testcase_receipts import normalize_testcase_receipts
        text = normalize_testcase_receipts(
            _tool_content_as_text(getattr(msg, "content", "") or ""), test_case_id
        )
        boundaries = list(re.finditer(
            r"\[CASE_STEP_EXECUTION\]\s*case=(\d+)\s+step=(\d+)",
            text,
            flags=re.IGNORECASE,
        ))
        if boundaries:
            for index, marker in enumerate(boundaries):
                case_id, step = int(marker.group(1)), int(marker.group(2))
                if test_case_id is not None and case_id != int(test_case_id):
                    continue
                end = boundaries[index + 1].start() if index + 1 < len(boundaries) else len(text)
                results = re.findall(
                    r"(?m)^\s*RESULT\s*=\s*(PASS|FAIL)\b",
                    text[marker.end():end],
                    flags=re.IGNORECASE,
                )
                if results and results[-1].upper() == "FAIL":
                    failed.append(step)
            continue
        case_id, step = _extract_helper_testcase_progress(text)
        if not step or (test_case_id is not None and case_id != int(test_case_id)):
            continue
        results = re.findall(
            r"(?m)^\s*RESULT\s*=\s*(PASS|FAIL)\b",
            text,
            flags=re.IGNORECASE,
        )
        if results and results[-1].upper() == "FAIL":
            failed.append(step)
    return min(failed) if failed else None


def _testcase_step_was_attempted(messages, *, step: int, test_case_id=None) -> bool:
    """Detect a tool call or screenshot for a step, regardless of its outcome."""
    for msg in messages or []:
        if type(msg).__name__ not in ("ToolMessage", "ToolMessageChunk"):
            continue
        if getattr(msg, "name", "") != "execute_skill_script":
            continue
        from .testcase_receipts import normalize_testcase_receipts
        text = normalize_testcase_receipts(
            _tool_content_as_text(getattr(msg, "content", "") or ""), test_case_id
        )
        boundaries = re.findall(
            r"\[CASE_STEP_EXECUTION\]\s*case=(\d+)\s+step=(\d+)",
            text,
            flags=re.IGNORECASE,
        )
        if boundaries:
            if any(
                (test_case_id is None or int(case_id) == int(test_case_id))
                and int(step_no) == int(step)
                for case_id, step_no in boundaries
            ):
                return True
            continue
        case_id, step_no = _extract_helper_testcase_progress(text)
        if step_no == int(step) and (test_case_id is None or case_id == int(test_case_id)):
            return True
    return False


async def _stream_ai_testcase_resume(agent, *, resume_prompt, config,
                                     session_id, test_case_id, total_steps):
    """Publish concrete tool evidence while the AI continues an incomplete case."""
    async with asyncio.timeout(180):
        async for update in agent.astream(
            {"messages": [HumanMessage(content=resume_prompt)]},
            config=config, stream_mode="updates",
        ):
            if should_stop(session_id):
                raise RuntimeError("用户停止执行")
            for node in update.values():
                if not isinstance(node, dict):
                    continue
                for msg in node.get("messages", []):
                    calls = getattr(msg, "tool_calls", None)
                    if calls:
                        for event in await _emit_testcase_tool_started(
                            calls, session_id=session_id, test_case_id=test_case_id,
                        ):
                            yield event
                    if type(msg).__name__ != "ToolMessage":
                        continue
                    content = getattr(msg, "content", "")
                    case_id, step = _extract_helper_testcase_progress(content)
                    if step is not None and case_id in (None, int(test_case_id)):
                        yield {
                            "type": "testcase_progress", "session_id": session_id,
                            "test_case_id": int(test_case_id), "step": step,
                            "total_steps": total_steps, "state": "completed",
                            "message": f"步骤 {step}/{total_steps} 执行已返回，正在校验结果",
                        }
                    yield {
                        "type": "tool_result", "tool_name": getattr(msg, "name", None),
                        "tool_output": content,
                    }


async def _run_and_stream_remaining_helper_steps(
    *,
    test_case_id: int,
    user_id: int,
    project_id: int,
    session_id: str,
    from_step: int,
    to_step: int,
    messages,
    previous_output: Any = "",
    agent=None,
    invoke_config=None,
):
    """Run remaining helper steps one by one and yield live chat events.

    Each step is streamed as:
    1. AI commentary (`步骤 N 通过` / `步骤 N+1: ...`)
    2. testcase_progress so the progress bar moves
    3. tool_result with the same screenshot card shape as execute_skill_script
    """
    from .builtin_tools.skill_tools import (
        auto_run_missing_testcase_helper_steps,
        infer_fixed_helper_call_template,
    )

    template = infer_fixed_helper_call_template(messages)
    try:
        from testcases.models import TestCase as _HelperCase
        from data_generation.testcase_pre_data import (
            is_claimable_ticket_detail_case,
            is_complete_ticket_ui_case,
            is_processing_handover_helper_case,
            is_transfer_ready_ticket_case,
            is_transfer_ui_helper_case,
        )

        _tc = await sync_to_async(
            _HelperCase.objects.filter(id=int(test_case_id)).first
        )()
        if _tc and await sync_to_async(is_claimable_ticket_detail_case)(_tc):
            template = "await helpers.runClaimableTicketCaseStep(page, {step});"
        elif _tc and await sync_to_async(is_transfer_ui_helper_case)(_tc):
            template = "await helpers.runTransferTicketCaseStep(page, {step});"
        elif _tc and await sync_to_async(is_processing_handover_helper_case)(_tc):
            template = "await helpers.runProcessingHandoverCaseStep(page, {step});"
        elif _tc and await sync_to_async(is_complete_ticket_ui_case)(_tc):
            template = "await helpers.runCompleteTicketCaseStep(page, {step});"
        elif template and "runClaimableTicketCaseStep" in template and not (
            _tc and await sync_to_async(is_transfer_ready_ticket_case)(_tc)
        ):
            template = None
    except Exception:
        template = None
    if not template:
        yield {
            "type": "_auto_run_bundle",
            # No fixed helper is not a test-step failure. Let the AI continue
            # the case instead of treating a missing optimization as evidence.
            "output": "",
            "last_step": int(from_step) - 1,
            "no_template": True,
        }
        return
    step_names = await sync_to_async(_get_testcase_step_names)(int(test_case_id))
    chunks: list[str] = []
    last_step = int(from_step)
    prev_status, prev_reason = _extract_helper_result_reason(previous_output)
    prev_step = int(from_step) - 1 if int(from_step) > 1 else None
    if previous_output and not prev_status and prev_step:
        prev_status, prev_reason = "PASS", "已完成"

    if previous_output and prev_step:
        prev_display = (
            previous_output
            if isinstance(previous_output, str)
            else json.dumps(previous_output, ensure_ascii=False)
        )
        await _persist_helper_step_to_checkpoint(
            agent=agent,
            invoke_config=invoke_config,
            commentary=_build_helper_step_commentary(
                next_step=prev_step,
                next_action=step_names.get(prev_step, f"步骤{prev_step}"),
            ),
            tool_output=prev_display,
            step=prev_step,
            command=template.format(
                step=prev_step
            ),
        )

    for step in range(int(from_step), int(to_step) + 1):
        commentary = _build_helper_step_commentary(
            prev_step=prev_step if step == int(from_step) else step - 1,
            prev_status=prev_status,
            prev_reason=prev_reason,
            next_step=step,
            next_action=step_names.get(step, f"步骤{step}"),
        )
        if commentary:
            yield {"type": "stream", "data": f"\n{commentary}\n"}
        yield {
            "type": "testcase_progress",
            "session_id": session_id,
            "test_case_id": int(test_case_id),
            "step": step,
            "total_steps": int(to_step),
            "state": "running",
            "message": f"正在执行步骤 {step}/{to_step}",
        }
        await asyncio.sleep(0.05)
        output = await sync_to_async(auto_run_missing_testcase_helper_steps)(
            test_case_id=int(test_case_id),
            user_id=int(user_id),
            project_id=int(project_id),
            chat_session_id=str(session_id),
            browser_session_id=f"case_{int(test_case_id)}",
            from_step=step,
            to_step=step,
            helper_call_template=template,
            total_steps=int(to_step),
        )
        if isinstance(output, dict):
            plain = output.get("plain") or ""
            display = output.get("display") or plain
        else:
            plain = output or ""
            display = plain
        chunks.append(plain)
        last_step = step
        yield {
            "type": "tool_result",
            "tool_name": "execute_skill_script",
            "tool_output": display,
            "summary": f"已执行步骤 {step}/{to_step}",
            "step": step,
        }
        command = template.format(
            step=step
        )
        await _persist_helper_step_to_checkpoint(
            agent=agent,
            invoke_config=invoke_config,
            commentary=commentary,
            tool_output=display if isinstance(display, str) else json.dumps(display, ensure_ascii=False),
            step=step,
            command=command,
        )
        prev_status, prev_reason = _extract_helper_result_reason(plain)
        if not prev_status and re.search(
            r"timed out|浏览器已重置|执行异常",
            plain,
            flags=re.IGNORECASE,
        ):
            prev_status, prev_reason = "FAIL", "执行超时，未完成截图"
        prev_step = step
        await asyncio.sleep(0.05)
        if prev_status == "FAIL" or re.search(
            r"RESULT\s*=\s*FAIL\b|Playwright exec timed out|浏览器已重置",
            plain,
            flags=re.IGNORECASE,
        ):
            fail_line = _build_helper_step_commentary(
                prev_step=step,
                prev_status="FAIL",
                prev_reason=prev_reason,
            )
            if fail_line:
                yield {"type": "stream", "data": f"\n{fail_line}\n"}
            break
    yield {
        "type": "_auto_run_bundle",
        "output": "\n".join(chunks).strip(),
        "last_step": last_step,
    }


def _build_incomplete_ai_resume_prompt(*, next_step: int, total_steps: int, step_details: dict[int, dict[str, str]], test_case_id: int, attempted_step: Optional[int] = None) -> str:
    remaining = "\n".join(
        (
            f"{number}. 用例原始步骤：{step_details.get(number, {}).get('description', f'步骤{number}')}\n"
            f"   预期结果：{step_details.get(number, {}).get('expected_result') or '未填写'}"
        )
        for number in range(next_step, total_steps + 1)
    )
    recovery_instruction = (
        f"步骤 {next_step} 已有浏览器调用或截图，但没有断言回执。先只读检查当前页面并核对该步骤预期；"
        "禁止重复点击、提交、创建、删除、发送、领取、审批等可能改变数据的动作。"
        "确认符合预期后，在这次浏览器工具输出中明确写 `RESULT=PASS: 步骤N...`；不符合或无法核实时写 `RESULT=FAIL: 步骤N...` 并说明实际状态。"
        if attempted_step == next_step
        else f"从第一条尚未尝试的步骤 {next_step} 开始按顺序实际操作；每步确认页面状态并保存该步截图。"
    )
    return (
        "【用例步骤续跑指令】上一轮提前结束，以下步骤尚未取得通过回执。\n"
        f"请在现有浏览器会话 session_id=case_{test_case_id} 中继续，不要重新登录或新建浏览器。\n"
        f"{recovery_instruction}\n"
        "以下步骤原文和预期结果来自测试用例，是本次执行的唯一数据来源。工单号、账号、筛选值、名称等字面值必须逐字照用；禁止根据日期、历史会话或模型推测替换/编造。\n"
        "执行输入步骤后，必须读取对应输入框的实际 value 并与用例原文核对；不能只因 fill helper 没报错就判通过。\n"
        "不要依据之前的文字报告推断步骤已经完成，不要编造操作结果、截图或通过标记。\n"
        "每次 execute_skill_script 必须传 step_number=当前用例步骤编号，包含只读补验调用。回执格式为 RESULT=PASS/FAIL: 步骤N...，N 必须是当前步骤号。"
        "每一步必须由浏览器工具输出明确的 RESULT=PASS 或 RESULT=FAIL 回执；截图和助手文字不能代替回执。所有列出的步骤都有对应工具执行结果后，才可以输出最终测试报告；遇到操作失败就停止并报告真实错误。\n"
        f"尚待执行步骤（共 {total_steps} 步）：\n{remaining}"
    )


def _build_incomplete_testcase_resume_prompt(*, done_step: int, total_steps: int) -> str:
    next_step = done_step + 1
    return (
        f"【强制继续】用例共 {total_steps} 步，目前只完成到步骤 {done_step}，"
        f"步骤 {next_step}～{total_steps} 尚未执行，也没有对应 `[CASE_SCREENSHOT]`。\n"
        f"禁止输出测试报告，禁止总结，禁止跳步。\n"
        f"立即按本用例已选定的执行脚本调用 `execute_skill_script`，"
        f"参数步骤号必须是 {next_step}；无法确认脚本类型时停止并报告。\n"
        f"工具输出必须含步骤 {next_step} 的 `[CASE_SCREENSHOT]` 与 RESULT=PASS/FAIL。"
    )


def _extract_testcase_step_from_tool_call(tool_call: Any) -> Optional[int]:
    """Return the declared testcase step for a Playwright tool call.

    This is deliberately based on the executable helper call rather than the
    model's prose.  A message such as “I will execute step 1” must not make the
    UI look as if the browser has started executing it.
    """
    if isinstance(tool_call, dict):
        name = str(tool_call.get("name") or "")
        args = tool_call.get("args") or tool_call.get("arguments") or {}
    else:
        name = str(getattr(tool_call, "name", "") or "")
        args = getattr(tool_call, "args", None) or getattr(tool_call, "arguments", None) or {}
    if name != "execute_skill_script":
        return None
    if isinstance(args, str):
        command = args
    elif isinstance(args, dict):
        declared_step = args.get("step_number")
        if isinstance(declared_step, int) and declared_step > 0:
            return declared_step
        command = str(args.get("command") or "")
    else:
        command = ""

    if re.search(r"\bloginStep1\s*\(", command, flags=re.IGNORECASE):
        return 1
    patterns = (
        r"run(?:ClaimableTicket|TransferTicket|ProcessingHandover|CompleteTicket|TicketDetail|Overview|Notification)?CaseStep\s*\(\s*page\s*,\s*(\d+)",
        r"screenshotCaseStep\s*\(\s*page\s*,\s*(\d+)",
        r"case[_-]\d+_step(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, command, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


async def _emit_testcase_tool_started(
    tool_calls: Any, *, session_id: str, test_case_id: Optional[int]
) -> list[dict[str, Any]]:
    """Build authoritative progress events for Playwright execution starts."""
    if not test_case_id:
        return []
    total_steps = await sync_to_async(_get_testcase_step_count)(int(test_case_id))
    events: list[dict[str, Any]] = []
    for tool_call in tool_calls or []:
        step = _extract_testcase_step_from_tool_call(tool_call)
        if step is not None:
            events.append(
                {
                    "type": "testcase_progress",
                    "session_id": session_id,
                    "test_case_id": int(test_case_id),
                    "step": step,
                    "total_steps": total_steps,
                    "state": "running",
                    "message": "正在等待浏览器资源并执行步骤",
                }
            )
    return events


def process_mcp_tool_output(content: Any) -> tuple:
    """
    处理 MCP 工具返回的内容，提取实际数据并生成摘要

    Args:
        content: 工具返回的原始内容

    Returns:
        tuple: (processed_content, summary)
    """
    # 处理 MCP 工具返回的列表格式，提取 text 内容
    if isinstance(content, list) and len(content) == 1:
        first_item = content[0]
        if isinstance(first_item, dict) and first_item.get("type") == "text":
            text_content = first_item.get("text")
            if text_content is not None:
                content = text_content
            # text 为 None 或空时保留原始列表格式

    # 确保 content 可序列化
    if content is None:
        content = ""
    elif not isinstance(content, (str, dict, list, int, float, bool)):
        content = str(content)

    # 生成摘要
    if isinstance(content, str):
        summary = content[:200]
    else:
        try:
            summary = json.dumps(content, ensure_ascii=False)[:200]
        except (TypeError, ValueError):
            summary = str(content)[:200]

    return content, summary


def _build_sanitized_messages(messages: List[Any]) -> tuple[List[Any], int]:
    """
    构建合法的消息列表：
    1) 为缺失 ToolMessage 响应的 tool_call 插入占位 ToolMessage
    2) 清理悬空的 ToolMessage（无匹配 tool_call）
    3) 清理有问题的 ToolMessage（content 非字符串 / 过长 / 含 base64）
    返回 (clean_messages, fix_count)
    """
    result: List[Any] = []
    fix_count = 0

    # 当前 pending 的 tool_call IDs 及其工具名
    pending_call_ids: List[str] = []
    pending_call_names: Dict[str, str] = {}

    def _copy_message(message: AIMessage) -> AIMessage:
        if hasattr(message, "model_copy"):
            return message.model_copy(deep=True)
        return message.copy(deep=True)

    def _normalize_ai_tool_calls(message: AIMessage) -> tuple[AIMessage, List[Dict[str, Any]], int]:
        """Normalize parsed and OpenAI-compatible raw tool-call representations."""
        parsed_calls = list(getattr(message, "tool_calls", None) or [])
        additional_kwargs = getattr(message, "additional_kwargs", None) or {}
        raw_calls = (
            additional_kwargs.get("tool_calls", [])
            if isinstance(additional_kwargs, dict)
            else []
        )
        source_calls = parsed_calls or raw_calls
        if not source_calls:
            return message, [], 0

        normalized_calls: List[Dict[str, Any]] = []
        raw_normalized_calls: List[Dict[str, Any]] = []
        generated_ids = 0

        for index, call in enumerate(source_calls):
            call_data = call if isinstance(call, dict) else {}
            raw_function = call_data.get("function")
            raw_function = raw_function if isinstance(raw_function, dict) else {}
            name = call_data.get("name") or raw_function.get("name") or "unknown"
            args = call_data.get("args", raw_function.get("arguments", {}))
            if isinstance(args, str):
                try:
                    parsed_args = json.loads(args)
                    args = parsed_args if isinstance(parsed_args, dict) else {}
                except (TypeError, ValueError):
                    args = {}
            elif not isinstance(args, dict):
                args = {}

            call_id = call_data.get("id")
            if not call_id:
                # A deterministic ID keeps a repaired checkpoint valid across retries.
                seed = "%s:%d:%s:%s" % (
                    getattr(message, "id", "") or "history",
                    index,
                    name,
                    json.dumps(args, ensure_ascii=True, sort_keys=True),
                )
                call_id = "history_tool_" + uuid.uuid5(uuid.NAMESPACE_URL, seed).hex
                generated_ids += 1
            call_id = str(call_id)

            normalized_calls.append({"id": call_id, "name": str(name), "args": args})
            raw_normalized_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": str(name),
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                }
            )

        normalized_message = _copy_message(message)
        normalized_message.tool_calls = normalized_calls
        normalized_message.additional_kwargs = dict(additional_kwargs)
        # ChatOpenAI serializes this raw representation when present, so it must
        # carry the same valid IDs as the parsed tool_calls field.
        normalized_message.additional_kwargs["tool_calls"] = raw_normalized_calls
        if hasattr(normalized_message, "invalid_tool_calls"):
            normalized_message.invalid_tool_calls = []
        return normalized_message, normalized_calls, generated_ids

    def _flush_pending() -> None:
        nonlocal fix_count
        for tc_id in pending_call_ids:
            result.append(
                ToolMessage(
                    content="[Tool execution was interrupted]",
                    tool_call_id=tc_id,
                    name=pending_call_names.get(tc_id, "unknown"),
                )
            )
            fix_count += 1
        pending_call_ids.clear()
        pending_call_names.clear()

    def _is_tool_content_problematic(msg: ToolMessage) -> bool:
        content = getattr(msg, "content", None)
        if content is None or not isinstance(content, str):
            return True
        if len(content) > _MAX_SAFE_TOOL_MESSAGE_CHARS:
            return True
        if "data:image/" in content and "base64," in content:
            return True
        return False

    for msg in messages:
        # AI tool calls may live in either tool_calls or OpenAI-compatible
        # additional_kwargs["tool_calls"]. Normalize both before they reach a model.
        if isinstance(msg, AIMessage):
            normalized_msg, tool_calls, generated_ids = _normalize_ai_tool_calls(msg)
            if not tool_calls:
                result.append(msg)
                continue

            _flush_pending()
            result.append(normalized_msg)
            fix_count += generated_ids

            for tc in tool_calls:
                tc_id = str(tc["id"])
                pending_call_ids.append(tc_id)
                pending_call_names[tc_id] = tc.get("name") or "unknown"
            continue

        # ToolMessage 消息
        if isinstance(msg, ToolMessage):
            tc_id = str(getattr(msg, "tool_call_id", "") or "")
            # Older streamed responses can omit the tool result ID. When there is
            # exactly one outstanding call it is unambiguous and can be repaired.
            if tc_id not in pending_call_ids and len(pending_call_ids) == 1:
                tc_id = pending_call_ids[0]
                msg = ToolMessage(
                    content=getattr(msg, "content", ""),
                    tool_call_id=tc_id,
                    name=getattr(msg, "name", None)
                    or pending_call_names.get(tc_id, "unknown"),
                )
                fix_count += 1
            if tc_id in pending_call_ids:
                pending_call_ids.remove(tc_id)
                if _is_tool_content_problematic(msg):
                    result.append(
                        ToolMessage(
                            content="[Tool output removed: content was invalid or too large]",
                            tool_call_id=tc_id,
                            name=getattr(msg, "name", None)
                            or pending_call_names.get(tc_id, "unknown"),
                        )
                    )
                    fix_count += 1
                else:
                    result.append(msg)
            else:
                # 悬空 ToolMessage，丢弃
                fix_count += 1
            continue

        # 其他消息类型
        _flush_pending()
        result.append(msg)

    _flush_pending()
    return result, fix_count


async def _sanitize_history_before_model_call(
    agent: Any,
    invoke_config: Dict[str, Any],
    log_prefix: str,
) -> Dict[str, Any]:
    """
    统一历史修复入口：读取状态，构建合法消息列表，若有修复则用 REMOVE_ALL + 完整列表覆写。
    """
    try:
        current_state = await agent.aget_state(invoke_config)
    except Exception as e:
        logger.warning("%s: Failed to load state for sanitize: %s", log_prefix, e)
        return {"removed_count": 0, "sanitized": False}

    values = (
        current_state.values
        if hasattr(current_state, "values") and current_state.values
        else {}
    )
    messages = values.get("messages", []) if isinstance(values, dict) else []
    if not messages:
        return {"removed_count": 0, "sanitized": False}

    clean_msgs, fix_count = _build_sanitized_messages(messages)
    if fix_count == 0:
        return {"removed_count": 0, "sanitized": False}

    # 用 REMOVE_ALL + 完整干净列表替换状态
    update_payload = [RemoveMessage(id="__remove_all__"), *clean_msgs]

    available_nodes = list(getattr(agent, "nodes", {}).keys())
    preferred_nodes = [n for n in ("model", "agent", "tools") if n in available_nodes]
    if available_nodes and available_nodes[0] not in preferred_nodes:
        preferred_nodes.append(available_nodes[0])
    preferred_nodes.append(None)

    success = False
    last_error: Optional[Exception] = None
    for as_node in preferred_nodes:
        try:
            if as_node is None:
                await agent.aupdate_state(invoke_config, {"messages": update_payload})
            else:
                await agent.aupdate_state(
                    invoke_config, {"messages": update_payload}, as_node=as_node
                )
            success = True
            logger.info(
                "%s: Sanitized history via REMOVE_ALL, fixed %d issues, %d clean messages (as_node=%s)",
                log_prefix,
                fix_count,
                len(clean_msgs),
                as_node or "auto",
            )
            break
        except Exception as e:
            last_error = e

    if not success:
        logger.error(
            "%s: Failed to sanitize history. fix_count=%d, available_nodes=%s, error=%s",
            log_prefix,
            fix_count,
            available_nodes,
            last_error,
        )
        return {"removed_count": 0, "sanitized": False}

    return {"removed_count": fix_count, "sanitized": True}


def calculate_context_tokens(
    messages: List[Any],
    model_name: str = "gpt-4o",
    tools: Optional[list] = None,
    system_prompt: Optional[str] = None,
) -> tuple[int, int, int]:
    """
    计算当前上下文 Token

    优先使用最后一条带 usage_metadata 的消息；
    如果 provider 未返回 usage_metadata，则回退到内容估算 + 真实开销。

    Args:
        messages: 消息列表
        model_name: 模型名称
        tools: 工具对象列表，用于精确计算开销
        system_prompt: 系统提示词，用于精确计算开销
    """
    # 1) 优先使用 usage_metadata（最准确）
    for msg in reversed(messages):
        if hasattr(msg, "usage_metadata") and msg.usage_metadata:
            usage = msg.usage_metadata
            input_tokens = usage.get("input_tokens", 0) or 0
            output_tokens = usage.get("output_tokens", 0) or 0
            total_tokens = usage.get("total_tokens", 0) or (
                input_tokens + output_tokens
            )

            if total_tokens > 0:
                return input_tokens, output_tokens, total_tokens

    # 2) 回退到内容估算 + 真实开销（避免 context_update 始终为 0）
    from orchestrator_integration.middleware_config import _calculate_overhead_tokens

    overhead = _calculate_overhead_tokens(model_name, tools, system_prompt)

    content_tokens = 0
    for msg in messages:
        if hasattr(msg, "content") and msg.content:
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            content_tokens += context_checker.count_tokens(content, model_name)

    estimated_total = content_tokens + overhead
    return 0, 0, estimated_total


def _is_unreliable_default_detected_limit(
    model_name: str, detected_limit: Optional[int]
) -> bool:
    """
    判断检测上限是否仅来自未知模型的默认回退值（如 128000）。
    该场景下应优先信任用户配置的 context_limit。
    """
    if not isinstance(detected_limit, int) or detected_limit <= 0:
        return False

    default_limit = int(MODEL_CONTEXT_LIMITS.get("default", 128000))
    if detected_limit != default_limit:
        return False

    normalized_name = (model_name or "").lower()
    for model_key in MODEL_CONTEXT_LIMITS.keys():
        if model_key == "default":
            continue
        if model_key in normalized_name:
            return False

    return True


def resolve_runtime_context_limit(
    config_context_limit: Optional[int], llm, model_name: str
) -> int:
    """
    运行时上下文限制（与 middleware_config 保持一致，用户优先）：
    - 用户配置存在时：直接使用 config
    - 无 config 时：profile > 可靠 detected > 默认值
    """
    config_limit = (
        config_context_limit
        if isinstance(config_context_limit, int) and config_context_limit > 0
        else None
    )
    detected_limit = (
        get_context_limit_from_llm(llm, fallback_model_name=model_name)
        if llm is not None
        else None
    )

    profile_limit = None
    if llm is not None:
        profile = getattr(llm, "profile", None)
        if isinstance(profile, dict):
            max_input_tokens = profile.get("max_input_tokens")
            if isinstance(max_input_tokens, int) and max_input_tokens > 0:
                profile_limit = max_input_tokens

    unreliable_detected_limit = _is_unreliable_default_detected_limit(
        model_name, detected_limit
    )

    if config_limit:
        return config_limit

    if profile_limit:
        return profile_limit

    if (
        isinstance(detected_limit, int)
        and detected_limit > 0
        and not unreliable_detected_limit
    ):
        return detected_limit

    return 128000


@method_decorator(csrf_exempt, name="dispatch")
class AgentLoopStreamAPIView(View):
    """
    Agent Loop 聊天 API (LangChain v1 重构版)

    核心特性：
    - 使用 create_agent() 统一创建 Agent
    - SummarizationMiddleware 自动上下文压缩
    - HumanInTheLoopMiddleware 处理 HITL 审批
    - 在流处理层检测工具调用生成步骤事件
    - 支持 stream 参数：
      - stream=true (默认)：返回 SSE 流式响应
      - stream=false：返回普通 JSON 响应
    """

    # 最大步骤数（用于前端显示）
    MAX_STEPS = 500

    def _update_session_token_usage(
        self, session_id: str, input_tokens: int, output_tokens: int,
        cache_read_tokens: int = 0, user_id=None, project_id=None,
    ):
        """更新会话的 Token 使用统计，同时写入独立的 TokenUsageRecord"""
        try:
            from django.db.models import F
            from django.utils import timezone

            ChatSession.objects.filter(session_id=session_id).update(
                total_input_tokens=F("total_input_tokens") + input_tokens,
                total_output_tokens=F("total_output_tokens") + output_tokens,
                total_tokens=F("total_tokens") + input_tokens + output_tokens,
                total_cache_read_tokens=F("total_cache_read_tokens") + cache_read_tokens,
                request_count=F("request_count") + 1,
                updated_at=timezone.now(),
            )
        except Exception as e:
            logger.warning(f"Failed to update session token usage: {e}")

        # 写入独立记录（不随会话删除而丢失）
        try:
            from langgraph_integration.models import TokenUsageRecord

            TokenUsageRecord.objects.create(
                user_id=user_id,
                project_id=project_id,
                session_id=session_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                cache_read_tokens=cache_read_tokens,
            )
        except Exception as e:
            logger.warning(f"Failed to create TokenUsageRecord: {e}")

    async def authenticate_request(self, request):
        """JWT 认证"""
        auth_header = request.META.get("HTTP_AUTHORIZATION")
        if not auth_header or not auth_header.startswith("Bearer "):
            raise AuthenticationFailed("Authentication credentials were not provided.")

        token = auth_header.split(" ")[1]
        jwt_auth = JWTAuthentication()

        try:
            validated_token = await sync_to_async(jwt_auth.get_validated_token)(token)
            user = await sync_to_async(jwt_auth.get_user)(validated_token)
            return user
        except Exception as e:
            raise AuthenticationFailed(f"Invalid token: {str(e)}")

    async def _create_stream_generator(
        self,
        request,
        user_message: str,
        session_id: str,
        project_id: str,
        project: Project,
        knowledge_base_id: Optional[int] = None,
        use_knowledge_base: bool = True,
        prompt_id: Optional[int] = None,
        uploaded_images_base64: Optional[List[str]] = None,
        generate_playwright_script: bool = False,
        test_case_id: Optional[int] = None,
        use_pytest: bool = True,
        file_ids: Optional[List[int]] = None,
        execution_mode: str = "ai_only",
        ui_test_case_id: Optional[int] = None,
    ):
        """
        创建 SSE 流式生成器（LangChain v1 重构版）

        使用 create_agent + astream 模式，替代旧的 AgentOrchestrator 循环。
        通过检测 updates 流中的工具调用来生成 step_start/step_complete 事件。
        """
        thread_id = f"{request.user.id}_{project_id}_{session_id}"
        file_ids = file_ids or []
        start_event_emitted = False
        if test_case_id:
            # 用例执行可能在 MCP/LLM 初始化与造数阶段阻塞较久；尽早下发 session_id，
            # 便于前端在造数完成前打开 LangGraph 窗口查看进度。
            yield create_flushed_sse(
                {
                    "type": "pre_data_start",
                    "test_case_id": int(test_case_id),
                    "session_id": session_id,
                }
            )
        else:
            # Establish the stream before attachment validation, MCP discovery,
            # or model initialization so the UI can render an active task and
            # the Stop action has a stable session id immediately.
            yield create_flushed_sse(
                {
                    "type": "start",
                    "session_id": session_id,
                    "thread_id": thread_id,
                    "project_id": project_id,
                    "display_message": user_message,
                    "mode": "agent_loop",
                    "created_at": None,
                }
            )
            start_event_emitted = True
        llm_attachment_context = ""
        if not (test_case_id and execution_mode == "script_only"):
            try:
                attached_files = await sync_to_async(validate_file_ids)(file_ids, project, request.user)
                llm_attachment_context = await sync_to_async(build_llm_attachment_context)(attached_files)
                if file_ids:
                    await sync_to_async(sync_file_references)(file_ids, project, FileReference.REF_LLM_CHAT, session_id, request.user)
            except Exception as exc:
                yield create_sse_data({"type": "error", "message": f"附件校验失败: {exc}"})
                return

        # Script-only is a distinct execution route: prepare data first, then
        # invoke the bound recorder case. It deliberately returns before LLM,
        # MCP, knowledge-tool, and Agent initialization.
        if test_case_id and (
            execution_mode == "script_only"
            or (execution_mode == "hybrid" and ui_test_case_id)
        ):
            if execution_mode == "script_only" and not ui_test_case_id:
                yield create_sse_data({
                    "type": "error",
                    "message": "仅执行 UI 自动化脚本需要先绑定 UI 自动化用例",
                })
                return

            prompt_obj = None
            from langgraph_integration.models import ChatMessage

            if prompt_id:
                try:
                    prompt_obj = await sync_to_async(UserPrompt.objects.get)(
                        id=prompt_id, user=request.user, is_active=True
                    )
                except UserPrompt.DoesNotExist:
                    pass
            chat_session, _ = await sync_to_async(ChatSession.objects.get_or_create)(
                session_id=session_id,
                defaults={
                    "user": request.user,
                    "project": project,
                    "prompt": prompt_obj,
                    "title": f"新对话 - {user_message[:30]}",
                },
            )
            from data_generation.testcase_pre_data import run_testcase_pre_data_by_id
            from testcases.run_record_service import (
                block_testcase_run_record,
                start_testcase_run_record,
            )

            try:
                pre_data_result = await sync_to_async(run_testcase_pre_data_by_id)(
                    int(test_case_id), user_id=request.user.id
                )
            except Exception as exc:
                logger.exception(
                    "AgentLoopStreamAPI: script-only pre-data failed, case=%s",
                    test_case_id,
                )
                yield create_sse_data({"type": "error", "message": f"前置数据准备异常：{exc}"})
                return

            if pre_data_result.blocked:
                display_message = await sync_to_async(
                    _build_testcase_execution_display_message
                )(int(test_case_id))
                await sync_to_async(start_testcase_run_record)(
                    testcase_id=int(test_case_id),
                    user_id=request.user.id,
                    session_id=session_id,
                    generate_playwright_script=bool(generate_playwright_script),
                    execution_mode=execution_mode,
                    ui_test_case_id=ui_test_case_id,
                )
                record = await sync_to_async(block_testcase_run_record)(
                    session_id=session_id, reason=pre_data_result.block_message
                )
                terminal_message = (
                    "测试数据准备失败，已中止执行。\n\n"
                    f"{pre_data_result.block_message}"
                )

                def persist_blocked_messages():
                    ChatMessage.objects.get_or_create(
                        session=chat_session,
                        user=request.user,
                        message_id=f"predata-fail-human-{session_id}",
                        defaults={
                            "role": "human",
                            "content": display_message or user_message,
                            "metadata": {"source": "pre_data_blocked"},
                        },
                    )
                    ChatMessage.objects.update_or_create(
                        session=chat_session,
                        user=request.user,
                        message_id=f"predata-fail-ai-{session_id}",
                        defaults={
                            "role": "ai",
                            "content": terminal_message,
                            "metadata": {
                                "source": "pre_data_blocked",
                                "run_record_id": getattr(record, "id", None),
                                "uuid": str(uuid.uuid4()),
                            },
                        },
                    )

                await sync_to_async(persist_blocked_messages)()
                yield create_sse_data({
                    "type": "start", "session_id": session_id,
                    "thread_id": session_id, "project_id": project_id,
                    "display_message": display_message,
                    "mode": "ui_script_only" if execution_mode == "script_only" else "agent_loop",
                    "created_at": chat_session.created_at.isoformat() if chat_session.created_at else None,
                })
                yield create_sse_data({"type": "stream", "data": terminal_message, "pre_data_status": "failed"})
                yield create_sse_data({
                    "type": "complete", "total_steps": 0,
                    "pre_data_status": "failed",
                    "test_case_run": {
                        "status": record.status if record else "error",
                        "summary": record.summary if record else terminal_message,
                    },
                })
                yield "data: [DONE]\n\n"
                return

            pre_data_run = pre_data_result.run
            if pre_data_run is not None:
                plan_name = await sync_to_async(
                    lambda: pre_data_run.plan.name if pre_data_run.plan_id else ""
                )()
                yield create_sse_data({
                    "type": "pre_data_complete",
                    "status": pre_data_run.status,
                    "plan_name": plan_name,
                    "source": pre_data_result.resolution.source,
                })

            display_message = await sync_to_async(
                _build_testcase_execution_display_message
            )(int(test_case_id), pre_data_run)
            await sync_to_async(start_testcase_run_record)(
                testcase_id=int(test_case_id),
                user_id=request.user.id,
                session_id=session_id,
                generate_playwright_script=bool(generate_playwright_script),
                execution_mode=execution_mode,
                ui_test_case_id=ui_test_case_id,
                data_generation_run_id=getattr(pre_data_run, "id", None),
            )
            if execution_mode == "hybrid":
                attempt_events = []
                terminal_events = []
                attempt_error = ""
                from testcases.models import TestCaseRunRecord

                # Mark the concrete route before awaiting the browser runner, so a
                # live record never remains indistinguishable from queued work.
                await sync_to_async(
                    lambda: TestCaseRunRecord.objects.filter(
                        session_id=session_id,
                        status=TestCaseRunRecord.STATUS_CHOICES[0][0],
                    ).update(actual_execution_path="bound_ui_script")
                )()
                try:
                    async with asyncio.timeout(_testcase_bound_ui_timeout_seconds()):
                        async for event in _run_testcase_with_bound_ui_script(
                            request=request,
                            project=project,
                            session_id=session_id,
                            thread_id=thread_id,
                            chat_session=chat_session,
                            testcase_id=int(test_case_id),
                            ui_test_case_id=int(ui_test_case_id),
                            display_message=display_message or user_message,
                            data_generation_run=pre_data_run,
                            execution_mode="hybrid",
                            actual_execution_path="bound_ui_script",
                            stream_mode="agent_loop",
                            suppress_failure_report=True,
                            emit_start=True,
                        ):
                            payload = _decode_sse_payload(event)
                            if payload and payload.get("type") in {"stream", "complete"}:
                                terminal_events.append(event)
                            elif isinstance(event, str) and event.strip() == "data: [DONE]":
                                terminal_events.append(event)
                            else:
                                yield event
                except asyncio.CancelledError:
                    from testcases.run_record_service import finalize_testcase_run_record

                    await sync_to_async(finalize_testcase_run_record)(
                        session_id=session_id,
                        error_message="绑定 UI 自动化执行流被中断，未确认用例完成。",
                    )
                    raise
                except Exception as exc:
                    attempt_error = str(exc)[:1000]
                    logger.warning(
                        "AgentLoopStreamAPI: hybrid UI script failed; handing off to Agent, case=%s: %s",
                        test_case_id,
                        attempt_error,
                        exc_info=True,
                    )

                attempt_status = None
                attempt_summary = ""
                for event in terminal_events:
                    payload = _decode_sse_payload(event)
                    if payload and payload.get("type") == "complete":
                        run_result = payload.get("test_case_run") or {}
                        attempt_status = run_result.get("status")
                        attempt_summary = str(run_result.get("summary") or "")
                if attempt_status == "pass":
                    for event in terminal_events:
                        yield event
                    return

                def prepare_agent_fallback():
                    record = TestCaseRunRecord.objects.get(session_id=session_id)
                    script_log = record.execution_log or attempt_summary or attempt_error
                    record.status = "running"
                    record.summary = ""
                    record.step_results = []
                    record.completed_at = None
                    record.injected_report = False
                    record.actual_execution_path = "agent_fallback"
                    record.execution_log = (
                        "[HYBRID_SCRIPT_ATTEMPT]\n" + str(script_log)
                    )[:8000]
                    record.data_usage = {}
                    record.save(update_fields=[
                        "status", "summary", "step_results", "completed_at",
                        "injected_report", "actual_execution_path", "execution_log",
                        "data_usage",
                    ])
                    return record.execution_log

                fallback_trace = await sync_to_async(prepare_agent_fallback)()
                failure_detail = attempt_error or attempt_summary or "绑定 UI 脚本未通过或未返回完成结果。"
                yield create_sse_data({
                    "type": "message",
                    "data": "绑定 UI 脚本未通过，智能双模正在切换 AI Agent 接管。\n"
                    + failure_detail[:1600],
                })
                hybrid_fallback_context = (
                    "\n\n## 智能双模脚本优先阶段结果\n"
                    "绑定的 UI 自动化脚本已执行但未通过；不要将脚本尝试视为功能用例通过。"
                    "现在请由 AI Agent 继续完成原功能用例全部步骤。前置造数已经完成，"
                    "必须复用本次生成的数据，不得再次执行造数。\n"
                    f"脚本失败摘要：{failure_detail[:1600]}\n"
                    f"脚本审计日志：{fallback_trace[-4000:]}"
                )

                async def finalize_hybrid_fallback_error(reason: str):
                    from testcases.run_record_service import finalize_testcase_run_record

                    return await sync_to_async(finalize_testcase_run_record)(
                        session_id=session_id,
                        error_message=f"AI Agent 接管失败：{reason}",
                    )

                hybrid_start_emitted = True
                pre_data_complete_emitted = pre_data_run is not None
            else:
                hybrid_fallback_context = ""
                pre_data_complete_emitted = pre_data_run is not None

            try:
                if execution_mode == "script_only":
                    async with asyncio.timeout(_testcase_bound_ui_timeout_seconds()):
                        async for event in _run_testcase_with_bound_ui_script(
                            request=request,
                            project=project,
                            session_id=session_id,
                            thread_id=thread_id,
                            chat_session=chat_session,
                            testcase_id=int(test_case_id),
                            ui_test_case_id=int(ui_test_case_id),
                            display_message=display_message or user_message,
                            data_generation_run=pre_data_run,
                        ):
                            yield event
            except asyncio.CancelledError:
                from testcases.run_record_service import finalize_testcase_run_record

                await sync_to_async(finalize_testcase_run_record)(
                    session_id=session_id,
                    error_message="UI 自动化执行流被中断，未确认用例完成。",
                )
                raise
            except Exception as exc:
                logger.exception(
                    "AgentLoopStreamAPI: script-only UI execution failed, case=%s ui_case=%s",
                    test_case_id, ui_test_case_id,
                )
                from testcases.run_record_service import finalize_testcase_run_record

                record = await sync_to_async(finalize_testcase_run_record)(
                    session_id=session_id,
                    error_message=f"UI 自动化脚本执行失败：{exc}",
                )
                summary = record.summary if record else f"UI 自动化脚本执行失败：{exc}"
                yield create_sse_data({"type": "stream", "data": summary})
                yield create_sse_data({
                    "type": "complete", "total_steps": len(record.step_results or []) if record else 0,
                    "test_case_run": {
                        "status": record.status if record else "error",
                        "summary": summary,
                        "run_record_id": getattr(record, "id", None),
                    },
                })
                yield "data: [DONE]\n\n"
            if execution_mode == "script_only":
                return

        # 1. 获取 LLM 配置
        try:
            active_config = await sync_to_async(LLMConfig.objects.get)(is_active=True)
            logger.info(f"AgentLoopStreamAPI: Using LLM config: {active_config.name}")
            context_limit = active_config.context_limit or 128000
            model_name = active_config.name or "gpt-4o"
        except LLMConfig.DoesNotExist:
            if locals().get("hybrid_fallback_context"):
                await finalize_hybrid_fallback_error("没有可用的 LLM 配置")
            yield create_sse_data(
                {"type": "error", "message": "No active LLM configuration found"}
            )
            return

        # 2. 验证多模态支持
        if uploaded_images_base64 and not active_config.supports_vision:
            if locals().get("hybrid_fallback_context"):
                await finalize_hybrid_fallback_error(
                    f"模型 {active_config.name} 不支持图片输入"
                )
            yield create_sse_data(
                {
                    "type": "error",
                    "message": f"模型 {active_config.name} 不支持图片输入",
                }
            )
            return

        try:
            # 3. 初始化 LLM
            llm = await sync_to_async(create_llm_instance)(
                active_config, temperature=0.7
            )
            context_limit = resolve_runtime_context_limit(
                active_config.context_limit, llm, model_name
            )

            # 4. 加载 MCP 工具
            tools: List[Any] = []
            try:
                active_mcp_configs = await sync_to_async(list)(
                    RemoteMCPConfig.objects.filter(is_active=True)
                )
                if active_mcp_configs:
                    client_config = {}
                    for cfg in active_mcp_configs:
                        key = cfg.name or f"remote_{cfg.id}"
                        client_config[key] = {
                            "url": cfg.url,
                            "transport": (cfg.transport or "streamable_http").replace(
                                "-", "_"
                            ),
                        }
                        if cfg.headers:
                            client_config[key]["headers"] = cfg.headers

                    if client_config:
                        mcp_timeout = _mcp_tool_load_timeout_seconds()
                        try:
                            mcp_tools = await asyncio.wait_for(
                                mcp_session_manager.get_tools_for_config(
                                    client_config,
                                    user_id=str(request.user.id),
                                    project_id=str(project_id),
                                    session_id=session_id,
                                ),
                                timeout=mcp_timeout,
                            )
                            tools.extend(mcp_tools)
                            logger.info(
                                f"AgentLoopStreamAPI: Loaded {len(mcp_tools)} MCP tools"
                            )
                            yield create_sse_data(
                                {
                                    "type": "info",
                                    "message": f"已加载 {len(mcp_tools)} 个工具",
                                }
                            )
                        except asyncio.TimeoutError:
                            logger.warning(
                                "AgentLoopStreamAPI: MCP tool loading timed out after %ss; continuing without remote tools",
                                mcp_timeout,
                            )
                            try:
                                await asyncio.wait_for(
                                    mcp_session_manager.cleanup_user_session(
                                        str(request.user.id),
                                        str(project_id),
                                        session_id,
                                    ),
                                    timeout=5,
                                )
                            except Exception as cleanup_error:
                                logger.warning(
                                    "AgentLoopStreamAPI: MCP cleanup after timeout failed: %s",
                                    cleanup_error,
                                )
                            yield create_sse_data(
                                {
                                    "type": "warning",
                                    "message": f"MCP 工具加载超时（{mcp_timeout:g}秒），已跳过远程工具",
                                }
                            )
            except Exception as e:
                logger.error(
                    f"AgentLoopStreamAPI: MCP tools loading failed: {e}", exc_info=True
                )
                yield create_sse_data(
                    {"type": "warning", "message": f"MCP 工具加载失败: {str(e)}"}
                )

            # 5. 添加知识库工具
            logger.info(
                f"AgentLoopStreamAPI: 检查知识库工具 - knowledge_base_id={knowledge_base_id}, use_knowledge_base={use_knowledge_base}"
            )
            if knowledge_base_id and use_knowledge_base:
                try:
                    from knowledge.langgraph_integration import create_knowledge_tool

                    logger.info(f"AgentLoopStreamAPI: 正在创建知识库工具...")
                    kb_tool = await sync_to_async(create_knowledge_tool)(
                        knowledge_base_id=knowledge_base_id, user=request.user
                    )
                    tools.append(kb_tool)
                    logger.info(
                        f"AgentLoopStreamAPI: ✅ 知识库工具已添加: {kb_tool.name}"
                    )
                except Exception as e:
                    logger.warning(
                        f"AgentLoopStreamAPI: ❌ Knowledge tool creation failed: {e}",
                        exc_info=True,
                    )
            else:
                logger.info(
                    f"AgentLoopStreamAPI: ⚠️ 跳过知识库工具 (knowledge_base_id={knowledge_base_id}, use_knowledge_base={use_knowledge_base})"
                )

            # 6. 添加内置工具（Playwright 脚本管理等）
            from orchestrator_integration.builtin_tools import get_builtin_tools

            builtin_tools = get_builtin_tools(
                user_id=request.user.id,
                project_id=int(project_id),
                test_case_id=test_case_id,
                chat_session_id=session_id,
                generate_playwright_script=bool(generate_playwright_script),
            )
            tools.extend(builtin_tools)
            logger.info(f"AgentLoopStreamAPI: Added {len(builtin_tools)} builtin tools")

            # 6.5 用例管理单条执行：未指定 prompt 时自动使用「测试用例执行」提示词
            if test_case_id and not prompt_id:
                exec_prompt = await sync_to_async(UserPrompt.get_user_prompt_by_type)(
                    request.user, PromptType.TEST_CASE_EXECUTION
                )
                if exec_prompt:
                    prompt_id = exec_prompt.id
                    logger.info(
                        "AgentLoopStreamAPI: Auto-selected test_case_execution prompt %s",
                        prompt_id,
                    )

            # 7. 获取或创建 ChatSession（使用 get_or_create 避免竞态条件）
            prompt_obj = None
            if prompt_id:
                try:
                    prompt_obj = await sync_to_async(UserPrompt.objects.get)(
                        id=prompt_id, user=request.user, is_active=True
                    )
                except UserPrompt.DoesNotExist:
                    pass

            chat_session, created = await sync_to_async(
                ChatSession.objects.get_or_create
            )(
                session_id=session_id,
                defaults={
                    "user": request.user,
                    "project": project,
                    "prompt": prompt_obj,
                    "title": f"新对话 - {user_message[:30]}",
                },
            )
            if created:
                logger.info(
                    f"AgentLoopStreamAPI: Created new ChatSession: {session_id}"
                )

            # 8. 获取系统提示词
            effective_prompt, prompt_source = await get_effective_system_prompt_async(
                request.user, prompt_id, project
            )
            if locals().get("hybrid_fallback_context"):
                effective_prompt = (effective_prompt or "") + hybrid_fallback_context

            # 8.1 如果需要生成脚本，追加脚本生成指令
            if generate_playwright_script:
                effective_prompt = (
                    effective_prompt or ""
                ) + PLAYWRIGHT_SCRIPT_INSTRUCTION
                logger.info(f"AgentLoopStreamAPI: 已追加脚本生成指令")

            # 8.15 用例管理单条执行：自动造数准备前置数据
            pre_data_result = locals().get("pre_data_result")
            pre_data_run = getattr(pre_data_result, "run", None)
            pre_data_run_id = getattr(pre_data_run, "id", None)
            effective_user_message = user_message
            if test_case_id:
                if pre_data_result is None:
                    from data_generation.testcase_pre_data import run_testcase_pre_data_by_id

                    pre_data_result = await sync_to_async(run_testcase_pre_data_by_id)(
                        int(test_case_id),
                        user_id=request.user.id,
                    )
                if pre_data_result.blocked:
                    # 数据准备可能在常规 start 事件之前中止。先发出标准 start，
                    # 让前端建立流状态并显示随后返回的失败原因，避免只留下用户消息。
                    display_message = await sync_to_async(
                        _build_testcase_execution_display_message
                    )(int(test_case_id))
                    terminal_message = (
                        "测试数据准备失败，已中止执行。\n\n"
                        f"{pre_data_result.block_message}"
                    )
                    # 前置数据失败同样是一轮可追溯的用例执行结果。落库后，
                    # 即使客户端在刷新会话历史时清除了本地流文本，也能从完成
                    # 事件和执行记录中稳定展示失败卡片。
                    from testcases.run_record_service import (
                        block_testcase_run_record,
                        start_testcase_run_record,
                    )

                    await sync_to_async(start_testcase_run_record)(
                        testcase_id=int(test_case_id),
                        user_id=request.user.id,
                        session_id=session_id,
                        generate_playwright_script=bool(generate_playwright_script),
                        execution_mode=execution_mode,
                        ui_test_case_id=ui_test_case_id,
                        data_generation_run_id=None,
                    )
                    record = await sync_to_async(block_testcase_run_record)(
                        session_id=session_id,
                        reason=pre_data_result.block_message,
                    )

                    # 造数失败时 agent/checkpointer 不会落消息；直接写入 ChatMessage，
                    # 避免前端刷新历史后只剩本地用户气泡、助手内容空白。
                    def _persist_pre_data_failure_messages() -> None:
                        import uuid

                        from langgraph_integration.models import ChatMessage

                        ChatMessage.objects.get_or_create(
                            session=chat_session,
                            user=request.user,
                            message_id=f"predata-fail-human-{session_id}",
                            defaults={
                                "role": "human",
                                "content": display_message or user_message,
                                "metadata": {"source": "pre_data_blocked"},
                            },
                        )
                        ChatMessage.objects.update_or_create(
                            session=chat_session,
                            user=request.user,
                            message_id=f"predata-fail-ai-{session_id}",
                            defaults={
                                "role": "ai",
                                "content": terminal_message,
                                "metadata": {
                                    "source": "pre_data_blocked",
                                    "run_record_id": getattr(record, "id", None),
                                    "uuid": str(uuid.uuid4()),
                                },
                            },
                        )

                    await sync_to_async(_persist_pre_data_failure_messages)()

                    yield create_sse_data(
                        {
                            "type": "start",
                            "session_id": session_id,
                            "thread_id": session_id,
                            "project_id": project_id,
                            "display_message": display_message,
                            "mode": "agent_loop",
                            "created_at": chat_session.created_at.isoformat()
                            if chat_session and chat_session.created_at
                            else None,
                        }
                    )
                    # 失败原因作为常规助手消息流返回。前端对 stream/complete
                    # 事件的渲染路径已用于所有正常对话，避免 error 事件在会话
                    # 尚未落盘时被丢弃而只留下用户消息。
                    yield create_sse_data(
                        {
                            "type": "stream",
                            "data": terminal_message,
                            "pre_data_status": "failed",
                        }
                    )
                    yield create_sse_data(
                        {
                            "type": "complete",
                            "total_steps": 0,
                            "pre_data_status": "failed",
                            "test_case_run": {
                                "status": record.status if record else "fail",
                                "summary": record.summary if record else "",
                            },
                        }
                    )
                    yield "data: [DONE]\n\n"
                    return

                if pre_data_result.run is not None:
                    pre_data_run = pre_data_result.run
                    pre_data_run_id = pre_data_result.run.id
                    # A reused run may not have its related plan cached. Resolve
                    # the relation off the async event loop before building SSE.
                    pre_data_plan_name = await sync_to_async(
                        lambda: pre_data_run.plan.name if pre_data_run.plan_id else ""
                    )()
                    if not locals().get("pre_data_complete_emitted", False):
                        yield create_sse_data(
                            {
                                "type": "pre_data_complete",
                                "status": pre_data_result.run.status,
                                "plan_name": pre_data_plan_name,
                                "source": pre_data_result.resolution.source,
                            }
                        )
                        pre_data_complete_emitted = True

                # 单条用例执行只接受数据库中的最新用例详情。前端消息可能来自
                # 列表缓存，若再次携带前置条件或步骤会与最新记录冲突。
                effective_user_message = (
                    f'执行用例管理功能测试用例 ID={int(test_case_id)}。'
                    '以下注入内容是唯一有效的执行上下文。'
                )
                if pre_data_result.message_suffix:
                    effective_user_message += pre_data_result.message_suffix

                from data_generation.testcase_pre_data import (
                    build_testcase_navigation_hint_by_id,
                    build_testcase_detail_suffix_by_id,
                )

                detail_suffix = await sync_to_async(build_testcase_detail_suffix_by_id)(
                    int(test_case_id)
                )
                if detail_suffix:
                    effective_user_message = effective_user_message + detail_suffix

                nav_hint = await sync_to_async(build_testcase_navigation_hint_by_id)(
                    int(test_case_id)
                )
                if nav_hint:
                    effective_user_message = effective_user_message + nav_hint

                from data_generation.testcase_pre_data import (
                    is_claimable_ticket_detail_case,
                    is_complete_ticket_ui_case,
                    is_overview_sla_detail_case,
                    is_processing_handover_helper_case,
                    is_transfer_ui_helper_case,
                    processing_handover_flow_kind,
                )
                from testcases.models import TestCase as ManualTestCaseModel

                sla_case = await sync_to_async(
                    ManualTestCaseModel.objects.filter(id=int(test_case_id)).first
                )()
                # 注意：is_overview_sla_detail_case 内部会惰性查询 testcase.steps，
                # 必须在 sync_to_async 线程中执行，否则抛 SynchronousOnlyOperation。
                is_sla_detail = bool(
                    sla_case
                    and await sync_to_async(is_overview_sla_detail_case)(sla_case)
                )
                is_claimable = bool(
                    sla_case
                    and await sync_to_async(is_claimable_ticket_detail_case)(sla_case)
                )
                is_transfer_ui = bool(
                    sla_case
                    and await sync_to_async(is_transfer_ui_helper_case)(sla_case)
                )
                is_processing_handover = bool(
                    sla_case
                    and await sync_to_async(is_processing_handover_helper_case)(sla_case)
                )
                is_complete_ticket = bool(
                    sla_case
                    and await sync_to_async(is_complete_ticket_ui_case)(sla_case)
                )
                if is_sla_detail:
                    effective_prompt = (effective_prompt or "") + OVERVIEW_SLA_DETAIL_EXECUTION_HINT
                    if nav_hint:
                        effective_prompt = effective_prompt + nav_hint
                elif is_claimable:
                    effective_prompt = (effective_prompt or "") + CLAIMABLE_PENDING_EXECUTION_HINT
                    if nav_hint:
                        effective_prompt = effective_prompt + nav_hint
                elif is_transfer_ui:
                    effective_prompt = (effective_prompt or "") + TRANSFER_UI_EXECUTION_HINT
                    if nav_hint:
                        effective_prompt = effective_prompt + nav_hint
                elif is_processing_handover:
                    flow_kind = await sync_to_async(processing_handover_flow_kind)(sla_case)
                    effective_prompt = (effective_prompt or "") + (
                        PENDING_NO_HANDOVER_EXECUTION_HINT
                        if flow_kind == "pending_no_handover"
                        else PROCESSING_HANDOVER_EXECUTION_HINT
                    )
                    if nav_hint:
                        effective_prompt = effective_prompt + nav_hint
                elif is_complete_ticket:
                    effective_prompt = (effective_prompt or "") + COMPLETE_TICKET_UI_EXECUTION_HINT
                    if nav_hint:
                        effective_prompt = effective_prompt + nav_hint

            # 8.2 用例管理「执行」传入的 test_case_id 需明确 ID 命名空间
            if test_case_id:
                effective_prompt = (effective_prompt or "") + MANUAL_TESTCASE_EXECUTION_HINT
                internal_execution_context = effective_user_message
                if llm_attachment_context:
                    internal_execution_context += (
                        "\n\n以下是用户附加文件内容，请作为本轮对话上下文使用:"
                        + llm_attachment_context
                    )
                # 完整用例上下文只供 Agent 执行，避免 helper 指令和数据快照出现在聊天记录。
                effective_prompt += (
                    "\n\n## 本次用例执行的内部上下文\n"
                    + internal_execution_context
                )
                logger.info(
                    f"AgentLoopStreamAPI: 已追加用例管理执行提示 test_case_id={test_case_id}"
                )
                from testcases.run_record_service import start_testcase_run_record

                await sync_to_async(start_testcase_run_record)(
                    testcase_id=int(test_case_id),
                    user_id=request.user.id,
                    session_id=session_id,
                    generate_playwright_script=bool(generate_playwright_script),
                    execution_mode=execution_mode,
                    ui_test_case_id=ui_test_case_id,
                    data_generation_run_id=pre_data_run_id,
                )

            streamed_assistant_content = ""
            if test_case_id:
                user_message_for_llm = await sync_to_async(
                    _build_testcase_execution_display_message
                )(int(test_case_id), pre_data_run)
            else:
                user_message_for_llm = effective_user_message
            if llm_attachment_context and not test_case_id:
                user_message_for_llm = user_message + "\n\n以下是用户附加文件内容，请作为本轮对话上下文使用:" + llm_attachment_context
            (
                human_message_content,
                human_message_kwargs,
                display_user_message,
            ) = await _prepare_agent_loop_human_message(
                user_message_for_llm,
                project=project,
                supports_vision=active_config.supports_vision,
                uploaded_images_base64=uploaded_images_base64,
            )
            user_msg = HumanMessage(
                content=human_message_content,
                additional_kwargs=human_message_kwargs,
            )

            # 10. 获取工具名列表用于 HITL
            tool_names = [t.name for t in tools] if tools else None

            # 11. 发送开始信号
            if (
                not locals().get("hybrid_start_emitted", False)
                and not start_event_emitted
            ):
                yield create_sse_data(
                    {
                        "type": "start",
                        "session_id": session_id,
                        "thread_id": thread_id,
                        "project_id": project_id,
                        "display_message": display_user_message,
                        "mode": "agent_loop",
                        "created_at": chat_session.created_at.isoformat()
                        if chat_session and chat_session.created_at
                        else None,
                    }
                )

            # 12. 创建 Agent（LangChain v1 统一路径）
            async with get_async_checkpointer() as checkpointer:
                # 获取中间件（需要同步到异步，因为内部有 ORM 查询）
                middleware = await sync_to_async(get_middleware_from_config)(
                    active_config,
                    llm,
                    user=request.user,
                    session_id=session_id,
                    all_tool_names=tool_names,
                    tools=tools,
                    system_prompt=effective_prompt,
                )

                agent = create_agent(
                    llm,
                    tools,
                    system_prompt=effective_prompt,
                    checkpointer=checkpointer,
                    middleware=middleware,
                )
                logger.info(
                    f"AgentLoopStreamAPI: Agent created with {len(tools)} tools"
                )

                # 13. 配置调用参数
                invoke_config = {
                    "configurable": {"thread_id": thread_id},
                    # A testcase has a finite step list. Keep a generous ceiling for
                    # normal exploration while preventing an unbounded model/tool loop.
                    "recursion_limit": 128 if test_case_id else 1000,
                }
                input_messages = {"messages": [user_msg]}

                # 13.1 发送前修复历史消息（配对错误 + 风险工具输出）
                await _sanitize_history_before_model_call(
                    agent=agent,
                    invoke_config=invoke_config,
                    log_prefix="AgentLoopStreamAPI",
                )

                # 14. 步骤跟踪状态
                step_count = 0
                current_tool_calls = []
                interrupt_detected = False
                user_stopped = False

                # 15. 流式执行
                stream_modes = ["updates", "messages"]

                try:
                    async for stream_mode, chunk in agent.astream(
                        input_messages, config=invoke_config, stream_mode=stream_modes
                    ):
                        # 检查用户停止信号
                        if should_stop(session_id):
                            user_stopped = True
                            clear_stop_signal(session_id)
                            logger.info(
                                f"AgentLoopStreamAPI: Stop signal received at step {step_count}"
                            )
                            yield create_sse_data(
                                {
                                    "type": "stopped",
                                    "message": "已停止生成",
                                    "step": step_count,
                                }
                            )
                            break

                        if stream_mode == "updates":
                            # 检查中断事件 (HITL)
                            if isinstance(chunk, dict) and "__interrupt__" in chunk:
                                interrupt_info = chunk["__interrupt__"]
                                logger.info(
                                    f"AgentLoopStreamAPI: HITL interrupt detected: {interrupt_info}"
                                )
                                logger.info(
                                    f"AgentLoopStreamAPI: interrupt_info type: {type(interrupt_info)}"
                                )

                                action_requests = []
                                interrupt_id = None
                                # 处理 tuple、list 或单个 Interrupt 对象
                                if isinstance(interrupt_info, (list, tuple)):
                                    interrupts_list = list(interrupt_info)
                                else:
                                    interrupts_list = [interrupt_info]

                                for intr in interrupts_list:
                                    logger.info(
                                        f"AgentLoopStreamAPI: Processing interrupt: type={type(intr)}, dir={dir(intr)}"
                                    )
                                    logger.info(
                                        f"AgentLoopStreamAPI: interrupt repr: {repr(intr)}"
                                    )

                                    if hasattr(intr, "id"):
                                        interrupt_id = intr.id
                                        logger.info(
                                            f"AgentLoopStreamAPI: interrupt_id from attr: {interrupt_id}"
                                        )
                                    elif isinstance(intr, dict) and "id" in intr:
                                        interrupt_id = intr["id"]
                                        logger.info(
                                            f"AgentLoopStreamAPI: interrupt_id from dict: {interrupt_id}"
                                        )

                                    intr_value = (
                                        getattr(intr, "value", intr)
                                        if hasattr(intr, "value")
                                        else intr
                                    )
                                    logger.info(
                                        f"AgentLoopStreamAPI: intr_value type={type(intr_value)}, value={intr_value}"
                                    )

                                    # 尝试多种方式获取 action_requests
                                    ars = []
                                    if isinstance(intr_value, dict):
                                        ars = intr_value.get("action_requests", [])
                                        logger.info(
                                            f"AgentLoopStreamAPI: action_requests from dict: {ars}"
                                        )
                                    elif hasattr(intr_value, "action_requests"):
                                        ars = intr_value.action_requests
                                        logger.info(
                                            f"AgentLoopStreamAPI: action_requests from attr: {ars}"
                                        )

                                    # 如果还是空的，尝试从 intr 本身获取
                                    if not ars and hasattr(intr, "action_requests"):
                                        ars = intr.action_requests
                                        logger.info(
                                            f"AgentLoopStreamAPI: action_requests from intr attr: {ars}"
                                        )

                                    logger.info(
                                        f"AgentLoopStreamAPI: Found {len(ars)} action_requests: {ars}"
                                    )

                                    for ar in ars:
                                        if isinstance(ar, dict):
                                            action_requests.append(
                                                {
                                                    "name": ar.get(
                                                        "name",
                                                        ar.get(
                                                            "action_name", "unknown"
                                                        ),
                                                    ),
                                                    "args": ar.get(
                                                        "arguments", ar.get("args", {})
                                                    ),
                                                    "description": ar.get(
                                                        "description", ""
                                                    ),
                                                }
                                            )
                                        else:
                                            action_requests.append(
                                                {
                                                    "name": getattr(
                                                        ar, "name", "unknown"
                                                    ),
                                                    "args": getattr(
                                                        ar,
                                                        "arguments",
                                                        getattr(ar, "args", {}),
                                                    ),
                                                    "description": getattr(
                                                        ar, "description", ""
                                                    ),
                                                }
                                            )

                                if action_requests:
                                    # 获取用户工具偏好，为 always_reject 的工具添加 auto_reject 标记
                                    user_approvals = await sync_to_async(
                                        get_user_tool_approvals
                                    )(request.user, session_id)
                                    for ar in action_requests:
                                        tool_name = ar.get("name", "")
                                        if (
                                            user_approvals.get(tool_name)
                                            == "always_reject"
                                        ):
                                            ar["auto_reject"] = True
                                            logger.info(
                                                f"AgentLoopStreamAPI: Tool {tool_name} marked as auto_reject"
                                            )

                                    interrupt_detected = True
                                    yield create_sse_data(
                                        {
                                            "type": "interrupt",
                                            "interrupt_id": interrupt_id
                                            or str(id(interrupt_info)),
                                            "action_requests": action_requests,
                                            "session_id": session_id,
                                            "thread_id": thread_id,
                                        }
                                    )
                                    logger.info(
                                        f"AgentLoopStreamAPI: Sent interrupt with {len(action_requests)} actions"
                                    )

                            # 检测工具调用开始（用于生成 step_start 事件）
                            elif isinstance(chunk, dict):
                                for node_name, node_output in chunk.items():
                                    if node_name == "agent" and isinstance(
                                        node_output, dict
                                    ):
                                        messages = node_output.get("messages", [])
                                        for msg in messages:
                                            if (
                                                hasattr(msg, "tool_calls")
                                                and msg.tool_calls
                                            ):
                                                # 新的工具调用 -> 新步骤开始
                                                step_count += 1
                                                current_tool_calls = msg.tool_calls
                                                tool_names_in_step = [
                                                    tc.get("name", "unknown")
                                                    if isinstance(tc, dict)
                                                    else getattr(tc, "name", "unknown")
                                                    for tc in current_tool_calls
                                                ]
                                                yield create_sse_data(
                                                    {
                                                        "type": "step_start",
                                                        "step": step_count,
                                                        "max_steps": self.MAX_STEPS,
                                                        "tools": tool_names_in_step,
                                                    }
                                                )
                                                for progress_event in await _emit_testcase_tool_started(
                                                    current_tool_calls,
                                                    session_id=session_id,
                                                    test_case_id=test_case_id,
                                                ):
                                                    yield create_sse_data(progress_event)
                                                logger.info(
                                                    f"AgentLoopStreamAPI: Step {step_count} started with tools: {tool_names_in_step}"
                                                )

                                    elif node_name == "tools" and isinstance(
                                        node_output, dict
                                    ):
                                        # 工具执行完成
                                        tool_messages = node_output.get("messages", [])
                                        for tool_msg in tool_messages:
                                            if hasattr(tool_msg, "content"):
                                                content = tool_msg.content
                                                tool_name = getattr(
                                                    tool_msg, "name", None
                                                ) or getattr(
                                                    tool_msg, "tool_name", "unknown"
                                                )
                                                helper_step = None
                                                total_steps = None

                                                # 使用辅助函数处理 MCP 工具输出
                                                content, summary = (
                                                    process_mcp_tool_output(content)
                                                )

                                                if test_case_id:
                                                    helper_case_id, helper_step = _extract_helper_testcase_progress(content)
                                                    if helper_step is not None:
                                                        total_steps = await sync_to_async(
                                                            _get_testcase_step_count
                                                        )(helper_case_id or int(test_case_id))
                                                        progress_text = (
                                                            f"正在执行步骤 {helper_step}/{total_steps}"
                                                            if total_steps
                                                            else f"正在执行步骤 {helper_step}"
                                                        )
                                                        content = f"[执行进度] {progress_text}\n{content}"
                                                        yield create_sse_data(
                                                            {
                                                                "type": "testcase_progress",
                                                                "session_id": session_id,
                                                                "test_case_id": int(test_case_id),
                                                                "step": helper_step,
                                                                "total_steps": total_steps,
                                                                "state": "completed",
                                                                "message": "步骤执行完成，正在校验结果",
                                                            }
                                                        )

                                                yield create_sse_data(
                                                    {
                                                        "type": "tool_result",
                                                        "tool_name": tool_name,
                                                        "tool_output": content,
                                                        "summary": summary,
                                                        "step": step_count,
                                                    }
                                                )

                                                # A testcase-bound browser tool may return without
                                                # the helper's RESULT/screenshot markers (for example,
                                                # exploratory page inspection). In that case the UI
                                                # otherwise remains stuck on "browser executing" while
                                                # LangGraph waits for the next model decision.
                                                if test_case_id:
                                                    yield create_sse_data(
                                                        {
                                                            "type": "testcase_progress",
                                                            "session_id": session_id,
                                                            "test_case_id": int(test_case_id),
                                                            "state": "waiting_llm",
                                                            "message": "浏览器操作已返回，正在等待模型分析并安排下一步",
                                                        }
                                                    )

                                                # Playwright helper 已给出确定失败时无需再等待
                                                # 模型生成“下一步/总结”。否则所有步骤都已结束，
                                                # 页面仍会长时间停在“正在准备下一步”。
                                                result_markers = re.findall(
                                                    r"RESULT\s*=\s*(PASS|FAIL)\b",
                                                    str(content),
                                                    flags=re.IGNORECASE,
                                                )
                                                if (
                                                    test_case_id
                                                    and tool_name == "execute_skill_script"
                                                    and "[CASE_TOOL_RETRY_LIMIT]" in str(content)
                                                ):
                                                    retry_failure = str(content)
                                                    state_messages = await _get_agent_state_messages(
                                                        agent, invoke_config
                                                    )
                                                    record, injected_report = await _complete_testcase_run_with_report(
                                                        session_id=session_id,
                                                        final_content=retry_failure,
                                                        messages=state_messages,
                                                        error_message=retry_failure,
                                                        agent=agent,
                                                        invoke_config=invoke_config,
                                                    )
                                                    if injected_report:
                                                        yield create_sse_data(
                                                            {"type": "stream", "data": "\n\n" + injected_report}
                                                        )
                                                    yield create_sse_data(
                                                        {
                                                            "type": "complete",
                                                            "total_steps": step_count,
                                                            "test_case_run": {
                                                                "status": record.status if record else "fail",
                                                                "summary": record.summary if record else retry_failure,
                                                            },
                                                        }
                                                    )
                                                    return
                                                if (
                                                    test_case_id
                                                    and tool_name == "execute_skill_script"
                                                    and result_markers
                                                    and result_markers[-1].upper() == "FAIL"
                                                ):
                                                    # 失败但没产出该步截图时，不要立刻收尾：
                                                    # 常见于最后一步卡在详情页筛选项，收尾前的
                                                    # coverage auto-run 还能再补跑一次并取证。
                                                    fail_case_id, fail_step = (
                                                        _extract_helper_testcase_progress(
                                                            content
                                                        )
                                                    )
                                                    has_fail_step_shot = bool(
                                                        fail_step
                                                        and re.search(
                                                            rf"\[(?:CASE_SCREENSHOT|SCREENSHOT_SAVED)\]\s+\S*case[_-](\d+)_step{int(fail_step)}\b",
                                                            str(content),
                                                            flags=re.IGNORECASE,
                                                        )
                                                    )
                                                    if not has_fail_step_shot:
                                                        logger.warning(
                                                            "AgentLoopStreamAPI: helper FAIL without "
                                                            "step screenshot case=%s step=%s; "
                                                            "defer early finalize for coverage auto-run",
                                                            fail_case_id or test_case_id,
                                                            fail_step,
                                                        )
                                                    else:
                                                        record, injected_report = await _complete_testcase_run_with_report(
                                                            session_id=session_id,
                                                            final_content=str(content),
                                                            messages=[],
                                                            agent=agent,
                                                            invoke_config=invoke_config,
                                                        )
                                                        if injected_report:
                                                            yield create_sse_data(
                                                                {"type": "stream", "data": "\n\n" + injected_report}
                                                            )
                                                        yield create_sse_data(
                                                            {
                                                                "type": "complete",
                                                                "total_steps": step_count,
                                                                "test_case_run": {
                                                                    "status": record.status if record else "fail",
                                                                    "summary": record.summary if record else "",
                                                                },
                                                            }
                                                        )
                                                        return
                                                coverage_messages = []
                                                contiguous_step = None
                                                if test_case_id and tool_name == "execute_skill_script":
                                                    coverage_messages = list(await _get_agent_state_messages(agent, invoke_config) or [])
                                                    coverage_messages.append(ToolMessage(
                                                        content=str(content), tool_call_id="coverage-current",
                                                        name="execute_skill_script",
                                                    ))
                                                    contiguous_step = _latest_passed_helper_step_from_messages(
                                                        coverage_messages, test_case_id=test_case_id,
                                                    )
                                                if test_case_id and contiguous_step == total_steps and _is_final_testcase_step_pass(
                                                    tool_name=tool_name,
                                                    content=str(content),
                                                    helper_step=helper_step,
                                                    total_steps=total_steps,
                                                ):
                                                    # The helper has already proved every required
                                                    # step. Do not wait for a further LLM turn just
                                                    # to produce a summary, as that turn can stall.
                                                    generation_messages = await _get_agent_state_messages(
                                                        agent, invoke_config
                                                    )
                                                    record, injected_report = await _complete_testcase_run_with_report(
                                                        session_id=session_id,
                                                        final_content=str(content),
                                                        messages=[],
                                                        agent=agent,
                                                        invoke_config=invoke_config,
                                                    )
                                                    if injected_report:
                                                        yield create_sse_data(
                                                            {"type": "stream", "data": "\n\n" + injected_report}
                                                        )
                                                    script_generation_result = None
                                                    if generate_playwright_script:
                                                        if record and record.status == "pass":
                                                            yield create_sse_data(
                                                                {
                                                                    "type": "testcase_progress",
                                                                    "session_id": session_id,
                                                                    "test_case_id": int(test_case_id),
                                                                    "state": "generating_ui_case",
                                                                    "message": "功能用例已通过，正在生成并回放验证 UI 自动化用例",
                                                                }
                                                            )
                                                        script_generation_result = await _queue_generated_ui_case(
                                                            record=record, llm_config_id=active_config.pk,
                                                        )
                                                    complete_event = {
                                                        "type": "complete",
                                                        "total_steps": step_count,
                                                        "test_case_run": {
                                                            "status": record.status
                                                            if record
                                                            else "pass",
                                                            "summary": record.summary
                                                            if record
                                                            else "",
                                                        },
                                                    }
                                                    if script_generation_result is not None:
                                                        complete_event["script_generation"] = script_generation_result
                                                    yield create_sse_data(complete_event)
                                                    return
                                                if (
                                                    test_case_id
                                                    and tool_name == "execute_skill_script"
                                                    and helper_step is not None
                                                    and total_steps
                                                    and contiguous_step is not None
                                                    and contiguous_step < total_steps
                                                    and result_markers
                                                    and result_markers[-1].upper() == "PASS"
                                                ):
                                                    # 固定 helper 用例：模型常在非末步 PASS 后直接出报告。
                                                    # 非末步 PASS 后立刻按步补跑，并推送进度，不再等 LLM。
                                                    next_step = int(contiguous_step) + 1
                                                    logger.warning(
                                                        "AgentLoopStreamAPI: non-final PASS "
                                                        "case=%s step=%s/%s, auto-running %s..%s",
                                                        test_case_id,
                                                        helper_step,
                                                        total_steps,
                                                        next_step,
                                                        total_steps,
                                                    )
                                                    auto_output = ""
                                                    auto_step = helper_step
                                                    auto_no_template = False
                                                    async for event in _run_and_stream_remaining_helper_steps(
                                                        test_case_id=int(test_case_id),
                                                        user_id=int(request.user.id),
                                                        project_id=int(project_id),
                                                        session_id=str(session_id),
                                                        from_step=next_step,
                                                        to_step=int(total_steps),
                                                        messages=coverage_messages,
                                                        previous_output=content,
                                                        agent=agent if "agent" in locals() else None,
                                                        invoke_config=invoke_config if "invoke_config" in locals() else None,
                                                    ):
                                                        if event.get("type") == "_auto_run_bundle":
                                                            auto_output = event.get("output") or ""
                                                            auto_step = event.get("last_step") or auto_step
                                                            auto_no_template = bool(event.get("no_template"))
                                                            continue
                                                        yield create_flushed_sse(event)
                                                    if auto_no_template:
                                                        logger.info(
                                                            "AgentLoopStreamAPI: no fixed helper for case=%s; "
                                                            "AI continues remaining steps",
                                                            test_case_id,
                                                        )
                                                        yield create_sse_data(
                                                            {
                                                                "type": "testcase_progress",
                                                                "session_id": session_id,
                                                                "test_case_id": int(test_case_id),
                                                                "step": next_step,
                                                                "total_steps": total_steps,
                                                                "state": "running",
                                                                "message": "无固定脚本，继续按用例步骤探索执行",
                                                            }
                                                        )
                                                        continue
                                                    if auto_output:
                                                        content = (
                                                            f"{content}\n{auto_output}"
                                                        ).strip()
                                                    if auto_step is not None:
                                                        yield create_sse_data(
                                                            {
                                                                "type": "testcase_progress",
                                                                "session_id": session_id,
                                                                "test_case_id": int(test_case_id),
                                                                "step": auto_step,
                                                                "total_steps": total_steps,
                                                                "state": "completed",
                                                                "message": "自动补跑步骤完成，正在校验结果",
                                                            }
                                                        )
                                                    # 剩余步骤已强制跑完：直接收尾，避免模型再写假报告
                                                    record, injected_report = await _complete_testcase_run_with_report(
                                                        session_id=session_id,
                                                        final_content=str(content),
                                                        messages=all_messages
                                                        if "all_messages" in locals()
                                                        else [],
                                                        agent=agent,
                                                        invoke_config=invoke_config,
                                                    )
                                                    if injected_report:
                                                        yield create_sse_data(
                                                            {
                                                                "type": "stream",
                                                                "data": "\n\n" + injected_report,
                                                            }
                                                        )
                                                    script_generation_result = None
                                                    if generate_playwright_script:
                                                        if record and record.status == "pass":
                                                            yield create_sse_data(
                                                                {
                                                                    "type": "testcase_progress",
                                                                    "session_id": session_id,
                                                                    "test_case_id": int(test_case_id),
                                                                    "state": "generating_ui_case",
                                                                    "message": "功能用例已通过，正在生成并回放验证 UI 自动化用例",
                                                                }
                                                            )
                                                        generation_messages = await _get_agent_state_messages(
                                                            agent, invoke_config
                                                        )
                                                        script_generation_result = await _queue_generated_ui_case(
                                                            record=record, llm_config_id=active_config.pk,
                                                        )
                                                    complete_event = {
                                                        "type": "complete",
                                                        "total_steps": step_count,
                                                        "test_case_run": {
                                                            "status": record.status
                                                            if record
                                                            else "pass",
                                                            "summary": record.summary
                                                            if record
                                                            else "",
                                                        },
                                                    }
                                                    if script_generation_result is not None:
                                                        complete_event["script_generation"] = script_generation_result
                                                    yield create_sse_data(complete_event)
                                                    return
                                        # 步骤完成
                                        if step_count > 0:
                                            yield create_sse_data(
                                                {
                                                    "type": "step_complete",
                                                    "step": step_count,
                                                }
                                            )

                        elif stream_mode == "messages":
                            # LLM Token 流式输出
                            # messages 模式返回元组 (token, metadata)
                            if isinstance(chunk, tuple) and len(chunk) >= 1:
                                token = chunk[0]
                                # 只发送 AI 消息，过滤掉 ToolMessage（工具结果已通过 tool_result 事件发送）
                                if hasattr(token, "content") and token.content:
                                    # 检查是否是 ToolMessage（通过类名或 type 属性）
                                    token_type = type(token).__name__
                                    if "ToolMessage" not in token_type:
                                        text_chunk = _stream_content_to_text(token.content)
                                        if not text_chunk:
                                            continue
                                        streamed_assistant_content += text_chunk
                                        yield create_sse_data(
                                            {"type": "stream", "data": text_chunk}
                                        )
                            elif hasattr(chunk, "content") and chunk.content:
                                # 兼容旧版本可能直接返回 message 的情况
                                # 同样过滤掉 ToolMessage
                                chunk_type = type(chunk).__name__
                                if "ToolMessage" not in chunk_type:
                                    text_chunk = _stream_content_to_text(chunk.content)
                                    if not text_chunk:
                                        continue
                                    streamed_assistant_content += text_chunk
                                    yield create_sse_data(
                                        {"type": "stream", "data": text_chunk}
                                    )

                except Exception as e:
                    friendly_error = get_user_friendly_llm_error(e)
                    if friendly_error:
                        logger.warning(
                            "AgentLoopStreamAPI: Friendly model error. session_id=%s, error_code=%s, message=%s",
                            session_id,
                            friendly_error.get("error_code"),
                            friendly_error.get("message"),
                        )
                        if test_case_id:
                            _, injected_report = await _complete_testcase_run_with_report(
                                session_id=session_id,
                                final_content=streamed_assistant_content,
                                messages=[],
                                error_message=friendly_error.get("message"),
                                agent=agent,
                                invoke_config=invoke_config,
                            )
                            if injected_report:
                                yield create_sse_data(
                                    {"type": "stream", "data": "\n\n" + injected_report}
                                )
                        yield create_sse_data(_build_sse_error_event(e))
                    else:
                        logger.error(
                            "AgentLoopStreamAPI: Streaming error. session_id=%s, thread_id=%s, "
                            "model=%s, error_type=%s, error=%s",
                            session_id,
                            thread_id,
                            model_name,
                            type(e).__name__,
                            e,
                            exc_info=True,
                        )
                        yield create_sse_data(
                            {"type": "error", "message": f"Streaming error: {str(e)}"}
                        )

                # 16. 处理结束状态
                # 无论是否发生 interrupt，都需要计算和发送 context_update
                try:
                    current_state = await agent.aget_state(invoke_config)
                    all_messages = (
                        current_state.values.get("messages", [])
                        if current_state.values
                        else []
                    )

                    # 获取当前上下文 token 使用量（优先 usage_metadata，回退估算）
                    input_tokens, output_tokens, total_tokens = (
                        calculate_context_tokens(
                            all_messages, model_name,
                            tools=tools, system_prompt=effective_prompt,
                        )
                    )

                    yield create_sse_data(
                        {
                            "type": "context_update",
                            "context_token_count": total_tokens,
                            "context_limit": context_limit,
                        }
                    )

                    # 记录 Token 使用量到 ChatSession + TokenUsageRecord
                    if input_tokens > 0 or output_tokens > 0:
                        # 提取缓存命中信息
                        cache_read_tokens = 0
                        for msg in reversed(all_messages):
                            if hasattr(msg, "usage_metadata") and msg.usage_metadata:
                                cache_info = msg.usage_metadata.get("input_token_details", {})
                                cache_read_tokens = cache_info.get("cache_read", 0) or 0
                                logger.info(
                                    "AgentLoopStreamAPI: Token usage - input=%d, output=%d, cache_read=%d, cache_info=%s",
                                    input_tokens, output_tokens, cache_read_tokens, cache_info,
                                )
                                break
                        else:
                            logger.info(
                                f"AgentLoopStreamAPI: Token usage recorded - input={input_tokens}, output={output_tokens}"
                            )

                        await sync_to_async(self._update_session_token_usage)(
                            session_id, input_tokens, output_tokens,
                            cache_read_tokens=cache_read_tokens,
                            user_id=request.user.id,
                            project_id=int(project_id) if project_id else None,
                        )
                except Exception as e:
                    logger.warning(
                        f"AgentLoopStreamAPI: Failed to calculate token count: {e}"
                    )

                # AI 自动总结会话标题
                if chat_session.title.startswith("新对话") and "llm" in locals() and llm:
                    try:
                        scheduled = schedule_auto_summarize_session_title(
                            llm,
                            chat_session,
                            user_message,
                        )
                        if scheduled:
                            logger.info(f"AgentLoopStreamAPI: Scheduled title auto-summarization for session {session_id}")
                    except Exception as summarize_err:
                        logger.error(f"AgentLoopStreamAPI: Failed to auto-summarize title: {summarize_err}", exc_info=True)

                if user_stopped:
                    if test_case_id:
                        _, injected_report = await _complete_testcase_run_with_report(
                            session_id=session_id,
                            final_content=streamed_assistant_content,
                            messages=all_messages if "all_messages" in locals() else [],
                            stopped=True,
                            agent=agent,
                            invoke_config=invoke_config,
                        )
                        if injected_report:
                            yield create_sse_data(
                                {"type": "stream", "data": "\n\n" + injected_report}
                            )
                    yield create_sse_data(
                        {"type": "complete", "status": "stopped", "steps": step_count}
                    )
                elif interrupt_detected:
                    logger.info(
                        "AgentLoopStreamAPI: Interrupt detected, returning early"
                    )
                    if test_case_id:
                        record, injected_report = await _complete_testcase_run_with_report(
                            session_id=session_id,
                            final_content=streamed_assistant_content,
                            messages=all_messages if "all_messages" in locals() else [],
                            stopped=True,
                            error_message=(
                                "执行已暂停：等待工具审批。"
                                "若未在对话中继续，请重新执行用例。"
                            ),
                            agent=agent,
                            invoke_config=invoke_config,
                        )
                        if injected_report:
                            yield create_sse_data(
                                {"type": "stream", "data": "\n\n" + injected_report}
                            )
                        yield create_sse_data(
                            {
                                "type": "complete",
                                "total_steps": step_count,
                                "status": "interrupt",
                                "test_case_run": {
                                    "status": record.status if record else "stopped",
                                    "summary": record.summary if record else "",
                                },
                            }
                        )
                else:
                    if test_case_id:
                        final_content = streamed_assistant_content
                        try:
                            if "all_messages" in locals() and all_messages:
                                for msg in reversed(all_messages):
                                    msg_type = type(msg).__name__
                                    if msg_type in ("ToolMessage", "HumanMessage"):
                                        continue
                                    content = getattr(msg, "content", None)
                                    if content:
                                        final_content = (
                                            content
                                            if isinstance(content, str)
                                            else str(content)
                                        )
                                        break
                        except Exception as extract_err:
                            logger.warning(
                                "AgentLoopStreamAPI: Failed to extract final testcase content: %s",
                                extract_err,
                            )
                        llm_failure_message = extract_llm_failure_message_from_text(
                            final_content
                        )
                        if llm_failure_message and test_case_id:
                            yield create_sse_data(
                                {
                                    "type": "testcase_progress",
                                    "session_id": session_id,
                                    "test_case_id": int(test_case_id),
                                    "state": "waiting_llm",
                                    "message": llm_failure_message,
                                }
                            )

                        # 模型常在倒数第二步后直接出报告，跳过最后一步截图。
                        # 收尾前若发现 helper 进度未到总步数，直接在持久化浏览器会话上
                        # 强制执行剩余步骤（不依赖模型），并自动上传截图。
                        incomplete_without_helper = ""
                        if (
                            not user_stopped
                            and not interrupt_detected
                            and "agent" in locals()
                            and "invoke_config" in locals()
                        ):
                            try:
                                total_case_steps = await sync_to_async(
                                    _get_testcase_step_count
                                )(int(test_case_id))
                                current_messages = (
                                    all_messages if "all_messages" in locals() else []
                                )
                                if not current_messages:
                                    try:
                                        st = await agent.aget_state(invoke_config)
                                        current_messages = (
                                            (st.values or {}).get("messages") or []
                                        )
                                    except Exception:
                                        current_messages = []
                                done_step = _latest_passed_helper_step_from_messages(
                                    current_messages, test_case_id=test_case_id
                                )
                                explicit_failed_step = _explicitly_failed_helper_step_from_messages(
                                    current_messages, test_case_id=test_case_id
                                )
                                # No tool evidence means no step is proven complete; resume from step 1.
                                if done_step is None:
                                    done_step = 0
                                logger.warning(
                                    "AgentLoopStreamAPI: step coverage check "
                                    "case=%s done=%s total=%s msgs=%s",
                                    test_case_id,
                                    done_step,
                                    total_case_steps,
                                    len(current_messages or []),
                                )
                                if (
                                    total_case_steps
                                    and done_step < total_case_steps
                                    and explicit_failed_step is None
                                ):
                                    next_step = done_step + 1
                                    logger.warning(
                                        "AgentLoopStreamAPI: incomplete steps "
                                        "case=%s done=%s/%s, auto-running %s..%s",
                                        test_case_id,
                                        done_step,
                                        total_case_steps,
                                        next_step,
                                        total_case_steps,
                                    )
                                    auto_output = ""
                                    helper_step = done_step
                                    auto_no_template = execution_mode == "ai_only"
                                    if not auto_no_template:
                                        async for event in _run_and_stream_remaining_helper_steps(
                                            test_case_id=int(test_case_id),
                                            user_id=int(request.user.id),
                                            project_id=int(project_id),
                                            session_id=str(session_id),
                                            from_step=next_step,
                                            to_step=int(total_case_steps),
                                            messages=current_messages,
                                            previous_output=str(final_content or streamed_assistant_content or ""),
                                            agent=agent if "agent" in locals() else None,
                                            invoke_config=invoke_config if "invoke_config" in locals() else None,
                                        ):
                                            if event.get("type") == "_auto_run_bundle":
                                                auto_output = event.get("output") or ""
                                                helper_step = event.get("last_step") or helper_step
                                                auto_no_template = bool(event.get("no_template"))
                                                continue
                                            yield create_flushed_sse(event)
                                    if auto_no_template and execution_mode != "script_only":
                                        step_details = await sync_to_async(_get_testcase_step_details)(
                                            int(test_case_id)
                                        )
                                        resume_prompt = _build_incomplete_ai_resume_prompt(
                                            next_step=next_step,
                                            total_steps=int(total_case_steps),
                                            step_details=step_details,
                                            test_case_id=int(test_case_id),
                                            attempted_step=(
                                                next_step
                                                if _testcase_step_was_attempted(
                                                    current_messages,
                                                    step=next_step,
                                                    test_case_id=test_case_id,
                                                )
                                                else None
                                            ),
                                        )
                                        yield create_sse_data({
                                            "type": "testcase_progress",
                                            "session_id": session_id,
                                            "test_case_id": int(test_case_id),
                                            "step": next_step,
                                            "total_steps": int(total_case_steps),
                                            "state": "running",
                                            "message": (
                                                f"已确认前序步骤 {done_step}/{total_case_steps}，"
                                                f"正在续跑步骤 {next_step}～{total_case_steps}"
                                            ),
                                        })
                                        try:
                                            if should_stop(session_id):
                                                incomplete_without_helper = (
                                                    f"用户停止执行，步骤 {next_step}～{total_case_steps} 尚未完成"
                                                )
                                            else:
                                                resume_config = dict(invoke_config)
                                                resume_config["recursion_limit"] = min(
                                                    int(resume_config.get("recursion_limit", 1000)), 48
                                                )
                                                async for resume_event in _stream_ai_testcase_resume(
                                                    agent, resume_prompt=resume_prompt,
                                                    config=resume_config, session_id=session_id,
                                                    test_case_id=int(test_case_id),
                                                    total_steps=int(total_case_steps),
                                                ):
                                                    yield create_flushed_sse(resume_event)
                                                resumed_messages = await _get_agent_state_messages(
                                                    agent, invoke_config
                                                )
                                                resumed_done = _latest_passed_helper_step_from_messages(
                                                    resumed_messages, test_case_id=test_case_id
                                                )
                                                if resumed_messages:
                                                    all_messages = resumed_messages
                                                if resumed_done is not None and resumed_done >= total_case_steps:
                                                    done_step = resumed_done
                                                    for msg in reversed(resumed_messages):
                                                        if type(msg).__name__ != "AIMessage":
                                                            continue
                                                        if getattr(msg, "tool_calls", None):
                                                            continue
                                                        body = getattr(msg, "content", "") or ""
                                                        if body:
                                                            final_content = body if isinstance(body, str) else str(body)
                                                            break
                                                    yield create_sse_data({
                                                        "type": "testcase_progress",
                                                        "session_id": session_id,
                                                        "test_case_id": int(test_case_id),
                                                        "step": int(total_case_steps),
                                                        "total_steps": int(total_case_steps),
                                                        "state": "completed",
                                                        "message": "剩余步骤已完成，正在核对执行证据",
                                                    })
                                                else:
                                                    first_missing = (resumed_done or 0) + 1
                                                    incomplete_without_helper = (
                                                        f"纯 AI 续跑后仍缺少步骤 {first_missing}～{total_case_steps} 的工具执行证据"
                                                    )
                                        except asyncio.CancelledError:
                                            raise
                                        except Exception as ai_resume_err:
                                            logger.warning(
                                                "AgentLoopStreamAPI: AI step continuation failed case=%s: %s",
                                                test_case_id, ai_resume_err, exc_info=True,
                                            )
                                            incomplete_without_helper = (
                                                f"纯 AI 续跑失败，步骤 {next_step}～{total_case_steps} 未完成：{ai_resume_err}"
                                            )
                                    elif auto_no_template:
                                        incomplete_without_helper = (
                                            f"步骤 {next_step}～{total_case_steps} 尚未完成；当前为仅脚本模式且没有可用固定脚本"
                                        )
                                    if auto_output:
                                        final_content = (
                                            f"{final_content}\n{auto_output}"
                                        ).strip()
                                        yield create_sse_data(
                                            {
                                                "type": "testcase_progress",
                                                "session_id": session_id,
                                                "test_case_id": int(test_case_id),
                                                "step": helper_step,
                                                "total_steps": total_case_steps,
                                                "state": "completed",
                                                "message": "自动补跑步骤完成，正在校验结果",
                                            }
                                        )
                                elif explicit_failed_step is not None:
                                    logger.info(
                                        "AgentLoopStreamAPI: explicit step failure; "
                                        "skip automatic replay case=%s step=%s",
                                        test_case_id,
                                        explicit_failed_step,
                                    )
                            except Exception as resume_err:
                                logger.warning(
                                    "AgentLoopStreamAPI: incomplete-step auto-resume failed: %s",
                                    resume_err,
                                    exc_info=True,
                                )

                        record, injected_report = await _complete_testcase_run_with_report(
                            session_id=session_id,
                            final_content=final_content,
                            messages=all_messages if "all_messages" in locals() else [],
                            error_message=llm_failure_message or incomplete_without_helper or None,
                            agent=agent,
                            invoke_config=invoke_config,
                        )
                        if injected_report:
                            yield create_sse_data(
                                {"type": "stream", "data": "\n\n" + injected_report}
                            )
                        complete_data = {
                            "type": "complete",
                            "total_steps": step_count,
                            "test_case_run": {
                                "status": record.status if record else "pass",
                                "summary": record.summary if record else "",
                            },
                        }
                    else:
                        complete_data = {"type": "complete", "total_steps": step_count}
                    if generate_playwright_script:
                        if record and record.status == "pass":
                            yield create_sse_data(
                                {
                                    "type": "testcase_progress",
                                    "session_id": session_id,
                                    "test_case_id": int(test_case_id) if test_case_id else None,
                                    "state": "generating_ui_case",
                                    "message": "功能用例已通过，正在生成并回放验证 UI 自动化用例",
                                }
                            )
                        script_generation_result = await _queue_generated_ui_case(
                            record=record, llm_config_id=active_config.pk,
                        )
                        if complete_data.get("test_case_run") and record:
                            complete_data["test_case_run"]["summary"] = record.summary
                        complete_data["script_generation"] = script_generation_result
                    yield create_sse_data(complete_data)

                yield "data: [DONE]\n\n"

        except asyncio.CancelledError:
            # asyncio cancellation is a BaseException, so the generic handler
            # below does not run. Without finalizing here, testcase records stay
            # "running" forever and the UI keeps showing the preparation spinner.
            if test_case_id:
                try:
                    cancelled_record, _ = await _complete_testcase_run_with_report(
                        session_id=session_id,
                        final_content=(
                            streamed_assistant_content
                            if "streamed_assistant_content" in locals()
                            else ""
                        ),
                        messages=(all_messages if "all_messages" in locals() else []),
                        error_message="自动化执行流被取消或中断，未完成的步骤未执行。",
                    )
                    if (
                        generate_playwright_script and cancelled_record
                        and cancelled_record.status == "pass" and not should_stop(session_id)
                    ):
                        await _queue_generated_ui_case(
                            record=cancelled_record,
                            llm_config_id=active_config.pk if "active_config" in locals() else None,
                        )
                except Exception:
                    logger.exception(
                        "AgentLoopStreamAPI: Failed to finalize cancelled testcase run. "
                        "session_id=%s test_case_id=%s",
                        session_id,
                        test_case_id,
                    )
            logger.warning(
                "AgentLoopStreamAPI: Testcase stream cancelled. "
                "session_id=%s test_case_id=%s",
                session_id,
                test_case_id,
            )
            raise
        except Exception as e:
            if locals().get("hybrid_fallback_context"):
                try:
                    await finalize_hybrid_fallback_error(str(e))
                except Exception:
                    logger.exception(
                        "AgentLoopStreamAPI: Failed to finalize hybrid fallback error. session_id=%s",
                        session_id,
                    )
            friendly_error = get_user_friendly_llm_error(e)
            if friendly_error:
                logger.warning(
                    "AgentLoopStreamAPI: Friendly model error. session_id=%s, error_code=%s, message=%s",
                    session_id,
                    friendly_error.get("error_code"),
                    friendly_error.get("message"),
                )
                yield create_sse_data(_build_sse_error_event(e))
            else:
                logger.error(
                    "AgentLoopStreamAPI: Error. session_id=%s, thread_id=%s, model=%s, "
                    "error_type=%s, error=%s",
                    session_id,
                    thread_id,
                    model_name if "model_name" in locals() else "unknown",
                    type(e).__name__,
                    e,
                    exc_info=True,
                )
                yield create_sse_data(
                    {"type": "error", "message": f"执行错误: {str(e)}"}
                )

    async def post(self, request, *args, **kwargs):
        """
        处理聊天请求

        支持 stream 参数：
        - stream=true (默认)：返回 SSE 流式响应
        - stream=false：返回普通 JSON 响应
        """
        # 1. 认证
        try:
            user = await self.authenticate_request(request)
            request.user = user
        except AuthenticationFailed as e:
            return api_error_response(str(e), 401)

        # 2. 解析请求
        try:
            body_data = json.loads(request.body.decode("utf-8"))
        except json.JSONDecodeError as e:
            return api_error_response(f"Invalid JSON: {e}", 400)

        user_message = body_data.get("message")
        session_id = body_data.get("session_id")
        project_id = body_data.get("project_id")
        knowledge_base_id = body_data.get("knowledge_base_id")
        use_knowledge_base = body_data.get("use_knowledge_base", True)
        prompt_id = body_data.get("prompt_id")
        file_ids = body_data.get("file_ids", [])

        # 调试日志：知识库参数
        logger.info(
            f"AgentLoopStreamAPI: knowledge_base_id={knowledge_base_id}, use_knowledge_base={use_knowledge_base}"
        )
        uploaded_images_base64 = _normalize_uploaded_image_base64_list(
            body_data.get("images"),
            body_data.get("image"),
        )

        # stream 参数：控制流式/非流式输出（默认 true）
        stream_mode = body_data.get("stream", True)
        if isinstance(stream_mode, str):
            stream_mode = stream_mode.lower() in ("true", "1", "yes")

        # Playwright 脚本生成参数
        generate_playwright_script = body_data.get("generate_playwright_script", False)
        test_case_id = body_data.get("test_case_id")  # 用于关联生成的脚本
        execution_mode = body_data.get("execution_mode")
        ui_test_case_id = body_data.get("ui_test_case_id")
        use_pytest = body_data.get("use_pytest", True)  # 生成 pytest 格式还是简单格式

        # 兜底：如果前端没传 test_case_id，尝试从消息中解析
        if not test_case_id and user_message:
            import re

            # 匹配“执行ID为 11 的测试用例”、“测试用例 ID：11”及
            # 用例管理按钮实际发送的“测试用例 ID=11”等模式。
            match = re.search(
                r"(?:执行\s*ID\s*为|测试用例\s*(?:ID|id)\s*[：:=]\s*|case[_-]?id\s*[：:=]\s*)(\d+)",
                user_message,
            )
            if match:
                test_case_id = int(match.group(1))
                logger.info(
                    f"AgentLoopStreamAPI: Parsed test_case_id from message: {test_case_id}"
                )

        # 3. 参数验证
        if not project_id:
            return api_error_response("project_id is required", 400)

        if not user_message:
            return api_error_response("message is required", 400)

        # 4. 项目权限检查
        project = await sync_to_async(check_project_permission)(
            request.user, project_id
        )
        if not project:
            return api_error_response("Project access denied", 403)

        # 旧调用方未传执行模式时沿用用例默认值；新执行弹窗会显式传入本次选择。
        allowed_execution_modes = {"hybrid", "script_only", "ai_only"}
        if test_case_id:
            from testcases.models import TestCase

            testcase_runtime = await sync_to_async(
                lambda: TestCase.objects.filter(id=test_case_id, project=project)
                .values("execution_mode", "ui_test_case_id")
                .first()
            )()
            if testcase_runtime is None:
                return api_error_response("test_case_id is invalid for this project", 400)
            if execution_mode is None:
                execution_mode = testcase_runtime.get("execution_mode") or "hybrid"
            if ui_test_case_id is None:
                ui_test_case_id = testcase_runtime.get("ui_test_case_id")
            if ui_test_case_id is not None:
                from ui_automation.models import UiTestCase

                valid_ui_case = await sync_to_async(
                    UiTestCase.objects.filter(
                        id=ui_test_case_id, project=project
                    ).exists
                )()
                if not valid_ui_case:
                    return api_error_response("ui_test_case_id is invalid for this project", 400)
        execution_mode = execution_mode or "ai_only"
        if execution_mode not in allowed_execution_modes:
            return api_error_response("execution_mode is invalid", 400)
        if test_case_id and execution_mode == "script_only" and not ui_test_case_id:
            return api_error_response(
                "script_only requires a bound ui_test_case_id", 400
            )

        # 5. 生成 session_id
        if not session_id:
            session_id = uuid.uuid4().hex
            logger.info(f"AgentLoopStreamAPI: Generated new session_id: {session_id}")

        # 5.1 清理陈旧停止信号，避免上一次"停止"残留影响本轮首次发送
        # 场景：前端先断开 SSE，再调用 stop API，可能导致信号留存到下一次请求
        if clear_stop_signal(session_id):
            logger.info(
                f"AgentLoopStreamAPI: Cleared stale stop signal for session {session_id}"
            )

        # 6. 根据 stream 参数决定响应方式
        if stream_mode:
            # 流式响应 (SSE)
            async def async_generator():
                async for chunk in self._create_stream_generator(
                    request,
                    user_message,
                    session_id,
                    project_id,
                    project,
                    knowledge_base_id,
                    use_knowledge_base,
                    prompt_id,
                    uploaded_images_base64,
                    generate_playwright_script,
                    test_case_id,
                    use_pytest,
                    file_ids,
                    execution_mode,
                    ui_test_case_id,
                ):
                    yield chunk

            response = StreamingHttpResponse(
                async_generator(), content_type="text/event-stream; charset=utf-8"
            )
            response["Cache-Control"] = "no-cache"
            response["X-Accel-Buffering"] = "no"
            return response
        else:
            # 非流式响应 (JSON)
            return await self._handle_non_stream_request(
                request,
                user_message,
                session_id,
                project_id,
                project,
                knowledge_base_id,
                use_knowledge_base,
                prompt_id,
                uploaded_images_base64,
                generate_playwright_script,
                test_case_id,
                use_pytest,
                file_ids,
            )

    async def _handle_non_stream_request(
        self,
        request,
        user_message: str,
        session_id: str,
        project_id: str,
        project: Project,
        knowledge_base_id: Optional[int] = None,
        use_knowledge_base: bool = True,
        prompt_id: Optional[int] = None,
        uploaded_images_base64: Optional[List[str]] = None,
        generate_playwright_script: bool = False,
        test_case_id: Optional[int] = None,
        use_pytest: bool = True,
        file_ids: Optional[List[int]] = None,
        execution_mode: str = "ai_only",
        ui_test_case_id: Optional[int] = None,
    ) -> JsonResponse:
        """
        处理非流式请求，收集所有流式事件后返回统一 JSON 响应
        """
        final_content = ""
        final_session_id = session_id
        tool_results = []
        total_steps = 0
        context_token_count = 0
        context_limit = 128000
        error_message = None
        error_status_code = 500
        error_details = None
        interrupt_info = None
        script_generation = None

        try:
            async for chunk in self._create_stream_generator(
                request,
                user_message,
                session_id,
                project_id,
                project,
                knowledge_base_id,
                use_knowledge_base,
                prompt_id,
                uploaded_images_base64,
                generate_playwright_script,
                test_case_id,
                use_pytest,
                file_ids,
                execution_mode,
                ui_test_case_id,
            ):
                # 解析 SSE 数据
                if isinstance(chunk, str) and chunk.startswith("data: "):
                    data_str = chunk[6:].strip()
                    if data_str == "[DONE]":
                        continue
                    try:
                        event = json.loads(data_str)
                        event_type = event.get("type")

                        if event_type == "start":
                            final_session_id = event.get("session_id", session_id)
                        elif event_type == "stream":
                            # 累积流式内容
                            final_content += event.get("data", "")
                        elif event_type == "tool_result":
                            tool_results.append(
                                {
                                    "summary": event.get("summary", ""),
                                    "tool_output": event.get("tool_output"),
                                    "tool_name": event.get("tool_name"),
                                    "step": event.get("step", 0),
                                }
                            )
                        elif event_type == "step_complete":
                            total_steps = max(total_steps, event.get("step", 0))
                        elif event_type == "context_update":
                            context_token_count = event.get("context_token_count", 0)
                            context_limit = event.get("context_limit", 128000)
                        elif event_type == "error":
                            error_message = event.get("message", "Unknown error")
                            error_status_code = event.get("code", 500)
                            error_details = event.get("errors")
                        elif event_type == "interrupt":
                            interrupt_info = {
                                "interrupt_id": event.get("interrupt_id"),
                                "action_requests": event.get("action_requests", []),
                            }
                        elif event_type == "complete":
                            if event.get("script_generation"):
                                script_generation = event.get("script_generation")
                    except json.JSONDecodeError:
                        continue

            # 构建响应
            if error_message:
                return api_error_response(
                    error_message, error_status_code, error_details
                )

            response_data = {
                "session_id": final_session_id,
                "content": final_content,
                "total_steps": total_steps,
                "tool_results": tool_results,
                "context_token_count": context_token_count,
                "context_limit": context_limit,
            }

            if interrupt_info:
                response_data["interrupt"] = interrupt_info

            if script_generation:
                response_data["script_generation"] = script_generation

            return api_success_response("Chat completed", response_data)

        except Exception as e:
            logger.error(
                "AgentLoopStreamAPI: Non-stream request error. session_id=%s, project_id=%s, "
                "error_type=%s, error=%s",
                session_id,
                project_id,
                type(e).__name__,
                e,
                exc_info=True,
            )
            friendly_error = get_user_friendly_llm_error(e)
            if friendly_error:
                return api_error_response(
                    friendly_error["message"],
                    friendly_error["status_code"],
                    friendly_error["errors"],
                )
            return api_error_response(f"执行错误: {str(e)}", 500)


@method_decorator(csrf_exempt, name="dispatch")
class AgentLoopStopAPIView(View):
    """
    Agent Loop 停止 API

    用于中断正在执行的 Agent Loop 任务。
    """

    async def authenticate_request(self, request):
        """JWT 认证（复用 AgentLoopStreamAPIView 的逻辑）"""
        auth_header = request.META.get("HTTP_AUTHORIZATION")
        if not auth_header or not auth_header.startswith("Bearer "):
            raise AuthenticationFailed("Authentication credentials were not provided.")

        token = auth_header.split(" ")[1]
        jwt_auth = JWTAuthentication()

        try:
            validated_token = await sync_to_async(jwt_auth.get_validated_token)(token)
            user = await sync_to_async(jwt_auth.get_user)(validated_token)
            return user
        except Exception as e:
            raise AuthenticationFailed(f"Invalid token: {str(e)}")

    async def post(self, request, *args, **kwargs):
        """处理停止请求"""
        from .stop_signal import set_stop_signal

        # 1. 认证
        try:
            user = await self.authenticate_request(request)
            request.user = user
        except AuthenticationFailed as e:
            return api_error_response(str(e), 401)

        # 2. 解析请求
        try:
            body_data = json.loads(request.body.decode("utf-8"))
        except json.JSONDecodeError as e:
            return api_error_response(f"Invalid JSON: {e}", 400)

        session_id = body_data.get("session_id")
        if not session_id:
            return api_error_response("session_id is required", 400)

        from testcases.models import TestCaseRunRecord
        from .ui_generation_jobs import request_job_stop
        generation_record = await sync_to_async(
            TestCaseRunRecord.objects.filter(session_id=session_id).first
        )()
        if generation_record:
            if generation_record.executor_id != user.pk and not user.is_superuser:
                return api_error_response("无权停止其他用户的任务", 403)
            await sync_to_async(request_job_stop)(generation_record)

        # 3. 设置停止信号
        success = set_stop_signal(session_id)

        # 停止不能只依赖运行中的流在“下一次产出”时轮询信号：当模型请求
        # 正在等待或重试时，流不会产出任何 chunk，记录会长时间停留在执行中。
        # 立即结束用例运行记录；后续旧请求即使返回，也不会覆盖已停止的结果。
        record, _ = await _complete_testcase_run_with_report(
            session_id=session_id,
            stopped=True,
            error_message="用户主动停止执行",
        )

        logger.info(
            f"AgentLoopStopAPI: Stop signal set for session {session_id} by user {user.id}"
        )

        return api_success_response(
            "已停止执行",
            {
                "session_id": session_id,
                "success": success,
                "run_record_id": getattr(record, "id", None),
                "run_status": getattr(record, "status", None),
            },
        )


@method_decorator(csrf_exempt, name="dispatch")
class AgentLoopResumeAPIView(View):
    """
    Agent Loop Resume API (SSE 流式版)

    用于恢复被 HITL 中断的 Agent Loop 任务。
    接收用户对工具调用的审批决策，然后通过 SSE 流式返回后续执行结果。

    这样前端可以像处理主流一样处理 resume 后的工具执行和 LLM 响应。
    """

    # 最大步骤数（与主流保持一致）
    MAX_STEPS = 500

    async def authenticate_request(self, request):
        """JWT 认证（复用 AgentLoopStreamAPIView 的逻辑）"""
        auth_header = request.META.get("HTTP_AUTHORIZATION")
        if not auth_header or not auth_header.startswith("Bearer "):
            raise AuthenticationFailed("Authentication credentials were not provided.")

        token = auth_header.split(" ")[1]
        jwt_auth = JWTAuthentication()

        try:
            validated_token = await sync_to_async(jwt_auth.get_validated_token)(token)
            user = await sync_to_async(jwt_auth.get_user)(validated_token)
            return user
        except Exception as e:
            raise AuthenticationFailed(f"Invalid token: {str(e)}")

    async def _create_resume_stream_generator(
        self,
        user,
        session_id: str,
        project_id: str,
        resume_data: dict,
        knowledge_base_id: Optional[str] = None,
        use_knowledge_base: bool = False,
    ):
        """
        创建 Resume SSE 流式生成器

        与主流的 _create_stream_generator 类似，但使用 Command(resume=...) 来恢复执行。
        """
        from langgraph.types import Command

        # 1. 解析 resume 数据
        interrupt_id = list(resume_data.keys())[0] if resume_data else None
        if not interrupt_id:
            yield create_sse_data(
                {"type": "error", "message": "Invalid resume data format"}
            )
            return

        decision_info = resume_data[interrupt_id].get("decisions", [{}])[0]
        decision_type = decision_info.get("type", "reject")

        # 获取工具调用数量（前端传递）
        action_count = resume_data[interrupt_id].get("action_count", 1)

        # 构建 resume 值 - HITL middleware 需要 decisions 格式
        # 为每个 pending 工具调用生成相同的决策
        resume_value = {
            "decisions": [{"type": decision_type} for _ in range(action_count)]
        }

        # 2. 发送 resume 开始信号
        yield create_sse_data(
            {
                "type": "resume_start",
                "session_id": session_id,
                "decision": decision_type,
            }
        )

        try:
            async with get_async_checkpointer() as checkpointer:
                # 3. 获取 LLM 配置
                active_config = await sync_to_async(
                    LLMConfig.objects.filter(is_active=True).first
                )()

                if not active_config:
                    yield create_sse_data(
                        {"type": "error", "message": "没有可用的 LLM 配置"}
                    )
                    return

                context_limit = active_config.context_limit or 128000
                model_name = active_config.name or "gpt-4o"
                llm = await sync_to_async(create_llm_instance)(active_config)
                context_limit = resolve_runtime_context_limit(
                    active_config.context_limit, llm, model_name
                )

                # 4. 加载工具
                tools = []

                # 加载 MCP 工具
                try:
                    active_mcp_configs = await sync_to_async(list)(
                        RemoteMCPConfig.objects.filter(is_active=True)
                    )
                    if active_mcp_configs:
                        client_config = {}
                        for cfg in active_mcp_configs:
                            key = cfg.name or f"remote_{cfg.id}"
                            client_config[key] = {
                                "url": cfg.url,
                                "transport": (
                                    cfg.transport or "streamable_http"
                                ).replace("-", "_"),
                            }
                            if cfg.headers:
                                client_config[key]["headers"] = cfg.headers

                        if client_config:
                            mcp_tools = await mcp_session_manager.get_tools_for_config(
                                client_config,
                                user_id=str(user.id),
                                project_id=str(project_id) if project_id else "0",
                                session_id=session_id,
                            )
                            tools.extend(mcp_tools)
                            logger.info(
                                f"AgentLoopResumeAPI: Loaded {len(mcp_tools)} MCP tools"
                            )
                except Exception as e:
                    logger.warning(f"AgentLoopResumeAPI: MCP tools loading failed: {e}")

                # 加载知识库工具
                if knowledge_base_id and use_knowledge_base:
                    try:
                        from knowledge.langgraph_integration import (
                            create_knowledge_tool,
                        )

                        kb_tool = await sync_to_async(create_knowledge_tool)(
                            knowledge_base_id=knowledge_base_id, user=user
                        )
                        tools.append(kb_tool)
                        logger.info(
                            f"AgentLoopResumeAPI: ✅ 知识库工具已添加: {kb_tool.name}"
                        )
                    except Exception as e:
                        logger.warning(
                            f"AgentLoopResumeAPI: ❌ Knowledge tool creation failed: {e}"
                        )

                # 加载内置工具
                try:
                    from orchestrator_integration.builtin_tools import get_builtin_tools

                    builtin_tools = get_builtin_tools(
                        user_id=user.id,
                        project_id=int(project_id) if project_id else 0,
                        test_case_id=None,
                        chat_session_id=session_id,
                    )
                    tools.extend(builtin_tools)
                    logger.info(
                        f"AgentLoopResumeAPI: Added {len(builtin_tools)} builtin tools"
                    )
                except Exception as e:
                    logger.warning(
                        f"AgentLoopResumeAPI: Builtin tools loading failed: {e}"
                    )

                # 5. 获取工具名列表和中间件配置
                # 尝试从 ChatSession 关联的 prompt 获取系统提示词，用于精确计算 overhead
                resume_system_prompt = None
                try:
                    chat_session = await sync_to_async(
                        ChatSession.objects.filter(session_id=session_id).select_related("prompt").first
                    )()
                    if chat_session and chat_session.prompt and chat_session.prompt.content:
                        resume_system_prompt = chat_session.prompt.content
                except Exception:
                    pass

                tool_names = [t.name for t in tools] if tools else []
                middleware = await sync_to_async(get_middleware_from_config)(
                    active_config,
                    llm,
                    user=user,
                    session_id=session_id,
                    all_tool_names=tool_names,
                    tools=tools,
                    system_prompt=resume_system_prompt,
                )

                # 6. 创建 agent
                agent = create_agent(
                    llm,
                    tools,
                    checkpointer=checkpointer,
                    middleware=middleware,
                )

                thread_id = (
                    f"{user.id}_{project_id}_{session_id}" if project_id else session_id
                )
                config = {
                    "configurable": {"thread_id": thread_id},
                    "recursion_limit": 1000,
                }

                # 6.1 恢复执行前，先修复历史消息（配对错误 + 风险工具输出）
                await _sanitize_history_before_model_call(
                    agent=agent,
                    invoke_config=config,
                    log_prefix="AgentLoopResumeAPI",
                )

                # 7. 构建 Command 来 resume
                command = Command(resume=resume_value)

                # 8. 步骤跟踪状态
                step_count = 0
                interrupt_detected = False

                # 9. 流式执行
                try:
                    async for stream_mode, chunk in agent.astream(
                        command, config=config, stream_mode=["updates", "messages"]
                    ):
                        if stream_mode == "updates":
                            # 检查中断事件 (HITL) - resume 后可能又触发新的中断
                            if isinstance(chunk, dict) and "__interrupt__" in chunk:
                                interrupt_info = chunk["__interrupt__"]
                                logger.info(
                                    f"AgentLoopResumeAPI: HITL interrupt detected after resume: {interrupt_info}"
                                )

                                action_requests = []
                                new_interrupt_id = None

                                if isinstance(interrupt_info, (list, tuple)):
                                    interrupts_list = list(interrupt_info)
                                else:
                                    interrupts_list = [interrupt_info]

                                for intr in interrupts_list:
                                    if hasattr(intr, "id"):
                                        new_interrupt_id = intr.id
                                    elif isinstance(intr, dict) and "id" in intr:
                                        new_interrupt_id = intr["id"]

                                    intr_value = (
                                        getattr(intr, "value", intr)
                                        if hasattr(intr, "value")
                                        else intr
                                    )
                                    if isinstance(intr_value, dict):
                                        ars = intr_value.get("action_requests", [])
                                    elif hasattr(intr_value, "action_requests"):
                                        ars = intr_value.action_requests
                                    else:
                                        ars = []

                                    for ar in ars:
                                        if isinstance(ar, dict):
                                            action_requests.append(
                                                {
                                                    "name": ar.get(
                                                        "name",
                                                        ar.get(
                                                            "action_name", "unknown"
                                                        ),
                                                    ),
                                                    "args": ar.get(
                                                        "arguments", ar.get("args", {})
                                                    ),
                                                    "description": ar.get(
                                                        "description", ""
                                                    ),
                                                }
                                            )
                                        else:
                                            action_requests.append(
                                                {
                                                    "name": getattr(
                                                        ar, "name", "unknown"
                                                    ),
                                                    "args": getattr(
                                                        ar,
                                                        "arguments",
                                                        getattr(ar, "args", {}),
                                                    ),
                                                    "description": getattr(
                                                        ar, "description", ""
                                                    ),
                                                }
                                            )

                                if action_requests:
                                    # 获取用户工具偏好，为 always_reject 的工具添加 auto_reject 标记
                                    user_approvals = await sync_to_async(
                                        get_user_tool_approvals
                                    )(user, session_id)
                                    for ar in action_requests:
                                        tool_name = ar.get("name", "")
                                        if (
                                            user_approvals.get(tool_name)
                                            == "always_reject"
                                        ):
                                            ar["auto_reject"] = True
                                            logger.info(
                                                f"AgentLoopResumeAPI: Tool {tool_name} marked as auto_reject"
                                            )

                                    interrupt_detected = True
                                    yield create_sse_data(
                                        {
                                            "type": "interrupt",
                                            "interrupt_id": new_interrupt_id
                                            or str(id(interrupt_info)),
                                            "action_requests": action_requests,
                                            "session_id": session_id,
                                            "thread_id": thread_id,
                                        }
                                    )

                            # 检测工具调用开始
                            elif isinstance(chunk, dict):
                                for node_name, node_output in chunk.items():
                                    if node_name == "agent" and isinstance(
                                        node_output, dict
                                    ):
                                        messages = node_output.get("messages", [])
                                        for msg in messages:
                                            if (
                                                hasattr(msg, "tool_calls")
                                                and msg.tool_calls
                                            ):
                                                step_count += 1
                                                tool_names_in_step = [
                                                    tc.get("name", "unknown")
                                                    if isinstance(tc, dict)
                                                    else getattr(tc, "name", "unknown")
                                                    for tc in msg.tool_calls
                                                ]
                                                yield create_sse_data(
                                                    {
                                                        "type": "step_start",
                                                        "step": step_count,
                                                        "max_steps": self.MAX_STEPS,
                                                        "tools": tool_names_in_step,
                                                    }
                                                )
                                                for progress_event in await _emit_testcase_tool_started(
                                                    msg.tool_calls,
                                                    session_id=session_id,
                                                    test_case_id=test_case_id,
                                                ):
                                                    yield create_sse_data(progress_event)

                                    elif node_name == "tools" and isinstance(
                                        node_output, dict
                                    ):
                                        tool_messages = node_output.get("messages", [])
                                        for tool_msg in tool_messages:
                                            if hasattr(tool_msg, "content"):
                                                content = tool_msg.content
                                                tool_name = getattr(
                                                    tool_msg, "name", None
                                                ) or getattr(
                                                    tool_msg, "tool_name", "unknown"
                                                )

                                                # 使用辅助函数处理 MCP 工具输出
                                                content, summary = (
                                                    process_mcp_tool_output(content)
                                                )

                                                helper_case_id, helper_step = _extract_helper_testcase_progress(content)
                                                if helper_step is not None:
                                                    total_steps = await sync_to_async(
                                                        _get_testcase_step_count
                                                    )(helper_case_id)
                                                    progress_text = (
                                                        f"正在执行步骤 {helper_step}/{total_steps}"
                                                        if total_steps
                                                        else f"正在执行步骤 {helper_step}"
                                                    )
                                                    content = f"[执行进度] {progress_text}\n{content}"
                                                    yield create_sse_data(
                                                        {
                                                            "type": "testcase_progress",
                                                            "session_id": session_id,
                                                            "step": helper_step,
                                                            "total_steps": total_steps,
                                                            "state": "completed",
                                                            "message": "步骤执行完成，正在校验结果",
                                                        }
                                                    )

                                                yield create_sse_data(
                                                    {
                                                        "type": "tool_result",
                                                        "tool_name": tool_name,
                                                        "tool_output": content,
                                                        "summary": summary,
                                                        "step": step_count,
                                                    }
                                                )
                                                resume_markers = re.findall(
                                                    r"RESULT\s*=\s*(PASS|FAIL)\b",
                                                    str(content),
                                                    flags=re.IGNORECASE,
                                                )
                                                if (
                                                    test_case_id
                                                    and tool_name == "execute_skill_script"
                                                    and helper_step is not None
                                                    and total_steps
                                                    and helper_step < total_steps
                                                    and resume_markers
                                                    and resume_markers[-1].upper() == "PASS"
                                                ):
                                                    yield create_sse_data(
                                                        {
                                                            "type": "testcase_progress",
                                                            "session_id": session_id,
                                                            "test_case_id": int(test_case_id),
                                                            "step": helper_step,
                                                            "total_steps": total_steps,
                                                            "state": "waiting_llm",
                                                            "message": (
                                                                "浏览器步骤已完成，正在调用模型生成下一步脚本"
                                                                "（模型或网络较慢时可能等待数分钟）"
                                                            ),
                                                        }
                                                    )
                                        if step_count > 0:
                                            yield create_sse_data(
                                                {
                                                    "type": "step_complete",
                                                    "step": step_count,
                                                }
                                            )

                        elif stream_mode == "messages":
                            # LLM Token 流式输出
                            # messages 模式返回元组 (token, metadata)
                            if isinstance(chunk, tuple) and len(chunk) >= 1:
                                token = chunk[0]
                                # 只发送 AI 消息，过滤掉 ToolMessage（工具结果已通过 tool_result 事件发送）
                                if hasattr(token, "content") and token.content:
                                    # 检查是否是 ToolMessage（通过类名或 type 属性）
                                    token_type = type(token).__name__
                                    if "ToolMessage" not in token_type:
                                        text_chunk = _stream_content_to_text(token.content)
                                        if not text_chunk:
                                            continue
                                        yield create_sse_data(
                                            {"type": "stream", "data": text_chunk}
                                        )
                            elif hasattr(chunk, "content") and chunk.content:
                                # 兼容旧版本可能直接返回 message 的情况
                                # 同样过滤掉 ToolMessage
                                chunk_type = type(chunk).__name__
                                if "ToolMessage" not in chunk_type:
                                    text_chunk = _stream_content_to_text(chunk.content)
                                    if not text_chunk:
                                        continue
                                    yield create_sse_data(
                                        {"type": "stream", "data": text_chunk}
                                    )

                except Exception as e:
                    friendly_error = get_user_friendly_llm_error(e)
                    if friendly_error:
                        logger.warning(
                            "AgentLoopResumeAPI: Friendly model error. session_id=%s, error_code=%s, message=%s",
                            session_id,
                            friendly_error.get("error_code"),
                            friendly_error.get("message"),
                        )
                        yield create_sse_data(_build_sse_error_event(e))
                    else:
                        logger.error(
                            "AgentLoopResumeAPI: Streaming error: %s", e, exc_info=True
                        )
                        yield create_sse_data(
                            {"type": "error", "message": f"Streaming error: {str(e)}"}
                        )

                # 10. 处理结束状态
                # 无论是否发生 interrupt，都需要计算和发送 context_update
                try:
                    current_state = await agent.aget_state(config)
                    all_messages = (
                        current_state.values.get("messages", [])
                        if current_state.values
                        else []
                    )

                    # 获取当前上下文 token 使用量（优先 usage_metadata，回退估算）
                    input_tokens, output_tokens, total_tokens = (
                        calculate_context_tokens(
                            all_messages, model_name,
                            tools=tools, system_prompt=resume_system_prompt,
                        )
                    )

                    yield create_sse_data(
                        {
                            "type": "context_update",
                            "context_token_count": total_tokens,
                            "context_limit": context_limit,
                        }
                    )

                    # 记录 Token 使用量到 ChatSession + TokenUsageRecord
                    if input_tokens > 0 or output_tokens > 0:
                        # 提取缓存命中信息
                        cache_read_tokens = 0
                        for msg in reversed(all_messages):
                            if hasattr(msg, "usage_metadata") and msg.usage_metadata:
                                cache_info = msg.usage_metadata.get("input_token_details", {})
                                cache_read_tokens = cache_info.get("cache_read", 0) or 0
                                break

                        await sync_to_async(
                            AgentLoopStreamAPIView()._update_session_token_usage
                        )(
                            session_id, input_tokens, output_tokens,
                            cache_read_tokens=cache_read_tokens,
                            user_id=user.id,
                            project_id=int(project_id) if project_id else None,
                        )
                        logger.info(
                            "AgentLoopResumeAPI: Token usage - input=%d, output=%d, cache_read=%d",
                            input_tokens, output_tokens, cache_read_tokens,
                        )
                except Exception as e:
                    logger.warning(
                        f"AgentLoopResumeAPI: Failed to calculate token count: {e}"
                    )

                if interrupt_detected:
                    logger.info(
                        "AgentLoopResumeAPI: New interrupt detected after resume"
                    )
                else:
                    yield create_sse_data(
                        {
                            "type": "complete",
                            "total_steps": step_count,
                            "decision": decision_type,
                        }
                    )

                yield "data: [DONE]\n\n"

        except Exception as e:
            friendly_error = get_user_friendly_llm_error(e)
            if friendly_error:
                logger.warning(
                    "AgentLoopResumeAPI: Friendly model error. session_id=%s, error_code=%s, message=%s",
                    session_id,
                    friendly_error.get("error_code"),
                    friendly_error.get("message"),
                )
                yield create_sse_data(_build_sse_error_event(e))
            else:
                logger.exception(
                    "AgentLoopResumeAPI: Error in resume stream for session %s",
                    session_id,
                )
                yield create_sse_data({"type": "error", "message": str(e)})

    async def post(self, request, *args, **kwargs):
        """处理 HITL resume 请求 - 返回 SSE 流式响应"""
        # 1. 认证
        try:
            user = await self.authenticate_request(request)
            request.user = user
        except AuthenticationFailed as e:
            return StreamingHttpResponse(
                iter(
                    [create_sse_data({"type": "error", "message": str(e), "code": 401})]
                ),
                content_type="text/event-stream; charset=utf-8",
                status=401,
            )

        # 2. 解析请求
        try:
            body_data = json.loads(request.body.decode("utf-8"))
        except json.JSONDecodeError as e:
            return StreamingHttpResponse(
                iter(
                    [
                        create_sse_data(
                            {
                                "type": "error",
                                "message": f"Invalid JSON: {e}",
                                "code": 400,
                            }
                        )
                    ]
                ),
                content_type="text/event-stream; charset=utf-8",
                status=400,
            )

        session_id = body_data.get("session_id")
        project_id = body_data.get("project_id")
        resume_data = body_data.get("resume", {})
        # 知识库参数（用于 resume 时重新加载知识库工具）
        knowledge_base_id = body_data.get("knowledge_base_id")
        use_knowledge_base = body_data.get("use_knowledge_base", False)

        if not session_id:
            return StreamingHttpResponse(
                iter(
                    [
                        create_sse_data(
                            {
                                "type": "error",
                                "message": "session_id is required",
                                "code": 400,
                            }
                        )
                    ]
                ),
                content_type="text/event-stream; charset=utf-8",
                status=400,
            )

        if not resume_data:
            return StreamingHttpResponse(
                iter(
                    [
                        create_sse_data(
                            {
                                "type": "error",
                                "message": "resume data is required",
                                "code": 400,
                            }
                        )
                    ]
                ),
                content_type="text/event-stream; charset=utf-8",
                status=400,
            )

        logger.info(
            f"AgentLoopResumeAPI: Resume request for session {session_id}, knowledge_base_id={knowledge_base_id}"
        )

        # 3. 返回 SSE 流式响应
        async def async_generator():
            async for chunk in self._create_resume_stream_generator(
                user,
                session_id,
                project_id,
                resume_data,
                knowledge_base_id,
                use_knowledge_base,
            ):
                yield chunk

        response = StreamingHttpResponse(
            async_generator(), content_type="text/event-stream; charset=utf-8"
        )
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response
