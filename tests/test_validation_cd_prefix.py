"""A builder's `cd <the job's folder> && <planned command>` is the planned command (production trial, 2026-11-07)."""
import unittest

from team_validation_gate import command_index, record_result

PROJECT = r'C:\AI_WorkSpace\Projects\Learning\fictional-memo'
PLANNED = 'python -m unittest discover -s tests -v'


class CdPrefixTests(unittest.TestCase):
    def test_a_cd_into_the_job_folder_counts_as_the_planned_command(self):
        for ran in ('cd C:/AI_WorkSpace/Projects/Learning/fictional-memo && ' + PLANNED,
                    'cd "C:/AI_WorkSpace/Projects/Learning/fictional-memo" && ' + PLANNED,
                    "cd 'c:/ai_workspace/projects/learning/fictional-memo/' &&  " + PLANNED,
                    r'cd C:\AI_WorkSpace\Projects\Learning\fictional-memo && ' + PLANNED):
            self.assertEqual(command_index([PLANNED], ran, PROJECT), 0, ran)
        runs = record_result({'role': 'builder'}, [PLANNED], 'cd C:/AI_WorkSpace/Projects/Learning/fictional-memo && '
                             + PLANNED, 0, False, PROJECT)
        self.assertEqual(runs[-1]['index'], 0)

    def test_other_prefixes_and_wrappers_stay_other_commands(self):
        folder = 'cd C:/AI_WorkSpace/Projects/Learning/fictional-memo'
        for ran in ('cd C:/AI_WorkSpace/Projects/Learning/other && ' + PLANNED,
                    'cd C:/AI_WorkSpace/Projects/Learning/fictional-memo/tests && ' + PLANNED,
                    folder + ' ; ' + PLANNED,
                    folder + ' || ' + PLANNED,
                    folder + ' && ' + PLANNED + ' 2>&1 | tail -40',
                    folder + ' && ' + PLANNED + ' && echo done',
                    folder + ' && cd tests && ' + PLANNED):
            self.assertIsNone(command_index([PLANNED], ran, PROJECT), ran)

    def test_without_the_job_folder_a_cd_prefix_is_not_accepted(self):
        ran = 'cd C:/AI_WorkSpace/Projects/Learning/fictional-memo && ' + PLANNED
        self.assertIsNone(command_index([PLANNED], ran))
        self.assertIsNone(record_result({'role': 'builder'}, [PLANNED], ran, 0, False))


if __name__ == '__main__':
    unittest.main()
