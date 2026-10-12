from types import SimpleNamespace
from django.test import TestCase
from ui_automation.tests import GeneratedUiTestCasePersistenceTests
from ui_automation.generated_case_service import persist_generated_ui_testcase, can_refresh_failed_generated_case, _resolve_active_auth_state
from ui_automation.models import UiTestCase, UiEnvironmentConfig, UiAuthState


class GeneratedRefreshTests(TestCase):
    setUp = GeneratedUiTestCasePersistenceTests.setUp

    def save(self, refresh=False):
        return persist_generated_ui_testcase(testcase_id=self.testcase.id,
            project_id=self.project.id, user_id=self.user.id,
            payload=self.payload, refresh_failed_generated=refresh)

    def failed_case(self):
        case = UiTestCase.objects.get(pk=self.save()['ui_test_case_id'])
        case.status = 3
        case.result_data = {'generation_validation': {'status': 'failed'}}
        case.save()
        return case

    def test_failed_generated_binding_refreshes_without_deleting_history(self):
        old = self.failed_case()
        old_steps = list(old.case_steps.values_list('page_step_id', flat=True))
        self.payload['name'] = 'Current observed execution'
        result = self.save(refresh=True)
        self.assertNotEqual(result['ui_test_case_id'], old.id)
        self.testcase.refresh_from_db()
        self.assertEqual(self.testcase.ui_test_case_id, result['ui_test_case_id'])
        self.assertEqual(self.testcase.ui_step_mapping['ui_test_case_id'], result['ui_test_case_id'])
        self.assertEqual(list(old.case_steps.values_list('page_step_id', flat=True)), old_steps)

    def test_invalid_refresh_rolls_back_and_retains_original_binding(self):
        old = self.failed_case()
        self.payload['steps'][0]['functional_step_numbers'] = [999]
        with self.assertRaises(ValueError):
            self.save(refresh=True)
        self.testcase.refresh_from_db()
        self.assertEqual(self.testcase.ui_test_case_id, old.id)
        self.assertEqual(UiTestCase.objects.count(), 1)

    def test_manual_case_is_not_replaced(self):
        old = self.failed_case()
        old.name = 'Manually maintained case'
        old.save()
        self.assertEqual(self.save(refresh=True)['ui_test_case_id'], old.id)

    def test_successful_generated_case_is_not_replaced(self):
        old = self.failed_case()
        old.status = 2
        old.save()
        self.assertEqual(self.save(refresh=True)['ui_test_case_id'], old.id)

    def test_default_call_keeps_existing_failed_case(self):
        old = self.failed_case()
        self.assertEqual(self.save()['ui_test_case_id'], old.id)

    def test_auth_selection_matches_account_before_url_recency(self):
        good_env = UiEnvironmentConfig.objects.create(project=self.project, name='Expected role',
            base_url='https://example.test/login', login_username='role-a')
        good = UiAuthState.objects.create(env_config=good_env, name='Expected role',
            state_json={'origins': [{'origin': 'https://example.test', 'localStorage': []}]})
        wrong_env = UiEnvironmentConfig.objects.create(project=self.project, name='Other role',
            base_url=good_env.base_url, login_username='role-b')
        UiAuthState.objects.create(env_config=wrong_env, name='Other role', state_json=good.state_json)
        result = _resolve_active_auth_state(project_id=self.project.id,
            observed_urls=['https://example.test/dashboard'], account_identity='role-a')
        self.assertEqual(result.id, good.id)
        self.assertIsNone(_resolve_active_auth_state(project_id=self.project.id,
            observed_urls=['https://example.test/dashboard'], account_identity='role-c'))

    def test_single_account_cannot_use_other_roles_default_auth(self):
        step = self.testcase.steps.first()
        step.description = '使用账号(role-a/secret)登录'
        step.save()
        env = UiEnvironmentConfig.objects.create(project=self.project, name='Other role',
            base_url='https://example.test/login', login_username='role-b')
        UiAuthState.objects.create(env_config=env, name='Other role',
            state_json={'origins': [{'origin': 'https://example.test', 'localStorage': []}]})
        with self.assertRaisesRegex(ValueError, '账号一致'):
            self.save()
        self.testcase.refresh_from_db()
        self.assertIsNone(self.testcase.ui_test_case_id)
        self.assertEqual(UiTestCase.objects.count(), 0)

    def test_failed_refresh_does_not_revalidate_old_script(self):
        from asgiref.sync import async_to_sync
        from unittest.mock import patch, AsyncMock
        from orchestrator_integration.agent_loop_view import _finalize_generated_ui_case
        old = self.failed_case()
        record = SimpleNamespace(status='pass', summary='Passed', save=lambda **kwargs: None)
        with patch('orchestrator_integration.agent_loop_view._force_save_generated_ui_case',
                   new_callable=AsyncMock, return_value={'status': 'failed', 'message': 'Missing evidence'}), patch(
                   'orchestrator_integration.agent_loop_view._validate_generated_ui_case',
                   new_callable=AsyncMock) as validate:
            result = async_to_sync(_finalize_generated_ui_case)(
                llm=object(), tools=[], messages=[], test_case_id=self.testcase.id,
                project_id=self.project.id, record=record, user=self.user, background=True)
        self.assertEqual(result['status'], 'failed')
        validate.assert_not_awaited()
        self.testcase.refresh_from_db()
        self.assertEqual(self.testcase.ui_test_case_id, old.id)

    def test_role_specific_auth_does_not_change_global_default(self):
        env = UiEnvironmentConfig.objects.create(project=self.project, name='Scoped role',
            base_url='https://example.test/login', login_username='role-a',
            extra_config={'account_scoped_auth': True})
        state = UiAuthState.objects.create(env_config=env, name='Scoped role',
            state_json={'origins': [{'origin': 'https://example.test', 'localStorage': []}]})
        args = dict(project_id=self.project.id, observed_urls=['https://example.test/dashboard'])
        self.assertIsNone(_resolve_active_auth_state(**args))
        self.assertEqual(_resolve_active_auth_state(**args, account_identity='role-a').id, state.id)
