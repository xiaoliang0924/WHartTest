#!/usr/bin/env python
"""SQL step execution tests for PlaywrightExecutor."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from executor import PlaywrightExecutor, StepConfig


class FakeCursor:
    def __init__(self):
        self.statements = []
        self.description = [('ID',)]
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def fetchall(self):
        return [(1,)]

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.cursor_obj = FakeCursor()
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class PlaywrightExecutorSqlTest(unittest.TestCase):
    def setUp(self):
        self.executor = PlaywrightExecutor()

    def _sql_step(self, sql_execute):
        return StepConfig(
            step_id=1,
            operation_type='',
            locator_type='xpath',
            locator_value='',
            step_type=2,
            sql_execute=sql_execute,
        )

    def test_normalize_sql_execute_defaults_select_to_fetchall(self):
        step = self._sql_step({
            'sql': 'SELECT * FROM users WHERE id = %(id)s',
            'params': {'id': 1},
        })

        config = self.executor._normalize_sql_execute(step)

        self.assertEqual(config['method'], 'fetchall')
        self.assertEqual(config['params'], {'id': 1})
        self.assertEqual(config['first_keyword'], 'select')

    def test_resolve_sql_connection_config_uses_db2_environment(self):
        sql_config = self.executor._normalize_sql_execute(self._sql_step('SELECT 1'))
        env_config = {
            'db_type': 'db2',
            'db2_config': {
                'host': 'db2.example.com',
                'port': 50000,
                'user': 'tester',
                'password': 'secret',
                'database': 'SAMPLE',
            },
        }

        db_type, db_config = self.executor._resolve_sql_connection_config(sql_config, env_config)

        self.assertEqual(db_type, 'db2')
        self.assertEqual(db_config['user'], 'tester')
        self.assertEqual(db_config['database'], 'SAMPLE')

    def test_sql_step_branch_does_not_need_page_or_locator(self):
        step = self._sql_step({'sql': 'SELECT 1'})
        step.operation_type = None
        env_config = {'db_rud_status': True}

        async def run_step():
            with patch.object(self.executor, '_execute_sql_step', return_value=(True, 'ok')) as run_sql:
                success, message, screenshot = await self.executor._execute_step(None, step, env_config)
                run_sql.assert_called_once_with(step, env_config)
                return success, message, screenshot

        success, message, screenshot = asyncio.run(run_step())

        self.assertTrue(success)
        self.assertEqual(message, 'ok')
        self.assertIsNone(screenshot)

    def test_execute_db2_fetchall_step_with_schema(self):
        step = self._sql_step({
            'sql': 'SELECT ID FROM USERS',
            'method': 'fetchall',
        })
        env_config = {
            'db_type': 'db2',
            'db_rud_status': True,
            'db2_config': {
                'host': 'db2.example.com',
                'port': 50000,
                'user': 'tester',
                'password': 'secret',
                'database': 'SAMPLE',
                'schema': 'APP',
            },
        }
        fake_conn = FakeConnection()

        with patch.object(self.executor, '_connect_db2', return_value=fake_conn):
            success, message = self.executor._execute_sql_step(step, env_config)

        self.assertTrue(success)
        self.assertIn('返回 1 行', message)
        self.assertEqual(fake_conn.cursor_obj.statements[0], ('SET CURRENT SCHEMA APP', None))
        self.assertEqual(fake_conn.cursor_obj.statements[1], ('SELECT ID FROM USERS', None))
        self.assertTrue(fake_conn.closed)


if __name__ == '__main__':
    unittest.main()
