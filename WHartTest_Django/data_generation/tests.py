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

    def test_claim_fixture_rejects_replacing_new_ticket_with_historical_ticket(self):
        plan = DataGenerationPlan.objects.create(
            project=self.project,
            name='claim-new-ticket-only',
            template_key='biz_create_and_claim',
            steps=[{'type': 'delay', 'name': '占位', 'seconds': 0}],
            created_by=self.user,
        )
        executor = PlanExecutor(plan, triggered_by=self.user)
        executor._claim_created_ticket_id = 5067
        executor._claim_created_ticket_no = '2026092356245017'
        executor.context.update(ticketId=5067, ticketNo='2026092356245017')
        executor._ensure_claim_created_ticket()

        executor.context.update(ticketId=5060, ticketNo='2026092339344010')
        with self.assertRaisesMessage(DataGenerationError, '禁止复用历史工单'):
            executor._ensure_claim_created_ticket()

    def test_reassign_fallback_does_not_adopt_historical_ticket_when_run_ticket_exists(self):
        plan = DataGenerationPlan.objects.create(
            project=self.project,
            name='assign-current-ticket-only',
            template_key='biz_create_and_assign',
            default_environment=self.environment,
            steps=[],
            created_by=self.user,
        )
        executor = PlanExecutor(plan, triggered_by=self.user)
        executor.context.update(ticketId=5091, ticketNo='2026092459709012')

        with patch('data_generation.services._query_ticket_rows_for_plan') as lookup:
            result = executor._adopt_existing_ticket_for_desired_assignee({
                'assigneeUserId': 46,
                'assigneeName': '李亮',
            })

        self.assertIsNone(result)
        lookup.assert_not_called()
        self.assertEqual(executor.context['ticketId'], 5091)
        self.assertEqual(executor.context['ticketNo'], '2026092459709012')

    def test_create_and_assign_reassigns_the_new_ticket_with_admin_transfer(self):
        plan = DataGenerationPlan.objects.create(
            project=self.project,
            name='assign-created-ticket-only',
            template_key='biz_create_and_assign',
            default_environment=self.environment,
            steps=[],
            created_by=self.user,
        )
        executor = PlanExecutor(
            plan,
            triggered_by=self.user,
            input_params={'assigneeUserId': 46, 'assigneeName': '李亮'},
        )
        executor.context.update(ticketId=5091, ticketNo='2026092459709012')
        step = {
            'type': 'api_call',
            'name': '分配工单',
            'variables': {
                'assigneeUserId': '{{assigneeUserId}}',
                'assigneeName': '{{assigneeName}}',
            },
        }
        original_row = {
            'id': 5091,
            'ticketNo': '2026092459709012',
            'status': 'pending_process',
            'assigneeUserId': 31,
            'assigneeName': '林业丰',
        }
        transferred_row = {
            **original_row,
            'assigneeUserId': 46,
            'assigneeName': '李亮',
        }
        transfer_interface = MagicMock()

        with patch.object(
                executor,
                '_find_ticket_row_for_context',
                side_effect=[original_row, transferred_row],
        ) as find_ticket, patch(
            'data_generation.services.ApiInterface.objects.filter'
        ) as filter_interfaces:
            with patch(
                'api_environments.token_refresh.refresh_environment_tokens',
                return_value={'adminToken': 'admin-token'},
            ) as refresh_tokens, patch.object(
                executor, '_run_named_ticket_mutation'
            ) as transfer:
                filter_interfaces.return_value.first.return_value = transfer_interface
                result = executor._reassign_if_wrong_assignee(step)

        self.assertEqual(result['reason'], 'created_ticket_transferred_to_assignee')
        self.assertEqual(result['ticketNo'], '2026092459709012')
        transfer.assert_called_once()
        self.assertEqual(transfer.call_args.kwargs['ticket_id'], 5091)
        self.assertEqual(
            transfer.call_args.kwargs['extra_variables']['targetUserId'],
            46,
        )

    def test_resolve_plan_reassigns_new_ticket_without_holder_login(self):
        plan = DataGenerationPlan.objects.create(
            project=self.project,
            name='resolve-current-ticket',
            template_key='biz_create_assign_resolve',
            default_environment=self.environment,
            steps=[],
            created_by=self.user,
        )
        with patch('data_generation.services.get_template_by_key', return_value=None):
            executor = PlanExecutor(
                plan,
                triggered_by=self.user,
                input_params={'assigneeUserId': 46, 'assigneeName': '李亮'},
            )
        executor.context.update(ticketId=5091, ticketNo='2026092459709012')
        row = {
            'id': 5091, 'ticketNo': '2026092459709012',
            'status': 'pending_process', 'assigneeUserId': 31,
        }
        transferred = {**row, 'assigneeUserId': 46}
        with patch.object(executor, '_find_ticket_row_for_context', side_effect=[row, transferred]), \
                patch('data_generation.services.ApiInterface.objects.filter') as interfaces, \
                patch('api_environments.token_refresh.refresh_environment_tokens', return_value={'adminToken': 'token'}), \
                patch.object(executor, '_run_named_ticket_mutation') as transfer, \
                patch.object(executor, '_login_any_username') as login:
            interfaces.return_value.first.return_value = MagicMock()
            result = executor._reassign_if_wrong_assignee({
                'type': 'api_call', 'name': '分配工单',
                'variables': {'assigneeUserId': '{{assigneeUserId}}'},
            })
        self.assertEqual(result['ticketNo'], '2026092459709012')
        transfer.assert_called_once()
        login.assert_not_called()

    def test_custom_create_plan_reassigns_its_new_ticket_without_holder_login(self):
        plan = DataGenerationPlan.objects.create(
            project=self.project,
            name='custom-create-and-assign',
            template_key='',
            default_environment=self.environment,
            steps=[],
            created_by=self.user,
        )
        executor = PlanExecutor(
            plan,
            triggered_by=self.user,
            input_params={'assigneeUserId': 46, 'assigneeName': '李亮'},
        )
        executor.context.update(ticketId=5091, ticketNo='2026092459709012')
        executor._created_ticket_id = 5091
        executor._created_ticket_no = '2026092459709012'
        original_row = {
            'id': 5091,
            'ticketNo': '2026092459709012',
            'status': 'pending_process',
            'assigneeUserId': 31,
            'assigneeName': '张岩',
        }
        transferred_row = {**original_row, 'assigneeUserId': 46, 'assigneeName': '李亮'}

        with patch.object(
            executor, '_find_ticket_row_for_context', side_effect=[original_row, transferred_row],
        ), patch(
            'data_generation.services.ApiInterface.objects.filter',
        ) as filter_interfaces, patch(
            'api_environments.token_refresh.refresh_environment_tokens',
            return_value={'adminToken': 'admin-token'},
        ), patch.object(executor, '_run_named_ticket_mutation') as transfer, patch.object(
            executor, '_login_any_username',
        ) as holder_login:
            filter_interfaces.return_value.first.return_value = MagicMock()
            result = executor._reassign_if_wrong_assignee({
                'type': 'api_call',
                'name': '分配工单',
                'variables': {'assigneeUserId': '{{assigneeUserId}}'},
            })

        self.assertEqual(result['reason'], 'created_ticket_transferred_to_assignee')
        self.assertEqual(result['ticketNo'], '2026092459709012')
        transfer.assert_called_once()
        self.assertEqual(
            transfer.call_args.kwargs['extra_variables']['assigneeToken'], 'admin-token',
        )
        holder_login.assert_not_called()

    def test_custom_plan_cannot_use_admin_transfer_for_untracked_ticket(self):
        plan = DataGenerationPlan.objects.create(
            project=self.project,
            name='custom-existing-ticket',
            template_key='',
            default_environment=self.environment,
            steps=[],
            created_by=self.user,
        )
        executor = PlanExecutor(plan, triggered_by=self.user)
        executor.context.update(ticketId=5091, ticketNo='2026092459709012')
        row = {
            'id': 5091,
            'ticketNo': '2026092459709012',
            'status': 'pending_process',
            'assigneeUserId': 31,
            'assigneeName': '张岩',
        }
        with patch.object(executor, '_find_ticket_row_for_context', return_value=row), patch(
            'data_generation.services.ApiInterface.objects.filter',
        ) as filter_interfaces, patch.object(
            executor, '_adopt_existing_ticket_for_desired_assignee', return_value=None,
        ), patch.object(
            executor, '_lookup_user_login_candidates', return_value=['802576'],
        ), patch.object(
            executor, '_login_any_username',
            side_effect=DataGenerationError('登录未配置'),
        ) as holder_login, patch(
            'api_environments.token_refresh.refresh_environment_tokens',
            return_value={'adminToken': 'admin-token'},
        ), patch.object(
            executor, '_run_named_ticket_mutation',
            side_effect=DataGenerationError('取消分配接口 HTTP 500'),
        ) as unassign:
            filter_interfaces.return_value.first.return_value = MagicMock()
            with self.assertRaisesMessage(DataGenerationError, '取消分配错误'):
                executor._reassign_if_wrong_assignee({
                    'type': 'api_call',
                    'name': '分配工单',
                    'variables': {'assigneeUserId': '{{assigneeUserId}}'},
                })
        unassign.assert_called_once()
        holder_login.assert_called_once()

    def test_login_rate_limit_stops_candidate_loop(self):
        plan = DataGenerationPlan.objects.create(
            project=self.project, name='login-check', steps=[], created_by=self.user,
        )
        executor = PlanExecutor(plan, triggered_by=self.user)
        with patch.object(executor, '_resolve_login_password_for_username', return_value=['bad1', 'bad2']), \
                patch.object(executor, '_login_user_token', side_effect=DataGenerationError('RATE_LIMIT_EXCEEDED')) as login:
            with self.assertRaisesMessage(DataGenerationError, '已停止继续尝试'):
                executor._login_any_username(
                    usernames=['user-a', 'user-b'],
                    environment=self.environment,
                    fallback_password='unused',
                )
        login.assert_called_once()

    def test_case_fixture_verifies_actual_status_and_assignee(self):
        plan = DataGenerationPlan.objects.create(
            project=self.project, name='case-state-check',
            template_key='biz_create_and_assign',
            default_environment=self.environment, steps=[], created_by=self.user,
        )
        with patch('data_generation.services.get_template_by_key', return_value=None):
            executor = PlanExecutor(
                plan, triggered_by=self.user,
                trigger_type=DataGenerationRun.TRIGGER_CASE_PRE,
                input_params={'assigneeUserId': 46},
            )
        executor.context.update(ticketId=100, ticketNo='T100')
        with patch.object(executor, '_find_ticket_row_for_context', return_value={
            'id': 100, 'ticketNo': 'T100', 'status': 'pending_process',
            'assigneeUserId': 46,
        }):
            executor._verify_case_fixture_state()
        with patch.object(executor, '_find_ticket_row_for_context', return_value={
            'id': 100, 'ticketNo': 'T100', 'status': 'resolved',
            'assigneeUserId': 46,
        }), patch('data_generation.services.time.sleep'):
            with self.assertRaisesMessage(DataGenerationError, '实际状态'):
                executor._verify_case_fixture_state()

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
    def test_fixture_statuses_read_precondition_not_expected_outcome(self):
        from data_generation.testcase_pre_data import required_fixture_statuses

        self.assertEqual(
            required_fixture_statuses('存在一条状态为「待处理」的测试工单'),
            ('待处理',),
        )
        self.assertEqual(
            required_fixture_statuses(
                '存在一条状态为「已关闭」的工单\n存在一条状态为「已解决」的工单'
            ),
            ('已关闭', '已解决'),
        )

    def test_claim_button_expectation_requires_detail_page_evidence(self):
        from types import SimpleNamespace
        from data_generation.testcase_pre_data import build_row_action_evidence_hints

        hint = build_row_action_evidence_hints([
            SimpleNamespace(
                step_number=4,
                expected_result='成功进入该工单的处理详情页，右上角可见蓝色“领取工单”按钮',
            ),
        ])
        self.assertIn("assertPageShows(page, ['领取工单'])", hint)
        self.assertIn('screenshotCaseStep(page, 4)', hint)
        self.assertIn('列表页截图不能作为该步骤证据', hint)

    def test_processing_status_uses_claim_template(self):
        from data_generation.intent_router import infer_business_template_key

        self.assertEqual(
            infer_business_template_key('筛选工单状态为处理中'),
            'biz_prepare_status_filter_data',
        )
        self.assertEqual(
            infer_business_template_key('筛选工单状态为待处理'),
            'biz_prepare_status_filter_data',
        )
        self.assertEqual(
            infer_business_template_key('点击查询，等待列表刷新完成\n工单状态筛选为待处理'),
            'biz_prepare_status_filter_data',
        )
        self.assertEqual(
            infer_business_template_key('完成工单并闭环'),
            'biz_create_assign_resolve',
        )

    def test_creation_time_filter_does_not_use_create_and_assign(self):
        from data_generation.intent_router import infer_business_template_key

        self.assertIsNone(
            infer_business_template_key('创建时间自定义-返回指定区间内工单'),
        )
        self.assertEqual(
            infer_business_template_key('点击创建工单按钮进入表单'),
            'biz_create_and_assign',
        )

    def test_status_filter_template_prepares_matching_and_contrast_records(self):
        from data_generation.templates import get_template_by_key

        template = get_template_by_key('biz_prepare_status_filter_data')
        self.assertIsNotNone(template)
        names = [step['name'] for step in template['steps']]
        self.assertIn('创建状态筛选样本：待分配', names)
        self.assertIn('创建状态筛选样本：待处理', names)
        self.assertIn('创建状态筛选样本：处理中', names)
        self.assertIn('创建状态筛选样本：已完成', names)

        created = [step for step in template['steps'] if step['name'].startswith('创建状态筛选样本')]
        self.assertEqual(len(created), 4)
        self.assertTrue(all(step['variables']['ticketType'] == 'TYPE_A' for step in created))
        for step in created:
            summary = step['variables'].get('summary') or ''
            # 模板字面量或渲染后都不应超过 20（无 timestamp）。
            self.assertLessEqual(len(str(summary).replace('{{timestamp}}', '123456')), 20)
            self.assertNotIn('timestamp', str(summary))

    def test_closed_template_closes_from_processing_without_resolve_step(self):
        from data_generation.templates import get_template_by_key

        template = get_template_by_key('biz_create_assign_close')
        self.assertIsNotNone(template)
        steps = template['steps']
        self.assertIn('关闭工单', [step.get('name') for step in steps])
        self.assertNotIn('完成工单', [step.get('name') for step in steps])
        self.assertEqual(
            next(step for step in steps if step.get('name') == '关闭工单')['interface_ref'],
            'close_ticket',
        )

    def test_unassigned_pending_case_does_not_route_to_assignment_or_resolve(self):
        from data_generation.intent_router import build_input_params, infer_business_template_key

        case_text = (
            '待处理工单-列表点击处理进入详情页-正常流程\n'
            '当前状态为待处理且处理人未分配给李亮/可领取\n'
            '筛选待处理后，不得出现处理中、已完成或已关闭的工单\n'
            '点击处理后可领取工单'
        )
        self.assertEqual(
            infer_business_template_key(case_text),
            'biz_create_type_a',
        )
        params = build_input_params(case_text, {'input_params': {}, 'steps': []})
        self.assertEqual(params['ticketType'], 'TYPE_A')
        self.assertEqual(params['summary'], '可领取待处理测试工单')
        self.assertLessEqual(len(params['summary']), 20)

    def test_assignment_templates_ignore_legacy_type_c_override(self):
        from data_generation.testcase_pre_data import _normalize_assignment_workflow_ticket_type

        self.assertEqual(
            _normalize_assignment_workflow_ticket_type(
                'biz_create_and_assign',
                {'ticketType': 'TYPE_C'},
            )['ticketType'],
            'TYPE_A',
        )

    def test_automatic_assignment_uses_create_only_type_a_data(self):
        from data_generation.intent_router import build_input_params, infer_business_template_key

        text = '创建工单后按系统自动分配规则分配给对应处理人，验证分配结果正确'
        self.assertEqual(infer_business_template_key(text), 'biz_create_type_a')
        self.assertEqual(
            build_input_params(text, {'input_params': {}, 'steps': []})['ticketType'],
            'TYPE_A',
        )

    def test_ticket_row_already_assigned_detects_pending_process(self):
        from data_generation.services import (
            _assignee_matches_desired,
            _ticket_row_is_already_assigned,
        )

        self.assertTrue(
            _ticket_row_is_already_assigned(
                {'status': 'pending_process', 'assigneeUserId': 39},
            )
        )
        self.assertFalse(
            _ticket_row_is_already_assigned(
                {'status': 'pending_assign', 'assigneeUserId': None},
            )
        )
        self.assertTrue(
            _assignee_matches_desired(
                {'assigneeUserId': 46, 'assigneeName': '李亮'},
                desired_user_id=46,
                desired_name='李亮',
            )
        )
        self.assertFalse(
            _assignee_matches_desired(
                {'assigneeUserId': 3, 'assigneeName': '张岩'},
                desired_user_id=46,
                desired_name='李亮',
            )
        )

    def test_claimable_pool_requires_auto_assigned_pending_ticket(self):
        from data_generation.testcase_pre_data import (
            _normalize_assignment_workflow_ticket_type,
            _ticket_row_supports_claim,
        )

        self.assertTrue(_ticket_row_supports_claim({
            'status': 'pending_process',
            'assigneeUserId': 39,
            'availableActions': ['claim'],
        }))
        self.assertFalse(_ticket_row_supports_claim({
            'status': 'pending_process',
            'assigneeUserId': None,
            'availableActions': ['claim'],
        }))
        self.assertEqual(
            _normalize_assignment_workflow_ticket_type(
                'biz_create_and_transfer',
                {'ticketType': 'TYPE_C'},
            )['ticketType'],
            'TYPE_A',
        )
        self.assertEqual(
            _normalize_assignment_workflow_ticket_type(
                'biz_create_and_claim',
                {'ticketType': 'TYPE_A'},
            )['ticketType'],
            'TYPE_C',
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

    def test_granted_access_permission_is_not_ticket_assign(self):
        from data_generation.intent_router import infer_business_template_key

        text = (
            '普通用户-工单总览页面访问-权限验证\n'
            '普通用户账号已分配访问权限\n'
            '尝试访问工单总览页面'
        )
        self.assertIsNone(infer_business_template_key(text))

    def test_transfer_button_visibility_uses_assign_not_transfer_api(self):
        from data_generation.intent_router import (
            build_input_params,
            extract_transfer_target_name,
            infer_business_template_key,
        )

        text = (
            '待处理工单详情页-转派按钮可见性验证-正常流程\n'
            '使用李亮账号(802714/000000)登录系统\n'
            '存在一条当前处理人为李亮，状态为「待处理」的测试工单\n'
            '可见橙色「转派工单」按钮'
        )
        self.assertEqual(infer_business_template_key(text), 'biz_create_type_a')
        params = build_input_params(text, {'input_params': {}, 'steps': []})
        self.assertNotIn('assigneeName', params)
        self.assertEqual(params.get('ticketType'), 'TYPE_A')

    def test_transfer_dialog_success_flow_does_not_pre_transfer(self):
        from data_generation.intent_router import (
            build_input_params,
            extract_transfer_target_name,
            infer_business_template_key,
        )

        text = (
            '转派弹窗-选择对象与填写原因-二次确认-转派成功-正常流程\n'
            '使用李亮账号登录\n'
            '存在一条当前处理人为李亮，状态为「待处理」的测试工单\n'
            '系统中存在用户黎沅欣(802434)可被选择为转派目标\n'
            '确认将工单 {工单号} 转派给 {姓名}？'
        )
        self.assertEqual(infer_business_template_key(text), 'biz_create_type_a')
        self.assertEqual(extract_transfer_target_name(text), '黎沅欣')
        params = build_input_params(text, {'input_params': {}, 'steps': []})
        self.assertNotIn('assigneeName', params)
        self.assertNotEqual(params.get('assigneeName'), '{姓名}？')

    def test_completed_transfer_fixture_keeps_distinct_parties(self):
        from data_generation.intent_router import (
            build_input_params,
            infer_business_template_key,
        )

        text = (
            '转派成功-原处理人我的工单列表-工单不可见-数据隔离\n'
            '使用李亮账号登录\n'
            '已按正常流程发起转派给黎沅欣，且转派成功'
        )
        self.assertEqual(infer_business_template_key(text), 'biz_create_and_transfer')
        params = build_input_params(text, {'input_params': {}, 'steps': []})
        self.assertEqual(params.get('sourceAssigneeName'), '李亮')
        self.assertEqual(params.get('assigneeName'), '黎沅欣')

        text_after = (
            '被转派人-列表可见\n'
            '已有一条李亮处理的待处理工单已成功转派给黎沅欣\n'
            '使用黎沅欣账号登录'
        )
        self.assertEqual(infer_business_template_key(text_after), 'biz_create_and_transfer')
        params_after = build_input_params(text_after, {'input_params': {}, 'steps': []})
        self.assertEqual(params_after.get('sourceAssigneeName'), '李亮')
        self.assertEqual(params_after.get('assigneeName'), '黎沅欣')

    def test_transfer_template_freezes_destination_before_assign_context_refresh(self):
        from data_generation.templates import BUILTIN_BUSINESS_TEMPLATES

        template = next(
            item for item in BUILTIN_BUSINESS_TEMPLATES
            if item.get('template_key') == 'biz_create_and_transfer'
        )
        transfer_step = next(
            item for item in template['steps']
            if item.get('name') == '转派工单'
        )
        self.assertEqual(
            transfer_step['variables']['targetUserId'],
            '{{transferTargetUserId}}',
        )
        self.assertEqual(
            transfer_step['variables']['targetRole'],
            '{{transferTargetRole}}',
        )

    def test_create_and_transfer_description_still_uses_transfer_template(self):
        from data_generation.intent_router import (
            build_input_params,
            infer_business_template_key,
        )

        text = '创建 TYPE_C 工单并转派给李亮'
        self.assertEqual(infer_business_template_key(text), 'biz_create_and_transfer')
        params = build_input_params(text, {'input_params': {}, 'steps': []})
        self.assertEqual(params.get('assigneeName'), '李亮')
        self.assertNotEqual(params.get('sourceAssigneeName'), params.get('assigneeName'))


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

    def test_stale_close_plan_is_rebound_to_single_ticket_endpoint(self):
        from api_interfaces.models import ApiInterface
        from data_generation.testcase_pre_data import ensure_project_template_plan

        batch = ApiInterface.objects.create(
            project=self.project, name='批量关闭工单', method='POST',
            url='/api/tickets/batch-close',
        )
        single = ApiInterface.objects.create(
            project=self.project, name='关闭工单', method='POST',
            url='/api/tickets/${ticketId}/close',
        )
        plan = DataGenerationPlan.objects.create(
            project=self.project, name='关闭样本', template_key='biz_create_assign_close',
            is_template=True, is_active=True, created_by=self.user,
            template_bindings={'interfaces': {'close_ticket': batch.id}},
            steps=[{'type': 'api_call', 'name': '关闭工单', 'interface_id': batch.id}],
        )

        ensured = ensure_project_template_plan(
            project_id=self.project.id, template_key='biz_create_assign_close',
        )
        self.assertEqual(ensured.id, plan.id)
        self.assertEqual(ensured.steps[0]['interface_id'], single.id)
        plan.refresh_from_db()
        self.assertEqual(plan.steps[0]['interface_id'], single.id)

    def test_claim_creator_is_not_rejected_for_unrelated_processing_ticket(self):
        from data_generation.testcase_pre_data import _inject_claim_creator_token

        with patch('data_generation.testcase_pre_data._list_processing_creator_names') as creators:
            with patch.object(ApiEnvironment, 'get_all_variables', return_value={
                'claimAssigneeToken_46': 'cached-token',
            }), patch('api_environments.token_refresh._token_still_valid', return_value=True), patch(
                'jwt.decode', return_value={'sub': 46},
            ):
                params = _inject_claim_creator_token(
                    {'assigneeUserId': 46, 'assigneeName': '李亮'},
                    project_id=self.project.id,
                    environment_id=self.environment.id,
                )
        self.assertEqual(params['claimCreatorTokenVar'], 'claimAssigneeToken_46')
        creators.assert_not_called()

    def test_explicit_fixture_creator_uses_token_variable_without_persisting_token(self):
        from data_generation.testcase_pre_data import _inject_case_creator_token

        with patch(
            'data_generation.testcase_pre_data._inject_claim_creator_token',
            return_value={'claimCreatorTokenVar': 'claimAssigneeToken_46'},
        ) as inject:
            params = _inject_case_creator_token(
                {'creatorUserId': 46, 'creatorName': '李亮', 'assigneeUserId': 39},
                project_id=self.project.id,
                environment_id=self.environment.id,
            )

        self.assertEqual(params['creatorTokenVar'], 'claimAssigneeToken_46')
        self.assertEqual(params['creatorUserId'], 46)
        self.assertNotIn('accessToken', params)
        inject.assert_called_once_with(
            {'assigneeUserId': 46, 'assigneeName': '李亮'},
            project_id=self.project.id,
            environment_id=self.environment.id,
        )

    def test_completion_step_metadata_follows_case_steps_without_login_secret(self):
        import json
        from orchestrator_integration.builtin_tools.skill_tools import (
            _set_complete_ticket_step_metadata,
        )

        testcase = ManualTestCase.objects.create(
            project=self.project, module=self.module,
            name='确认完成弹窗', creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=testcase, step_number=1,
            description='用密码 secret-should-not-leak 登录', creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=testcase, step_number=4,
            description='点击弹窗蓝色「确定」按钮',
            expected_result='状态变为已解决', creator=self.user,
        )
        env = {}
        _set_complete_ticket_step_metadata(env, testcase)
        payload = json.loads(env['WHARTTEST_COMPLETE_STEPS_JSON'])
        self.assertNotIn('1', payload)
        self.assertEqual(payload['4']['description'], '点击弹窗蓝色「确定」按钮')
        self.assertNotIn('secret-should-not-leak', env['WHARTTEST_COMPLETE_STEPS_JSON'])

    def test_whole_state_machine_is_not_forced_into_completion_dialog_helper(self):
        from data_generation.testcase_pre_data import is_complete_ticket_ui_case

        testcase = ManualTestCase.objects.create(
            project=self.project, module=self.module,
            name='工单状态机流转验证', creator=self.user,
        )
        for number, description in enumerate(
            ('分配工单', '领取工单', '点击工单完成', '移交审批', '关闭工单'), 1,
        ):
            TestCaseStep.objects.create(
                test_case=testcase, step_number=number,
                description=description, creator=self.user,
            )
        self.assertFalse(is_complete_ticket_ui_case(testcase))

    def test_processing_handover_reuses_only_actionable_ticket(self):
        from data_generation.testcase_pre_data import _reusable_processing_handover_row

        row = {
            'ticketNo': 'T-1435',
            'status': 'processing',
            'approvalStatus': 'none',
            'assigneeUserId': 46,
            'assigneeName': '李亮',
            'availableActions': ['submit_approval'],
        }
        check = lambda item: _reusable_processing_handover_row(
            item, ticket_no='T-1435', assignee_id=46, assignee_name='李亮',
        )
        self.assertTrue(check(row))
        self.assertFalse(check({**row, 'status': 'pending_process'}))
        self.assertFalse(check({**row, 'approvalStatus': 'pending'}))
        self.assertFalse(check({**row, 'assigneeUserId': 39}))
        self.assertFalse(check({**row, 'availableActions': ['resolve']}))

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

    def test_unmatched_read_only_case_skips_auto_data_and_continues(self):
        from data_generation.testcase_pre_data import (
            resolve_pre_data_for_testcase,
            run_testcase_pre_data,
        )

        testcase = ManualTestCase.objects.create(
            project=self.project,
            module=self.module,
            name='工单列表-审批状态标签展示',
            precondition='工单列表中存在可查看审批状态的工单数据',
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=testcase,
            step_number=1,
            description='打开工单列表并查看审批状态标签',
            expected_result='审批状态标签显示正确',
            creator=self.user,
        )

        resolution = resolve_pre_data_for_testcase(testcase)
        self.assertEqual(resolution.source, 'unmatched_ticket_fixture')
        result = run_testcase_pre_data(testcase)

        self.assertFalse(result.blocked)
        self.assertIsNone(result.run)
        self.assertIn('继续使用目标环境现有数据执行', result.message_suffix)

    def test_precondition_status_wins_over_expected_result(self):
        from data_generation.testcase_pre_data import resolve_pre_data_for_testcase

        testcase = ManualTestCase.objects.create(
            project=self.project, module=self.module,
            name='确认完成后工单状态变为已解决',
            precondition='存在一条状态为「处理中」的测试工单',
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=testcase, step_number=1,
            description='点击工单完成', expected_result='状态变为已解决',
            creator=self.user,
        )
        resolution = resolve_pre_data_for_testcase(testcase)
        self.assertEqual(resolution.template_key, 'biz_create_and_claim')

    def test_resolved_precondition_routes_to_resolve_plan(self):
        from data_generation.testcase_pre_data import resolve_pre_data_for_testcase

        testcase = ManualTestCase.objects.create(
            project=self.project, module=self.module,
            name='已解决工单不显示关闭按钮',
            precondition='存在一条状态为「已解决」的工单',
            creator=self.user,
        )
        resolution = resolve_pre_data_for_testcase(testcase)
        self.assertEqual(resolution.template_key, 'biz_create_assign_resolve')

    def test_multi_state_precondition_is_not_misreported_as_one_ticket(self):
        from data_generation.testcase_pre_data import resolve_pre_data_for_testcase

        testcase = ManualTestCase.objects.create(
            project=self.project, module=self.module,
            name='比较已关闭和已解决工单',
            precondition='存在状态为「已关闭」的工单；存在状态为「已解决」的工单',
            creator=self.user,
        )
        resolution = resolve_pre_data_for_testcase(testcase)
        self.assertEqual(resolution.source, 'terminal_pair')
        self.assertEqual(resolution.template_key, 'biz_prepare_terminal_pair')
        self.assertTrue(resolution.fail_fast)

    def test_terminal_pair_composes_two_distinct_ticket_runs(self):
        from data_generation.testcase_pre_data import (
            _run_terminal_pair_pre_data, resolve_pre_data_for_testcase,
        )

        testcase = ManualTestCase.objects.create(
            project=self.project, module=self.module,
            name='已关闭和已解决详情按钮验证',
            precondition='存在状态为「已关闭」的工单；存在状态为「已解决」的工单',
            creator=self.user,
        )
        resolution = resolve_pre_data_for_testcase(testcase)
        resolved_plan = DataGenerationPlan.objects.create(
            project=self.project, name='resolved', template_key='biz_create_assign_resolve',
            default_environment=self.environment, steps=[], created_by=self.user,
        )
        closed_plan = DataGenerationPlan.objects.create(
            project=self.project, name='closed', template_key='biz_create_assign_close',
            default_environment=self.environment, steps=[], created_by=self.user,
        )
        closed_run = MagicMock(status=DataGenerationRun.STATUS_SUCCESS, output_snapshot={
            'ticketId': 1, 'ticketNo': 'C-1', 'ticketStatus': 'closed',
        })
        resolved_run = MagicMock(status=DataGenerationRun.STATUS_SUCCESS, output_snapshot={
            'ticketId': 2, 'ticketNo': 'R-2', 'ticketStatus': 'resolved',
        })
        with patch('data_generation.testcase_pre_data.ensure_project_template_plan', side_effect=[resolved_plan, closed_plan]), \
                patch('data_generation.testcase_pre_data.execute_plan', side_effect=[closed_run, resolved_run]) as execute:
            result = _run_terminal_pair_pre_data(testcase, resolution)
        self.assertFalse(result.blocked)
        self.assertIn('已关闭工单号: C-1', result.message_suffix)
        self.assertIn('已解决工单号: R-2', result.message_suffix)
        self.assertEqual(resolved_run.output_snapshot['closedTicketNo'], 'C-1')
        self.assertEqual(resolved_run.output_snapshot['resolvedTicketNo'], 'R-2')
        self.assertEqual(execute.call_count, 2)

    def test_navigation_target_status_routes_without_title_shortcut(self):
        from data_generation.testcase_pre_data import resolve_pre_data_for_testcase

        processing = ManualTestCase.objects.create(
            project=self.project, module=self.module,
            name='处理中工单-不再展示领取按钮',
            precondition='执行前创建隔离工单并注入工单号', creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=processing, step_number=2,
            description='找到该处理中工单，点击处理进入详情页',
            expected_result='不展示领取按钮', creator=self.user,
        )
        self.assertEqual(
            resolve_pre_data_for_testcase(processing).template_key,
            'biz_create_and_claim',
        )

        pending = ManualTestCase.objects.create(
            project=self.project, module=self.module,
            name='处理中-底部发送区完整展示',
            precondition='执行前创建隔离工单并注入工单号', creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=pending, step_number=2,
            description='选择工单状态：待处理，查询本次工单',
            expected_result='进入详情页', creator=self.user,
        )
        self.assertEqual(
            resolve_pre_data_for_testcase(pending).template_key,
            'biz_create_and_assign',
        )

    def test_transfer_ui_helper_navigation_hint(self):
        from data_generation.testcase_pre_data import (
            build_testcase_navigation_hint,
            is_transfer_ui_helper_case,
            transfer_ui_helper_flow,
        )

        visibility = ManualTestCase.objects.create(
            project=self.project,
            module=self.module,
            name='待处理工单详情页-转派按钮可见性验证-正常流程',
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=visibility,
            step_number=4,
            description='查看页面右上角按钮区域',
            expected_result='可见橙色「转派工单」按钮',
            creator=self.user,
        )
        self.assertTrue(is_transfer_ui_helper_case(visibility))
        self.assertEqual(transfer_ui_helper_flow(visibility), 'visibility')
        hint_vis = build_testcase_navigation_hint(visibility)
        self.assertIn('runTransferTicketCaseStep', hint_vis)

        dialog = ManualTestCase.objects.create(
            project=self.project,
            module=self.module,
            name='转派弹窗-选择对象与填写原因-二次确认-转派成功-正常流程',
            creator=self.user,
        )
        for n in range(1, 10):
            TestCaseStep.objects.create(
                test_case=dialog,
                step_number=n,
                description=f'步骤{n}',
                expected_result='ok',
                creator=self.user,
            )
        self.assertTrue(is_transfer_ui_helper_case(dialog))
        self.assertEqual(transfer_ui_helper_flow(dialog), 'dialog')
        hint_dlg = build_testcase_navigation_hint(dialog)
        self.assertIn('runTransferTicketCaseStep(page, 9)', hint_dlg)
        self.assertNotIn('await helpers.runClaimableTicketCaseStep(', hint_dlg)

    def test_handover_lifecycle_routes_by_required_ticket_state_and_steps(self):
        from data_generation.testcase_pre_data import (
            build_testcase_navigation_hint,
            is_processing_handover_case,
            is_processing_handover_helper_case,
            is_processing_handover_submission_case,
            is_processing_handover_visibility_case,
            processing_handover_flow_kind,
            resolve_pre_data_for_testcase,
        )

        scenarios = (
            ('待处理工单详情页-不展示移交审批按钮-状态拦截验证', 'pending_no_handover', 3,
             {2: ('进入待处理工单详情页', '成功进入工单详情页'),
              3: ('查看页面右上角按钮区域', '不展示「移交审批」按钮')}),
            ('处理中工单详情页-移交审批按钮可见性验证', 'visibility', 4,
             {4: ('查看右上角按钮', '移交审批按钮可见')}),
            ('选择审批人弹窗-确认移交', 'submit_split', 7,
             {4: ('选择审批人', '显示人员'), 7: ('点击确认移交', '移交成功')}),
            ('移交成功-移交审批按钮消失展示审批详情', 'submit_compact', 4,
             {3: ('选择审批人黎沅欣，点击确认移交', '移交成功'),
              4: ('查看按钮区域', '显示审批详情')}),
            ('移交成功-点击审批详情进入审批流程页', 'approval_detail', 3,
             {3: ('点击审批详情', '进入审批流程页')}),
            ('移交成功-审批人我的工单列表可见', 'approver_list', 3,
             {3: ('按工单号搜索被移交审批的测试工单', '审批状态展示待审批')}),
            ('移交成功-工单摘要字段验证', 'approval_summary', 4,
             {3: ('点击审批详情', '进入审批流程页'),
              4: ('查看工单摘要', '待审批，审批人黎沅欣')}),
            ('已提交审批工单-仅展示审批详情', 'approval_button', 3,
             {3: ('查看右上角按钮', '展示审批详情，不展示移交审批')}),
            ('选择审批人弹窗-未选择拦截', 'selection_required', 5,
             {4: ('不选择任何审批人', '确认移交禁用'),
              5: ('点击确认移交', '拦截')}),
            ('选择审批人弹窗-点击取消', 'cancel', 5,
             {4: ('点击取消', '弹窗关闭'),
              5: ('查看按钮', '仍可见移交审批')}),
            ('移交审批原因-输入200字', 'submit_reason_boundary', 4,
             {3: ('点击移交审批，输入正好200个字符', '输入正常'),
              4: ('点击确认移交', '展示审批详情')}),
        )
        submitted_flows = {'approval_detail', 'approver_list', 'approval_summary', 'approval_button'}
        submission_flows = {'submit_split', 'submit_compact', 'submit_reason_boundary'}
        for name, expected_flow, step_count, special_steps in scenarios:
            with self.subTest(name=name):
                testcase = ManualTestCase.objects.create(
                    project=self.project,
                    module=self.module,
                    name=name,
                    precondition='工单中心存在可供操作的目标测试工单',
                    creator=self.user,
                )
                for step_number in range(1, step_count + 1):
                    description, expected_result = special_steps.get(
                        step_number, (f'步骤{step_number}', '按用例预期验证'),
                    )
                    TestCaseStep.objects.create(
                        test_case=testcase,
                        step_number=step_number,
                        description=description,
                        expected_result=expected_result,
                        creator=self.user,
                    )
                self.assertEqual(processing_handover_flow_kind(testcase), expected_flow)
                self.assertTrue(is_processing_handover_case(testcase))
                self.assertEqual(
                    is_processing_handover_visibility_case(testcase),
                    expected_flow == 'visibility',
                )
                self.assertEqual(
                    is_processing_handover_submission_case(testcase),
                    expected_flow in submission_flows,
                )
                self.assertTrue(is_processing_handover_helper_case(testcase))
                hint = build_testcase_navigation_hint(testcase)
                self.assertIn(f'runProcessingHandoverCaseStep(page, {step_count})', hint)
                if expected_flow in submitted_flows:
                    self.assertEqual(
                        resolve_pre_data_for_testcase(testcase).template_key,
                        'biz_create_and_claim',
                    )

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

    def test_legacy_claimable_plan_migrates_to_type_a_create(self):
        from data_generation.models import DataGenerationPlan
        from data_generation.testcase_pre_data import resolve_pre_data_for_testcase

        legacy_plan = DataGenerationPlan.objects.create(
            project=self.project,
            name='旧待领取造数',
            template_key='biz_create_claimable_pending',
            steps=[
                {'type': 'api_call', 'name': '创建工单', 'interface_id': 1},
                {'type': 'api_call', 'name': '分配工单进入待处理', 'interface_id': 2},
            ],
            created_by=self.user,
        )
        testcase = ManualTestCase.objects.create(
            project=self.project,
            module=self.module,
            name='待处理工单-列表点击处理进入详情页-正常流程',
            precondition='存在待处理且处理人未分配的工单',
            pre_data_plan=legacy_plan,
            creator=self.user,
        )
        resolution = resolve_pre_data_for_testcase(testcase)
        self.assertEqual(resolution.template_key, 'biz_create_type_a')
        self.assertEqual(resolution.source, 'claimable-auto')

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

    def test_overview_dashboard_case_skips_inferred_pre_data(self):
        from data_generation.testcase_pre_data import resolve_pre_data_for_testcase

        overview_module = TestCaseModule.objects.create(
            project=self.project,
            name='工单总览',
            creator=self.user,
        )
        testcase = ManualTestCase.objects.create(
            project=self.project,
            module=overview_module,
            name='工单总览-默认近7天进入-KPI与图表加载完整',
            precondition=(
                '1. 使用李亮账号(17670400361/000000)登录系统'
                '(http://test.bot.by56.com/work-order/login)\n'
                '2. 系统近7天内存在工单新增、处理、关闭、超时等数据\n'
                '3. 存在不同SLA状态（红灯/黄灯/绿灯）的工单样本'
            ),
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=testcase,
            step_number=1,
            description='使用李亮账号登录系统',
            expected_result='登录成功进入首页',
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=testcase,
            step_number=2,
            description='点击左侧导航进入工单总览页',
            expected_result='默认展示近7天数据，KPI与图表加载完整',
            creator=self.user,
        )
        resolution = resolve_pre_data_for_testcase(testcase)
        self.assertEqual(resolution.source, 'none')
        self.assertIsNone(resolution.template_key)
        self.assertIn('工单总览', resolution.skip_reason)

    def test_overview_sla_detail_case_classification(self):
        from data_generation.testcase_pre_data import (
            is_overview_dashboard_case,
            is_overview_sla_detail_case,
            resolve_pre_data_for_testcase,
        )

        overview_module = TestCaseModule.objects.create(
            project=self.project,
            name='工单总览',
            creator=self.user,
        )
        testcase = ManualTestCase.objects.create(
            project=self.project,
            module=overview_module,
            name='工单总览-SLA预警-点击工单ID进入工单详情',
            precondition=(
                '1. 使用李亮账号登录系统\n'
                '2. SLA预警明细表格中存在至少一行数据，工单ID列为蓝色可点击链接'
            ),
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=testcase,
            step_number=2,
            description='在左侧菜单点击进入【工单中心】>【工单总览】',
            expected_result='成功进入数据总览页面',
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=testcase,
            step_number=3,
            description='在SLA预警明细表格中，点击第一行的蓝色工单ID链接',
            expected_result='成功跳转到该工单的详情页面',
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=testcase,
            step_number=4,
            description='查看工单详情页面工单ID',
            expected_result='详情页面显示的工单ID与点击的链接工单ID一致',
            creator=self.user,
        )

        self.assertTrue(is_overview_sla_detail_case(testcase))
        self.assertFalse(is_overview_dashboard_case(testcase))
        resolution = resolve_pre_data_for_testcase(testcase)
        self.assertEqual(resolution.source, 'none')
        self.assertIsNone(resolution.template_key)

    def test_page_access_permission_case_skips_ticket_pre_data(self):
        from data_generation.testcase_pre_data import (
            extract_login_credentials,
            resolve_pre_data_for_testcase,
        )

        testcase = ManualTestCase.objects.create(
            project=self.project,
            module=self.module,
            name='普通用户-工单总览页面访问-权限验证',
            precondition=(
                '1. 使用普通用户账号(test/test123)登录系统'
                '(http://test.bot.by56.com/work-order/login)\n'
                '2. 普通用户账号已分配访问权限'
            ),
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=testcase,
            step_number=1,
            description='使用普通用户账号登录系统',
            expected_result='登录成功进入系统',
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=testcase,
            step_number=2,
            description='尝试访问工单总览页面',
            expected_result='正常进入页面，数据根据权限展示',
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=testcase,
            step_number=3,
            description='检查页面数据范围',
            expected_result='只展示该用户可见范围内的统计数据，数据隔离正确',
            creator=self.user,
        )
        resolution = resolve_pre_data_for_testcase(testcase)
        self.assertEqual(resolution.source, 'none')
        self.assertIsNone(resolution.template_key)

        creds = extract_login_credentials(testcase.precondition)
        self.assertEqual(creds['username'], 'test')
        self.assertEqual(creds['password'], 'test123')
        self.assertIn('/work-order/login', creds['login_url'])

    def test_create_page_display_case_skips_inferred_pre_data(self):
        from data_generation.testcase_pre_data import resolve_pre_data_for_testcase

        testcase = ManualTestCase.objects.create(
            project=self.project,
            module=self.module,
            name='页面入口展示-三大区块均展示',
            precondition=(
                '使用李亮账号(17670400361/000000)登录系统'
                '(http://test.bot.by56.com/work-order/login)'
            ),
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=testcase,
            step_number=1,
            description='使用李亮账号登录系统',
            expected_result='登录成功进入首页',
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=testcase,
            step_number=2,
            description='点击左侧导航【工单中心】->【工单列表】',
            expected_result='进入工单列表页',
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=testcase,
            step_number=3,
            description='点击右上角【+ 创建工单】按钮',
            expected_result='进入创建工单页面',
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=testcase,
            step_number=4,
            description='查看页面中“基本信息”区块',
            expected_result='展示工单类型、工单摘要字段',
            creator=self.user,
        )
        resolution = resolve_pre_data_for_testcase(testcase)
        self.assertEqual(resolution.source, 'none')
        self.assertIsNone(resolution.template_key)
        self.assertEqual(resolution.skip_reason, '未识别到工单类前置数据需求')

    def test_create_form_field_case_skips_inferred_pre_data(self):
        from data_generation.testcase_pre_data import resolve_pre_data_for_testcase

        testcase = ManualTestCase.objects.create(
            project=self.project,
            module=self.module,
            name='创建工单',
            precondition='使用卖家账号(17670400028/000000)登录系统',
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=testcase,
            step_number=3,
            description='点击右上角创建工单按钮',
            expected_result='跳转创建页，表单展示',
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=testcase,
            step_number=4,
            description='查看页面中“工单基本信息”区块',
            expected_result='包含工单类型、工单来源字段',
            creator=self.user,
        )
        resolution = resolve_pre_data_for_testcase(testcase)
        self.assertEqual(resolution.source, 'none')
        self.assertIsNone(resolution.template_key)

    def test_pending_filter_export_case_skips_inferred_pre_data(self):
        from data_generation.testcase_pre_data import resolve_pre_data_for_testcase

        testcase = ManualTestCase.objects.create(
            project=self.project,
            module=self.module,
            name='待处理状态筛选后-文件仅含待处理工单',
            precondition='系统中同时存在状态为「待处理」和其他状态的工单',
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=testcase,
            step_number=3,
            description='在筛选条件区域选择工单状态为待处理',
            expected_result='筛选框显示待处理',
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=testcase,
            step_number=4,
            description='点击查询按钮，等待列表刷新完成',
            expected_result='列表当前状态列仅显示待处理',
            creator=self.user,
        )
        TestCaseStep.objects.create(
            test_case=testcase,
            step_number=6,
            description='点击导出全部，核对导出文件数据行数',
            expected_result='文件可打开且仅含待处理工单',
            creator=self.user,
        )
        resolution = resolve_pre_data_for_testcase(testcase)
        self.assertEqual(resolution.source, 'none')
        self.assertIsNone(resolution.template_key)

    def test_message_suffix_skips_row_action_when_case_is_page_access(self):
        from data_generation.testcase_pre_data import (
            PreDataResolution,
            _build_message_suffix,
        )

        run = DataGenerationRun(
            plan=None,
            status=DataGenerationRun.STATUS_SUCCESS,
            output_snapshot={'ticketNo': '2026090753915004'},
            step_logs=[],
        )
        resolution = PreDataResolution(
            plan=None,
            template_key=None,
            input_params={},
            default_environment_id=None,
            source='inferred',
            fail_fast=False,
        )
        suffix = _build_message_suffix(
            run,
            resolution,
            case_text='普通用户-工单总览页面访问-权限验证\n尝试访问工单总览页面',
        )
        self.assertIn('2026090753915004', suffix)
        self.assertNotIn('fillFilterField', suffix)
        self.assertNotIn('clickRowAction', suffix)
        self.assertNotIn("'审批'", suffix)

    def test_execution_hints_require_stable_login_and_unique_navigation(self):
        from data_generation.testcase_pre_data import (
            build_testcase_step_script_hints,
        )

        testcase = ManualTestCase.objects.create(
            project=self.project,
            module=self.module,
            name='页面访问',
            creator=self.user,
        )
        hints = build_testcase_step_script_hints(testcase)
        self.assertIn('只有 stdout 出现 `RESULT=PASS` 才能判通过', hints)
        self.assertIn('禁止再调 screenshotCaseStep', hints)
        self.assertIn("navigateByMenu(page, '步骤里的菜单名')", hints)
        self.assertIn('禁止用 `/work-order` 这类父级公共前缀', hints)
