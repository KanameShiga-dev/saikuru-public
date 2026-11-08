"""A WBS request may work directly in its own Git folder (design of 2026-11-08), with the same scope check."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from team_harness import execution_workspace, new_project_folder, workspace_registration_problem


def git(root, *args):
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fictional', '-c', 'user.email=fictional@localhost', *args],
                   check=True, capture_output=True)


class ProjectFolderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='wbs-folder-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve() / 'project'
        self.root.mkdir()

    def repository(self):
        git(self.root, 'init', '-q', '-b', 'main')
        (self.root / 'SCOPE.md').write_bytes('架空\n'.encode('utf-8'))
        git(self.root, 'add', '--', 'SCOPE.md')
        git(self.root, 'commit', '-q', '-m', 'Fictional start')

    def test_only_an_empty_folder_counts_as_new(self):
        self.assertTrue(new_project_folder(str(self.root)))
        (self.root / 'notes.txt').write_text('架空\n', encoding='utf-8')
        self.assertFalse(new_project_folder(str(self.root)))

    def test_a_folder_with_files_and_no_repository_is_still_refused(self):
        (self.root / 'notes.txt').write_text('架空\n', encoding='utf-8')
        self.assertEqual(workspace_registration_problem(str(self.root)), 'workspace_formal_worktree_required')

    def test_its_own_repository_is_ready_and_records_the_branch(self):
        self.repository()
        self.assertIsNone(workspace_registration_problem(str(self.root)))
        boundary = execution_workspace(str(self.root), ['memo.py'])
        self.assertEqual((boundary['workspace_layout'], boundary['execution_branch']),
                         ('project_repository', 'refs/heads/main'))
        self.assertEqual(boundary['integration_root'], boundary['execution_root'])

    def test_a_change_outside_the_approved_files_is_refused(self):
        self.repository()
        (self.root / 'other.py').write_text('x = 1\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'workspace_dirty_outside_approved_scope'):
            execution_workspace(str(self.root), ['memo.py'])

    def test_a_change_inside_the_approved_files_is_reported(self):
        self.repository()
        (self.root / 'memo.py').write_text('x = 1\n', encoding='utf-8')
        boundary = execution_workspace(str(self.root), ['memo.py'])
        self.assertEqual([row['path'] for row in boundary['dirty_changes']], ['memo.py'])


if __name__ == '__main__':
    unittest.main()
