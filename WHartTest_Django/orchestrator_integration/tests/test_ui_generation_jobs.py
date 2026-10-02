import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from projects.models import Project
from testcases.models import TestCase, TestCaseModule, TestCaseRunRecord
from testcases.views import TestCaseViewSet
from ..ui_generation_jobs import enqueue_generation, update_job, request_job_stop, reconcile_expired_job
from ..tasks import generate_ui_case_from_run, _supervise_generation


class GenerationJobTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(username='job-user', password='test')
        self.project = Project.objects.create(name='Project A', creator=self.user)
        self.module = TestCaseModule.objects.create(name='Module', project=self.project, creator=self.user)
        self.case = TestCase.objects.create(name='Functional', project=self.project, module=self.module)
        self.record = TestCaseRunRecord.objects.create(
            testcase=self.case, executor=self.user, session_id='job-session', status='pass',
            generate_playwright_script=True, summary='测试执行结果：通过\n功能步骤已通过',
        )
        self.publish = patch('orchestrator_integration.tasks.generate_ui_case_from_run.apply_async').start()
        self.addCleanup(patch.stopall)

    def queue(self):
        return enqueue_generation(self.record.pk, llm_config_id=5)

    def test_submission_persists_pending_result_and_queues_only_ids(self):
        state = self.queue()
        self.record.refresh_from_db()
        self.assertEqual(state['status'], 'queued')
        self.assertEqual(self.record.status, 'pass')
        self.assertIn('切页或刷新', self.record.summary)
        self.assertEqual(self.publish.call_args.kwargs['args'], [self.record.pk, state['task_id']])

    def test_duplicate_submission_reuses_job(self):
        first = self.queue()
        second = self.queue()
        self.assertEqual(first['task_id'], second['task_id'])
        self.publish.assert_called_once()

    def test_other_run_for_same_case_cannot_generate_concurrently(self):
        self.queue()
        other = TestCaseRunRecord.objects.create(testcase=self.case, executor=self.user,
            session_id='second-run', status='pass', generate_playwright_script=True)
        with self.assertRaises(ValueError):
            enqueue_generation(other.pk)
        self.publish.assert_called_once()

    def test_different_project_can_queue_independently(self):
        self.queue()
        project = Project.objects.create(name='Project B', creator=self.user)
        module = TestCaseModule.objects.create(name='Module B', project=project)
        case = TestCase.objects.create(name='Functional B', project=project, module=module)
        other = TestCaseRunRecord.objects.create(testcase=case, executor=self.user,
            session_id='other-project', status='pass', generate_playwright_script=True)
        self.assertEqual(enqueue_generation(other.pk)['status'], 'queued')
        self.assertEqual(self.publish.call_count, 2)

    def test_failed_functional_run_is_not_generated(self):
        self.record.status = 'fail'
        self.record.save()
        with self.assertRaises(ValueError):
            self.queue()
        self.publish.assert_not_called()

    def test_retry_changes_job_id_without_reexecuting_functional_steps(self):
        first = self.queue()
        update_job(self.record.pk, first['task_id'], status='failed', message='生成失败')
        second = enqueue_generation(self.record.pk, retry=True)
        self.assertNotEqual(first['task_id'], second['task_id'])
        self.assertEqual(second['attempt'], 2)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, 'pass')
        self.assertEqual(self.record.summary.count('### UI 自动化用例生成'), 1)

    def test_old_job_cannot_overwrite_retry(self):
        first = self.queue()
        update_job(self.record.pk, first['task_id'], status='failed')
        second = enqueue_generation(self.record.pk, retry=True)
        self.assertFalse(update_job(self.record.pk, first['task_id'], status='success'))
        self.record.refresh_from_db()
        self.assertEqual(self.record.ui_generation['task_id'], second['task_id'])

    def test_queued_stop_prevents_worker_execution(self):
        state = self.queue()
        self.record.refresh_from_db()
        request_job_stop(self.record)
        with patch('orchestrator_integration.tasks._supervise_generation') as work:
            generate_ui_case_from_run(self.record.pk, state['task_id'])
        work.assert_not_called()
        self.record.refresh_from_db()
        self.assertEqual(self.record.ui_generation['status'], 'stopped')

    def test_running_stop_cannot_be_overwritten_by_stage_change(self):
        state = self.queue()
        update_job(self.record.pk, state['task_id'], status='generating')
        self.record.refresh_from_db()
        request_job_stop(self.record)
        self.assertFalse(update_job(self.record.pk, state['task_id'], status='validating'))
        self.assertTrue(update_job(self.record.pk, state['task_id'], status='stopped'))

    def test_expired_job_is_reported_without_changing_functional_result(self):
        state = self.queue()
        update_job(self.record.pk, state['task_id'], deadline_at=(timezone.now()-timedelta(seconds=1)).isoformat())
        self.record.refresh_from_db()
        reconcile_expired_job(self.record)
        self.assertEqual(self.record.ui_generation['status'], 'failed')
        self.assertEqual(self.record.status, 'pass')
        self.assertIn('超时', self.record.summary)

    def test_broker_failure_is_saved_as_failure(self):
        self.publish.side_effect = RuntimeError('broker down')
        result = self.queue()
        self.assertEqual(result['status'], 'failed')
        self.assertIn('队列', result['reason'])

    def test_stale_record_cannot_expire_a_worker_with_a_fresh_deadline(self):
        state = self.queue()
        update_job(self.record.pk, state['task_id'], deadline_at=(timezone.now()-timedelta(seconds=1)).isoformat())
        self.record.refresh_from_db()
        update_job(self.record.pk, state['task_id'], status='generating',
            deadline_at=(timezone.now()+timedelta(seconds=30)).isoformat())
        reconcile_expired_job(self.record)
        self.assertEqual(self.record.ui_generation['status'], 'generating')

    def test_stop_waits_for_cleanup_when_worker_started_after_page_loaded(self):
        state = self.queue()
        self.record.refresh_from_db()
        update_job(self.record.pk, state['task_id'], status='generating')
        self.assertEqual(request_job_stop(self.record)['status'], 'stopping')

    def test_worker_acknowledges_stop_after_async_cleanup(self):
        state = self.queue()
        cleaned = []
        async def pending(*args):
            from asgiref.sync import sync_to_async
            try:
                await sync_to_async(request_job_stop)(self.record)
                await asyncio.Event().wait()
            finally:
                cleaned.append(True)
        with patch('orchestrator_integration.tasks._generate_from_run', side_effect=pending), patch(
            'orchestrator_integration.tasks.Redis.from_url'
        ) as redis:
            redis.return_value.lock.return_value.acquire.return_value = True
            generate_ui_case_from_run(self.record.pk, state['task_id'])
        self.record.refresh_from_db()
        self.assertEqual(self.record.ui_generation['status'], 'stopped')
        self.assertEqual(cleaned, [True])

    def test_worker_finishes_without_http_or_websocket_connection(self):
        state = self.queue()
        async def generate(*args):
            await asyncio.sleep(0.01)
            return {'status': 'success', 'message': 'UI 用例已生成且回放通过', 'ui_test_case_id': 123}
        with patch('orchestrator_integration.tasks._generate_from_run', side_effect=generate), patch(
            'orchestrator_integration.tasks.Redis.from_url'
        ) as redis:
            redis.return_value.lock.return_value.acquire.return_value = True
            generate_ui_case_from_run(self.record.pk, state['task_id'])
            # Redelivery must not perform another replay.
            generate_ui_case_from_run(self.record.pk, state['task_id'])
            redis.assert_called_once()
        self.record.refresh_from_db()
        self.assertEqual(self.record.ui_generation['status'], 'success')
        self.assertIn('回放通过', self.record.summary)

    def test_worker_restores_original_context_and_scoped_save_tool(self):
        from contextlib import asynccontextmanager
        from langgraph_integration.models import LLMConfig
        from langchain_core.messages import HumanMessage, ToolMessage
        config = LLMConfig.objects.create(config_name='Background fixture', name='fixture',
            api_url='http://unused.invalid/v1')
        state = enqueue_generation(self.record.pk, llm_config_id=config.pk)
        messages = [HumanMessage(content='Isolated functional execution evidence')]
        final_evidence = ToolMessage(content='RESULT=PASS: final browser step', tool_call_id='final-step')
        expected_messages = [*messages, final_evidence]
        checkpoint = SimpleNamespace(aget_tuple=AsyncMock(return_value=SimpleNamespace(
            checkpoint={'channel_values': {'messages': messages}},
            pending_writes=[('tool-task', 'messages', [final_evidence])])))
        @asynccontextmanager
        async def checkpoint_context():
            yield checkpoint
        async def finalize(**kwargs):
            self.assertEqual(kwargs['messages'], expected_messages)
            self.assertEqual(kwargs['project_id'], self.project.pk)
            self.assertEqual(kwargs['test_case_id'], self.case.pk)
            self.assertEqual(kwargs['user'].pk, self.user.pk)
            self.assertTrue(kwargs['background'])
            self.assertIn('save_generated_ui_testcase', [tool.name for tool in kwargs['tools']])
            await kwargs['on_stage']('validating')
            return {'status': 'success', 'message': 'UI 用例已生成并通过回放验证'}
        with patch('wharttest_django.checkpointer.get_async_checkpointer', checkpoint_context), patch(
            'langgraph_integration.views.create_llm_instance'
        ) as llm, patch('orchestrator_integration.agent_loop_view._finalize_generated_ui_case',
            side_effect=finalize), patch('orchestrator_integration.tasks.Redis.from_url') as redis:
            redis.return_value.lock.return_value.acquire.return_value = True
            generate_ui_case_from_run(self.record.pk, state['task_id'])
            self.assertEqual(llm.call_args.args[0].pk, config.pk)
        checkpoint.aget_tuple.assert_awaited_once_with({'configurable': {
            'thread_id': f'{self.user.pk}_{self.project.pk}_{self.record.session_id}'}})
        self.record.refresh_from_db()
        self.assertEqual(self.record.ui_generation['status'], 'success')

    def test_disabled_executor_cannot_start_generation(self):
        state = self.queue()
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        with patch('orchestrator_integration.agent_loop_view._finalize_generated_ui_case') as finalize, patch(
            'orchestrator_integration.tasks.Redis.from_url'
        ) as redis:
            redis.return_value.lock.return_value.acquire.return_value = True
            generate_ui_case_from_run(self.record.pk, state['task_id'])
        finalize.assert_not_called()
        self.record.refresh_from_db()
        self.assertEqual(self.record.ui_generation['status'], 'failed')
        self.assertIn('用户已失效', self.record.ui_generation['reason'])

    def test_timeout_cancels_model_work_and_saves_failure(self):
        state = self.queue()
        cleaned = []
        async def pending(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(True)
        with patch('orchestrator_integration.tasks._generate_from_run', side_effect=pending), patch(
            'orchestrator_integration.tasks.job_timeout_seconds', return_value=0.01
        ), patch('orchestrator_integration.tasks.Redis.from_url') as redis:
            redis.return_value.lock.return_value.acquire.return_value = True
            generate_ui_case_from_run(self.record.pk, state['task_id'])
        self.record.refresh_from_db()
        self.assertEqual(self.record.ui_generation['status'], 'failed')
        self.assertEqual(cleaned, [True])

    def test_api_project_scope_rejects_wrong_project(self):
        view = TestCaseViewSet.as_view({'post': 'manage_ui_generation'})
        request = APIRequestFactory().post('/', {'action': 'retry'}, format='json')
        force_authenticate(request, self.user)
        response = view(request, project_pk=self.project.pk + 100, pk=self.case.pk, record_id=self.record.pk)
        self.assertEqual(response.status_code, 404)
        self.publish.assert_not_called()


class BackgroundRunnerTests(SimpleTestCase):
    def test_detached_runner_requires_no_websocket_transport(self):
        from ui_automation.background_runner import create_background_runner
        runner = create_background_runner(SimpleNamespace(username='tester'))
        self.assertEqual(runner.scope['user'].username, 'tester')
        async_to_sync(runner.send_json)({'irrelevant': 'notification'})
        async_to_sync(runner.channel_layer.group_send)('ui_automation', {})

    def test_request_cancellation_does_not_cancel_queue_handoff(self):
        from ..agent_loop_view import _queue_generated_ui_case
        import threading
        entered, release, handed_off = threading.Event(), threading.Event(), threading.Event()
        def enqueue(*args, **kwargs):
            entered.set()
            release.wait(3)
            handed_off.set()
            return {'status': 'queued'}
        async def scenario():
            with patch('orchestrator_integration.ui_generation_jobs.enqueue_generation', side_effect=enqueue):
                task = asyncio.create_task(_queue_generated_ui_case(
                    record=SimpleNamespace(pk=1, status='pass'), llm_config_id=1))
                await asyncio.to_thread(entered.wait, 2)
                task.cancel()
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.to_thread(handed_off.wait, 2)
        async_to_sync(scenario)()
        self.assertTrue(handed_off.is_set())


class RecorderBackgroundIntegrationTests(TransactionTestCase):
    def test_replay_persists_results_without_any_websocket(self):
        import os
        if os.getenv('RUN_RECORDER_INTEGRATION_TESTS') != '1':
            self.skipTest('Set RUN_RECORDER_INTEGRATION_TESTS=1 with installed Chromium')
        from urllib.parse import quote
        from ui_automation.models import (
            UiModule, UiPage, UiPageSteps, UiPageStepsDetailed, UiElement,
            UiTestCase, UiCaseStepsDetailed, UiExecutionRecord,
        )
        from ui_automation.consumers import SocketUserManager
        from ..agent_loop_view import _validate_generated_ui_case
        user = get_user_model().objects.create_superuser(username='headless-user', password='test')
        project = Project.objects.create(name='Isolated browser fixture', creator=user)
        module = UiModule.objects.create(name='Fixture', project=project)
        page = UiPage.objects.create(name='Fixture', project=project, module=module,
            url='data:text/html,' + quote('<html><body><h1 id="done">Ready</h1></body></html>'))
        steps = UiPageSteps.objects.create(project=project, module=module, page=page, name='Check fixture')
        element = UiElement.objects.create(page=page, name='Heading', locator_type='css', locator_value='#done')
        UiPageStepsDetailed.objects.create(page_step=steps, element=element, ope_key='assert_text',
            step_type=1, ope_value={'expected': 'Ready'}, step_sort=0)
        case = UiTestCase.objects.create(project=project, module=module, name='Background fixture')
        UiCaseStepsDetailed.objects.create(test_case=case, page_step=steps)
        self.assertIsNone(SocketUserManager.get_web_user(user.username))
        result = async_to_sync(_validate_generated_ui_case)(user=user, project_id=project.pk,
            ui_test_case_id=case.pk, validation_session_id='isolated-replay', background=True)
        self.assertEqual(result['status'], 'success', result)
        record = UiExecutionRecord.objects.get(pk=result['execution_record_id'])
        self.assertEqual(record.executor_id, user.pk)
        self.assertEqual(record.test_case.project_id, project.pk)
        self.assertEqual(record.status, 2)
        self.assertTrue(record.step_results)
