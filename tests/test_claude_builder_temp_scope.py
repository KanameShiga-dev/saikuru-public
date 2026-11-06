import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from team_adapters import ProviderError, _set_scoped_test_temp


class Store:
    def __init__(self, job):
        self.job = job

    def get(self, job_id, kind):
        if job_id != 'job-1' or kind != 'job':
            raise AssertionError('unexpected job lookup')
        return self.job


def context(root, role='builder', validation_commands=None):
    job = {'validation_commands': validation_commands or []}
    return SimpleNamespace(
        project=str(root),
        task={'job_id': 'job-1', 'role': role},
        engine=SimpleNamespace(store=Store(job)),
    )


class ClaudeBuilderTempScopeTests(unittest.TestCase):
    def test_builder_test_process_uses_existing_project_temp_folder(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            target = root / '.local' / 'test-tmp'
            target.mkdir(parents=True)
            env = {'TEMP': 'outside', 'TMP': 'outside'}

            _set_scoped_test_temp(context(root, validation_commands=['python -m unittest discover -s tests -v']), env)

            self.assertEqual(env['TEMP'], str(target.resolve()))
            self.assertEqual(env['TMP'], str(target.resolve()))

    def test_non_builder_keeps_environment(self):
        env = {'TEMP': 'unchanged', 'TMP': 'unchanged'}
        _set_scoped_test_temp(context(Path.cwd(), role='reviewer'), env)
        self.assertEqual(env, {'TEMP': 'unchanged', 'TMP': 'unchanged'})

    def test_missing_folder_stops_before_provider_start_without_creating_it(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            env = {'TEMP': 'outside', 'TMP': 'outside'}
            with self.assertRaises(ProviderError) as caught:
                _set_scoped_test_temp(context(root), env)
            self.assertEqual(caught.exception.code, 'test_temp_scope')
            self.assertEqual(caught.exception.model_call_state, 'not_started')
            self.assertFalse((root / '.local').exists())
            self.assertEqual(env, {'TEMP': 'outside', 'TMP': 'outside'})

    def test_unsafe_folder_stops_before_provider_start(self):
        with tempfile.TemporaryDirectory() as workspace:
            (Path(workspace) / '.local').write_text('fictional file', encoding='utf-8')
            env = {'TEMP': 'outside', 'TMP': 'outside'}
            with self.assertRaises(ProviderError) as caught:
                _set_scoped_test_temp(
                    context(Path(workspace), validation_commands=['python -m unittest']), env)
            self.assertEqual(caught.exception.code, 'test_temp_scope')
            self.assertEqual(caught.exception.model_call_state, 'not_started')
            self.assertEqual(env, {'TEMP': 'outside', 'TMP': 'outside'})


if __name__ == '__main__':
    unittest.main()
