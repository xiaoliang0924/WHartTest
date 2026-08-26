from types import SimpleNamespace

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from projects.models import Project
from testcases.models import TestCase as TestCaseModel, TestCaseModule, TestCaseStep
from testcases.run_record_service import (
    build_execution_result_report,
    ensure_execution_result_report,
    finalize_testcase_run_record,
    has_execution_result_report,
    is_filled_execution_result_report,
    start_testcase_run_record,
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
