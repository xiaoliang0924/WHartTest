from types import SimpleNamespace

import os
import tempfile

from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from PIL import Image

from projects.models import Project
from testcases.models import (
    TestCase as TestCaseModel,
    TestCaseModule,
    TestCaseScreenshot,
    TestCaseStep,
)
from testcases.run_record_service import (
    attach_testcase_step_screenshot,
    build_execution_result_report,
    build_pre_data_usage,
    build_step_results_from_execution_evidence,
    ensure_execution_result_report,
    finalize_testcase_run_record,
    has_execution_result_report,
    is_filled_execution_result_report,
    list_missing_step_screenshots,
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
