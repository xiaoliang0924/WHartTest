from types import SimpleNamespace
from django.test import SimpleTestCase
from langchain_core.messages import AIMessage, ToolMessage
from orchestrator_integration.testcase_receipts import normalize_testcase_receipts
from orchestrator_integration.agent_loop_view import _latest_passed_helper_step_from_messages
from orchestrator_integration.builtin_tools.skill_tools import _tag_testcase_step_execution
from testcases.run_record_service import collect_tool_execution_transcript, build_step_results_from_execution_evidence
from unittest.mock import patch


class TestcaseReceiptTests(SimpleTestCase):
    def tool(self, output):
        return ToolMessage(content=output, name='execute_skill_script', tool_call_id='call')

    def test_readonly_verification_without_screenshot_has_step_boundary(self):
        text = '[PERSISTENT_SESSION] session_id=case_1146\nRESULT=INFO: value=WO-NOT-EXIST-0001\nRESULT=PASS: 步骤4通过，输入值符合预期'
        normalized = _tag_testcase_step_execution(text, 'const value = await page.locator("input").inputValue();', 1146)
        self.assertIn('[CASE_STEP_EXECUTION] case=1146 step=4\nRESULT=PASS', normalized)
        self.assertEqual(normalize_testcase_receipts(normalized, 1146), normalized)

    def test_checkpoint_contiguous_prefix_accepts_supplemental_receipts(self):
        messages = [self.tool(f'[CASE_STEP_EXECUTION] case=1146 step={n}\nRESULT=PASS') for n in range(1, 4)]
        messages += [self.tool('[CASE_STEP_EXECUTION] case=1146 step=4\n[CASE_SCREENSHOT] /tmp/case_1146_step4.png')]
        messages += [self.tool(f'[PERSISTENT_SESSION] session_id=case_1146\nRESULT=PASS: 步骤{n}通过') for n in (4, 5)]
        self.assertEqual(_latest_passed_helper_step_from_messages(messages, test_case_id=1146), 5)
        case = SimpleNamespace(id=1146, steps=[SimpleNamespace(step_number=n, description='read', expected_result='verified') for n in range(1, 6)])
        with patch('testcases.models.TestCaseScreenshot.objects.filter', side_effect=RuntimeError('no database')):
            rows = build_step_results_from_execution_evidence(case, collect_tool_execution_transcript(messages), fail_reason='')
        self.assertEqual([row['status'] for row in rows], ['pass'] * 5)

    def test_no_pass_from_assistant_screenshot_or_generic_output(self):
        for text in ['RESULT=PASS', '步骤4通过', '[CASE_SCREENSHOT] /tmp/case_1146_step4.png', "console.log('RESULT=PASS: 步骤4通过')", 'RESULT=INFO: 步骤4通过']:
            self.assertEqual(normalize_testcase_receipts(text, 1146), text)
        self.assertEqual(collect_tool_execution_transcript([AIMessage(content='RESULT=PASS: 步骤4通过')]), '')

    def test_rejects_other_case_and_supports_multiple_steps_and_failure(self):
        text = '[PERSISTENT_SESSION] session_id=case_999\nRESULT=PASS: 步骤4通过'
        self.assertEqual(normalize_testcase_receipts(text, 1146), text)
        result = normalize_testcase_receipts('RESULT = PASS: step 4 verified\nRESULT=FAIL: 步骤5失败', 1146)
        self.assertIn('case=1146 step=4', result)
        self.assertIn('case=1146 step=5', result)
        self.assertIn('RESULT=FAIL', result)
