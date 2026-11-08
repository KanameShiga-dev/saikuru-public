"""The account check follows what is built, not how the plan explains itself (trial 6, 2026-11-08)."""
import unittest

from team_code_test_contract import planning_issues

PLAN = {'required': True, 'reason': '架空のメモ帳', 'cases': ['追加して検索できる'], 'command': 'python -m unittest',
        'existing_tests': [], 'test_methods': ['unit'], 'preconditions': ['Python'], 'setup_steps': ['一時DB'],
        'synthetic_accounts': [], 'ui_test_required': False, 'ui_test_tool': 'not_applicable',
        'ui_test_reason': 'コマンド式', 'ui_test_command': '', 'cleanup_steps': ['一時DBを消す']}
MEMO = {'code_test_contract_version': 1, 'goal': '小さなメモ帳をコマンドで使えるように作る。'}


def asks_for_accounts(result, job):
    return any('架空アカウント' in issue for issue in planning_issues(dict(result, code_test_plan=PLAN), job))


class AuthCheckScopeTests(unittest.TestCase):
    def test_the_plan_explaining_there_is_no_login_does_not_ask_for_accounts(self):
        result = {'summary': '認証・権限機能は元の依頼にないため、認証用の架空アカウントは計画しません。',
                  'tasks': [{'title': '実装', 'instruction': '実行権限を変更しない。', 'role': 'builder'}]}
        self.assertFalse(asks_for_accounts(result, MEMO))

    def test_a_login_in_the_request_still_asks_for_accounts(self):
        self.assertTrue(asks_for_accounts({}, dict(MEMO, goal='ログイン機能のあるメモ帳を作る。')))

    def test_a_login_in_the_acceptance_criteria_still_asks_for_accounts(self):
        result = {'goal_contract': {'deliverables': ['メモ帳'], 'acceptance_criteria': ['ログインした人だけが読める']}}
        self.assertTrue(asks_for_accounts(result, MEMO))


if __name__ == '__main__':
    unittest.main()
