from django.test import SimpleTestCase
from orchestrator_integration.builtin_tools.skill_tools import unsafe_global_table_selector


class TableAssertionGuardTests(SimpleTestCase):
    def test_rejects_global_header_body_and_calendar_cell_reads(self):
        for command in ["page.locator('tbody tr td:nth-child(3)').allTextContents()",
                        'page.locator("thead th").evaluateAll(headers => headers)',
                        "document.querySelectorAll('td:nth-child(2)')",
                        "page.$$eval('table tbody tr', rows => rows.length)",
                        "page.locator('table:visible').count()"]:
            self.assertIsNotNone(unsafe_global_table_selector(command), command)

    def test_allows_scoped_real_locators_and_table_helper(self):
        for command in ["page.locator('#orders tbody tr').count()",
                        "page.locator('table.orders tbody tr').count()",
                        "table.querySelectorAll('tbody tr')",
                        "helpers.assertTableColumnEquals(page, '工单类型', '人工报价')"]:
            self.assertIsNone(unsafe_global_table_selector(command), command)
