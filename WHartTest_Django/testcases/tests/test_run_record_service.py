from types import SimpleNamespace
from unittest.mock import patch

import os
import tempfile
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from PIL import Image

from projects.models import Project
from testcases.models import (
    TestCase as TestCaseModel,
    TestCaseModule,
    TestCaseScreenshot,
    TestCaseRunRecord,
    TestCaseStep,
)
from testcases.serializers import TestCaseRunRecordSerializer
from testcases.run_record_service import (
    _append_bounded_tool_output,
    attach_testcase_step_screenshot,
    append_testcase_tool_execution_output,
    build_execution_result_report,
    build_pre_data_usage,
    build_step_results_from_execution_evidence,
    ensure_execution_result_report,
    finalize_testcase_run_record,
    has_execution_result_report,
    is_filled_execution_result_report,
    list_missing_step_screenshots,
    reconcile_stale_bound_ui_run_records,
    start_testcase_run_record,
    _extract_fail_context,
)


def _fake_case(**kwargs):
    steps = kwargs.pop(
        "steps",
        [
            SimpleNamespace(step_number=1, description="登录", expected_result="登录成功"),
            SimpleNamespace(
                step_number=2,
                description="筛选处理中",
                expected_result="列表均为处理中",
            ),
            SimpleNamespace(step_number=3, description="点击处理", expected_result="进入详情"),
            SimpleNamespace(step_number=4, description="发送图片", expected_result="发送成功"),
        ],
    )
    return SimpleNamespace(
        id=kwargs.get("id", 1520),
        name=kwargs.get("name", "发送图片-正常流程"),
        level=kwargs.get("level", "P0"),
        steps=steps,
    )


class ExecutionResultReportTests(SimpleTestCase):
    def test_run_record_serializer_rebuilds_summary_when_status_conflicts(self):
        case = _fake_case(id=1313)
        stale_summary = build_execution_result_report(
            case,
            status="pass",
            step_results=[
                {
                    "step_number": step.step_number,
                    "description": step.description,
                    "expected_result": step.expected_result,
                    "actual_result": "符合预期",
                    "status": "pass",
                }
                for step in case.steps
            ],
            fail_reason="",
        )
        record = SimpleNamespace(
            status="fail",
            summary=stale_summary,
            testcase=case,
            step_results=[
                {
                    "step_number": step.step_number,
                    "description": step.description,
                    "expected_result": step.expected_result,
                    "actual_result": "步骤已有浏览器执行记录，但没有返回明确断言回执"
                    if step.step_number == 3
                    else "通过",
                    "status": "fail" if step.step_number == 3 else "pass",
                }
                for step in case.steps
            ],
        )

        summary = TestCaseRunRecordSerializer().get_summary(record)

        self.assertIn("## 测试执行结果: 不通过", summary)
        self.assertIn("| 3 | 点击处理 | 失败：步骤已有浏览器执行记录", summary)
        self.assertIn("步骤 3 失败", summary)

    def test_structured_tool_boundary_associates_result_before_screenshot(self):
        case = _fake_case(id=1777)
        transcript = (
            "[CASE_STEP_EXECUTION] case=1777 step=2\n"
            "RESULT=PASS: 页面列表已核对工单号\n"
            "[CASE_SCREENSHOT] /tmp/case_1777_step2.png\n"
        )
        results = build_step_results_from_execution_evidence(
            case, transcript, fail_reason=""
        )
        by_step = {item["step_number"]: item for item in results}
        self.assertEqual(by_step[2]["status"], "pass")

    def test_structured_tool_boundary_keeps_identifier_evidence_before_screenshot(self):
        ticket_no = "2026100858033021"
        case = _fake_case(
            id=1780,
            steps=[
                SimpleNamespace(step_number=1, description="登录", expected_result="成功"),
                SimpleNamespace(
                    step_number=2,
                    description=f"核对本次造数工单号 {ticket_no}",
                    expected_result="列表显示匹配工单",
                ),
            ],
        )
        transcript = (
            "[CASE_STEP_EXECUTION] case=1780 step=2\n"
            f"RESULT=PASS: 列表显示并核对工单号 {ticket_no}\n"
            "[CASE_SCREENSHOT] /tmp/case_1780_step2.png\n"
        )
        results = build_step_results_from_execution_evidence(
            case, transcript, fail_reason=""
        )
        by_step = {item["step_number"]: item for item in results}
        self.assertEqual(by_step[2]["status"], "pass")

    def test_structured_tool_boundary_preserves_step_timeout_without_receipt(self):
        case = _fake_case(id=1778)
        transcript = (
            "[CASE_STEP_EXECUTION] case=1778 step=2\n"
            "locator.getByText: Timeout 15000ms exceeded.\n"
            "[CASE_SCREENSHOT] /tmp/case_1778_step2.png\n"
        )
        results = build_step_results_from_execution_evidence(
            case, transcript, fail_reason=""
        )
        by_step = {item["step_number"]: item for item in results}
        self.assertEqual(by_step[2]["status"], "fail")
        self.assertIn("Timeout 15000ms exceeded", by_step[2]["actual_result"])

    def test_timeout_remains_the_failure_reason_when_retry_guidance_contains_result_examples(self):
        case = _fake_case(id=1782)
        transcript = (
            "[CASE_STEP_EXECUTION] case=1782 step=2\\n"
            "locator.waitFor: Timeout 15000ms exceeded\\n"
            "【脚本错误，请修正后重试】其它步骤截图；RESULT=FAIL 时禁止继续。"
        )
        results = build_step_results_from_execution_evidence(
            case, transcript, fail_reason=""
        )
        by_step = {item["step_number"]: item for item in results}
        self.assertEqual(by_step[2]["status"], "fail")
        self.assertIn("Timeout 15000ms exceeded", by_step[2]["actual_result"])
        self.assertNotIn("时禁止继续", by_step[2]["actual_result"])

    def test_bounded_tool_log_keeps_a_parseable_delimiter_after_truncation(self):
        marker = "\n[TESTCASE_TOOL_OUTPUT]\n"
        first = _append_bounded_tool_output("", "old output")
        bounded = _append_bounded_tool_output(first, "x" * 9000 + "CASE_STEP_EXECUTION")
        self.assertLessEqual(len(bounded), 8000)
        self.assertTrue(bounded.startswith(marker))
        self.assertIn("CASE_STEP_EXECUTION", bounded)

    def test_structured_tool_boundary_reports_missing_assertion_without_inventing_pass(self):
        case = _fake_case(id=1779)
        transcript = (
            "[CASE_STEP_EXECUTION] case=1779 step=2\n"
            "[CASE_SCREENSHOT] /tmp/case_1779_step2.png\n"
        )
        results = build_step_results_from_execution_evidence(
            case, transcript, fail_reason=""
        )
        by_step = {item["step_number"]: item for item in results}
        self.assertEqual(by_step[2]["status"], "fail")
        self.assertIn("没有返回浏览器断言回执", by_step[2]["actual_result"])

    def test_screenshot_only_followup_does_not_overwrite_navigation_failure_receipt(self):
        case = _fake_case(id=1781)
        transcript = (
            "[执行进度] 正在执行步骤 2/4\n"
            '{"command":"navigateByMenu(...)",'
            '"result":"RESULT=FAIL: 导航菜单后页面未变化，当前=/work-order/my-tickets"}\n'
            "[CASE_STEP_EXECUTION] case=1781 step=2\n"
            "[CASE_SCREENSHOT] /tmp/case_1781_step2.png\n"
        )
        results = build_step_results_from_execution_evidence(
            case, transcript, fail_reason=""
        )
        by_step = {item["step_number"]: item for item in results}
        self.assertEqual(by_step[2]["status"], "fail")
        self.assertIn("导航菜单后页面未变化", by_step[2]["actual_result"])
        self.assertNotIn("缺少对应的浏览器断言回执", by_step[2]["actual_result"])

    def test_missing_ai_steps_do_not_pass_without_fixed_helper(self):
        outcome = ensure_execution_result_report(
            _fake_case(),
            transcript=(
                "[执行进度] 正在执行步骤 1/4\n"
                "[CASE_SCREENSHOT] /tmp/case_1520_step1.png\n"
                "RESULT=PASS: 步骤1登录成功"
            ),
            error_message="步骤 2～4 尚未完成；纯 AI 执行不能以缺少固定脚本代替实际步骤",
        )
        self.assertEqual(outcome["status"], "fail")
        self.assertEqual(outcome["step_results"][0]["status"], "pass")
        self.assertTrue(any(step["status"] == "skip" for step in outcome["step_results"][1:]))

    def test_confirms_generated_ticket_used_from_assistant_tool_trace(self):
        data_run = SimpleNamespace(output_snapshot={"ticketNo": "2026091570663010"})
        usage = build_pre_data_usage(
            data_run,
            "await helpers.fillFilterField(page, '工单号', '2026091570663010');\n"
            "await helpers.clickRowAction(page, '2026091570663010', '处理');",
        )
        self.assertEqual(usage["status"], "verified_used")
        self.assertEqual(usage["matched_identifier"]["value"], "2026091570663010")

    def test_confirms_generated_ticket_from_visible_row_and_matching_ticket_url(self):
        ticket_no = "2026091570663010"
        data_run = SimpleNamespace(output_snapshot={"ticketNo": ticket_no})
        usage = build_pre_data_usage(
            data_run,
            "ROW_VISIBLE=true\n"
            f"RESULT=PASS: 已进入工单详情 URL=https://test.example/work-order/my-tickets/{ticket_no}\n"
            "[CASE_SCREENSHOT] /tmp/case_1478_step3.png",
        )
        self.assertEqual(usage["status"], "verified_used")
        self.assertIn("ROW_VISIBLE=true", usage["evidence"])

    def test_does_not_accept_unrelated_row_action_as_generated_data_evidence(self):
        ticket_no = "2026091570663010"
        data_run = SimpleNamespace(output_snapshot={"ticketNo": ticket_no})
        usage = build_pre_data_usage(
            data_run,
            "clickRowAction(page, 'OTHER-TICKET', '处理')\n"
            + ("无关日志。" * 100)
            + f"\n日志引用工单号 {ticket_no}，但无本条工单查询或定位证据。",
        )
        self.assertEqual(usage["status"], "referenced")

    def test_does_not_accept_visible_row_for_a_different_ticket(self):
        ticket_no = "2026091570663010"
        data_run = SimpleNamespace(output_snapshot={"ticketNo": ticket_no})
        usage = build_pre_data_usage(
            data_run,
            "ROW_VISIBLE=true\n"
            "RESULT=PASS: 页面显示了其他工单 URL=https://test.example/work-order/my-tickets/OTHER-1\n"
            f"执行摘要引用本次造数标识 {ticket_no}。",
        )
        self.assertEqual(usage["status"], "referenced")

    def test_does_not_treat_prompt_injection_as_usage_without_agent_evidence(self):
        data_run = SimpleNamespace(output_snapshot={"ticketNo": "2026091570663010"})
        usage = build_pre_data_usage(data_run, "已进入工单列表，开始按状态筛选。")
        self.assertEqual(usage["status"], "not_confirmed")

    def test_step_results_from_helper_evidence_does_not_raise_on_py311_flags(self):
        case = _fake_case(
            id=1402,
            steps=[
                SimpleNamespace(step_number=n, description=f"步骤{n}", expected_result="")
                for n in range(1, 8)
            ],
        )
        transcript = (
            "5. 断言失败不要杀进程：`console.log('RESULT=FAIL: ...')` + 截图。通过则 `RESULT=PASS`。\n"
            "[CASE_SCREENSHOT] /tmp/case_1402_step1.png\\n"
            "RESULT=PASS: 步骤1登录成功 URL=https://example.test/work-order/my-tickets\\n"
            "[AUTO_RESUME] 强制执行步骤 2/7: await helpers.runClaimableTicketCaseStep(page, 2);\n"
            "[执行进度] 正在执行步骤 2/7\n"
            "[CASE_SCREENSHOT] /tmp/case_1402_step2.png\n"
            "RESULT=PASS: 步骤2已进入我的工单列表\n"
            "[AUTO_RESUME] 强制执行步骤 7/7: await helpers.runClaimableTicketCaseStep(page, 7);\n"
            "RESULT=FAIL: 列表中未找到状态为“处理中”的本次造数工单 2026092174132138\n"
        )
        results = build_step_results_from_execution_evidence(
            case, transcript, fail_reason=""
        )
        by_step = {item["step_number"]: item for item in results}
        self.assertEqual(by_step[1]["status"], "pass")
        self.assertEqual(by_step[2]["status"], "pass")
        self.assertEqual(by_step[7]["status"], "fail")
        fail_ctx = _extract_fail_context(transcript)
        self.assertIn("处理中", fail_ctx["reason"])
        self.assertNotIn("console.log", fail_ctx["reason"])

    def test_step_block_uses_last_result_not_first_pass(self):
        case = _fake_case(
            id=1402,
            steps=[
                SimpleNamespace(step_number=n, description=f"步骤{n}", expected_result="")
                for n in range(1, 8)
            ],
        )
        transcript = (
            "[执行进度] 正在执行步骤 1/7\nRESULT=PASS: 步骤1登录成功\n"
            "[执行进度] 正在执行步骤 2/7\nRESULT=PASS: 步骤2已进入我的工单列表\n"
            "[执行进度] 正在执行步骤 3/7\nRESULT=PASS: 步骤3已找到造数工单\n"
            "[执行进度] 正在执行步骤 4/7\n"
            "RESULT=PASS: 详情页已展示工单号 2026092237247130\n"
            "[CASE_SCREENSHOT] /tmp/case_1402_step4.png\n"
            "RESULT=FAIL: 详情页未出现“领取工单”按钮，已保存详情页证据截图\n"
        )
        results = build_step_results_from_execution_evidence(
            case, transcript, fail_reason=""
        )
        by_step = {item["step_number"]: item for item in results}
        self.assertEqual(by_step[1]["status"], "pass")
        self.assertEqual(by_step[2]["status"], "pass")
        self.assertEqual(by_step[3]["status"], "pass")
        self.assertEqual(by_step[4]["status"], "fail")
        self.assertEqual(by_step[5]["status"], "skip")

    def test_step7_playwright_timeout_is_fail_not_pass(self):
        case = _fake_case(
            id=1402,
            steps=[
                SimpleNamespace(step_number=n, description=f"步骤{n}", expected_result="")
                for n in range(1, 8)
            ],
        )
        transcript = (
            "[CASE_SCREENSHOT] /tmp/case_1402_step6.png\n"
            "RESULT=PASS: 步骤6已确认领取，详情页状态为“处理中”\n"
            "步骤 7: 返回我的工单列表\n"
            "[执行进度] 正在执行步骤 7/7\n"
            "Playwright exec timed out after 45000ms\n"
            "Playwright exec timed out after 45000ms\n"
            "浏览器已重置。请先重新打开该步骤所在页面\n"
        )
        results = build_step_results_from_execution_evidence(
            case, transcript, fail_reason=""
        )
        by_step = {item["step_number"]: item for item in results}
        self.assertEqual(by_step[6]["status"], "pass")
        self.assertEqual(by_step[7]["status"], "fail")
        outcome = ensure_execution_result_report(case, transcript=transcript)
        self.assertEqual(outcome["status"], "fail")
        self.assertEqual(outcome["step_results"][6]["status"], "fail")
        fail_ctx = _extract_fail_context(transcript)
        self.assertIn("timed out", fail_ctx["reason"].lower())

    def test_locator_timeout_after_helper_pass_marks_current_step_failed(self):
        case = _fake_case(
            id=1191,
            steps=[
                SimpleNamespace(step_number=n, description=f"步骤{n}", expected_result="")
                for n in range(1, 5)
            ],
        )
        transcript = (
            "[执行进度] 正在执行步骤 1/4\nRESULT=PASS: 步骤1登录成功\n"
            "[执行进度] 正在执行步骤 2/4\nRESULT=PASS: 步骤2已进入工单列表\n"
            "[执行进度] 正在执行步骤 3/4\n"
            "RESULT=PASS: 下拉「工单状态」已选中「待处理」\n"
            "locator.innerText: Timeout 15000ms exceeded.\n"
            "Call log:\n- waiting for locator('.el-form-item').filter({ hasText: '工单状态' })\n"
        )

        results = build_step_results_from_execution_evidence(
            case, transcript, fail_reason="locator.innerText: Timeout 15000ms exceeded"
        )
        by_step = {item["step_number"]: item for item in results}

        self.assertEqual(by_step[1]["status"], "pass")
        self.assertEqual(by_step[2]["status"], "pass")
        self.assertEqual(by_step[3]["status"], "fail")
        self.assertEqual(by_step[4]["status"], "skip")

    def test_timeout_recovered_by_later_pass_does_not_fail_step(self):
        case = _fake_case(
            id=1403,
            steps=[
                SimpleNamespace(step_number=n, description=f"步骤{n}", expected_result="")
                for n in range(1, 3)
            ],
        )
        transcript = (
            "[执行进度] 正在执行步骤 1/2\nRESULT=PASS: 步骤1登录成功\n"
            "[执行进度] 正在执行步骤 2/2\n"
            "locator.innerText: Timeout 15000ms exceeded; retrying with helper\n"
            "RESULT=PASS: 步骤2已选中待处理并校验筛选框值\n"
        )

        results = build_step_results_from_execution_evidence(
            case, transcript, fail_reason="locator.innerText: Timeout 15000ms exceeded"
        )
        by_step = {item["step_number"]: item for item in results}

        self.assertEqual(by_step[1]["status"], "pass")
        self.assertEqual(by_step[2]["status"], "pass")

    def test_detects_existing_report(self):
        self.assertTrue(has_execution_result_report("## 测试执行结果: 不通过\n### 基本信息\n- 测试用例ID: 1520"))
        self.assertFalse(has_execution_result_report("【执行失败】\n- 失败步骤：第1步"))

    def test_ignores_prompt_template_as_report(self):
        template = (
            "## 测试执行结果: 通过/不通过\n\n### 基本信息\n- 测试用例ID:\n"
            "| 1 | … | 通过 / 失败：具体原因 / 未执行 |"
        )
        self.assertFalse(is_filled_execution_result_report(template))

    def test_synthesizes_when_only_prompt_template_present(self):
        user_prompt = (
            "执行结束后必须输出完整「测试执行结果」报告\n"
            "## 测试执行结果: 通过/不通过\n- 测试用例ID:\n"
            "6. 若失败或中途无法继续：立刻输出完整「不通过」报告"
        )
        outcome = ensure_execution_result_report(
            _fake_case(),
            transcript=f"{user_prompt}\n步骤1 登录失败\nRESULT=FAIL: 登录页未出现账号密码框",
            assistant_transcript="",
        )
        self.assertTrue(outcome["injected"])
        self.assertEqual(outcome["status"], "fail")
        self.assertIn("1520", outcome["report"])

    def test_synthesizes_report_when_model_ends_silently_after_result_fail(self):
        outcome = ensure_execution_result_report(
            _fake_case(),
            transcript="步骤2 筛选处理中\nRESULT=FAIL: 筛选未生效，列表仍为混合状态",
        )
        self.assertTrue(outcome["injected"])
        self.assertEqual(outcome["status"], "fail")
        self.assertIn("## 测试执行结果: 不通过", outcome["report"])
        self.assertIn("筛选处理中", outcome["report"])
        self.assertIn("未执行", outcome["report"])
        self.assertEqual(outcome["step_results"][0]["status"], "pass")
        self.assertEqual(outcome["step_results"][1]["status"], "fail")
        self.assertEqual(outcome["step_results"][2]["status"], "skip")

    def test_ignores_result_fail_mentioned_only_in_guard_instructions(self):
        outcome = ensure_execution_result_report(
            _fake_case(),
            transcript=(
                "RESULT=PASS: fixed helper complete\n"
                "helper 返回 null 或输出 RESULT=FAIL 时立即判不通过，"
                "禁止自行补截图或改报通过。"
            ),
        )
        self.assertEqual(outcome["status"], "pass")

    def test_synthesizes_pass_report_from_all_fixed_helper_steps(self):
        transcript = "\n".join(
            f"[CASE_SCREENSHOT] /tmp/case_1520_step{step}.png\nRESULT=PASS: 步骤{step}通过"
            for step in range(1, 5)
        )
        outcome = ensure_execution_result_report(_fake_case(), transcript=transcript)
        self.assertTrue(outcome["injected"])
        self.assertEqual(outcome["status"], "pass")
        self.assertTrue(all(item["status"] == "pass" for item in outcome["step_results"]))

    def test_all_step_evidence_overrides_model_timeout_failure_text(self):
        transcript = "\n".join(
            f"[CASE_SCREENSHOT] /tmp/case_1520_step{step}.png\nRESULT=PASS: 步骤{step}通过"
            for step in range(1, 5)
        )
        outcome = ensure_execution_result_report(
            _fake_case(),
            transcript=(
                transcript
                + "\nModel call failed after 4 attempts with APITimeoutError.\n"
                + "RESULT=FAIL: ...')` + 截图。通过则 RESULT=PASS。"
            ),
        )
        self.assertEqual(outcome["status"], "pass")
        self.assertIn("## 测试执行结果: 通过", outcome["report"])

    def test_synthesizes_report_for_missing_screenshot_file(self):
        outcome = ensure_execution_result_report(
            _fake_case(),
            transcript="命令执行失败 (退出码 1)\n文件不存在: /tmp/case_1520_step1.png",
        )
        self.assertTrue(outcome["injected"])
        self.assertEqual(outcome["status"], "fail")
        self.assertIn("文件不存在", outcome["report"])
        self.assertEqual(outcome["step_results"][0]["status"], "fail")
        self.assertEqual(outcome["step_results"][1]["status"], "skip")

    def test_does_not_duplicate_existing_full_report(self):
        existing = (
            "## 测试执行结果: 通过\n\n### 基本信息\n- 测试用例ID: 1520\n"
            "### 执行过程与结果\n| 1 | 登录 | 通过 |"
        )
        outcome = ensure_execution_result_report(_fake_case(), transcript=existing)
        self.assertFalse(outcome["injected"])
        self.assertEqual(outcome["status"], "pass")
        self.assertTrue(outcome["report"].startswith("## 测试执行结果: 通过"))

    def test_authoritative_ai_tool_evidence_replaces_contradictory_model_report(self):
        case = _fake_case(id=1520)
        model_report = build_execution_result_report(
            case,
            status="pass",
            step_results=[
                {
                    "step_number": step.step_number,
                    "description": step.description,
                    "expected_result": step.expected_result,
                    "actual_result": "符合预期",
                    "status": "pass",
                }
                for step in case.steps
            ],
            fail_reason="",
        )
        tool_transcript = "\n".join(
            f"[CASE_STEP_EXECUTION] case=1520 step={step}\n"
            + ("[CASE_SCREENSHOT] /tmp/case_1520_step3.png" if step == 3
               else f"RESULT=PASS: 步骤{step}符合预期")
            for step in range(1, 5)
        )

        outcome = ensure_execution_result_report(
            case,
            transcript="",
            assistant_transcript=model_report,
            execution_mode="ai_only",
            tool_transcript=tool_transcript,
        )

        self.assertEqual(outcome["status"], "fail")
        self.assertTrue(outcome["injected"])
        self.assertIn("## 测试执行结果: 不通过", outcome["summary"])
        self.assertNotIn("## 测试执行结果: 通过", outcome["summary"])
        self.assertEqual(outcome["step_results"][2]["status"], "fail")
        self.assertIn("没有返回浏览器断言回执", outcome["summary"])

    def test_report_contains_basic_info_and_analysis_sections(self):
        report = build_execution_result_report(
            _fake_case(id=1436, name="移交审批按钮缺失提示"),
            status="fail",
            step_results=[
                {
                    "step_number": 1,
                    "description": "登录",
                    "status": "pass",
                    "actual_result": "通过",
                },
                {
                    "step_number": 2,
                    "description": "查找移交审批按钮",
                    "status": "fail",
                    "actual_result": "按钮缺失",
                },
            ],
            fail_reason="缺少符合前置条件的测试数据",
        )
        self.assertIn("### 基本信息", report)
        self.assertIn("1436", report)
        self.assertIn("### 问题分析", report)
        self.assertIn("### 结论", report)


class FinalizeRunRecordReportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username="run-record-user",
            password="password",
            email="run@example.com",
        )
        self.project = Project.objects.create(
            name="Report Project",
            description="d",
            creator=self.user,
        )
        self.module = TestCaseModule.objects.create(
            project=self.project,
            name="Report Module",
            creator=self.user,
        )
        self.testcase = TestCaseModel.objects.create(
            project=self.project,
            module=self.module,
            name="移交审批-正常流程",
            level="P0",
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=self.testcase,
            step_number=1,
            description="登录",
            expected_result="登录成功",
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=self.testcase,
            step_number=2,
            description="点击移交审批",
            expected_result="出现弹窗",
            creator=self.user,
        )

    def test_stale_ui_script_runs_are_closed_but_fallback_and_recent_runs_are_preserved(self):
        stale_script = start_testcase_run_record(
            testcase_id=self.testcase.id,
            user_id=self.user.id,
            session_id="sess-stale-script-only",
            execution_mode="script_only",
        )
        stale_hybrid = start_testcase_run_record(
            testcase_id=self.testcase.id,
            user_id=self.user.id,
            session_id="sess-stale-hybrid-script",
            execution_mode="hybrid",
            ui_test_case_id=654,
        )
        fallback = start_testcase_run_record(
            testcase_id=self.testcase.id,
            user_id=self.user.id,
            session_id="sess-active-agent-fallback",
            execution_mode="hybrid",
            ui_test_case_id=654,
        )
        fallback.actual_execution_path = "agent_fallback"
        fallback.save(update_fields=["actual_execution_path"])
        recent_script = start_testcase_run_record(
            testcase_id=self.testcase.id,
            user_id=self.user.id,
            session_id="sess-recent-script-only",
            execution_mode="script_only",
        )

        stale_time = timezone.now() - timedelta(hours=1)
        TestCaseRunRecord.objects.filter(
            pk__in=[stale_script.pk, stale_hybrid.pk]
        ).update(started_at=stale_time)

        self.assertEqual(reconcile_stale_bound_ui_run_records(), 2)

        stale_script.refresh_from_db()
        stale_hybrid.refresh_from_db()
        fallback.refresh_from_db()
        recent_script.refresh_from_db()
        self.assertEqual(stale_script.status, "error")
        self.assertEqual(stale_hybrid.status, "error")
        self.assertIsNotNone(stale_script.completed_at)
        self.assertIn("执行超时或执行进程中断", stale_script.summary)
        self.assertEqual(fallback.status, "running")
        self.assertIsNone(fallback.completed_at)
        self.assertEqual(recent_script.status, "running")
        self.assertIsNone(recent_script.completed_at)

    def test_finalize_injects_report_when_assistant_text_is_empty(self):
        start_testcase_run_record(
            testcase_id=self.testcase.id,
            user_id=self.user.id,
            session_id="sess-silent-fail",
        )
        record = finalize_testcase_run_record(
            session_id="sess-silent-fail",
            final_content="",
            transcript="RESULT=FAIL: 页面没有移交审批按钮，只有审批详情",
        )
        self.assertIsNotNone(record)
        self.assertEqual(record.status, "fail")
        self.assertIn("## 测试执行结果: 不通过", record.summary)
        self.assertIn("点击移交审批", record.summary)
        self.assertTrue(getattr(record, "injected_report", False))
        self.assertEqual(len(record.step_results), 2)
        self.assertEqual(record.step_results[0]["status"], "fail")
        self.assertEqual(record.step_results[1]["status"], "skip")

    def test_finalize_uses_durable_tool_output_when_checkpoint_misses_result(self):
        session_id = "sess-tool-output-checkpoint-gap"
        testcase = self._create_message_case(
            "列表精确筛选",
            [
                ("登录", "登录成功"),
                ("查询并核对列表工单号", "仅显示本次造数工单"),
            ],
        )
        start_testcase_run_record(
            testcase_id=testcase.id,
            user_id=self.user.id,
            session_id=session_id,
            execution_mode="ai_only",
        )
        durable_output = (
            f"[执行进度] 正在执行步骤 1/2\n"
            f"[CASE_SCREENSHOT] /tmp/case_{testcase.id}_step1.png\n"
            "RESULT=PASS: 步骤1登录成功\n"
            f"[执行进度] 正在执行步骤 2/2\n"
            f"[CASE_SCREENSHOT] /tmp/case_{testcase.id}_step2.png\n"
            "RESULT=PASS: 步骤2列表仅显示匹配工单"
        )
        self.assertTrue(
            append_testcase_tool_execution_output(
                session_id=session_id,
                testcase_id=testcase.id,
                output=durable_output,
            )
        )
        with (
            patch(
                "testcases.run_record_service.collect_checkpoint_execution_transcript",
                return_value="",
            ),
            patch(
                "testcases.run_record_service.backfill_missing_step_screenshots",
                return_value=[],
            ),
        ):
            record = finalize_testcase_run_record(session_id=session_id)

        self.assertEqual(record.status, "pass")
        self.assertTrue(all(step["status"] == "pass" for step in record.step_results))

    def test_finalize_keeps_hybrid_script_attempt_in_final_audit_log(self):
        record = start_testcase_run_record(
            testcase_id=self.testcase.id,
            user_id=self.user.id,
            session_id="sess-hybrid-fallback-finalize",
            execution_mode="hybrid",
            ui_test_case_id=654,
        )
        record.actual_execution_path = "agent_fallback"
        record.execution_log = (
            "[HYBRID_SCRIPT_ATTEMPT]\nUI script timed out while locating button"
        )
        record.save(update_fields=["actual_execution_path", "execution_log"])
        finalized = finalize_testcase_run_record(
            session_id="sess-hybrid-fallback-finalize",
            final_content="## 测试执行结果: 通过\n全部步骤已完成。",
            transcript=(
                "RESULT=PASS: 已通过 Agent 接管并完成测试用例全部步骤。\n"
                "已进入工单详情 URL=https://test.example/work-order/my-tickets/2026"
            ),
        )
        finalized.refresh_from_db()
        self.assertEqual(finalized.actual_execution_path, "agent_fallback")
        self.assertIn("[HYBRID_SCRIPT_ATTEMPT]", finalized.execution_log)
        self.assertIn("[HYBRID_AGENT_FALLBACK]", finalized.execution_log)
        self.assertIn("UI script timed out", finalized.execution_log)
    def test_start_run_record_keeps_selected_mode_and_ui_case(self):
        record = start_testcase_run_record(
            testcase_id=self.testcase.id,
            user_id=self.user.id,
            session_id="sess-mode-audit",
            execution_mode="script_only",
            ui_test_case_id=321,
        )
        self.assertEqual(record.execution_mode, "script_only")
        self.assertEqual(record.actual_execution_path, "bound_ui_script")
        self.assertEqual(record.ui_test_case_id_snapshot, 321)

        hybrid_without_script = start_testcase_run_record(
            testcase_id=self.testcase.id,
            user_id=self.user.id,
            session_id="sess-mode-hybrid-no-script",
            execution_mode="hybrid",
        )
        self.assertEqual(
            hybrid_without_script.actual_execution_path,
            "agent_no_bound_script",
        )
        hybrid_with_script = start_testcase_run_record(
            testcase_id=self.testcase.id,
            user_id=self.user.id,
            session_id="sess-mode-hybrid-script",
            execution_mode="hybrid",
            ui_test_case_id=654,
        )
        self.assertEqual(
            hybrid_with_script.actual_execution_path,
            "hybrid_script_pending",
        )
    def test_pending_button_case_requires_actual_button_assertion(self):
        testcase = TestCaseModel.objects.create(
            project=self.project,
            module=self.module,
            name="待处理工单详情页-不展示移交审批按钮-状态拦截验证",
            level="P1",
            creator=self.user,
        )
        steps = (
            ("使用李亮账号登录", "登录成功"),
            ("进入待处理工单详情页", "成功进入工单详情页"),
            ("查看页面右上角按钮区域", "不展示「移交审批」按钮"),
        )
        for number, (description, expected) in enumerate(steps, 1):
            TestCaseStep.objects.create(
                test_case=testcase,
                step_number=number,
                description=description,
                expected_result=expected,
                creator=self.user,
            )
        with tempfile.TemporaryDirectory() as temp_dir:
            for run_number, final_result, expected_status in (
                (1, "步骤3已找到造数工单；步骤4将进领取详情", "fail"),
                (2, "步骤3已确认待处理工单详情未显示移交审批按钮", "pass"),
            ):
                session_id = f"pending-button-{run_number}"
                start_testcase_run_record(
                    testcase_id=testcase.id,
                    user_id=self.user.id,
                    session_id=session_id,
                )
                evidence = []
                for step_number in range(1, 4):
                    path = os.path.join(temp_dir, f"step-{run_number}-{step_number}.png")
                    Image.new("RGB", (16, 16)).save(path, format="PNG")
                    attach_testcase_step_screenshot(
                        testcase_id=testcase.id,
                        step_number=step_number,
                        filepath=path,
                        uploader_id=self.user.id,
                    )
                    message = final_result if step_number == 3 else f"步骤{step_number}通过"
                    evidence.append(
                        f"[执行进度] 正在执行步骤 {step_number}/3\n"
                        f"[CASE_SCREENSHOT] {path}\nRESULT=PASS: {message}"
                    )
                record = finalize_testcase_run_record(
                    session_id=session_id,
                    transcript="\n".join(evidence),
                )
                self.assertEqual(record.status, expected_status)
                self.assertEqual(record.step_results[2]["status"], expected_status)
                if expected_status == "fail":
                    self.assertIn("执行结果与用例步骤不一致", record.summary)

    def _write_step_png(self, directory, step_number):
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"case_{self.testcase.id}_step{step_number}.png")
        Image.new("RGB", (16, 16), color=(20, 80, 20)).save(path, format="PNG")
        return path

    def _create_message_case(self, name, steps):
        testcase = TestCaseModel.objects.create(
            project=self.project,
            module=self.module,
            name=name,
            level="P1",
            creator=self.user,
        )
        for number, (description, expected) in enumerate(steps, 1):
            TestCaseStep.objects.create(
                test_case=testcase,
                step_number=number,
                description=description,
                expected_result=expected,
                creator=self.user,
            )
        return testcase

    def _finalize_message_case(self, testcase, session_id, results):
        start_testcase_run_record(
            testcase_id=testcase.id,
            user_id=self.user.id,
            session_id=session_id,
        )
        evidence = [
            f"[执行进度] 正在执行步骤 {number}/{len(results)}\nRESULT=PASS: {result}"
            for number, result in enumerate(results, 1)
        ]
        # These tests target result reconciliation, not screenshot storage.
        with patch("testcases.run_record_service.backfill_missing_step_screenshots", return_value=[]):
            return finalize_testcase_run_record(
                session_id=session_id,
                transcript="\n".join(evidence),
            )

    def test_no_send_visibility_case_skips_message_content_validation(self):
        from data_generation.testcase_pre_data import (
            extract_expected_send_message_texts,
            is_ticket_detail_boundary_case,
            is_ticket_message_send_case,
        )

        testcase = self._create_message_case(
            "工单完成成功-底部沟通区不展示发送功能-功能拦截验证",
            [
                ("使用李亮账号登录系统", "登录成功"),
                ("进入【我的工单】处理详情页", "成功进入工单详情并展示沟通记录"),
                (
                    "查看页面底部沟通区",
                    "沟通记录为只读，不展示「发送消息」入口；「图片/文件/价格表」不可发送",
                ),
            ],
        )
        self.assertFalse(is_ticket_message_send_case(testcase))
        self.assertTrue(is_ticket_detail_boundary_case(testcase))
        self.assertEqual(extract_expected_send_message_texts(testcase), [])

        record = self._finalize_message_case(
            testcase,
            "message-hidden-control-case",
            [
                "步骤1登录成功",
                "步骤2已进入工单详情并展示沟通记录",
                "步骤3已确认沟通区只读且未展示发送入口",
            ],
        )
        self.assertEqual(record.status, "pass")
        self.assertTrue(all(step["status"] == "pass" for step in record.step_results))
        self.assertNotIn("消息内容校验", record.summary)
        self.assertIn("测试执行全部 3 个步骤均通过，测试通过", record.summary)

    def test_real_message_send_validation_failure_marks_step_and_report(self):
        from data_generation.testcase_pre_data import (
            extract_expected_send_message_texts,
            is_ticket_message_send_case,
        )

        testcase = self._create_message_case(
            "工单沟通区发送消息-正常流程",
            [
                ("使用李亮账号登录系统", "登录成功"),
                ("进入【我的工单】处理详情页", "成功进入工单详情"),
                (
                    "在沟通区输入「自动化校验消息」并点击「发送」按钮",
                    "沟通记录出现「自动化校验消息」",
                ),
            ],
        )
        self.assertTrue(is_ticket_message_send_case(testcase))
        self.assertEqual(extract_expected_send_message_texts(testcase), ["自动化校验消息"])

        record = self._finalize_message_case(
            testcase,
            "message-send-missing-assertion",
            ["步骤1登录成功", "步骤2已进入工单详情", "步骤3已点击发送按钮"],
        )
        self.assertEqual(record.status, "fail")
        self.assertEqual(record.step_results[2]["status"], "fail")
        self.assertIn("失败：真正的消息发送步骤缺少沟通记录内容断言", record.summary)
        self.assertIn("测试执行在步骤 3 失败", record.summary)
        self.assertNotIn("测试执行全部 3 个步骤均通过，测试通过", record.summary)

    def test_finalize_backfills_runtime_screenshot_and_passes(self):
        start_testcase_run_record(
            testcase_id=self.testcase.id,
            user_id=self.user.id,
            session_id="sess-backfill-pass",
        )
        runtime_dir = os.path.join(
            settings.MEDIA_ROOT,
            "skill_runtime",
            "screenshots",
            str(self.project.id),
            str(self.testcase.id),
        )
        step1 = self._write_step_png(runtime_dir, 1)
        step2 = self._write_step_png(runtime_dir, 2)
        record = finalize_testcase_run_record(
            session_id="sess-backfill-pass",
            transcript=(
                f"[CASE_SCREENSHOT] {step1}\nRESULT=PASS: 步骤1通过\n"
                f"[CASE_SCREENSHOT] {step2}\nRESULT=PASS: 步骤2通过"
            ),
        )
        self.assertIsNotNone(record)
        self.assertEqual(record.status, "pass")
        self.assertNotIn("截图校验", record.summary or "")
        self.assertEqual(
            TestCaseScreenshot.objects.filter(test_case=self.testcase).count(),
            2,
        )

    def test_finalize_still_fails_when_step_screenshot_file_is_missing(self):
        start_testcase_run_record(
            testcase_id=self.testcase.id,
            user_id=self.user.id,
            session_id="sess-missing-shot",
        )
        record = finalize_testcase_run_record(
            session_id="sess-missing-shot",
            transcript="RESULT=PASS: 步骤1通过\nRESULT=PASS: 步骤2通过",
        )
        self.assertEqual(record.status, "fail")
        self.assertIn("截图校验", record.summary or "")

    def test_start_does_not_wipe_screenshots_of_running_session(self):
        start_testcase_run_record(
            testcase_id=self.testcase.id,
            user_id=self.user.id,
            session_id="sess-keep-shots",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_step_png(tmp, 1)
            self.assertTrue(
                attach_testcase_step_screenshot(
                    testcase_id=self.testcase.id,
                    step_number=1,
                    filepath=path,
                    uploader_id=self.user.id,
                )
            )
        start_testcase_run_record(
            testcase_id=self.testcase.id,
            user_id=self.user.id,
            session_id="sess-keep-shots",
        )
        self.assertTrue(
            TestCaseScreenshot.objects.filter(
                test_case=self.testcase,
                step_number=1,
            ).exists()
        )
        self.assertEqual(
            list_missing_step_screenshots(self.testcase),
            [2],
        )

    def test_finalize_backfills_last_png_for_missing_final_step(self):
        start_testcase_run_record(
            testcase_id=self.testcase.id,
            user_id=self.user.id,
            session_id="sess-last-png-step2",
        )
        runtime_dir = os.path.join(
            settings.MEDIA_ROOT,
            "skill_runtime",
            "screenshots",
            str(self.project.id),
            str(self.testcase.id),
        )
        step1 = self._write_step_png(runtime_dir, 1)
        os.makedirs(runtime_dir, exist_ok=True)
        last_png = os.path.join(runtime_dir, "last.png")
        Image.new("RGB", (24, 24), color=(80, 20, 20)).save(last_png, format="PNG")
        record = finalize_testcase_run_record(
            session_id="sess-last-png-step2",
            transcript=(
                f"[CASE_SCREENSHOT] {step1}\nRESULT=PASS: 步骤1通过\n"
                f"[CASE_SCREENSHOT] {os.path.join(runtime_dir, f'case_{self.testcase.id}_step2.png')}\n"
                "RESULT=PASS: 步骤2通过"
            ),
        )
        self.assertIsNotNone(record)
        self.assertEqual(record.status, "pass")
        self.assertNotIn("截图校验", record.summary or "")
        self.assertTrue(
            TestCaseScreenshot.objects.filter(
                test_case=self.testcase,
                step_number=2,
            ).exists()
        )
