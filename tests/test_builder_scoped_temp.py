import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from team_adapters import _set_scoped_test_temp, ProviderError

class BuilderTempTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='synthetic-builder-temp-')
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)/'project'
        self.target = self.project/'.local'/'test-tmp'
        self.target.mkdir(parents=True)

    def ctx(self, role='builder', commands=None):
        return SimpleNamespace(project=self.project, task={'role': role, 'job_id': 'fictional'},
            engine=SimpleNamespace(store=SimpleNamespace(get=lambda *a: {'validation_commands': commands or []})))

    def test_t01_empty_commands(self):
        env = {'TMPDIR': 'fictional-untrusted'}
        _set_scoped_test_temp(self.ctx(), env)
        self.assertEqual({env[k] for k in ('TEMP','TMP','TMPDIR')}, {str(self.target.resolve())})

    def test_t02_registered_commands(self):
        env = {}
        _set_scoped_test_temp(self.ctx(commands=['python -m unittest discover -s tests -v']), env)
        self.assertEqual(env['TEMP'], str(self.target.resolve()))
        self.assertEqual(env['TMP'], env['TMPDIR'])

    def test_t03_non_builder_unchanged(self):
        for role in ('planner','researcher','reviewer'):
            with self.subTest(role=role):
                env = {'TEMP': 'unchanged'}
                _set_scoped_test_temp(self.ctx(role), env)
                self.assertEqual(env, {'TEMP':'unchanged'})

    def test_t04_unsafe_temp_rejected(self):
        with patch.object(Path, 'is_symlink', return_value=True):
            with self.assertRaises(ProviderError):
                _set_scoped_test_temp(self.ctx(), {})
        with patch.object(Path, 'is_junction', return_value=True):
            with self.assertRaises(ProviderError):
                _set_scoped_test_temp(self.ctx(), {})
        with patch.object(Path, 'resolve', side_effect=[self.project, Path(self.tmp.name)/'outside']):
            with self.assertRaises(ProviderError):
                _set_scoped_test_temp(self.ctx(), {})

    def test_t05_child_uses_scoped_temp(self):
        env = os.environ.copy()
        _set_scoped_test_temp(self.ctx(), env)
        code = "import tempfile,pathlib; p=pathlib.Path(tempfile.gettempdir()); assert p==pathlib.Path(__import__('sys').argv[1]); t=tempfile.TemporaryDirectory(); assert pathlib.Path(t.name).is_relative_to(p); t.cleanup(); print('scoped-temp-ok')"
        result = subprocess.run([sys.executable,'-B','-c',code,str(self.target.resolve())], env=env,
                                capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'scoped-temp-ok')
        self.assertEqual(list(self.target.iterdir()),[])

    def test_t06_child_bytecode_disabled(self):
        source = Path(__file__).resolve().parents[1]/'team_adapters.py'
        text = source.read_text(encoding='utf-8')
        ast.parse(text)
        self.assertIn("env['PYTHONDONTWRITEBYTECODE'] = '1'",text)
        self.assertIn('Process(command, ctx.project, env)',text)
        module = self.project/'fictional_module.py'
        module.write_text('VALUE = 1',encoding='utf-8')
        env = os.environ.copy()
        env['PYTHONDONTWRITEBYTECODE']='1'
        _set_scoped_test_temp(self.ctx(), env)
        result = subprocess.run([sys.executable,'-c',"import sys; assert sys.dont_write_bytecode; import fictional_module; print('bytecode-off')"],cwd=self.project,env=env,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'bytecode-off')
        self.assertFalse((self.project/'__pycache__').exists())
