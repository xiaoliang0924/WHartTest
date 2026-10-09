from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from langchain_core.messages import AIMessage, ToolMessage

from .. import agent_loop_view


class AiResumeProgressTests(SimpleTestCase):
    def collect(self, messages, stopped=False):
        async def stream(*args, **kwargs):
            self.assertEqual(kwargs['stream_mode'], 'updates')
            self.assertEqual(args[0]['messages'][0].content, 'continue')
            for message in messages:
                yield {'node': {'messages': [message]}}

        async def run():
            with patch.object(agent_loop_view, 'should_stop', return_value=stopped), patch.object(
                agent_loop_view, '_emit_testcase_tool_started',
                new=AsyncMock(return_value=[{'type': 'testcase_progress', 'state': 'running', 'step': 4}]),
            ):
                return [event async for event in agent_loop_view._stream_ai_testcase_resume(
                    SimpleNamespace(astream=stream), resume_prompt='continue', config={},
                    session_id='test', test_case_id=1215, total_steps=4,
                )]

        return async_to_sync(run)()

    def test_streams_start_and_completed_step_before_returning_tool_result(self):
        events = self.collect([
            AIMessage(content='', tool_calls=[{'name': 'execute_skill_script', 'args': {}, 'id': 'call'}]),
            ToolMessage(content='[CASE_STEP_EXECUTION] case=1215 step=4\nRESULT=PASS: verified', tool_call_id='call', name='execute_skill_script'),
        ])
        self.assertEqual([e['type'] for e in events], ['testcase_progress', 'testcase_progress', 'tool_result'])
        self.assertEqual(events[0]['state'], 'running')
        self.assertEqual(events[1]['state'], 'completed')
        self.assertEqual(events[1]['step'], 4)
        self.assertEqual(events[1]['total_steps'], 4)

    def test_does_not_attribute_other_case_or_natural_language_to_progress(self):
        for content in ['[CASE_STEP_EXECUTION] case=999 step=4\nRESULT=PASS', '步骤4通过']:
            events = self.collect([ToolMessage(content=content, tool_call_id='call')])
            self.assertEqual([e['type'] for e in events], ['tool_result'])

    def test_stops_continuation_when_user_stops(self):
        with self.assertRaisesRegex(RuntimeError, '用户停止执行'):
            self.collect([ToolMessage(content='RESULT=PASS', tool_call_id='call')], stopped=True)
