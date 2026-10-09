import unittest
from ui_automation.replay_plan import prepare_authenticated_group


class AuthenticationReplayPlanTests(unittest.TestCase):
    def setUp(self):
        self.profile = {'url': 'https://portal.example/auth/session', 'username': 'role-a'}
        self.steps = [
            {'detail_id': 11, 'ope_key': 'goto', 'ope_value': {'url': self.profile['url']}},
            {'detail_id': 12, 'ope_key': 'fill', 'element': {'locator_value': 'Employee ID'}, 'ope_value': {'value': 'role-a'}},
            {'detail_id': 13, 'ope_key': 'fill', 'element': {'locator_value': 'input[type="password"]'}, 'ope_value': {'value': '${{password}}'}},
            {'detail_id': 14, 'ope_key': 'click', 'element': {'locator_value': 'button:Sign in'}},
            {'detail_id': 15, 'ope_key': 'assert_url', 'ope_value': {'expected': 'https://portal.example/dashboard', 'match': 'path'}},
        ]

    def plan(self, steps=None, mode='auto', first=True, profile=True):
        return prepare_authenticated_group(self.steps if steps is None else steps, mode=mode,
                                           login_profile=self.profile if profile else None, first_group=first)

    def test_same_account_login_retains_assertion_and_id(self):
        self.assertEqual(self.plan(), [self.steps[-1]])
        self.assertEqual(len(self.steps), 5)

    def test_other_account_requires_explicit_policy(self):
        self.steps[1]['ope_value']['value'] = 'role-b'
        with self.assertRaisesRegex(ValueError, '账号不一致'):
            self.plan()

    def test_mixed_business_actions_are_not_removed(self):
        self.steps.insert(-1, {'ope_key': 'click', 'element': {'locator_value': 'Delete'}})
        with self.assertRaises(ValueError):
            self.plan()

    def test_later_role_switch_is_not_skipped(self):
        with self.assertRaises(ValueError):
            self.plan(first=False)

    def test_script_authentication_preserves_every_action(self):
        self.assertEqual(self.plan(mode='script'), self.steps)

    def test_bound_snapshots_without_environment_login_preserve_actions(self):
        self.assertEqual(self.plan(profile=False), self.steps)

    def test_environment_authentication_requires_assertions_and_profile(self):
        with self.assertRaises(ValueError):
            self.plan(mode='environment')
        with self.assertRaises(ValueError):
            self.plan([self.steps[-1]], mode='environment', profile=False)
        self.assertEqual(self.plan([self.steps[-1]], mode='environment'), [self.steps[-1]])

    def test_no_assertion_does_not_hide_missing_coverage(self):
        with self.assertRaises(ValueError):
            self.plan(self.steps[:-1])

    def test_business_group_is_unchanged(self):
        steps = [{'ope_key': 'fill', 'element': {'locator_value': 'Search'}, 'ope_value': {'value': 'keyword'}}]
        self.assertEqual(self.plan(steps), steps)

    def test_password_change_business_group_is_not_treated_as_login(self):
        steps = [self.steps[2], self.steps[3]]
        self.assertEqual(self.plan(steps), steps)

    def test_explicit_environment_assertions_support_per_role_snapshots(self):
        assertions = [self.steps[-1]]
        result = prepare_authenticated_group(
            assertions, mode='environment', login_profile=None,
            first_group=False, bound_authentication=True,
        )
        self.assertEqual(result, assertions)
