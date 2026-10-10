import unittest

from team_plan_check import issues, repair_note


class PlanCheckTest(unittest.TestCase):
    def test_read_only_role_asked_to_run_commands(self):
        plan = {'tasks': [
            {'role': 'researcher', 'title': '事前確認', 'instruction': '読み取りのみで確認する。インストール（pip / npm など）は禁止。\n例：python --version で版を確認する。'},
            {'role': 'builder', 'title': '作成', 'instruction': 'python -c で作る'},
            {'role': 'reviewer', 'title': '検証', 'instruction': 'Get-FileHash でハッシュを照合する'}]}
        found = issues(plan, document_job=False)
        self.assertEqual([f[0] for f in found], [1, 3])
        self.assertIn('作業担当（builder）', repair_note(found, False))

    def test_negated_mentions_are_not_requests(self):
        plan = {'tasks': [{'role': 'researcher', 'title': '調査', 'instruction': 'Web検索で調べる。コマンドは使わない。pip install は禁止。'}]}
        self.assertEqual(issues(plan, document_job=False), [])

    def test_document_job_no_role_runs_commands(self):
        plan = {'tasks': [{'role': 'builder', 'title': '作成', 'instruction': 'generate_media で作る。'},
                          {'role': 'builder', 'title': '確認', 'instruction': 'python -c "import pptx" で確認する'}]}
        found = issues(plan, document_job=True)
        self.assertEqual([f[0] for f in found], [2])
        self.assertIn('資料用の専用ツール', repair_note(found, True))


if __name__ == '__main__':
    unittest.main()
