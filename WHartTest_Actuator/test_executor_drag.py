#!/usr/bin/env python
"""Drag step tests for PlaywrightExecutor."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent))

from executor import PlaywrightExecutor, StepConfig


class FakeMouse:
    def __init__(self):
        self.moves = []
        self.down_count = 0
        self.up_count = 0

    async def move(self, x, y, steps=None):
        self.moves.append((x, y, steps))

    async def down(self):
        self.down_count += 1

    async def up(self):
        self.up_count += 1


class FakePage:
    def __init__(self):
        self.mouse = FakeMouse()

    async def wait_for_timeout(self, ms):
        return None


class FakeLocator:
    def __init__(self, box=None, visible=True):
        self.box = box or {'x': 100, 'y': 200, 'width': 40, 'height': 40}
        self.visible = visible

    async def bounding_box(self):
        return self.box

    async def wait_for(self, state=None, timeout=None):
        if not self.visible:
            raise RuntimeError('not visible')


class PlaywrightExecutorDragTest(unittest.TestCase):
    def setUp(self):
        self.executor = PlaywrightExecutor()
        self.page = FakePage()

    def _drag_step(self, ope_params):
        return StepConfig(
            step_id=1,
            operation_type='drag',
            locator_type='xpath',
            locator_value='//div[@class="slider"]',
            ope_params=ope_params,
        )

    def test_drag_relative_right(self):
        step = self._drag_step({
            'mode': 'relative',
            'direction': 'right',
            'distance': 262,
            'steps': 20,
            'delay_ms': 10,
        })
        locator = FakeLocator()

        async def run():
            with patch.object(self.page, 'wait_for_timeout', new=AsyncMock()):
                return await self.executor._execute_drag(self.page, locator, step, self.page)

        success, message = asyncio.run(run())

        self.assertTrue(success)
        self.assertIn('262', message)
        self.assertEqual(self.page.mouse.down_count, 1)
        self.assertEqual(self.page.mouse.up_count, 1)
        self.assertEqual(len(self.page.mouse.moves), 2)
        start_x, start_y, _ = self.page.mouse.moves[0]
        end_x, end_y, steps = self.page.mouse.moves[1]
        self.assertEqual(start_x, 120)
        self.assertEqual(start_y, 220)
        self.assertEqual(end_x, 382)
        self.assertEqual(end_y, 220)
        self.assertEqual(steps, 20)

    def test_drag_to_element(self):
        step = self._drag_step({
            'mode': 'to_element',
            'target_locator_type': 'css',
            'target_locator_value': '.slider-track-end',
            'steps': 15,
        })
        source = FakeLocator({'x': 10, 'y': 10, 'width': 20, 'height': 20})
        target = FakeLocator({'x': 300, 'y': 10, 'width': 20, 'height': 20})

        async def run():
            with patch.object(self.executor, '_get_locator', return_value=target):
                return await self.executor._execute_drag(self.page, source, step, self.page)

        success, message = asyncio.run(run())

        self.assertTrue(success)
        self.assertIn('拖到元素', message)
        self.assertEqual(self.page.mouse.moves[-1][0], 310)
        self.assertEqual(self.page.mouse.moves[-1][2], 15)

    def test_drag_missing_distance(self):
        step = self._drag_step({'mode': 'relative', 'direction': 'right', 'distance': 'abc'})

        async def run():
            return await self.executor._execute_drag(self.page, FakeLocator(), step, self.page)

        success, message = asyncio.run(run())
        self.assertFalse(success)
        self.assertIn('数字', message)

    def test_drag_xy_offset_format(self):
        step = self._drag_step({'x': 260, 'y': 0})
        locator = FakeLocator({'x': 100, 'y': 200, 'width': 40, 'height': 40})

        async def run():
            return await self.executor._execute_drag(self.page, locator, step, self.page)

        success, message = asyncio.run(run())

        self.assertTrue(success)
        self.assertIn('260', message)
        self.assertEqual(self.page.mouse.moves[-1][0], 360)


if __name__ == '__main__':
    unittest.main()
