"""造数管理单元测试。"""

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase as DjangoTestCase
from rest_framework.exceptions import ValidationError

from api_environments.models import ApiEnvironment
from data_generation.models import DataGenerationPlan, DataGenerationRun
from data_generation.exceptions import DataGenerationError
from data_generation.plan_validation import (
    ensure_plan_has_environment,
    plan_requires_default_environment,
)
from data_generation.serializers import DataGenerationPlanSerializer
from data_generation.services import PlanExecutor, substitute_templates
from projects.models import Project
from testcases.models import TestCase as ManualTestCase, TestCaseModule, TestCaseStep


class SubstituteTemplatesTests(DjangoTestCase):
    def test_replace_simple_variable(self):
        result = substitute_templates('工单{{work_order_id}}', {'work_order_id': 123})
        self.assertEqual(result, '工单123')

    def test_replace_dynamic_uuid(self):
        result = substitute_templates('{{uuid}}', {})
        self.assertTrue(isinstance(result, str))
        self.assertGreater(len(result), 10)

    def test_replace_nested_dict(self):
        result = substitute_templates(
            {'title': '测试{{suffix}}'},
            {'suffix': 'A'},
        )
        self.assertEqual(result['title'], '测试A')


class PlanExecutorValidationTests(DjangoTestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username='dg_user', password='pass')
        self.project = Project.objects.create(name='DG Project', creator=self.user)
        self.environment = ApiEnvironment.objects.create(
            name='test-env',
            base_url='http://example.com',
            project=self.project,
            created_by=self.user,
        )

    def test_empty_steps_fail(self):
        plan = DataGenerationPlan.objects.create(
            project=self.project,
            name='empty-plan',
            steps=[],
            created_by=self.user,
        )
        run = PlanExecutor(plan, triggered_by=self.user).execute()
        self.assertEqual(run.status, DataGenerationRun.STATUS_FAILED)
        self.assertIn('没有配置步骤', run.error_message)

    @patch('data_generation.services.InterfaceRunner')
    def test_api_call_success_updates_context(self, mock_runner_cls):
        mock_runner = MagicMock()
        mock_runner_cls.return_value = mock_runner
        mock_runner.run_interface.return_value = mock_runner
        mock_runner.get_response.return_value = {
            'success': True,
            'status_code': 200,
            'extracted_variables': {'token': 'abc'},
            'response': {'content': '{"data": {"id": 99}}'},
        }

        mock_interface = MagicMock()
        mock_interface.id = 1
        mock_interface.name = 'create-order'
        mock_interface.project_id = self.project.id
        mock_interface.get_interface_data.return_value = {'type': 'http', 'method': 'POST', 'url': '/orders'}

        plan = DataGenerationPlan.objects.create(
            project=self.project,
            name='api-plan',
            default_environment=self.environment,
            steps=[
                {
                    'type': 'api_call',
                    'name': '创建工单',
                    'interface_id': 1,
                    'extract': {'work_order_id': 'data.id'},
                }
            ],
            created_by=self.user,
        )

        with patch('data_generation.services.ApiInterface.objects.get', return_value=mock_interface):
            run = PlanExecutor(plan, triggered_by=self.user).execute()

        self.assertEqual(run.status, DataGenerationRun.STATUS_SUCCESS)
        self.assertEqual(run.output_snapshot.get('token'), 'abc')
        self.assertEqual(run.output_snapshot.get('work_order_id'), 99)

    @patch('data_generation.services.InterfaceRunner')
    def test_continue_on_error_allows_partial_success(self, mock_runner_cls):
        mock_runner = MagicMock()
        mock_runner_cls.return_value = mock_runner
        mock_runner.run_interface.return_value = mock_runner

        responses = [
            {
                'success': True,
                'status_code': 200,
                'extracted_variables': {'ticketId': 1},
                'response': {'content': {'ticketId': 1}},
            },
            {
                'success': False,
                'status_code': 500,
                'response': {'content': {'message': 'boom'}},
            },
        ]
        mock_runner.get_response.side_effect = responses

        mock_interface = MagicMock()
        mock_interface.id = 1
        mock_interface.name = 'iface'
        mock_interface.project_id = self.project.id
        mock_interface.get_interface_data.return_value = {'type': 'http', 'method': 'POST', 'url': '/x'}

        plan = DataGenerationPlan.objects.create(
            project=self.project,
            name='partial-plan',
            default_environment=self.environment,
            steps=[
                {
                    'type': 'api_call',
                    'name': 'ok-step',
                    'interface_id': 1,
                },
                {
                    'type': 'api_call',
                    'name': 'optional-step',
                    'interface_id': 1,
                    'continue_on_error': True,
                },
            ],
            created_by=self.user,
        )

        with patch('data_generation.services.ApiInterface.objects.get', return_value=mock_interface):
            run = PlanExecutor(plan, triggered_by=self.user).execute()

        self.assertEqual(run.status, DataGenerationRun.STATUS_SUCCESS)
        self.assertIn('optional-step', run.error_message or '')
        self.assertEqual(run.step_logs[-1]['status'], 'failed_continued')

    def test_set_env_var_skips_unresolved_template_values(self):
        plan = DataGenerationPlan.objects.create(
            project=self.project,
            name='skip-unresolved-plan',
            default_environment=self.environment,
            steps=[
                {
                    'type': 'set_env_var',
                    'name': '写入变量',
                    'environment_id': self.environment.id,
                    'variables': {
                        'ticketId': '{{ticketId}}',
                        'approvalToken': '{{approvalToken}}',
                    },
                }
            ],
            created_by=self.user,
        )
        executor = PlanExecutor(plan, triggered_by=self.user)
        executor.context = {'ticketId': 999}
        run = executor.execute()
        self.assertEqual(run.status, DataGenerationRun.STATUS_SUCCESS)
        from api_environments.models import ApiEnvironmentVariable

        self.assertTrue(
            ApiEnvironmentVariable.objects.filter(
                environment=self.environment,
                name='ticketId',
                value='999',
            ).exists()
        )
        self.assertFalse(
            ApiEnvironmentVariable.objects.filter(
                environment=self.environment,
                name='approvalToken',
            ).exists()
        )


class PlanEnvironmentValidationTests(DjangoTestCase):
    def test_detects_missing_step_environment(self):
        steps = [{'type': 'api_call', 'interface_id': 445}]
        self.assertTrue(plan_requires_default_environment(steps))

    def test_allows_when_step_has_environment(self):
        steps = [{'type': 'api_call', 'interface_id': 445, 'environment_id': 4}]
        self.assertFalse(plan_requires_default_environment(steps))

    def test_requires_default_environment_when_steps_need_it(self):
        with self.assertRaises(ValidationError):
            ensure_plan_has_environment(
                steps=[{'type': 'set_env_var', 'variables': {'ticketId': '1'}}],
                default_environment_id=None,
            )

    def test_serializer_rejects_plan_without_environment(self):
        user_model = get_user_model()
        user = user_model.objects.create_user(username='env_user', password='pass')
        project = Project.objects.create(name='Env Project', creator=user)
        serializer = DataGenerationPlanSerializer(
            data={
                'name': 'missing-env-plan',
                'target_type': 'both',
                'steps': [{'type': 'api_call', 'interface_id': 445}],
                'cleanup_steps': [],
                'is_active': True,
            },
            context={'project_id': project.id},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn('default_environment', serializer.errors)


class AnalysisGapTests(DjangoTestCase):
    def test_build_generation_description_from_gap(self):
        from data_generation.analysis import build_generation_description_from_gap

        text = build_generation_description_from_gap(
            '回归套件',
            ['ticketId', 'work_order_id'],
            'biz_create_type_a',
        )
        self.assertIn('回归套件', text)
        self.assertIn('ticketId', text)
        self.assertIn('biz_create_type_a', text)


class DefaultCleanupTemplateTests(DjangoTestCase):
    def test_business_template_has_default_cleanup(self):
        from data_generation.templates import get_template_by_key

        template = get_template_by_key('biz_create_type_a')
        self.assertIsNotNone(template)
        self.assertGreater(len(template.get('cleanup_steps') or []), 0)
        self.assertEqual(template['cleanup_steps'][0]['type'], 'sql')


class BindSuitePreDataPlanTests(DjangoTestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username='bind_user', password='pass')
        self.project = Project.objects.create(name='Bind Project', creator=self.user)
        self.environment = ApiEnvironment.objects.create(
            name='bind-env',
            base_url='http://example.com',
            project=self.project,
            created_by=self.user,
        )
        from testcases.models import TestCase, TestCaseModule, TestSuite

        self.module = TestCaseModule.objects.create(
            project=self.project,
            name='默认模块',
            creator=self.user,
        )
        self.testcase = TestCase.objects.create(
            project=self.project,
            module=self.module,
            name='工单用例',
            precondition='使用 ${{ticketId}} 登录',
            creator=self.user,
        )
        self.suite = TestSuite.objects.create(
            project=self.project,
            name='绑定测试套件',
            creator=self.user,
            max_concurrent_tasks=1,
        )
        self.suite.testcases.add(self.testcase)

    @patch('data_generation.llm_plan_generator._resolve_plan_steps_for_project')
    @patch('data_generation.llm_plan_generator.generate_plan_from_description_with_llm')
    def test_bind_suite_creates_plan_and_enables_cleanup(self, mock_generate, mock_resolve):
        from data_generation.services import bind_suite_pre_data_plan

        mock_generate.return_value = {
            'name': '套件前置造数',
            'description': 'auto',
            'target_type': 'both',
            'steps': [{'type': 'set_env_var', 'name': '写入', 'environment_id': self.environment.id, 'variables': {'ticketId': '1'}}],
            'cleanup_steps': [{'type': 'sql', 'name': '删除', 'database_config_id': 1, 'sql': 'DELETE FROM ticket WHERE id = 1', 'method': 'delete'}],
            'default_environment': self.environment.id,
            'generation_method': 'rule_match',
        }
        mock_resolve.side_effect = lambda plan, **kwargs: plan

        result = bind_suite_pre_data_plan(
            self.suite,
            created_by=self.user,
            default_environment_id=self.environment.id,
            use_llm=False,
        )
        self.suite.refresh_from_db()
        self.assertTrue(result['bound'])
        self.assertIsNotNone(self.suite.pre_data_plan_id)
        self.assertTrue(self.suite.post_data_cleanup)
        self.assertGreater(len(result['plan'].cleanup_steps or []), 0)

    @patch('data_generation.llm_plan_generator._resolve_plan_steps_for_project')
    @patch('data_generation.llm_plan_generator.build_plan_from_template_key')
    def test_bind_suite_reuses_existing_plan_name(self, mock_template, mock_resolve):
        from data_generation.services import bind_suite_pre_data_plan

        generated = {
            'name': '创建待分配工单 TYPE_A',
            'description': 'auto',
            'target_type': 'both',
            'steps': [{'type': 'set_env_var', 'name': '写入', 'environment_id': self.environment.id, 'variables': {'ticketId': '1'}}],
            'cleanup_steps': [{'type': 'sql', 'name': '删除', 'database_config_id': 1, 'sql': 'DELETE FROM ticket WHERE id = 1', 'method': 'delete'}],
            'default_environment': self.environment.id,
            'template_key': 'biz_create_type_a',
            'generation_method': 'rule_match',
        }
        mock_template.return_value = generated
        mock_resolve.side_effect = lambda plan, **kwargs: plan

        first = bind_suite_pre_data_plan(
            self.suite,
            created_by=self.user,
            default_environment_id=self.environment.id,
            use_llm=False,
        )
        second = bind_suite_pre_data_plan(
            self.suite,
            created_by=self.user,
            default_environment_id=self.environment.id,
            use_llm=False,
        )
        self.assertTrue(first['created'])
        self.assertFalse(second['created'])
        self.assertEqual(first['plan'].id, second['plan'].id)


class IntentRouterStateTests(DjangoTestCase):
    def test_processing_status_uses_claim_template(self):
        from data_generation.intent_router import infer_business_template_key

        self.assertEqual(
            infer_business_template_key('筛选工单状态为处理中'),
            'biz_create_and_claim',
        )
        self.assertEqual(
            infer_business_template_key('筛选工单状态为待处理'),
            'biz_create_and_assign',
        )

    def test_approval_ticket_uses_approval_processing_template(self):
        from data_generation.intent_router import (
            build_input_params,
            infer_business_template_key,
        )

        text = '工单列表中存在处理中且审批状态为空的审批工单数据'
        self.assertEqual(
            infer_business_template_key(text),
            'biz_create_approval_processing',
        )
        params = build_input_params(text, {'input_params': {}, 'steps': []})
        self.assertEqual(params['ticketType'], 'approval')


class TestcasePreDataResolverTests(DjangoTestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username='tc_pre_user', password='pass')
        self.project = Project.objects.create(name='TC Pre Project', creator=self.user)
        self.module = TestCaseModule.objects.create(
            project=self.project,
            name='工单列表',
            creator=self.user,
        )
        self.environment = ApiEnvironment.objects.create(
            name='tc-pre-env',
            base_url='http://example.com',
            project=self.project,
            created_by=self.user,
        )

    def test_infer_pending_assign_from_case_text(self):
        from data_generation.testcase_pre_data import resolve_pre_data_for_testcase

        testcase = ManualTestCase.objects.create(
            project=self.project,
            module=self.module,
            name='待分配-弹出选择处理人弹窗',
            precondition='系统内存在至少1个状态为待分配的工单',
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=testcase,
            step_number=1,
            description='在筛选条件中将工单状态选为待分配并搜索',
            expected_result='列表展示待分配工单',
            creator=self.user,
        )
        resolution = resolve_pre_data_for_testcase(testcase)
        self.assertEqual(resolution.source, 'inferred')
        self.assertEqual(resolution.template_key, 'biz_create_type_a')

    def test_approval_processing_case_inference(self):
        from data_generation.testcase_pre_data import resolve_pre_data_for_testcase

        testcase = ManualTestCase.objects.create(
            project=self.project,
            module=self.module,
            name='处理中-筛选找到审批工单记录',
            precondition='工单列表中存在处理中且审批状态为空的审批工单数据',
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=testcase,
            step_number=1,
            description='添加工单类型筛选条件为"审批工单"，当前状态筛选为"处理中"，点击搜索',
            expected_result='列表筛选成功，可找到审批状态为空的目标记录',
            creator=self.user,
        )
        resolution = resolve_pre_data_for_testcase(testcase)
        self.assertEqual(resolution.source, 'inferred')
        self.assertEqual(resolution.template_key, 'biz_create_approval_processing')
        self.assertEqual(resolution.input_params.get('ticketType'), 'approval')

    def test_module_plan_overrides_inference(self):
        from data_generation.models import DataGenerationPlan
        from data_generation.testcase_pre_data import resolve_pre_data_for_testcase

        plan = DataGenerationPlan.objects.create(
            project=self.project,
            name='模块默认造数',
            steps=[{'type': 'delay', 'name': 'wait', 'seconds': 0.01}],
            created_by=self.user,
        )
        self.module.pre_data_plan = plan
        self.module.save(update_fields=['pre_data_plan'])

        testcase = ManualTestCase.objects.create(
            project=self.project,
            module=self.module,
            name='待分配-弹出选择处理人弹窗',
            precondition='系统内存在至少1个状态为待分配的工单',
            creator=self.user,
        )
        resolution = resolve_pre_data_for_testcase(testcase)
        self.assertEqual(resolution.source, 'module')
        self.assertEqual(resolution.plan.id, plan.id)
