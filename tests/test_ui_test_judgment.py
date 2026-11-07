"""The plan check asks for screen tests only when what is built has a screen (second end-to-end trial)."""
import tempfile
import unittest

from team_code_test_contract import planning_issues

SCREEN = '画面を持つシステムですが、画面操作テストを計画していません。'


def plan_without_screen_tests():
    return {'required': True, 'reason': '', 'cases': ['架空のメモを追加して検索できる'], 'command': 'python -B -m unittest',
            'existing_tests': [], 'test_methods': ['unit'], 'preconditions': ['.local/test-tmp を使う'],
            'setup_steps': ['架空のデータを作る'], 'synthetic_accounts': [], 'ui_test_required': False,
            'ui_test_tool': 'not_applicable', 'ui_test_reason': '', 'ui_test_command': '', 'cleanup_steps': ['片付ける']}


class UiTestJudgmentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = temp.name

    def issues(self, goal, deliverables=(), summary='', instruction=''):
        job = {'code_test_contract_version': 1, 'goal': goal, 'project': self.project}
        result = {'summary': summary, 'goal_contract': {'deliverables': list(deliverables), 'acceptance_criteria': []},
                  'tasks': [{'role': 'builder', 'title': '実装', 'instruction': instruction}],
                  'code_test_plan': plan_without_screen_tests()}
        return planning_issues(result, job)

    def test_a_command_tool_whose_plan_says_no_screen_tests_is_not_sent_back(self):
        issues = self.issues('メモを追加して検索できる小さなメモ帳。コマンドで使えれば十分です。',
                             summary='画面操作テストは対象外です。', instruction='画面は作らない。結果は画面に表示する。')
        self.assertNotIn(SCREEN, issues)

    def test_a_screen_in_the_request_still_needs_screen_tests(self):
        self.assertIn(SCREEN, self.issues('Web画面で使える家計簿を作ってください。'))

    def test_a_request_that_says_no_screen_does_not_need_them(self):
        self.assertNotIn(SCREEN, self.issues('画面は不要です。ログに出力するだけの集計を作ってください。'))

    def test_a_screen_in_the_deliverables_still_needs_screen_tests(self):
        self.assertIn(SCREEN, self.issues('一覧を作ってください。', deliverables=['ブラウザで開く一覧ページ']))

    def test_a_positive_mention_wins_over_an_exclusion(self):
        self.assertIn(SCREEN, self.issues('管理画面は不要です。ブラウザで開くページも作ってください。'))


if __name__ == '__main__':
    unittest.main()
