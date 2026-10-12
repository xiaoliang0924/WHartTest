import unittest
from scripts.merge_picker_runtime_helper import merge_picker


class PickerMergeTests(unittest.TestCase):
    def test_keeps_other_hotfixes_and_replaces_state_check_idempotently(self):
        source = "async function readFilterDialogSelection() { return true; }\nasync function selectFilterDialogOption() { return 'new'; }\nfunction confirmFilterDialog() {}\n/**\n * 确认弹窗后\n */\n"
        target = "function existingHotfix() {}\nasync function selectFilterDialogOption() { return 'old'; }\nfunction confirmFilterDialog() {}\n/**\n * 确认弹窗后\n */\n"
        result = merge_picker(source, target)
        self.assertIn('function existingHotfix() {}', result)
        self.assertIn('function confirmFilterDialog() {}', result)
        self.assertNotIn("return 'old'", result)
        self.assertEqual(result.count('async function readFilterDialogSelection'), 1)
        self.assertEqual(merge_picker(source, result), result)
