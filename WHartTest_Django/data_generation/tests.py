"""造数管理单元测试。"""

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from api_environments.models import ApiEnvironment
from data_generation.models import DataGenerationPlan, DataGenerationRun
from data_generation.exceptions import DataGenerationError
from data_generation.services import PlanExecutor, substitute_templates
from projects.models import Project


class SubstituteTemplatesTests(TestCase):
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


class PlanExecutorValidationTests(TestCase):
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
