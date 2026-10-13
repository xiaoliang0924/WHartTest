from django.test import SimpleTestCase, TestCase
from types import SimpleNamespace
from langchain_core.messages import AIMessage, ToolMessage
from ui_automation.tests import GeneratedUiTestCasePersistenceTests
from testcases.models import TestCaseStep
from orchestrator_integration.builtin_tools.skill_tools import (
    extract_playwright_inline_code,
    normalize_playwright_skill_command,
    verified_filter_step_script,
    verified_filter_step_command,
)
from orchestrator_integration.agent_loop_view import _build_ui_generation_observations


class FilterPlanTests(SimpleTestCase):
    def setUp(self):
        self.before = [{'step_number': 3, 'description': '点击筛选条件区域的「工单类型」'}]
        self.selection = {'step_number': 4, 'description': '在弹窗中勾选「人工报价」类型'}

    def test_selection_uses_state_aware_helper(self):
        code = verified_filter_step_script(self.selection, self.before)
        self.assertIn('selectFilterDialogOption(page, "工单类型", "人工报价")', code)
        self.assertNotIn('.el-checkbox', code)

    def test_confirmation_verifies_backfill(self):
        step = {'step_number': 5, 'description': '点击弹窗中的「确定」按钮'}
        code = verified_filter_step_script(step, self.before + [self.selection])
        self.assertIn('confirmFilterDialog', code)
        self.assertIn('assertFilterFieldShows', code)

    def test_column_expectation_is_preserved(self):
        step = {'step_number': 7, 'description': '查看列表中每条记录的「工单类型」列', 'expected_result': '列表中所有工单类型均为「人工报价」'}
        code = verified_filter_step_script(step, self.before)
        self.assertIn('assertTableColumnEquals(page, "工单类型", "人工报价")', code)
        self.assertIn('verification.status !== "pass"', code)

    def test_ambiguous_or_non_filter_goals_are_not_rewritten(self):
        self.assertIsNone(verified_filter_step_script(self.selection, []))
        self.assertIsNone(verified_filter_step_script({'step_number': 4, 'description': '勾选「人工报价」「运价查询」'}, self.before))
        code = 'node run.js "await helpers.screenshotCaseStep(page, 4);"'
        self.assertEqual(verified_filter_step_command(1254, 4, code), code)

    def test_generation_uses_verified_source_not_discarded_script(self):
        messages = [AIMessage(content='', tool_calls=[{'id': 'call-1', 'name': 'execute_skill_script', 'args': {'command': 'WRONG_CHECKBOX_SCRIPT'}}]),
                    ToolMessage(content='[VERIFIED_STEP_SCRIPT] helpers.selectFilterDialogOption(page,"工单类型","人工报价")', tool_call_id='call-1', name='execute_skill_script')]
        result = _build_ui_generation_observations(messages)
        self.assertNotIn('WRONG_CHECKBOX_SCRIPT', result)
        self.assertIn('selectFilterDialogOption', result)


class FilterCommandIntegrationTests(TestCase):
    setUp = GeneratedUiTestCasePersistenceTests.setUp

    def test_actual_failed_locator_command_is_normalized_from_functional_goal(self):
        TestCaseStep.objects.create(test_case=self.testcase, step_number=3,
            description='点击筛选条件区域的「工单类型」', expected_result='弹窗打开', creator=self.user)
        TestCaseStep.objects.create(test_case=self.testcase, step_number=4,
            description='在弹窗中勾选「人工报价」类型', expected_result='人工报价选中', creator=self.user)
        wrong = 'node run.js "await option.click(); const checkedCount = await dialog.locator(\'.el-checkbox.is-checked\').count();"'
        command = verified_filter_step_command(self.testcase.id, 4, wrong)
        self.assertIn('selectFilterDialogOption', command)
        self.assertIn('[VERIFIED_STEP_SCRIPT]', command)
        self.assertNotIn('.el-checkbox', command)
        self.assertEqual(command.count('RESULT=PASS:'), 1)
        self.assertLess(command.index('selectFilterDialogOption'), command.index('[VERIFIED_STEP_SCRIPT]'))

    def test_complete_explicit_filter_tail_runs_in_order(self):
        for number, description, expected in [
            (3, '点击筛选条件区域的「工单类型」', '弹窗打开'),
            (4, '在弹窗中勾选「人工报价」类型', '人工报价选中'),
            (5, '点击弹窗中的「确定」按钮', '筛选值人工报价'),
            (6, '点击页面上的「查询」按钮', '列表刷新'),
            (7, '查看列表中每条记录的「工单类型」列', '列表类型均为「人工报价」'),
        ]:
            TestCaseStep.objects.create(test_case=self.testcase, step_number=number,
                description=description, expected_result=expected, creator=self.user)
        command = verified_filter_step_command(self.testcase.id, 4,
            'node run.js "await option.click(); await helpers.clickPageButton(page, \'查询\');"')
        for name in ('selectFilterDialogOption', 'confirmFilterDialog', 'queryFilterResults', 'assertTableColumnEquals'):
            self.assertIn(name, command)
        self.assertEqual(command.count('RESULT=PASS:'), 4)
        self.assertNotIn('helpers.clickPageButton', command)

    def test_verified_script_survives_persistent_executor_quote_unescaping(self):
        TestCaseStep.objects.create(test_case=self.testcase, step_number=3,
            description='点击筛选条件区域的「工单类型」', expected_result='弹窗打开', creator=self.user)
        TestCaseStep.objects.create(test_case=self.testcase, step_number=4,
            description='在弹窗中勾选「人工报价」类型', expected_result='人工报价选中', creator=self.user)
        TestCaseStep.objects.create(test_case=self.testcase, step_number=5,
            description='点击弹窗中的「确定」按钮', expected_result='筛选值人工报价', creator=self.user)
        TestCaseStep.objects.create(test_case=self.testcase, step_number=6,
            description='点击页面上的「查询」按钮', expected_result='列表刷新', creator=self.user)
        TestCaseStep.objects.create(test_case=self.testcase, step_number=7,
            description='查看列表中每条记录的「工单类型」列', expected_result='列表类型均为「人工报价」', creator=self.user)
        command = verified_filter_step_command(self.testcase.id, 4,
            'node run.js "await helpers.selectFilterDialogOption(page, \'工单类型\', \'人工报价\'); await helpers.screenshotCaseStep(page, 4);"')
        normalized = normalize_playwright_skill_command(command)
        extracted = extract_playwright_inline_code(command, normalized)
        self.assertIsNotNone(extracted)
        self.assertIn('[VERIFIED_STEP_SCRIPT]', extracted)
        self.assertIn('Buffer.from(', extracted)
        self.assertIn('assertTableColumnEquals', extracted)
        self.assertNotIn('console.log("[VERIFIED_STEP_SCRIPT] " + "await helpers', extracted)
