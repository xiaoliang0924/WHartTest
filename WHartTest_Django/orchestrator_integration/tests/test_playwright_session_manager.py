import threading
import time
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from orchestrator_integration.builtin_tools.persistent_playwright import (
    PlaywrightPersistentSessionError,
    PlaywrightSessionManager,
    _SessionEntry,
)
from orchestrator_integration.builtin_tools.skill_tools import (
    _cross_step_click_mismatch,
    _ensure_testcase_playwright_session_id,
    _infer_pending_testcase_step,
    _persist_testcase_tool_output,
    _requested_testcase_step,
    _tag_testcase_step_execution,
)


class TestcasePlaywrightSessionRoutingTests(SimpleTestCase):
    def test_extracts_one_explicit_step_from_browser_command(self):
        self.assertEqual(
            _requested_testcase_step(
                "await helpers.fillFilterField(page, '工单号', '123'); "
                "await helpers.screenshotCaseStep(page, 5);"
            ),
            5,
        )

    def test_extracts_login_as_step_one(self):
        self.assertEqual(
            _requested_testcase_step("await helpers.loginStep1(page);"),
            1,
        )

    def test_does_not_guess_when_command_targets_multiple_steps(self):
        self.assertIsNone(
            _requested_testcase_step(
                "await helpers.screenshotCaseStep(page, 2); "
                "await helpers.screenshotCaseStep(page, 3);"
            )
        )

    def test_tags_output_with_case_step_boundary(self):
        output = _tag_testcase_step_execution(
            "RESULT=PASS: 列表已核对\n[CASE_SCREENSHOT] /tmp/case_42_step5.png",
            "await helpers.fillFilterField(page, '工单号', '123'); "
            "await helpers.screenshotCaseStep(page, 5);",
            42,
        )
        self.assertTrue(output.startswith("[CASE_STEP_EXECUTION] case=42 step=5\n"))

    def test_inferred_step_override_tags_action_that_failed_before_screenshot(self):
        output = _tag_testcase_step_execution(
            "locator.click: Timeout 5000ms exceeded",
            "await helpers.clickPageButton(page, '查询');",
            42,
            step_override=4,
        )
        self.assertTrue(output.startswith("[CASE_STEP_EXECUTION] case=42 step=4\n"))

    def test_pending_step_follows_contiguous_persisted_pass_receipts(self):
        record = MagicMock()
        record.execution_log = (
            "[CASE_STEP_EXECUTION] case=42 step=1\nRESULT=PASS: login\n"
            "[CASE_STEP_EXECUTION] case=42 step=2\nRESULT=PASS: page\n"
            "[CASE_STEP_EXECUTION] case=42 step=3\nRESULT=PASS: filtered\n"
        )
        record.testcase.steps.count.return_value = 4
        queryset = MagicMock()
        queryset.only.return_value.first.return_value = record
        with patch("testcases.models.TestCaseRunRecord.objects.filter", return_value=queryset):
            pending = _infer_pending_testcase_step(42, "session-42")
        self.assertEqual(pending, 4)

    def test_persistent_browser_output_is_durably_saved_before_tool_return(self):
        with patch(
            "testcases.run_record_service.append_testcase_tool_execution_output",
            return_value=True,
        ) as persist:
            _persist_testcase_tool_output(
                session_id="session-42", testcase_id=42, output="RESULT=PASS: step 1"
            )
        persist.assert_called_once_with(
            session_id="session-42",
            testcase_id=42,
            output="RESULT=PASS: step 1",
        )

    def test_blocks_click_repeated_from_a_different_step(self):
        case = MagicMock()
        case.steps.filter.return_value.values_list.return_value.first.return_value = (
            "点击查询按钮",
            "列表筛选成功",
        )
        case.steps.exclude.return_value.values_list.return_value = [
            (2, '点击筛选区"工单类型"'),
        ]
        queryset = MagicMock()
        queryset.prefetch_related.return_value.first.return_value = case
        with patch("testcases.models.TestCase.objects.filter", return_value=queryset):
            mismatch = _cross_step_click_mismatch(
                1379, 4, "await helpers.clickPageButton(page, '工单类型');"
            )
        self.assertIn("该控件属于步骤2", mismatch)
        self.assertIn("已拦截", mismatch)

    def test_screenshot_only_call_does_not_create_step_execution_boundary(self):
        output = _tag_testcase_step_execution(
            "[CASE_SCREENSHOT] /tmp/case_42_step5.png",
            "await helpers.screenshotCaseStep(page, 5);",
            42,
        )
        self.assertEqual(output, "[CASE_SCREENSHOT] /tmp/case_42_step5.png")

    def test_does_not_duplicate_existing_step_boundary(self):
        output = _tag_testcase_step_execution(
            "[CASE_STEP_EXECUTION] case=42 step=5\nRESULT=PASS",
            "await helpers.screenshotCaseStep(page, 5);",
            42,
        )
        self.assertEqual(output.count("[CASE_STEP_EXECUTION]"), 1)

    def test_missing_session_id_uses_testcase_persistent_session(self):
        self.assertEqual(
            _ensure_testcase_playwright_session_id(
                None,
                skill_name="playwright-skill",
                run_js_args=["await page.getByText('查询').click()"],
                test_case_id=1163,
            ),
            "case_1163",
        )

    def test_explicit_session_id_is_preserved(self):
        self.assertEqual(
            _ensure_testcase_playwright_session_id(
                "case_42",
                skill_name="playwright-skill",
                run_js_args=["await page.title()"],
                test_case_id=1163,
            ),
            "case_42",
        )

    def test_non_javascript_skill_commands_do_not_get_a_browser_session(self):
        self.assertIsNone(
            _ensure_testcase_playwright_session_id(
                None,
                skill_name="playwright-skill",
                run_js_args=None,
                test_case_id=1163,
            )
        )


class PlaywrightSessionManagerConcurrencyTests(SimpleTestCase):
    def test_max_concurrent_executions_queues_second_call(self):
        manager = PlaywrightSessionManager(
            idle_timeout_seconds=60,
            max_sessions=2,
            max_concurrent_executions=1,
            exec_queue_timeout_seconds=5,
        )
        gate = threading.Event()
        release = threading.Event()

        def fake_exec(*args, **kwargs):
            gate.set()
            release.wait(timeout=10)
            return "ok"

        mock_proc = MagicMock()
        mock_proc.exec_run_js.side_effect = fake_exec

        entry = _SessionEntry(
            session_key="k1",
            skill_dir="/tmp/skill",
            proc=mock_proc,
            last_used_monotonic=time.monotonic(),
        )
        manager._entries["k1"] = entry

        results = []
        errors = []

        def run():
            try:
                out = manager.execute_run_js(
                    session_key="k1",
                    skill_dir="/tmp/skill",
                    run_js_args=["await page.goto('about:blank')"],
                    env={},
                    timeout_seconds=30,
                )
                results.append(out)
            except PlaywrightPersistentSessionError as exc:
                errors.append(str(exc))

        t1 = threading.Thread(target=run)
        t2 = threading.Thread(target=run)
        t1.start()
        self.assertTrue(gate.wait(timeout=3), "first exec should start")
        t2.start()
        time.sleep(0.3)
        release.set()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(results, ["ok", "ok"])
        self.assertEqual(errors, [])

    def test_queue_timeout_raises(self):
        manager = PlaywrightSessionManager(
            max_concurrent_executions=1,
            exec_queue_timeout_seconds=1,
        )
        manager._exec_semaphore.acquire()

        with self.assertRaises(PlaywrightPersistentSessionError):
            manager.execute_run_js(
                session_key="blocked",
                skill_dir="/tmp/skill",
                run_js_args=["1"],
                env={},
            )

        manager._exec_semaphore.release()
