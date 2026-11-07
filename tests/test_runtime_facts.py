"""The two stops of the first end-to-end trial: command evidence and the facts about this PC."""
import unittest
from unittest.mock import patch

import team_config
from team_config import runtime_rule
from team_validation_gate import command_index, record_result

PLANNED = r'powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\Run-Tests.ps1'


class CommandEvidenceTests(unittest.TestCase):
    def test_git_bash_dropping_backslashes_still_counts_as_the_planned_command(self):
        ran = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./tests/Run-Tests.ps1'
        self.assertEqual(command_index(['python -B -m unittest', PLANNED], ran), 1)
        runs = record_result({'role': 'builder'}, ['python -B -m unittest', PLANNED], '  ' + ran + ' ', 0, False)
        self.assertEqual(runs[-1]['index'], 1)

    def test_other_differences_are_still_other_commands(self):
        for ran in ('powershell.exe -NoProfile -File ./tests/Run-Tests.ps1',
                    'powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./tests/Other.ps1',
                    PLANNED + ' 2>&1 | tail -40'):
            self.assertIsNone(command_index([PLANNED], ran), ran)
            self.assertIsNone(record_result({'role': 'builder'}, [PLANNED], ran, 0, False))

    def test_only_the_builder_records_evidence(self):
        self.assertIsNone(record_result({'role': 'reviewer'}, [PLANNED], PLANNED, 0, False))


class RuntimeRuleTests(unittest.TestCase):
    def test_a_restricted_policy_tells_assignees_not_to_rely_on_ps1(self):
        rule = runtime_rule({'powershell_policy': 'Restricted', 'python': 'Python 3.14.4', 'node': 'missing'})
        self.assertIn('設定を変えずには .ps1 を実行できない（実行ポリシー：Restricted）', rule)
        self.assertIn('利用者が使う手順に .ps1 の実行を含めない', rule)
        self.assertIn('Python が使える（Python 3.14.4）', rule)
        self.assertIn('Node.js は見つからない', rule)
        self.assertIn('Git Bash', rule)
        self.assertIn('説明どおりに使える', rule)

    def test_an_allowing_policy_or_an_unknown_one_is_said_as_it_is(self):
        self.assertIn('設定を変えずにプロジェクト内の .ps1 を実行できる',
                      runtime_rule({'powershell_policy': 'RemoteSigned', 'python': None, 'node': None}))
        unknown = runtime_rule({'powershell_policy': None, 'python': None, 'node': None})
        self.assertIn('確認できなかった', unknown)
        self.assertIn('Windows PowerShell は見つからない', runtime_rule({'powershell_policy': 'missing'}))

    def test_the_facts_are_checked_once_without_an_inherited_policy_or_module_path(self):
        calls = []

        class Done:
            returncode, stdout, stderr = 0, 'Restricted\r\n', ''

        def run(command, **options):
            calls.append((command, options['env']))
            return Done()
        with patch.object(team_config, '_RUNTIME_FACTS', None), \
                patch.dict('os.environ', {'PSExecutionPolicyPreference': 'Bypass', 'PSModulePath': 'C:/fictional'}), \
                patch.object(team_config.shutil, 'which', side_effect=lambda name: 'C:/fictional/' + name), \
                patch.object(team_config.subprocess, 'run', side_effect=run):
            first = team_config.runtime_facts()
            second = team_config.runtime_facts()
        self.assertIs(first, second)
        self.assertEqual(first['powershell_policy'], 'Restricted')
        self.assertEqual(len(calls), 3)
        for _, env in calls:
            self.assertFalse({'PSExecutionPolicyPreference', 'PSModulePath'} & set(env))


if __name__ == '__main__':
    unittest.main()
