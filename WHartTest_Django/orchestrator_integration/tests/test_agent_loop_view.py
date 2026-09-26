import os
import tempfile
import time
from unittest.mock import patch
from types import SimpleNamespace

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
    _is_final_testcase_step_pass,
    _extract_helper_testcase_progress,
    _extract_helper_result_reason,
    _build_helper_step_commentary,
    _latest_passed_helper_step_from_messages,
    _build_incomplete_testcase_resume_prompt,
    _build_sanitized_messages,
    _extract_linked_image_urls,
    _is_linked_image_url_allowed,
    _normalize_uploaded_image_base64_list,
    _prepare_agent_loop_human_message,
    _build_ui_generation_observations,
    _force_save_generated_ui_case,
    _run_and_stream_remaining_helper_steps,
    _extract_testcase_step_from_tool_call,
    _map_ui_script_results_to_testcase_steps,
)


class ScriptOnlyResultMappingTests(SimpleTestCase):
    def setUp(self):
        self.steps = [
            SimpleNamespace(step_number=1, description="登录", expected_result="首页"),
            SimpleNamespace(step_number=2, description="打开详情", expected_result="详情页"),
        ]

    def test_maps_each_successful_ui_group_to_functional_step(self):
        results, status, reason = _map_ui_script_results_to_testcase_steps(
            self.steps,
            [{"group": 1, "status": "success"}, {"group": 2, "status": "pass"}],
            ui_status=2,
        )
        self.assertEqual(status, "pass")
        self.assertEqual(reason, "")
        self.assertEqual([item["status"] for item in results], ["pass", "pass"])

    def test_group_count_mismatch_fails_closed(self):
        results, status, reason = _map_ui_script_results_to_testcase_steps(
            self.steps, [{"group": 1, "status": "success"}], ui_status=2,
        )
        self.assertEqual(status, "fail")
        self.assertIn("无法逐步对应", reason)
        self.assertEqual([item["status"] for item in results], ["fail", "skip"])

    def test_failed_ui_group_fails_functional_step(self):
        results, status, _ = _map_ui_script_results_to_testcase_steps(
            self.steps,
            [{"group": 1, "status": "success"}, {"group": 2, "status": "failed"}],
            ui_status=3,
            ui_error="按钮未出现",
        )
        self.assertEqual(status, "fail")
        self.assertEqual([item["status"] for item in results], ["pass", "fail"])


class GeneratedUiCaseFollowupTests(SimpleTestCase):
    def test_followup_forces_save_tool_using_only_observed_browser_evidence(self):
        class SaveTool:
            name = "save_generated_ui_testcase"

            async def ainvoke(self, args):
                return '{"status":"success","ui_test_case_id":321}'

        class BoundModel:
            async def ainvoke(self, messages):
                self.messages = messages
                return AIMessage(tool_calls=[{
                    "name": "save_generated_ui_testcase",
                    "args": {"payload_json": '{"steps":[]}'}
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
            AIMessage(tool_calls=[{
                "name": "execute_skill_script",
                "args": {"command": "await page.getByRole('button', {name: '返回'}).click();"},
            }]),
            ToolMessage(
                content="RESULT=PASS: 页面返回成功",
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
        self.assertIn("RESULT=PASS", prompt_text)

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
        self.assertIn("浏览器操作记录", result["message"])


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
        self.assertEqual(_latest_passed_helper_step_from_messages(messages), 6)

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
        exc = Exception("Request timed out.")
        exc.__class__.__name__ = "APITimeoutError"

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

    def test_missing_file_on_whart_test_appends_retry_then_report_reminder(self):
        output = _with_failure_reminder(
            "文件不存在: /tmp/case_1520_step1.png",
            skill_name="whart-test",
        )
        self.assertIn("脚本错误，请修正后重试", output)
        self.assertIn("测试执行结果: 不通过", output)
