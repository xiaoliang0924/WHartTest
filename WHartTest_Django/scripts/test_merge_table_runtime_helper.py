import unittest
from scripts.merge_table_runtime_helper import merge_table_assertion


class MergeTableHelperTests(unittest.TestCase):
    def test_preserves_other_hotfixes_and_is_idempotent(self):
        source = "async function assertTableColumnEquals() { return 'tested'; }\n/**\n * 断言页面\n */\n"
        target = "function existingHotfix() { return 42; }\n/**\n * 断言页面\n */\nmodule.exports = {\n  assertPageShows,\n};\n"
        merged = merge_table_assertion(source, target)
        self.assertIn('function existingHotfix() { return 42; }', merged)
        self.assertIn("return 'tested'", merged)
        self.assertEqual(merged.count('  assertTableColumnEquals,'), 1)
        self.assertEqual(merge_table_assertion(source, merged), merged)

    def test_replaces_only_old_table_assertion(self):
        source = "async function assertTableColumnEquals() { return 'new'; }\n/**\n * 断言页面\n */\n"
        target = "function oldHotfix() {}\nasync function assertTableColumnEquals() { return 'old'; }\n/**\n * 断言页面\n */\nmodule.exports = {\n  assertPageShows,\n  assertTableColumnEquals,\n};\n"
        merged = merge_table_assertion(source, target)
        self.assertNotIn("return 'old'", merged)
        self.assertIn('function oldHotfix() {}', merged)
        self.assertEqual(merged.count('  assertTableColumnEquals,'), 1)
