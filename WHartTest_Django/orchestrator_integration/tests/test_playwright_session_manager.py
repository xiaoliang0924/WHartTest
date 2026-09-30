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
    _ensure_testcase_playwright_session_id,
)


class TestcasePlaywrightSessionRoutingTests(SimpleTestCase):
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
