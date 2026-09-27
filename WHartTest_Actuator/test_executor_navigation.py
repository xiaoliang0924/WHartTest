"""Tests for env-aware navigation options (timeout / wait_until)."""

import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock


def _install_playwright_stub() -> None:
    if 'playwright' in sys.modules:
        return
    playwright = types.ModuleType('playwright')
    async_api = types.ModuleType('playwright.async_api')
    async_api.async_playwright = MagicMock()
    async_api.Browser = object
    async_api.BrowserContext = object
    async_api.FrameLocator = object
    async_api.Page = object
    async_api.Playwright = object
    async_api.expect = MagicMock()
    sys.modules['playwright'] = playwright
    sys.modules['playwright.async_api'] = async_api


_install_playwright_stub()

from executor import PlaywrightExecutor, StepConfig  # noqa: E402


class NavigationOptionsTests(unittest.TestCase):
    def setUp(self):
        self.executor = PlaywrightExecutor(action_timeout=10000)

    def test_wait_until_from_extra_config(self):
        env = {
            'timeout': 30000,
            'extra_config': {'wait_until': 'domcontentloaded'},
        }
        self.assertEqual(self.executor._get_wait_until(env), 'domcontentloaded')

    def test_wait_until_default_is_domcontentloaded(self):
        self.assertEqual(self.executor._get_wait_until(None), 'domcontentloaded')
        self.assertEqual(self.executor._get_wait_until({}), 'domcontentloaded')

    def test_wait_until_rejects_invalid(self):
        env = {'extra_config': {'wait_until': 'something-else'}}
        self.assertEqual(self.executor._get_wait_until(env), 'domcontentloaded')

    def test_nav_timeout_prefers_env(self):
        env = {'timeout': 30000}
        self.assertEqual(self.executor._get_nav_timeout_ms(env), 30000)
        self.assertEqual(self.executor._get_nav_timeout_ms(None), 10000)

    def test_extra_config_json_string(self):
        env = {'extra_config': '{"wait_until": "load"}'}
        self.assertEqual(self.executor._get_wait_until(env), 'load')

    def test_goto_falls_back_from_networkidle(self):
        page = MagicMock()
        page.goto = AsyncMock(
            side_effect=[
                TimeoutError('Page.goto: Timeout 10000ms exceeded'),
                None,
            ]
        )
        env = {
            'timeout': 30000,
            'extra_config': {'wait_until': 'networkidle'},
        }

        asyncio.run(self.executor._goto_with_env(page, 'https://example.com', env))

        self.assertEqual(page.goto.await_count, 2)
        first_kwargs = page.goto.await_args_list[0].kwargs
        second_kwargs = page.goto.await_args_list[1].kwargs
        self.assertEqual(first_kwargs['wait_until'], 'networkidle')
        self.assertEqual(first_kwargs['timeout'], 30000)
        self.assertEqual(second_kwargs['wait_until'], 'domcontentloaded')
        self.assertEqual(second_kwargs['timeout'], 30000)

    def test_page_url_assertion_does_not_require_locator(self):
        page = MagicMock()
        page.locator = MagicMock(side_effect=AssertionError('page assertion must not resolve a locator'))
        expected = 'https://example.com/work-order/my-tickets'

        from executor import expect
        expect.return_value.to_have_url = AsyncMock()
        step = StepConfig(
            step_id=2,
            operation_type='assert_url',
            locator_type='',
            locator_value='',
            input_value=expected,
        )

        success, message, screenshot = asyncio.run(self.executor._execute_step(page, step))

        self.assertTrue(success)
        self.assertIn('页面断言 url 通过', message)
        self.assertIsNone(screenshot)
        expect.return_value.to_have_url.assert_awaited_once_with(expected)
        page.locator.assert_not_called()

    def test_page_title_assertion_does_not_require_locator(self):
        page = MagicMock()
        page.locator = MagicMock(side_effect=AssertionError('page assertion must not resolve a locator'))
        expected = 'My page title'

        from executor import expect
        expect.return_value.to_have_title = AsyncMock()
        step = StepConfig(
            step_id=3,
            operation_type='assert_title',
            locator_type='',
            locator_value='',
            input_value=expected,
        )

        success, _message, _screenshot = asyncio.run(self.executor._execute_step(page, step))

        self.assertTrue(success)
        expect.return_value.to_have_title.assert_awaited_once_with(expected)
        page.locator.assert_not_called()


if __name__ == '__main__':
    unittest.main()
