"""LLM 造数计划生成测试。"""

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from api_environments.models import ApiEnvironment
from data_generation.assignee_resolver import (
    AssigneeResolutionError,
    resolve_assignee_params,
)
from data_generation.intent_router import (
    infer_business_template_key,
    normalize_custom_steps,
    route_llm_payload,
)
from data_generation.llm_plan_generator import (
    _expand_template_plan,
    _extract_json_object,
    generate_plan_from_description_with_llm,
)
from projects.models import Project


class LlmJsonParseTests(TestCase):
    def test_extract_json_from_codeblock(self):
        raw = '```json\n{"generation_mode":"template","template_key":"biz_create_type_a"}\n```'
        parsed = _extract_json_object(raw)
        self.assertEqual(parsed['template_key'], 'biz_create_type_a')


class TemplateExpandTests(TestCase):
    def test_expand_template_plan(self):
        plan = _expand_template_plan(
            {
                'template_key': 'biz_create_type_a',
                'input_params': {'summary': '测试工单'},
            },
            description='创建工单',
            default_environment_id=4,
        )
        self.assertEqual(plan['source'], 'llm:template:biz_create_type_a')
        self.assertTrue(plan['steps'])
        self.assertEqual(plan['suggested_input_params']['summary'], '测试工单')
        self.assertEqual(
            plan['template_params_schema']['summary']['default'],
            '测试工单',
        )

    def test_normalizes_transfer_custom_steps_to_builtin_template(self):
        payload = route_llm_payload(
            '创建 TYPE_C 工单并转派给李亮',
            {
                'generation_mode': 'custom',
                'steps': [{
                    'type': 'api_call',
                    'input_params': {
                        'targetUserName': '李亮',
                        'targetUserId': 46,
                        'ticketType': 'TYPE_C',
                    },
                }],
            },
        )

        self.assertEqual(payload['generation_mode'], 'template')
        self.assertEqual(payload['template_key'], 'biz_create_and_transfer')
        self.assertEqual(payload['input_params']['assigneeName'], '李亮')
        self.assertEqual(payload['input_params']['assigneeUserId'], 46)

    def test_pending_process_routes_to_assign_template(self):
        payload = route_llm_payload(
            '帮助我创建一个TYPE_C的自动化工单，并且工单状态是待处理',
            {
                'generation_mode': 'custom',
                'steps': [{
                    'type': 'api_call',
                    'interface_id': 445,
                    'body': {
                        'ticketType': 'TYPE_C',
                        'summary': '自动化测试TYPE_C待处理工单',
                    },
                }],
            },
        )

        self.assertEqual(payload['generation_mode'], 'template')
        self.assertEqual(payload['template_key'], 'biz_create_and_assign')
        self.assertEqual(payload['input_params']['ticketType'], 'TYPE_C')

    def test_infer_pending_process_template_key(self):
        self.assertEqual(
            infer_business_template_key('创建 TYPE_C 工单，状态待处理'),
            'biz_create_and_assign',
        )

    def test_normalize_custom_step_body_to_variables(self):
        steps = normalize_custom_steps([{
            'type': 'api_call',
            'interface_id': 445,
            'body': {'ticketType': 'TYPE_C', 'summary': '测试'},
        }])
        self.assertEqual(steps[0]['variables']['ticketType'], 'TYPE_C')
        self.assertNotIn('body', steps[0])


class AssigneeResolverTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        user = user_model.objects.create_user(username='resolver_user', password='pass')
        self.project = Project.objects.create(name='Resolver Project', creator=user)
        self.environment = ApiEnvironment.objects.create(
            name='resolver-env',
            base_url='http://example.com',
            project=self.project,
            created_by=user,
        )

    @patch('data_generation.assignee_resolver.requests.get')
    @patch('data_generation.assignee_resolver.refresh_environment_tokens')
    def test_resolves_name_to_consistent_user_metadata(self, mock_refresh, mock_get):
        mock_refresh.return_value = {'accessToken': 'token'}
        response = MagicMock()
        response.json.return_value = {
            'data': [{
                'id': 51,
                'username': '15819330097',
                'name': '陈锐',
                'departmentNames': ['AI与数字化中心/AI产品与项目组'],
                'isActive': True,
            }],
        }
        mock_get.return_value = response

        result = resolve_assignee_params(
            {'assigneeName': '陈锐'},
            project_id=self.project.id,
            environment_id=self.environment.id,
        )

        self.assertEqual(result['assigneeUserId'], 51)
        self.assertEqual(result['assigneeName'], '陈锐')
        self.assertEqual(
            result['assigneeDepartment'],
            'AI与数字化中心/AI产品与项目组',
        )

    def test_requires_explicit_assignee_name(self):
        with self.assertRaises(AssigneeResolutionError):
            resolve_assignee_params(
                {},
                project_id=self.project.id,
                environment_id=self.environment.id,
            )


class LlmGenerateIntegrationTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username='llm_dg_user', password='pass')
        self.project = Project.objects.create(name='LLM DG Project', creator=self.user)
        self.environment = ApiEnvironment.objects.create(
            name='test-env',
            base_url='http://example.com',
            project=self.project,
            created_by=self.user,
        )

    @patch('data_generation.llm_plan_generator.resolve_assignee_params')
    @patch('data_generation.llm_plan_generator._invoke_llm_for_plan')
    def test_llm_template_mode(self, mock_invoke, mock_resolve):
        mock_invoke.return_value = {
            'generation_mode': 'template',
            'template_key': 'biz_create_and_assign',
            'input_params': {'summary': '分配测试', 'assigneeName': '陈锐'},
        }
        mock_resolve.return_value = {
            'summary': '分配测试',
            'assigneeName': '陈锐',
            'assigneeUserId': 51,
            'assigneeDepartment': 'AI与数字化中心/AI产品与项目组',
            'assigneeRole': 'customer_service',
        }
        plan = generate_plan_from_description_with_llm(
            '创建并分配工单',
            project_id=self.project.id,
            default_environment_id=self.environment.id,
            use_llm=True,
        )
        self.assertTrue(plan.get('llm_used'))
        self.assertEqual(plan['source'], 'llm:template:biz_create_and_assign')
        self.assertGreater(len(plan['steps']), 1)
        self.assertEqual(
            plan['template_params_schema']['assigneeUserId']['default'],
            51,
        )

    @patch('data_generation.llm_plan_generator._get_active_llm')
    def test_fallback_when_no_llm_config(self, mock_get_llm):
        mock_get_llm.return_value = (None, None)
        plan = generate_plan_from_description_with_llm(
            '创建工单 ticket',
            project_id=self.project.id,
            default_environment_id=self.environment.id,
            use_llm=True,
        )
        self.assertFalse(plan.get('llm_used'))
        self.assertTrue(
            'rules:fallback' in plan.get('source', '')
            or plan.get('source', '').startswith('template:')
        )

    def test_rules_when_use_llm_false(self):
        plan = generate_plan_from_description_with_llm(
            '创建工单',
            project_id=self.project.id,
            default_environment_id=self.environment.id,
            use_llm=False,
        )
        self.assertFalse(plan.get('llm_used', True))
        self.assertIn('template:', plan.get('source', ''))
