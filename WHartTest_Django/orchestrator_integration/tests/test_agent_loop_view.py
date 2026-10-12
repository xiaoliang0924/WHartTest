import json
import os
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from django.test import TestCase
from django.test.utils import override_settings
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk

from .. import agent_loop_view
from ..agent_loop_view import (
    _decode_sse_payload,
    _stream_content_to_text,
    _is_final_testcase_step_pass,
    _extract_helper_testcase_progress,
    _extract_helper_result_reason,
    _build_helper_step_commentary,
    _latest_passed_helper_step_from_messages,
    _explicitly_failed_helper_step_from_messages,
    _testcase_step_was_attempted,
    _build_incomplete_testcase_resume_prompt,
    _build_incomplete_ai_resume_prompt,
    _build_sanitized_messages,
    _extract_linked_image_urls,
    _is_linked_image_url_allowed,
    _normalize_uploaded_image_base64_list,
    _prepare_agent_loop_human_message,
    _build_ui_generation_observations,
    _normalize_generated_ui_payload,
    _force_save_generated_ui_case,
    _run_and_stream_remaining_helper_steps,
    _extract_testcase_step_from_tool_call,
    _map_ui_script_results_to_testcase_steps,
    _testcase_bound_ui_timeout_seconds,
    _mcp_tool_load_timeout_seconds,
    _promote_hidden_current_route_navigation,
)
from ..builtin_tools.skill_tools import (
    _is_retryable_case_tool_error,
    _normalize_testcase_skill_arguments,
    _record_testcase_step_tool_result,
)
from ..middleware_config import build_dynamic_interrupt_on
from ..playwright_instructions import MANUAL_TESTCASE_EXECUTION_HINT


class SsePayloadDecodeTests(SimpleTestCase):
    def test_decodes_json_sse_payload(self):
        payload = _decode_sse_payload('data: {"type":"complete","status":"pass"}\n\n')
        self.assertEqual(payload, {"type": "complete", "status": "pass"})

    def test_ignores_non_json_terminal_event(self):
        self.assertIsNone(_decode_sse_payload("data: [DONE]\n\n"))


class AnthropicStreamContentTests(SimpleTestCase):
    def test_normalizes_anthropic_text_blocks_and_skips_non_text_blocks(self):
        content = [
            {"type": "text", "text": "Claude "},
            {"type": "thinking", "thinking": "private reasoning"},
            {"type": "text", "text": "response"},
        ]

        self.assertEqual(_stream_content_to_text(content), "Claude response")

    def test_preserves_plain_text_chunks(self):
        self.assertEqual(_stream_content_to_text("hello"), "hello")


class BoundUiTimeoutConfigTests(SimpleTestCase):
    def test_uses_safe_default_timeout(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TESTCASE_BOUND_UI_TIMEOUT_SECONDS", None)
            self.assertEqual(_testcase_bound_ui_timeout_seconds(), 600.0)

    def test_accepts_configured_timeout_and_enforces_minimum(self):
        with patch.dict(os.environ, {"TESTCASE_BOUND_UI_TIMEOUT_SECONDS": "900"}):
            self.assertEqual(_testcase_bound_ui_timeout_seconds(), 900.0)
        with patch.dict(os.environ, {"TESTCASE_BOUND_UI_TIMEOUT_SECONDS": "1"}):
            self.assertEqual(_testcase_bound_ui_timeout_seconds(), 30.0)

    def test_invalid_timeout_falls_back_to_default(self):
        with patch.dict(os.environ, {"TESTCASE_BOUND_UI_TIMEOUT_SECONDS": "invalid"}):
            self.assertEqual(_testcase_bound_ui_timeout_seconds(), 600.0)


class McpToolLoadTimeoutConfigTests(SimpleTestCase):
    def test_uses_safe_default_timeout(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_TOOL_LOAD_TIMEOUT_SECONDS", None)
            self.assertEqual(_mcp_tool_load_timeout_seconds(), 30.0)

    def test_accepts_configured_timeout_and_enforces_bounds(self):
        with patch.dict(os.environ, {"MCP_TOOL_LOAD_TIMEOUT_SECONDS": "90"}):
            self.assertEqual(_mcp_tool_load_timeout_seconds(), 90.0)
        with patch.dict(os.environ, {"MCP_TOOL_LOAD_TIMEOUT_SECONDS": "1"}):
            self.assertEqual(_mcp_tool_load_timeout_seconds(), 5.0)
        with patch.dict(os.environ, {"MCP_TOOL_LOAD_TIMEOUT_SECONDS": "999"}):
            self.assertEqual(_mcp_tool_load_timeout_seconds(), 300.0)

    def test_invalid_timeout_falls_back_to_default(self):
        with patch.dict(os.environ, {"MCP_TOOL_LOAD_TIMEOUT_SECONDS": "invalid"}):
            self.assertEqual(_mcp_tool_load_timeout_seconds(), 30.0)


class TestcaseSkillDefaultsTests(SimpleTestCase):
    def test_missing_skill_and_session_default_only_for_testcase_playwright(self):
        self.assertEqual(
            _normalize_testcase_skill_arguments(None, None, 175),
            ("playwright-skill", "case_175"),
        )
        self.assertEqual(
            _normalize_testcase_skill_arguments("whart-test", None, 175),
            ("whart-test", None),
        )
        self.assertEqual(
            _normalize_testcase_skill_arguments(None, None, None),
            (None, None),
        )

    def test_browser_errors_are_retryable_but_passes_are_not(self):
        self.assertTrue(
            _is_retryable_case_tool_error(
                "waiting for getByText('工单') to be visible: Timeout 10000ms exceeded"
            )
        )
        self.assertFalse(_is_retryable_case_tool_error("RESULT=PASS: 页面正常"))
        self.assertFalse(_is_retryable_case_tool_error("当前页面 URL: /work-order"))

    def test_repeated_errors_stop_at_three_and_a_clean_result_resets_count(self):
        failures = {}
        error = "waiting for button to be visible: Timeout 10000ms exceeded"
        self.assertIsNone(_record_testcase_step_tool_result(
            failures, testcase_id=175, step=3, output=error
        ))
        self.assertIsNone(_record_testcase_step_tool_result(
            failures, testcase_id=175, step=3, output=error
        ))
        terminal = _record_testcase_step_tool_result(
            failures, testcase_id=175, step=3, output=error
        )
        self.assertIn("[CASE_TOOL_RETRY_LIMIT]", terminal)
        self.assertIn("[CASE_STEP_EXECUTION] case=175 step=3", terminal)
        self.assertIn("RESULT=FAIL", terminal)
        self.assertIsNone(_record_testcase_step_tool_result(
            failures, testcase_id=175, step=3, output="页面已加载"
        ))
        self.assertEqual(failures, {})


class HitlToolSelectionTests(SimpleTestCase):
    def test_read_only_skill_tool_is_not_blocked_by_default(self):
        config = build_dynamic_interrupt_on(
            base_tools={
                "execute_skill_script": {
                    "allowed_decisions": ["approve", "reject"],
                },
            },
            all_tool_names=["read_skill_content", "execute_skill_script"],
        )

        self.assertNotIn("read_skill_content", config)
        self.assertIn("execute_skill_script", config)

    def test_explicitly_configured_tools_keep_hitl(self):
        config = build_dynamic_interrupt_on(
            base_tools={
                "playwright_click": {
                    "allowed_decisions": ["approve", "reject"],
                },
            },
            all_tool_names=["read_skill_content", "playwright_click"],
        )

        self.assertEqual(
            config["playwright_click"]["allowed_decisions"],
            ["approve", "reject"],
        )


class GeneratedUiCaseFollowupTests(SimpleTestCase):
    def test_helper_dropdown_action_expands_only_from_matching_observed_trace(self):
        payload = {"steps": [{"functional_step_numbers": [4], "actions": [{
            "type": "select_filter_option", "field_label": "工单状态", "option_text": "待处理",
        }]}]}
        actions = [
            {"type": "click", "selector": {"locator_type": "css", "locator_value": ".observed-select"}},
            {"type": "click", "selector": {"locator_type": "text", "locator_value": "待处理"}},
            {"type": "assert", "mode": "contain_text", "value": "待处理",
             "selector": {"locator_type": "css", "locator_value": ".observed-select"}},
        ]
        trace = {"version": 1, "kind": "select_filter_option", "field_label": "工单状态",
                 "option_text": "待处理", "actions": actions}

        normalized = _normalize_generated_ui_payload(json.dumps(payload, ensure_ascii=False), [trace])

        self.assertEqual(json.loads(normalized)["steps"][0]["actions"], actions)
        with self.assertRaisesRegex(ValueError, "没有对应的成功浏览器交互轨迹"):
            _normalize_generated_ui_payload(json.dumps(payload, ensure_ascii=False), [])

    def test_followup_persists_model_dropdown_action_from_helper_trace(self):
        actions = [
            {"type": "click", "selector": {"locator_type": "css", "locator_value": ".observed-trigger"}},
            {"type": "click", "selector": {"locator_type": "css", "locator_value": ".observed-option"}},
            {"type": "assert", "mode": "contain_text", "value": "待处理",
             "selector": {"locator_type": "css", "locator_value": ".observed-select"}},
        ]
        payload = {"steps": [{"functional_step_numbers": [4], "actions": [{
            "type": "select_filter_option", "field_label": "工单状态", "option_text": "待处理",
        }]}]}

        class SaveTool:
            name = "save_generated_ui_testcase"

            async def ainvoke(self, args):
                self.saved_payload = json.loads(args["payload_json"])
                return '{"status":"success","ui_test_case_id":321}'

        class BoundModel:
            async def ainvoke(self, _messages):
                return AIMessage(content="", tool_calls=[{
                    "name": "save_generated_ui_testcase",
                    "args": {"payload_json": json.dumps(payload, ensure_ascii=False)},
                    "id": "save-ui-trace",
                }])

        class FakeModel:
            def bind_tools(self, _tools, **_kwargs):
                return BoundModel()

        trace = {"version": 1, "kind": "select_filter_option", "field_label": "工单状态",
                 "option_text": "待处理", "actions": actions}
        messages = [
            AIMessage(content="", tool_calls=[{
                "name": "execute_skill_script", "args": {"command": "helpers.selectFormDropdownOption(...)"},
                "id": "browser-call",
            }]),
            ToolMessage(content=f"UI_ACTION_TRACE={json.dumps(trace, ensure_ascii=False)}",
                        tool_call_id="browser-call", name="execute_skill_script"),
        ]
        saver = SaveTool()

        result = async_to_sync(_force_save_generated_ui_case)(
            llm=FakeModel(), tools=[saver], messages=messages,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(saver.saved_payload["steps"][0]["actions"], actions)

    def test_environment_auth_validation_gets_one_corrective_save_attempt(self):
        invalid_payload = {"steps": [{
            "authentication_mode": "environment",
            "functional_step_numbers": [1],
            "actions": [{"type": "click", "selector": {
                "locator_type": "role", "locator_value": "button[name='登录']",
            }}],
        }]}
        corrected_payload = {"steps": [{
            "authentication_mode": "environment",
            "functional_step_numbers": [1],
            "actions": [{"type": "assert", "mode": "url", "match": "contains",
                         "value": "/work-order/tickets"}],
        }]}

        class SaveTool:
            name = "save_generated_ui_testcase"

            def __init__(self):
                self.payloads = []

            async def ainvoke(self, args):
                self.payloads.append(json.loads(args["payload_json"]))
                if len(self.payloads) == 1:
                    return json.dumps({
                        "status": "failed",
                        "message": "第 1 步由环境认证，只能保存登录成功的断言。",
                    }, ensure_ascii=False)
                return '{"status":"success","ui_test_case_id":322}'

        class BoundModel:
            def __init__(self):
                self.prompts = []

            async def ainvoke(self, messages):
                self.prompts.append(messages)
                payload = invalid_payload if len(self.prompts) == 1 else corrected_payload
                return AIMessage(content="", tool_calls=[{
                    "name": "save_generated_ui_testcase",
                    "args": {"payload_json": json.dumps(payload, ensure_ascii=False)},
                    "id": f"save-ui-auth-{len(self.prompts)}",
                }])

        class FakeModel:
            def __init__(self):
                self.bound = BoundModel()

            def bind_tools(self, _tools, **_kwargs):
                return self.bound

        model = FakeModel()
        tool = SaveTool()
        messages = [
            AIMessage(content="", tool_calls=[{
                "name": "execute_skill_script", "args": {"command": "await helpers.loginStep1();"},
                "id": "browser-login-call",
            }]),
            ToolMessage(content="RESULT=PASS: 登录成功，当前 URL=https://example.test/work-order/tickets",
                        tool_call_id="browser-login-call", name="execute_skill_script"),
        ]

        result = async_to_sync(_force_save_generated_ui_case)(
            llm=model, tools=[tool], messages=messages,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["ui_test_case_id"], 322)
        self.assertEqual(len(tool.payloads), 2)
        self.assertIn("请修正后再次调用保存工具一次", model.bound.prompts[1][-1].content)

    def test_missing_save_tool_call_gets_retried_with_explicit_feedback(self):
        payload = {"steps": [{
            "functional_step_numbers": [1],
            "actions": [{"type": "assert", "mode": "url", "value": "/tickets"}],
        }]}

        class SaveTool:
            name = "save_generated_ui_testcase"

            async def ainvoke(self, _args):
                return '{"status":"success","ui_test_case_id":323}'

        class BoundModel:
            def __init__(self):
                self.prompts = []

            async def ainvoke(self, messages):
                self.prompts.append(messages)
                if len(self.prompts) == 1:
                    return AIMessage(content="我将整理本次执行结果。")
                return AIMessage(content="", tool_calls=[{
                    "name": "save_generated_ui_testcase",
                    "args": {"payload_json": json.dumps(payload)},
                    "id": "save-ui-retry",
                }])

        class FakeModel:
            def __init__(self):
                self.bound = BoundModel()

            def bind_tools(self, _tools, **_kwargs):
                return self.bound

        model = FakeModel()
        result = async_to_sync(_force_save_generated_ui_case)(
            llm=model,
            tools=[SaveTool()],
            messages=[
                AIMessage(content="", tool_calls=[{
                    "name": "execute_skill_script", "args": {"command": "await page.goto('/tickets')"},
                    "id": "browser-call",
                }]),
                ToolMessage(content="RESULT=PASS: URL=/tickets", tool_call_id="browser-call",
                            name="execute_skill_script"),
            ],
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["ui_test_case_id"], 323)
        self.assertEqual(len(model.bound.prompts), 2)
        self.assertIn("没有调用 save_generated_ui_testcase", model.bound.prompts[1][-1].content)

    def test_followup_forces_save_tool_using_only_observed_browser_evidence(self):
        class SaveTool:
            name = "save_generated_ui_testcase"

            async def ainvoke(self, args):
                return '{"status":"success","ui_test_case_id":321}'

        class BoundModel:
            async def ainvoke(self, messages):
                self.messages = messages
                return AIMessage(content="", tool_calls=[{
                    "name": "save_generated_ui_testcase",
                    "args": {"payload_json": '{"steps":[]}'},
                    "id": "save-ui-1",
                }])

        class FakeModel:
            def bind_tools(self, tools, **kwargs):
                self.tools = tools
                self.kwargs = kwargs
                self.bound = BoundModel()
                return self.bound

        model = FakeModel()
        tool = SaveTool()
        messages = [
            HumanMessage(content="执行用例 1464"),
            AIMessage(content="", tool_calls=[{
                "name": "execute_skill_script",
                "args": {"command": "await page.getByRole('button', {name: '返回'}).click();"},
                "id": "browser-call-1",
            }]),
            ToolMessage(
                content=(
                    'RESULT=PASS: 页面返回成功\n'
                    'UI_ACTION_TRACE={"version":1,"kind":"select_filter_option",'
                    '"field_label":"工单状态","option_text":"待处理","actions":[]}\n'
                    + ('执行输出 ' * 8_000)
                ),
                tool_call_id="browser-1",
                name="execute_skill_script",
            ),
        ]

        result = async_to_sync(_force_save_generated_ui_case)(
            llm=model,
            tools=[tool],
            messages=messages,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["ui_test_case_id"], 321)
        self.assertEqual(model.kwargs["tool_choice"], "required")
        prompt_text = "\n".join(str(message.content) for message in model.bound.messages)
        self.assertIn("getByRole", prompt_text)
        self.assertIn('"kind": "select_filter_option"', prompt_text)
        self.assertIn("UI_ACTION_TRACE", prompt_text)


    def test_successful_fast_finish_generates_and_persists_ui_binding(self):
        class QuerySet:
            def __init__(self):
                self.values = iter([None, 321])

            def values_list(self, *args, **kwargs):
                return self

            def first(self):
                return next(self.values)

        class Record:
            status = "pass"
            summary = "测试执行结果：通过"

            def save(self, update_fields=None):
                self.saved_fields = update_fields

        record = Record()
        query_set = QuerySet()
        messages = [
            AIMessage(content="", tool_calls=[{
                "name": "execute_skill_script",
                "args": {"command": "await page.getByRole('button').click();"},
                "id": "browser-call-1",
            }]),
            ToolMessage(
                content="RESULT=PASS: 最后一步完成",
                tool_call_id="browser-call-1",
                name="execute_skill_script",
            ),
        ]

        with patch("testcases.models.TestCase.objects.filter", return_value=query_set), patch(
            "orchestrator_integration.agent_loop_view._force_save_generated_ui_case",
            new_callable=AsyncMock,
            return_value={"status": "success", "ui_test_case_id": 321},
        ) as save_generated:
            result = async_to_sync(agent_loop_view._finalize_generated_ui_case)(
                llm=object(),
                tools=[],
                messages=messages,
                test_case_id=1478,
                project_id=1,
                record=record,
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["ui_test_case_id"], 321)
        self.assertEqual(record.saved_fields, ["summary"])
        self.assertIn("UI 自动化用例 #321 已生成并绑定", record.summary)
        save_generated.assert_awaited_once()

    def test_followup_reports_missing_browser_evidence_without_calling_llm(self):
        class SaveTool:
            name = "save_generated_ui_testcase"

        class NeverCalledModel:
            def bind_tools(self, *args, **kwargs):
                raise AssertionError("LLM must not be called without browser evidence")

        result = async_to_sync(_force_save_generated_ui_case)(
            llm=NeverCalledModel(),
            tools=[SaveTool()],
            messages=[HumanMessage(content="执行用例")],
        )

        self.assertEqual(result["status"], "failed")
        self.assertIn("浏览器操作证据", result["message"])


class HiddenNavigationRepairEvidenceTests(SimpleTestCase):
    def test_promotes_hidden_menu_click_when_target_route_is_already_open(self):
        diagnosis = {
            "root_cause_type": "SCRIPT_DEFECT",
            "healing_suggestion": {
                "can_self_heal": True,
                "repair_type": "locator",
                "element_id": 999,
            },
            "evidence": {
                "mapping_status": "resolved",
                "operation_type": "click",
                "element_id": 732,
                "locator_value": "工单总览",
                "actual_url": "https://test.example/work-order/dashboard?from=list",
                "page_url": "https://test.example/work-order/dashboard",
                # Diagnosis truncates long Playwright logs before the final visibility line.
                "error_message": 'locator.click: Timeout 15000ms exceeded; locator resolved to <li role="menuitem">',

            },
        }

        self.assertTrue(_promote_hidden_current_route_navigation(diagnosis))
        suggestion = diagnosis["healing_suggestion"]
        self.assertEqual(suggestion["repair_type"], "navigation_assertion")
        self.assertEqual(suggestion["element_id"], 732)

    def test_does_not_promote_when_current_route_differs_from_target(self):
        diagnosis = {
            "root_cause_type": "SCRIPT_DEFECT",
            "healing_suggestion": {"repair_type": "locator"},
            "evidence": {
                "mapping_status": "resolved",
                "operation_type": "click",
                "element_id": 732,
                "actual_url": "https://test.example/work-order/dashboard",
                "page_url": "https://test.example/work-order/tickets",
                "error_message": "role=menuitem element is not visible",
            },
        }

        self.assertFalse(_promote_hidden_current_route_navigation(diagnosis))
        self.assertEqual(diagnosis["healing_suggestion"]["repair_type"], "locator")


class ScriptOnlyResultMappingTests(SimpleTestCase):
    def setUp(self):
        self.steps = [
            SimpleNamespace(step_number=1, description="登录", expected_result="进入首页"),
            SimpleNamespace(step_number=2, description="打开工单", expected_result="进入详情"),
        ]

    def test_maps_successful_recorder_groups_to_functional_steps(self):
        results, status, _reason = _map_ui_script_results_to_testcase_steps(
            self.steps,
            [
                {"group": 1, "status": "success", "description": "输入账号"},
                {"group": 2, "status": "success", "description": "点击工单"},
            ],
            ui_status=2,
        )

        self.assertEqual(status, "pass")
        self.assertEqual([item["status"] for item in results], ["pass", "pass"])

    def test_authentication_prestep_does_not_count_as_a_business_group(self):
        steps = [
            SimpleNamespace(step_number=index, description=f"业务步骤{index}", expected_result="通过")
            for index in range(1, 5)
        ]
        raw_results = [
            {"step_id": "group0_0", "status": "success", "description": "手动登录并验证"},
            *[
                {"step_id": f"group{index}_1", "status": "success", "description": f"业务动作{index}"}
                for index in range(1, 5)
            ],
        ]

        results, status, reason = _map_ui_script_results_to_testcase_steps(
            steps,
            raw_results,
            ui_status=2,
        )

        self.assertEqual(status, "pass")
        self.assertEqual(reason, "")
        self.assertEqual([item["status"] for item in results], ["pass"] * 4)

    def test_combined_query_and_assertion_group_maps_to_two_functional_steps(self):
        steps = [
            SimpleNamespace(step_number=1, description="登录系统", expected_result="登录成功"),
            SimpleNamespace(step_number=2, description="进入我的工单", expected_result="页面打开"),
            SimpleNamespace(step_number=3, description="输入工单号并点击查询", expected_result="筛选结果"),
            SimpleNamespace(step_number=4, description="检查列表中是否存在该工单", expected_result="目标工单可见"),
        ]
        raw_results = [
            {"step_id": "group0_0", "status": "success", "description": "登录"},
            {"step_id": "group1_1", "status": "success", "description": "打开页面"},
            {"step_id": "group2_1", "status": "success", "description": "打开我的工单"},
            {"step_id": "group3_1", "status": "success", "description": "输入工单号"},
            {"step_id": "group3_2", "status": "success", "description": "查询"},
            {"step_id": "group3_3", "status": "success", "description": "等待工单可见"},
            {"step_id": "group3_4", "status": "success", "description": "断言工单可见"},
        ]
        ui_case_groups = [
            {"id": 101, "case_sort": 1, "name": "登录并进入我的工单", "description": "登录", "operations": ["assert_url"]},
            {"id": 102, "case_sort": 2, "name": "进入我的工单", "description": "打开菜单", "operations": ["click"]},
            {
                "id": 103,
                "case_sort": 3,
                "name": "按工单号查询并确认列表可见",
                "description": "输入工单号并点击查询，页面文本可见该工单号",
                "operations": ["fill", "click", "wait_for_visible", "assert_visible"],
                "elements": ["工单号", "查询", "工单号结果"],
            },
        ]

        results, status, reason = _map_ui_script_results_to_testcase_steps(
            steps,
            raw_results,
            ui_status=2,
            ui_case_groups=ui_case_groups,
            ui_test_case_id=55,
            ui_step_mapping={
                "version": 1,
                "ui_test_case_id": 55,
                "groups": [
                    {"ui_case_step_id": 101, "functional_step_numbers": [1]},
                    {"ui_case_step_id": 102, "functional_step_numbers": [2]},
                    {"ui_case_step_id": 103, "functional_step_numbers": [3, 4]},
                ],
            },
        )

        self.assertEqual(status, "pass")
        self.assertEqual(reason, "")
        self.assertEqual([item["status"] for item in results], ["pass"] * 4)

    def test_failed_recorder_action_fails_its_functional_step(self):
        results, status, reason = _map_ui_script_results_to_testcase_steps(
            self.steps,
            [
                {"group": 1, "status": "success"},
                {"group": 2, "status": "failed", "message": "元素未找到"},
            ],
            ui_status=3,
            ui_error="元素未找到",
        )

        self.assertEqual(status, "fail")
        self.assertEqual(reason, "元素未找到")
        self.assertEqual([item["status"] for item in results], ["pass", "fail"])

    def test_group_count_mismatch_fails_closed(self):
        results, status, reason = _map_ui_script_results_to_testcase_steps(
            self.steps,
            [{"group": 1, "status": "success"}],
            ui_status=2,
        )

        self.assertEqual(status, "fail")
        self.assertIn("缺少明确步骤映射", reason)
        self.assertEqual([item["status"] for item in results], ["skip", "skip"])
        self.assertTrue(all("映射" in item["actual_result"] for item in results))

    def test_explicit_mapping_allows_different_group_and_functional_step_counts(self):
        steps = [
            SimpleNamespace(step_number=1, description="登录", expected_result="完成"),
            SimpleNamespace(step_number=2, description="进入列表", expected_result="打开"),
            SimpleNamespace(step_number=3, description="滚动并核对行数", expected_result="行数正确"),
        ]
        ui_case_groups = [
            {"id": 201, "case_sort": 1},
            {"id": 202, "case_sort": 2},
        ]
        mapping = {
            "version": 1,
            "ui_test_case_id": 90,
            "groups": [
                {"ui_case_step_id": 201, "functional_step_numbers": [1]},
                {"ui_case_step_id": 202, "functional_step_numbers": [2, 3]},
            ],
        }
        results, status, reason = _map_ui_script_results_to_testcase_steps(
            steps,
            [
                {"step_id": "group1_1", "status": "success"},
                {"step_id": "group2_1", "status": "success"},
                {"step_id": "group2_2", "status": "success"},
            ],
            ui_status=2,
            ui_case_groups=ui_case_groups,
            ui_step_mapping=mapping,
            ui_test_case_id=90,
        )

        self.assertEqual(status, "pass")
        self.assertEqual(reason, "")
        self.assertEqual([item["status"] for item in results], ["pass", "pass", "pass"])

    def test_incomplete_explicit_mapping_marks_all_steps_uncovered(self):
        ui_case_groups = [
            {"id": 301, "case_sort": 1},
            {"id": 302, "case_sort": 2},
        ]
        results, status, reason = _map_ui_script_results_to_testcase_steps(
            self.steps,
            [
                {"step_id": "group1_1", "status": "success"},
                {"step_id": "group2_1", "status": "success"},
            ],
            ui_status=2,
            ui_case_groups=ui_case_groups,
            ui_step_mapping={
                "version": 1,
                "ui_test_case_id": 91,
                "groups": [
                    {"ui_case_step_id": 301, "functional_step_numbers": [1]},
                ],
            },
            ui_test_case_id=91,
        )

        self.assertEqual(status, "fail")
        self.assertIn("映射不完整", reason)
        self.assertEqual([item["status"] for item in results], ["skip", "skip"])

    def test_recorder_flattened_actions_are_grouped_by_step_id(self):
        results, status, reason = _map_ui_script_results_to_testcase_steps(
            self.steps,
            [
                {"step_id": "group1_1", "status": "success", "description": "goto"},
                {"step_id": "group1_2", "status": "success", "description": "assert_url"},
                {"step_id": "group2_1", "status": "success", "description": "click"},
            ],
            ui_status=2,
        )

        self.assertEqual(status, "pass")
        self.assertEqual(reason, "")
        self.assertEqual([item["status"] for item in results], ["pass", "pass"])

    def test_failed_run_reports_execution_error_not_step_count_mismatch(self):
        results, status, reason = _map_ui_script_results_to_testcase_steps(
            self.steps,
            [
                {"step_id": "group1_1", "status": "success", "description": "goto"},
                {"step_id": "group1_2", "status": "failed", "message": "URL 断言失败"},
            ],
            ui_status=3,
            ui_error="URL 断言失败",
        )

        self.assertEqual(status, "fail")
        self.assertEqual(reason, "URL 断言失败")
        self.assertNotIn("无法逐步对应", reason)
        self.assertEqual([item["status"] for item in results], ["fail", "skip"])

    def test_failed_run_does_not_mark_unmapped_tail_as_passed(self):
        steps = [
            SimpleNamespace(step_number=1, description="登录", expected_result="完成"),
            SimpleNamespace(step_number=2, description="进入列表", expected_result="打开"),
            SimpleNamespace(step_number=3, description="滚动列表", expected_result="完成"),
        ]
        results, status, reason = _map_ui_script_results_to_testcase_steps(
            steps,
            [
                {"step_id": "group1_1", "status": "success"},
                {"step_id": "group2_1", "status": "failed", "message": "页面超时"},
            ],
            ui_status=3,
            ui_error="页面超时",
        )

        self.assertEqual(status, "fail")
        self.assertEqual(reason, "页面超时")
        self.assertEqual([item["status"] for item in results], ["pass", "fail", "skip"])


class CompletionHelperProgressTests(SimpleTestCase):
    def test_complete_ticket_helper_step_is_reported(self):
        self.assertEqual(
            _extract_testcase_step_from_tool_call({
                'name': 'execute_skill_script',
                'args': {'command': 'await helpers.runCompleteTicketCaseStep(page, 4);'},
            }),
            4,
        )


class FinalTestcaseStepTests(SimpleTestCase):
    def test_helper_template_is_not_guessed_for_ai_only_case(self):
        self.assertEqual(infer_fixed_helper_call_template([]), "")
        self.assertEqual(
            infer_fixed_helper_call_template([AIMessage(content="查看工单列表当前状态列")]),
            "",
        )
        self.assertEqual(
            infer_fixed_helper_call_template([
                AIMessage(content="await helpers.runCompleteTicketCaseStep(page, 1);")
            ]),
            "await helpers.runCompleteTicketCaseStep(page, {step});",
        )

    def test_final_fixed_helper_pass_is_terminal(self):
        self.assertTrue(
            _is_final_testcase_step_pass(
                tool_name="execute_skill_script",
                content="[执行进度] 正在执行步骤 4/4\nRESULT=PASS: 步骤4通过",
                helper_step=4,
                total_steps=4,
            )
        )

    def test_non_final_or_failed_step_is_not_terminal(self):
        self.assertFalse(
            _is_final_testcase_step_pass(
                tool_name="execute_skill_script",
                content="RESULT=PASS: 步骤3通过",
                helper_step=3,
                total_steps=4,
            )
        )
        self.assertFalse(
            _is_final_testcase_step_pass(
                tool_name="execute_skill_script",
                content="RESULT=FAIL: 步骤4失败",
                helper_step=4,
                total_steps=4,
            )
        )

    def test_progress_uses_latest_declared_screenshot(self):
        case_id, step = _extract_helper_testcase_progress(
            "[CASE_SCREENSHOT] /tmp/case_1402_step1.png\n"
            "[SCREENSHOT_SAVED] /media/skill_runtime/screenshots/1/1402/case_1402_step7.png"
        )
        self.assertEqual(case_id, 1402)
        self.assertEqual(step, 7)

    def test_progress_uses_explicit_step_execution_boundary_without_screenshot(self):
        case_id, step = _extract_helper_testcase_progress(
            "[CASE_STEP_EXECUTION] case=1402 step=5\n"
            "RESULT=PASS: 列表已核对"
        )
        self.assertEqual(case_id, 1402)
        self.assertEqual(step, 5)

    def test_latest_passed_step_uses_result_in_same_tool_call_as_step_marker(self):
        messages = [
            ToolMessage(
                content=(
                    "[CASE_STEP_EXECUTION] case=1402 step=1\nRESULT=PASS: 登录成功\n"
                    "[CASE_STEP_EXECUTION] case=1402 step=2\nRESULT=PASS: 已进入列表\n"
                    "[CASE_STEP_EXECUTION] case=1402 step=3\n"
                    "[CASE_SCREENSHOT] /tmp/case_1402_step3.png\n"
                    "locator.getByText: Timeout 15000ms exceeded"
                ),
                tool_call_id="boundary-test",
                name="execute_skill_script",
            )
        ]
        self.assertEqual(
            _latest_passed_helper_step_from_messages(messages, test_case_id=1402),
            2,
        )

    def test_latest_passed_step_decodes_json_wrapped_mcp_text_blocks(self):
        output = (
            '[{"type":"text","text":"[CASE_STEP_EXECUTION] case=1313 step=1\\n'
            'RESULT=PASS: 登录成功"},{"type":"text","text":"'
            '[CASE_STEP_EXECUTION] case=1313 step=2\\nRESULT=PASS: 已进入列表"}]'
        )
        messages = [
            ToolMessage(
                content=output,
                tool_call_id="json-wrapped-output",
                name="execute_skill_script",
            )
        ]
        self.assertEqual(
            _latest_passed_helper_step_from_messages(messages, test_case_id=1313),
            2,
        )
        self.assertEqual(
            _extract_helper_testcase_progress(output),
            (1313, 2),
        )

    def test_latest_passed_helper_step_from_tool_messages(self):
        messages = [
            ToolMessage(
                content=(
                    "[CASE_SCREENSHOT] /tmp/case_1401_step6.png\n"
                    "RESULT=PASS: 步骤6已取消领取"
                ),
                tool_call_id="1",
                name="execute_skill_script",
            ),
            AIMessage(content="接下来执行步骤7并输出报告"),
        ]
        self.assertEqual(_latest_passed_helper_step_from_messages(messages), 0)

    def test_helper_coverage_requires_contiguous_passes(self):
        def result(step, outcome="PASS", case=1334):
            return ToolMessage(
                content=f"[CASE_SCREENSHOT] /tmp/case_{case}_step{step}.png\nRESULT={outcome}",
                tool_call_id=str(step), name="execute_skill_script",
            )
        messages = [result(step) for step in (1, 2, 4, 5, 6, 7)]
        self.assertEqual(_latest_passed_helper_step_from_messages(messages, test_case_id=1334), 2)
        self.assertEqual(_latest_passed_helper_step_from_messages(messages + [result(3)], test_case_id=1334), 7)
        self.assertEqual(_latest_passed_helper_step_from_messages(messages + [result(3, "FAIL")], test_case_id=1334), 2)
        self.assertEqual(_latest_passed_helper_step_from_messages(messages + [result(3, case=999)], test_case_id=1334), 2)
        self.assertEqual(_latest_passed_helper_step_from_messages([result(1), result(1, "FAIL")]), 0)
        shot_only = result(3)
        shot_only.content = "[CASE_SCREENSHOT] /tmp/case_1334_step3.png"
        self.assertEqual(_latest_passed_helper_step_from_messages(messages + [shot_only]), 2)

    def test_detects_attempted_step_without_receipt_for_read_only_recovery(self):
        attempted = ToolMessage(
            content=(
                "[CASE_STEP_EXECUTION] case=1245 step=5\n"
                "[CASE_SCREENSHOT] /tmp/case_1245_step5.png"
            ),
            tool_call_id="attempted-step",
            name="execute_skill_script",
        )
        messages = [attempted]
        self.assertTrue(
            _testcase_step_was_attempted(messages, step=5, test_case_id=1245)
        )
        self.assertIsNone(
            _explicitly_failed_helper_step_from_messages(
                messages, test_case_id=1245
            )
        )

    def test_explicit_failure_is_not_auto_replayed(self):
        failed = ToolMessage(
            content=(
                "[CASE_STEP_EXECUTION] case=1245 step=5\n"
                "RESULT=FAIL: 未找到目标工单"
            ),
            tool_call_id="failed-step",
            name="execute_skill_script",
        )
        self.assertEqual(
            _explicitly_failed_helper_step_from_messages(
                [failed], test_case_id=1245
            ),
            5,
        )

    def test_instructional_fail_sample_does_not_count_as_browser_failure(self):
        passed_with_reminder = ToolMessage(
            content=(
                "[CASE_STEP_EXECUTION] case=1245 step=5\n"
                "RESULT=PASS: 列表已核对\n"
                "【脚本错误，请修正后重试】RESULT=FAIL 时禁止继续。"
            ),
            tool_call_id="instructional-marker",
            name="execute_skill_script",
        )
        messages = [passed_with_reminder]
        self.assertIsNone(
            _explicitly_failed_helper_step_from_messages(
                messages, test_case_id=1245
            )
        )
        self.assertEqual(
            _latest_passed_helper_step_from_messages(
                messages, test_case_id=1245
            ),
            0,
        )

    def test_incomplete_ai_resume_verifies_attempted_step_without_replaying_actions(self):
        prompt = _build_incomplete_ai_resume_prompt(
            next_step=5,
            total_steps=5,
            step_details={5: {"description": "核对列表", "expected_result": "工单号匹配"}},
            test_case_id=1245,
            attempted_step=5,
        )
        self.assertIn("先只读检查当前页面", prompt)
        self.assertIn("禁止重复点击、提交、创建、删除、发送、领取、审批", prompt)
        self.assertIn("明确写 `RESULT=PASS", prompt)

    def test_incomplete_resume_prompt_forbids_report(self):
        prompt = _build_incomplete_testcase_resume_prompt(done_step=6, total_steps=7)
        self.assertIn("步骤 7", prompt)
        self.assertIn("禁止输出测试报告", prompt)
        self.assertIn("无法确认脚本类型时停止并报告", prompt)
        self.assertNotIn("runClaimableTicketCaseStep", prompt)

    def test_helper_result_reason_ignores_skill_doc_samples(self):
        status, reason = _extract_helper_result_reason(
            "RESULT=PASS: 步骤2已进入我的工单列表\n"
        )
        self.assertEqual(status, "PASS")
        self.assertEqual(reason, "步骤2已进入我的工单列表")

    def test_helper_result_reason_treats_playwright_timeout_as_fail(self):
        status, reason = _extract_helper_result_reason(
            "[执行进度] 正在执行步骤 7/7\n"
            "Playwright exec timed out after 45000ms\n"
            "浏览器已重置。请先重新打开该步骤所在页面\n"
        )
        self.assertEqual(status, "FAIL")
        self.assertIn("超时", reason)

    def test_claimable_steps_3_to_7_use_long_playwright_timeout(self):
        ms, seconds = _playwright_exec_timeout_for_helper_code(
            "await helpers.runClaimableTicketCaseStep(page, 7);"
        )
        self.assertEqual(ms, 120000)
        self.assertEqual(seconds, 150)
        fast_ms, fast_sec = _playwright_exec_timeout_for_helper_code(
            "await helpers.runClaimableTicketCaseStep(page, 1);"
        )
        self.assertEqual(fast_ms, 45000)
        self.assertEqual(fast_sec, 55)

    def test_helper_step_commentary_matches_live_chat(self):
        text = _build_helper_step_commentary(
            prev_step=1,
            prev_status="PASS",
            prev_reason="使用自动处理人登录成功",
            next_step=2,
            next_action="进入我的工单列表页面",
        )
        self.assertIn("步骤 1 通过: 使用自动处理人登录成功", text)
        self.assertIn("步骤 2: 进入我的工单列表页面", text)

    def test_auto_resume_streams_ai_line_then_tool_card(self):
        async def collect():
            events = []
            with patch(
                "orchestrator_integration.builtin_tools.skill_tools.auto_run_missing_testcase_helper_steps",
                return_value={
                    "plain": (
                        "[PERSISTENT_SESSION] session_id=case_1402\n"
                        "[CASE_SCREENSHOT] /tmp/case_1402_step2.png\n"
                        "RESULT=PASS: 步骤2已进入我的工单列表"
                    ),
                    "display": (
                        "[PERSISTENT_SESSION] session_id=case_1402\n"
                        "[CASE_SCREENSHOT] /tmp/case_1402_step2.png\n"
                        "RESULT=PASS: 步骤2已进入我的工单列表"
                    ),
                },
            ), patch(
                "orchestrator_integration.agent_loop_view._get_testcase_step_names",
                return_value={2: "进入我的工单列表页面", 3: "筛选待处理工单"},
            ), patch(
                "orchestrator_integration.builtin_tools.skill_tools.infer_fixed_helper_call_template",
                return_value="await helpers.runClaimableTicketCaseStep(page, {step});",
            ), patch(
                "testcases.models.TestCase.objects.filter",
            ) as case_filter, patch(
                "data_generation.testcase_pre_data.is_claimable_ticket_detail_case",
                return_value=True,
            ):
                case_filter.return_value.first.return_value = object()
                async for event in _run_and_stream_remaining_helper_steps(
                    test_case_id=1402,
                    user_id=1,
                    project_id=1,
                    session_id="sess",
                    from_step=2,
                    to_step=2,
                    messages=[],
                    previous_output="RESULT=PASS: 使用自动处理人登录成功",
                ):
                    events.append(event)
            return events

        events = async_to_sync(collect)()
        types = [event.get("type") for event in events]
        self.assertEqual(types[0], "stream")
        self.assertIn("步骤 1 通过", events[0]["data"])
        self.assertIn("步骤 2: 进入我的工单列表页面", events[0]["data"])
        self.assertEqual(types[1], "testcase_progress")
        self.assertEqual(events[1]["step"], 2)
        self.assertEqual(types[2], "tool_result")
        self.assertEqual(events[2]["tool_name"], "execute_skill_script")
        self.assertIn("RESULT=PASS", events[2]["tool_output"])
        self.assertEqual(types[-1], "_auto_run_bundle")

    def test_auto_resume_without_fixed_helper_defers_to_ai(self):
        async def collect():
            with patch(
                "orchestrator_integration.builtin_tools.skill_tools.infer_fixed_helper_call_template",
                return_value="",
            ), patch(
                "orchestrator_integration.agent_loop_view._get_testcase_step_names",
                side_effect=AssertionError("No script should run"),
            ):
                return [
                    event async for event in _run_and_stream_remaining_helper_steps(
                        test_case_id=1142,
                        user_id=1,
                        project_id=1,
                        session_id="sess",
                        from_step=2,
                        to_step=4,
                        messages=[],
                    )
                ]

        events = async_to_sync(collect)()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "_auto_run_bundle")
        self.assertTrue(events[0]["no_template"])
        self.assertEqual(events[0]["output"], "")
        self.assertEqual(events[0]["last_step"], 1)
from ..builtin_tools.skill_tools import (
    _auto_upload_case_screenshots,
    _build_skill_artifacts_dir,
    _collect_skill_artifacts,
    _build_skill_screenshots_dir,
    _finalize_skill_result,
    _get_skill_runtime_env,
    _playwright_exec_timeout_for_helper_code,
    _prepare_skill_screenshots_dir,
    _SKILL_DIR_STALE_SECONDS,
    _sanitize_runtime_path_segment,
    _with_failure_reminder,
    _get_skill_content_timeout_seconds,
    infer_fixed_helper_call_template,
)
from ..builtin_tools.output_sanitizer import strip_terminal_control_sequences
from ..middleware_config import (
    extract_llm_failure_message_from_text,
    get_user_friendly_llm_error,
    _model_retry_should_retry,
)
from projects.models import Project, ProjectMember
from requirements.models import DocumentImage, RequirementDocument


class SkillRuntimeEnvTests(SimpleTestCase):
    @override_settings(WHARTTEST_API_KEY="", WHARTTEST_BACKEND_URL="")
    def test_runtime_environment_preserves_configured_api_key(self):
        with patch.dict(
            os.environ,
            {
                "WHARTTEST_API_KEY": "runtime-api-key",
                "WHARTTEST_BACKEND_URL": "http://runtime-backend:8000",
            },
        ):
            env = _get_skill_runtime_env()

        self.assertEqual(env["WHARTTEST_API_KEY"], "runtime-api-key")
        self.assertEqual(
            env["WHARTTEST_BACKEND_URL"], "http://runtime-backend:8000"
        )


class SkillContentTimeoutTests(SimpleTestCase):
    @override_settings(SKILL_CONTENT_TIMEOUT_SECONDS=12)
    def test_uses_configured_timeout(self):
        self.assertEqual(_get_skill_content_timeout_seconds(), 12.0)

    @override_settings(SKILL_CONTENT_TIMEOUT_SECONDS=0)
    def test_enforces_safe_timeout_bounds(self):
        self.assertEqual(_get_skill_content_timeout_seconds(), 1.0)

    @override_settings(SKILL_CONTENT_TIMEOUT_SECONDS=999)
    def test_caps_excessive_timeout(self):
        self.assertEqual(_get_skill_content_timeout_seconds(), 60.0)


class ToolCallHistorySanitizerTests(SimpleTestCase):
    def test_repairs_ai_tool_call_without_id_and_inserts_interrupted_result(self):
        message = AIMessage(
            content="",
            tool_calls=[
                {
                    "id": None,
                    "name": "knowledge_search",
                    "args": {"query": "test"},
                }
            ],
        )

        sanitized, fixed_count = _build_sanitized_messages([message])

        self.assertEqual(len(sanitized), 2)
        self.assertTrue(sanitized[0].tool_calls[0]["id"])
        self.assertIsInstance(sanitized[1], ToolMessage)
        self.assertEqual(
            sanitized[1].tool_call_id, sanitized[0].tool_calls[0]["id"]
        )
        self.assertEqual(fixed_count, 2)

    def test_repairs_raw_tool_call_history_before_next_user_message(self):
        message = AIMessage(
            content="",
            additional_kwargs={
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {
                            "name": "knowledge_search",
                            "arguments": '{"query":"test"}',
                        },
                    }
                ]
            },
        )

        sanitized, fixed_count = _build_sanitized_messages(
            [HumanMessage(content="search"), message, HumanMessage(content="continue")]
        )

        self.assertEqual(fixed_count, 1)
        self.assertEqual(len(sanitized), 4)
        self.assertIsInstance(sanitized[2], ToolMessage)
        self.assertEqual(sanitized[2].tool_call_id, "call_123")
        self.assertIsInstance(sanitized[3], HumanMessage)


class CompatibleToolCallChunkTests(SimpleTestCase):
    def test_filters_duplicate_tool_call_chunk_without_id(self):
        from langgraph_integration.openai_compatible_chat_model import (
            repair_tool_call_chunks,
        )

        message = AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": "knowledge_search",
                    "args": '{"query":"test"}',
                    "id": "call_123",
                    "index": 0,
                    "type": "tool_call_chunk",
                },
                {
                    "name": None,
                    "args": '{"query":"test"}',
                    "id": None,
                    "index": 0,
                    "type": "tool_call_chunk",
                },
            ],
        )

        self.assertTrue(repair_tool_call_chunks(message, {0: "call_123"}))
        self.assertEqual(len(message.tool_call_chunks), 1)
        self.assertEqual(
            message.tool_calls,
            [
                {
                    "name": "knowledge_search",
                    "args": {"query": "test"},
                    "id": "call_123",
                    "type": "tool_call",
                }
            ],
        )

    def test_attaches_id_to_later_tool_argument_fragments(self):
        from langgraph_integration.openai_compatible_chat_model import (
            repair_tool_call_chunks,
        )

        ids = {}
        first = AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": "knowledge_search",
                    "args": None,
                    "id": "call_123",
                    "index": 0,
                    "type": "tool_call_chunk",
                }
            ],
        )
        second = AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": None,
                    "args": '{"query":"test"}',
                    "id": None,
                    "index": 0,
                    "type": "tool_call_chunk",
                }
            ],
        )

        self.assertFalse(repair_tool_call_chunks(first, ids))
        self.assertTrue(repair_tool_call_chunks(second, ids))
        combined = first + second

        self.assertEqual(combined.tool_calls[0]["id"], "call_123")
        self.assertEqual(combined.tool_calls[0]["name"], "knowledge_search")
        self.assertEqual(combined.tool_calls[0]["args"], {"query": "test"})

    def test_async_stream_repairs_later_tool_argument_fragments(self):
        from langgraph_integration.openai_compatible_chat_model import (
            ToolCallCompatibleChatOpenAI,
        )
        from langchain_openai import ChatOpenAI

        first = AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": "knowledge_search",
                    "args": None,
                    "id": "call_123",
                    "index": 0,
                    "type": "tool_call_chunk",
                }
            ],
        )
        second = AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": None,
                    "args": '{"query":"test"}',
                    "id": None,
                    "index": 0,
                    "type": "tool_call_chunk",
                }
            ],
        )

        async def fake_astream(self, *args, **kwargs):
            yield ChatGenerationChunk(message=first)
            yield ChatGenerationChunk(message=second)

        async def collect_chunks(model):
            chunks = []
            async for chunk in model._astream([]):
                chunks.append(chunk.message)
            return chunks

        model = ToolCallCompatibleChatOpenAI(model="test", api_key="test")
        with patch.object(ChatOpenAI, "_astream", fake_astream):
            chunks = async_to_sync(collect_chunks)(model)

        combined = chunks[0] + chunks[1]
        self.assertEqual(combined.tool_calls[0]["id"], "call_123")
        self.assertEqual(combined.tool_calls[0]["args"], {"query": "test"})


class LLMFriendlyErrorTests(SimpleTestCase):
    def test_model_cooldown_error_returns_friendly_payload(self):
        exc = Exception(
            "Error code: 429 - {'error': {'code': 'model_cooldown', 'message': 'All credentials for model coder-model are cooling down', 'model': 'coder-model', 'reset_seconds': 27211, 'reset_time': '7h33m31s'}}"
        )

        result = get_user_friendly_llm_error(exc)

        if result is None:
            raise AssertionError("expected friendly error payload")
        self.assertEqual(result["status_code"], 429)
        self.assertEqual(result["error_code"], "model_cooldown")
        self.assertEqual(result["model"], "coder-model")
        self.assertEqual(result["reset_seconds"], 27211)
        self.assertEqual(result["reset_time"], "7h33m31s")
        self.assertIn("coder-model", result["message"])
        self.assertIn("7h33m31s", result["message"])

    def test_generic_rate_limit_error_returns_friendly_payload(self):
        exc = Exception("HTTP 429 Too Many Requests")

        result = get_user_friendly_llm_error(exc)

        if result is None:
            raise AssertionError("expected friendly error payload")
        self.assertEqual(result["status_code"], 429)
        self.assertEqual(result["error_code"], "rate_limit")
        self.assertEqual(result["message"], "当前模型服务请求过于频繁，请稍后重试。")

    def test_api_timeout_returns_friendly_payload(self):
        api_timeout_error = type("APITimeoutError", (Exception,), {})
        exc = api_timeout_error("Request timed out.")

        result = get_user_friendly_llm_error(exc)

        if result is None:
            raise AssertionError("expected friendly timeout payload")
        self.assertEqual(result["status_code"], 504)
        self.assertEqual(result["error_code"], "llm_timeout")
        self.assertIn("超时", result["message"])

    def test_extract_model_retry_failure_text(self):
        text = "Model call failed after 4 attempts with APITimeoutError: Request timed out."
        message = extract_llm_failure_message_from_text(text)
        self.assertIsNotNone(message)
        self.assertIn("超时", message or "")

    def test_model_cooldown_error_will_not_retry(self):
        exc = Exception(
            "Error code: 429 - {'error': {'code': 'model_cooldown', 'message': 'All credentials for model coder-model are cooling down', 'model': 'coder-model', 'reset_seconds': 27211, 'reset_time': '7h33m31s'}}"
        )

        self.assertFalse(_model_retry_should_retry(exc))

    def test_cooling_down_text_without_code_still_maps_to_model_cooldown(self):
        exc = Exception(
            "RateLimitError: provider says model service is cooling down, retry-after: 6m0s"
        )

        result = get_user_friendly_llm_error(exc)

        if result is None:
            raise AssertionError("expected friendly cooldown payload")
        self.assertEqual(result["status_code"], 429)
        self.assertEqual(result["error_code"], "model_cooldown")
        self.assertIn("冷却中", result["message"])


class LinkedImageUrlExtractionTests(SimpleTestCase):
    def test_extract_plain_http_url_stops_before_chinese_description(self):
        text = "请访问 https://localhost:8080，准备注册信息：用户名testuser010、密码abcdef123"

        self.assertEqual(_extract_linked_image_urls(text), ["https://localhost:8080"])

    def test_extract_markdown_image_url_trims_wrapping_punctuation(self):
        text = "参考截图 ![image](https://example.com/demo.png)，然后继续分析"

        self.assertEqual(
            _extract_linked_image_urls(text),
            ["https://example.com/demo.png"],
        )

    def test_extract_invalid_unicode_netloc_does_not_raise(self):
        text = "异常链接 https://localhost:8080：准备注册信息：用户名testuser014"

        self.assertEqual(_extract_linked_image_urls(text), ["https://localhost:8080"])

    def test_extract_plain_http_url_stops_before_ascii_comma_description(self):
        text = "Open http://localhost:8080,then fill the registration form"

        self.assertEqual(_extract_linked_image_urls(text), ["http://localhost:8080"])

    def test_extract_plain_http_url_stops_before_closing_parenthesis_text(self):
        text = "查看截图 https://example.com/demo.png)后继续分析"

        self.assertEqual(
            _extract_linked_image_urls(text),
            ["https://example.com/demo.png"],
        )

    def test_allowlist_check_rejects_invalid_url_without_raising(self):
        with patch.object(
            agent_loop_view, "_LINKED_IMAGE_URL_ALLOWLIST", {"example.com"}
        ):
            self.assertFalse(
                _is_linked_image_url_allowed(
                    "https://localhost:8080：准备注册信息：用户名testuser014"
                )
            )


class UploadedImageNormalizationTests(SimpleTestCase):
    def test_normalize_uploaded_images_merges_legacy_and_array_fields(self):
        result = _normalize_uploaded_image_base64_list(
            ["img-a", " img-b ", "", "img-a"],
            "img-c",
        )

        self.assertEqual(result, ["img-a", "img-b", "img-c"])

    def test_normalize_uploaded_images_accepts_legacy_single_image_only(self):
        result = _normalize_uploaded_image_base64_list(None, " legacy-img ")

        self.assertEqual(result, ["legacy-img"])


class AgentLoopRequirementImageMessageTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.override_media = override_settings(MEDIA_ROOT=self.temp_dir.name)
        self.override_media.enable()
        self.addCleanup(self.override_media.disable)

        self.user = get_user_model().objects.create_user(
            username="agent-loop-user",
            password="password123",
        )
        self.project = Project.objects.create(
            name="Agent Loop Image Project",
            creator=self.user,
        )
        ProjectMember.objects.create(
            project=self.project,
            user=self.user,
            role="member",
        )
        self.document = RequirementDocument.objects.create(
            project=self.project,
            title="Requirement With Images",
            document_type="docx",
            uploader=self.user,
            has_images=True,
            image_count=1,
        )
        DocumentImage.objects.create(
            document=self.document,
            image_id="img_000",
            order=0,
            content_type="image/png",
            file_size=3,
            image_file=SimpleUploadedFile(
                "img-000.png",
                b"png",
                content_type="image/png",
            ),
        )

    def test_prepare_agent_loop_human_message_rewrites_requirement_placeholders(self):
        message = (
            "请分析以下需求\n\n"
            "![图片](docimg://img_000)\n\n"
            f"(这些需求模块来源于需求文档ID: {self.document.id})"
        )

        human_message_content, additional_kwargs, display_message = async_to_sync(
            _prepare_agent_loop_human_message
        )(
            message,
            project=self.project,
            supports_vision=False,
            uploaded_images_base64=[],
        )

        expected_url = (
            f"/api/requirements/documents/{self.document.id}/images/img_000/"
        )
        self.assertEqual(human_message_content, display_message)
        self.assertIn(expected_url, display_message)
        self.assertEqual(
            additional_kwargs["requirement_document_id"], str(self.document.id)
        )

    def test_prepare_agent_loop_human_message_attaches_requirement_images_for_vision(self):
        message = (
            "请分析以下需求\n\n"
            "![图片](docimg://img_000)\n\n"
            f"(这些需求模块来源于需求文档ID: {self.document.id})"
        )

        human_message_content, additional_kwargs, display_message = async_to_sync(
            _prepare_agent_loop_human_message
        )(
            message,
            project=self.project,
            supports_vision=True,
            uploaded_images_base64=[],
        )

        self.assertIsInstance(human_message_content, list)
        self.assertEqual(human_message_content[0]["type"], "text")
        self.assertIn(
            f"/api/requirements/documents/{self.document.id}/images/img_000/",
            human_message_content[0]["text"],
        )
        self.assertEqual(human_message_content[1]["type"], "image_url")
        self.assertTrue(
            human_message_content[1]["image_url"]["url"].startswith(
                "data:image/png;base64,"
            )
        )
        self.assertEqual(
            additional_kwargs["requirement_document_id"], str(self.document.id)
        )
        self.assertEqual(additional_kwargs["image_source"], "requirement_document")
        self.assertIn(
            f"/api/requirements/documents/{self.document.id}/images/img_000/",
            display_message,
        )


class SkillScreenshotDirectoryTests(SimpleTestCase):
    def test_sanitize_runtime_path_segment_blocks_path_traversal(self):
        self.assertEqual(
            _sanitize_runtime_path_segment("../case/89", "_default"),
            "__case_89",
        )

    def test_build_skill_screenshots_dir_uses_runtime_media_root(self):
        with tempfile.TemporaryDirectory() as temp_media_root:
            with override_settings(MEDIA_ROOT=temp_media_root):
                screenshots_dir = _build_skill_screenshots_dir(1, "89")

        self.assertTrue(screenshots_dir.endswith("skill_runtime/screenshots/1/89"))
        self.assertNotIn("/skills/1/11/", screenshots_dir)

    def test_build_skill_screenshots_dir_keeps_path_inside_media_root(self):
        with tempfile.TemporaryDirectory() as temp_media_root:
            with override_settings(MEDIA_ROOT=temp_media_root):
                screenshots_dir = _build_skill_screenshots_dir(1, "../case/89")

        self.assertTrue(screenshots_dir.startswith(temp_media_root))
        self.assertNotIn("..", screenshots_dir)

    def test_auto_upload_uses_last_png_when_named_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            last_png = os.path.join(tmp, "last.png")
            with open(last_png, "wb") as handle:
                handle.write(b"\x89PNG" + b"x" * 80)
            with patch(
                "testcases.run_record_service.attach_testcase_step_screenshot",
                return_value=True,
            ) as attach:
                notes = _auto_upload_case_screenshots(
                    "[CASE_SCREENSHOT] /missing/case_1401_step7.png",
                    project_id=1,
                    case_id=1401,
                    env={"SCREENSHOT_DIR": tmp},
                )
            self.assertIn("上传成功", notes)
            self.assertTrue(os.path.isfile(os.path.join(tmp, "case_1401_step7.png")))
            attach.assert_called_once()
            self.assertEqual(attach.call_args.kwargs["step_number"], 7)

    def test_prepare_skill_screenshots_dir_clears_idle_dir(self):
        with tempfile.TemporaryDirectory() as temp_media_root:
            with override_settings(MEDIA_ROOT=temp_media_root):
                screenshots_dir = _prepare_skill_screenshots_dir(1, "89")
                stale_file = os.path.join(screenshots_dir, "old.png")
                with open(stale_file, "w", encoding="utf-8") as f:
                    f.write("old screenshot")

                # 目录仍新鲜：不清理，避免误删当前会话截图
                refreshed_dir = _prepare_skill_screenshots_dir(1, "89")
                self.assertEqual(refreshed_dir, screenshots_dir)
                self.assertTrue(os.path.exists(stale_file))

                # 子目录内文件也参与闲置判定：子目录内容变新则不清空
                sub_file = os.path.join(screenshots_dir, "sub", "recent.png")
                os.makedirs(os.path.dirname(sub_file), exist_ok=True)
                with open(sub_file, "w", encoding="utf-8") as f:
                    f.write("recent")
                refreshed_dir = _prepare_skill_screenshots_dir(1, "89")
                self.assertTrue(os.path.exists(stale_file))

                # 目录树闲置超过阈值后清理
                old = time.time() - _SKILL_DIR_STALE_SECONDS - 60
                os.utime(stale_file, (old, old))
                os.utime(sub_file, (old, old))
                refreshed_dir = _prepare_skill_screenshots_dir(1, "89")
                self.assertEqual(refreshed_dir, screenshots_dir)
                self.assertFalse(os.path.exists(stale_file))

    def test_build_skill_artifacts_dir_uses_runtime_media_root(self):
        with tempfile.TemporaryDirectory() as temp_media_root:
            with override_settings(MEDIA_ROOT=temp_media_root):
                artifacts_dir = _build_skill_artifacts_dir(1, "session-1")

        self.assertTrue(artifacts_dir.endswith("skill_runtime/artifacts/1/session-1"))
        self.assertNotIn("/skills/1/11/", artifacts_dir)

    def test_collect_skill_artifacts_detects_named_generated_file(self):
        with tempfile.TemporaryDirectory() as temp_media_root:
            with override_settings(MEDIA_ROOT=temp_media_root, MEDIA_URL="/media/"):
                skill_dir = os.path.join(temp_media_root, "skills", "1", "11")
                os.makedirs(skill_dir, exist_ok=True)
                generated_file = os.path.join(skill_dir, "order-payment-flow.drawio")
                with open(generated_file, "w", encoding="utf-8") as f:
                    f.write("<mxfile></mxfile>")

                artifacts = _collect_skill_artifacts(
                    "已帮你生成 draw.io 文件：order-payment-flow.drawio",
                    skill_dir=skill_dir,
                    artifacts_dir=os.path.join(temp_media_root, "skill_runtime", "artifacts", "1", "s1"),
                    artifacts_before={},
                )

        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["name"], "order-payment-flow.drawio")
        self.assertEqual(artifacts[0]["url"], "/media/skills/1/11/order-payment-flow.drawio")

    def test_finalize_skill_result_wraps_output_with_file_payload(self):
        with tempfile.TemporaryDirectory() as temp_media_root:
            with override_settings(MEDIA_ROOT=temp_media_root, MEDIA_URL="/media/"):
                skill_dir = os.path.join(temp_media_root, "skills", "1", "11")
                os.makedirs(skill_dir, exist_ok=True)
                generated_file = os.path.join(skill_dir, "demo.drawio")
                with open(generated_file, "w", encoding="utf-8") as f:
                    f.write("<mxfile></mxfile>")

                wrapped = _finalize_skill_result(
                    "已生成文件 demo.drawio",
                    skill_dir=skill_dir,
                    artifacts_dir=os.path.join(temp_media_root, "skill_runtime", "artifacts", "1", "s1"),
                    artifacts_before={},
                )

        self.assertIn('"type": "file"', wrapped)
        self.assertIn('/media/skills/1/11/demo.drawio', wrapped)


class TerminalOutputSanitizerTests(SimpleTestCase):
    def test_strip_terminal_control_sequences_removes_ansi_color_codes(self):
        raw = "\x1b[32m✓\x1b[0m Browser closed"

        self.assertEqual(strip_terminal_control_sequences(raw), "✓ Browser closed")


class SkillFailureReminderTests(SimpleTestCase):
    def test_result_fail_appends_full_report_reminder(self):
        output = _with_failure_reminder(
            "RESULT=FAIL: 筛选未生效，列表仍为混合状态",
            skill_name="playwright-skill",
        )
        self.assertIn("必须输出完整测试报告", output)
        self.assertIn("## 测试执行结果: 不通过", output)

    def test_timeout_after_step_pass_requests_script_repair(self):
        output = _with_failure_reminder(
            "[CASE_SCREENSHOT] /tmp/case_1191_step3.png\n"
            "RESULT=PASS: 步骤3已选择待处理\n"
            "locator.innerText: Timeout 15000ms exceeded",
            skill_name="playwright-skill",
        )
        self.assertIn("脚本错误，请修正后重试", output)
        self.assertNotIn("【步骤已通过】", output)

    def test_verified_dropdown_trace_survives_duplicate_legacy_locator_timeout(self):
        trace = {
            "version": 1,
            "kind": "select_filter_option",
            "field_label": "工单状态",
            "option_text": "处理中",
            "component": "Element Plus .el-select",
            "evidence": "visible field read back the selected value",
        }
        output = _with_failure_reminder(
            "UI_ACTION_TRACE=" + json.dumps(trace, ensure_ascii=False) + "\n"
            "RESULT=PASS: 下拉「工单状态」已选中「处理中」\n"
            "locator.inputValue: Timeout 15000ms exceeded\n"
            "waiting for locator('.el-form-item').locator('input[readonly]').inputValue()",
            skill_name="playwright-skill",
        )
        self.assertIn("【步骤已通过】", output)
        self.assertIn("继续执行下一测试步骤", output)
        self.assertNotIn("脚本错误，请修正后重试", output)

    def test_unrelated_timeout_after_verified_dropdown_is_not_ignored(self):
        trace = {
            "version": 1,
            "kind": "select_filter_option",
            "field_label": "工单状态",
            "option_text": "处理中",
        }
        output = _with_failure_reminder(
            "UI_ACTION_TRACE=" + json.dumps(trace, ensure_ascii=False) + "\n"
            "RESULT=PASS: 下拉「工单状态」已选中「处理中」\n"
            "locator.click: Timeout 15000ms exceeded\n"
            "waiting for locator('.el-button--primary')",
            skill_name="playwright-skill",
        )
        self.assertNotIn("【步骤已通过】", output)
        self.assertIn("脚本错误，请修正后重试", output)

    def test_missing_file_on_whart_test_appends_retry_then_report_reminder(self):
        output = _with_failure_reminder(
            "文件不存在: /tmp/case_1520_step1.png",
            skill_name="whart-test",
        )
        self.assertIn("脚本错误，请修正后重试", output)
        self.assertIn("测试执行结果: 不通过", output)


class FilterAssertionInstructionRegressionTests(SimpleTestCase):
    def test_dropdown_failure_cannot_be_overridden_by_page_wide_text(self):
        self.assertIn("筛选控件自身当前值与目标选项完全一致", MANUAL_TESTCASE_EXECUTION_HINT)
        self.assertIn("页面任意位置文本存在性", MANUAL_TESTCASE_EXECUTION_HINT)

    def test_status_filter_assertion_uses_matching_positive_and_negative_fixtures(self):
        self.assertIn("其他状态的造数工单不出现", MANUAL_TESTCASE_EXECUTION_HINT)
        self.assertIn("先核实每个造数工单号与状态的对应关系", MANUAL_TESTCASE_EXECUTION_HINT)
        self.assertIn("结果不可验证", MANUAL_TESTCASE_EXECUTION_HINT)

    def test_table_assertions_map_columns_from_visible_headers(self):
        self.assertIn("先读取当前表格可见的列头", MANUAL_TESTCASE_EXECUTION_HINT)
        self.assertIn("禁止凭经验硬编码列序号", MANUAL_TESTCASE_EXECUTION_HINT)
        self.assertIn("列表未展示该字段，无法按列验证", MANUAL_TESTCASE_EXECUTION_HINT)

    def test_negative_button_assertions_are_scoped_and_diagnostics_match(self):
        self.assertIn("负向断言必须检查目标控件本身", MANUAL_TESTCASE_EXECUTION_HINT)
        self.assertIn("页面任意位置的 `getByText()` 文案命中不代表按钮存在", MANUAL_TESTCASE_EXECUTION_HINT)
        self.assertIn("失败信息必须报告触发判定的同一组定位器和计数", MANUAL_TESTCASE_EXECUTION_HINT)
