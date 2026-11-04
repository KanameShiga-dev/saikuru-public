"""Contract: the standard test entry finds every test module, same as an explicit list."""
import configparser
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {'.git', '__pycache__'}


def discover_modules(root, patterns=('test_*.py',), excluded=()):
    """Return sorted dotted module names of test files under root, including dirs without __init__.py."""
    skip = SKIP_DIRS | set(excluded)
    names = set()
    for path in Path(root).rglob('*.py'):
        rel = path.relative_to(root)
        if any(part in skip for part in rel.parts[:-1]):
            continue
        if any(path.match(p) for p in patterns):
            names.add('.'.join(rel.with_suffix('').parts))
    return sorted(names)


def read_pytest_ini(path):
    parser = configparser.ConfigParser()
    parser.read(path, encoding='utf-8')
    return parser['pytest']


class FixtureDiscoveryTests(unittest.TestCase):
    """Fictional tree: nested dir without __init__.py and a basename shared with the root."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        body = 'import unittest\nclass T(unittest.TestCase):\n{}\n'
        (self.tmp / 'test_a.py').write_text(body.format('    def test_1(self): pass\n    def test_2(self): pass'))
        (self.tmp / 'sub' / 'deep').mkdir(parents=True)
        (self.tmp / 'sub' / 'test_a.py').write_text(body.format('    def test_3(self): pass'))
        (self.tmp / 'sub' / 'deep' / 'test_b.py').write_text(body.format('    def test_4(self): pass'))
        (self.tmp / 'sub' / 'helper.py').write_text('X = 1\n')
        (self.tmp / 'docs').mkdir()
        (self.tmp / 'docs' / 'test_hidden.py').write_text(body.format('    def test_5(self): pass'))

    def test_finds_nested_and_same_basename(self):
        self.assertEqual(discover_modules(self.tmp, excluded=('docs',)),
                         ['sub.deep.test_b', 'sub.test_a', 'test_a'])

    def test_helper_files_are_not_tests(self):
        self.assertNotIn('sub.helper', discover_modules(self.tmp))

    def test_unittest_discover_alone_misses_nested_dirs(self):
        """Why a path-based list is needed: stdlib discovery skips dirs without __init__.py."""
        suite = unittest.TestLoader().discover(str(self.tmp), top_level_dir=str(self.tmp))
        found = suite.countTestCases()
        explicit = self.load_explicit(discover_modules(self.tmp, excluded=('docs',)))
        self.assertEqual(explicit, 4)
        self.assertLess(found, explicit)

    def test_explicit_run_matches_discovered_set(self):
        modules = discover_modules(self.tmp, excluded=('docs',))
        self.assertEqual(self.load_explicit(modules), 4)

    def load_explicit(self, modules):
        sys.path.insert(0, str(self.tmp))
        self.addCleanup(self.drop_path)
        self.addCleanup(self.drop_modules, modules)
        loader = unittest.TestLoader()
        return sum(loader.loadTestsFromName(m).countTestCases() for m in modules)

    def drop_path(self):
        if str(self.tmp) in sys.path:
            sys.path.remove(str(self.tmp))

    @staticmethod
    def drop_modules(modules):
        for name in modules:
            for key in [k for k in sys.modules if k == name or k.startswith(name + '.') or name.startswith(k + '.')]:
                sys.modules.pop(key, None)


class CheckoutDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = read_pytest_ini(ROOT / 'pytest.ini')
        cls.excluded = cls.cfg['norecursedirs'].split()
        cls.modules = discover_modules(ROOT, excluded=cls.excluded)
        cls.everything = discover_modules(ROOT)
        cls.inventory = json.loads((ROOT / 'tests' / 'known_failures.json').read_text(encoding='utf-8'))['inventory_modules']

    def test_pytest_ini_pattern_is_test_star(self):
        self.assertEqual(self.cfg['python_files'].split(), ['test_*.py'])

    def test_exclusions_hide_no_test_file(self):
        self.assertEqual(self.modules, self.everything,
                         'norecursedirs would hide test files: %s' % sorted(set(self.everything) - set(self.modules)))

    def test_testpaths_cover_root(self):
        self.assertEqual(self.cfg['testpaths'].split(), ['.'])

    def test_shared_basenames_need_importlib_mode(self):
        basenames = [m.rsplit('.', 1)[-1] for m in self.modules]
        duplicated = {b for b in basenames if basenames.count(b) > 1}
        self.assertTrue(duplicated, 'fixture assumption changed; revisit pytest.ini import mode')
        self.assertIn('--import-mode=importlib', self.cfg['addopts'])

    def test_no_recorded_module_dropped(self):
        missing = sorted(set(self.inventory) - set(self.modules))
        self.assertEqual(missing, [], 'recorded test modules no longer discovered')

    def test_inventory_has_no_duplicates(self):
        self.assertEqual(len(self.inventory), len(set(self.inventory)))

    def test_requirements_test_pins_pytest(self):
        lines = [l.strip() for l in (ROOT / 'requirements-test.txt').read_text(encoding='utf-8').splitlines()
                 if l.strip() and not l.startswith('#')]
        self.assertTrue(lines)
        for line in lines:
            self.assertIn('==', line, 'unpinned requirement: ' + line)


@unittest.skipUnless(importlib.util.find_spec('pytest'), 'pytest is not installed (requirements-test.txt)')
class PytestCollectionTests(unittest.TestCase):
    """The standard runner (python -m pytest) must collect the same files as the explicit module list."""

    @staticmethod
    def collect(args):
        proc = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q', '-p', 'no:cacheprovider'] + args,
                              cwd=str(ROOT), capture_output=True, text=True, timeout=300)
        nodes = {line.strip() for line in proc.stdout.splitlines() if '::' in line}
        errors = set(re.findall(r'^ERROR (\S+\.py)\s*$', proc.stdout, re.MULTILINE))
        return proc.returncode, proc.stdout + proc.stderr, nodes, errors

    @classmethod
    def setUpClass(cls):
        cfg = read_pytest_ini(ROOT / 'pytest.ini')
        files = [m.replace('.', '/') + '.py' for m in discover_modules(ROOT, excluded=cfg['norecursedirs'].split())]
        cls.expected = set(files)
        cls.recorded_errors = {
            e['file'] for e in json.loads((ROOT / 'tests' / 'known_failures.json').read_text(encoding='utf-8'))['collection_errors']}
        cls.default_rc, cls.default_out, cls.default_nodes, cls.default_errors = cls.collect([])
        cls.explicit_rc, cls.explicit_out, cls.explicit_nodes, cls.explicit_errors = cls.collect(files)
        cls.collected = {n.split('::', 1)[0] for n in cls.default_nodes}

    def test_collection_errors_are_exactly_the_recorded_ones(self):
        """A new unimportable module fails here; a fixed one must be removed from the tracker."""
        self.assertEqual(sorted(self.default_errors), sorted(self.recorded_errors), self.default_out[-2000:])
        self.assertEqual(sorted(self.explicit_errors), sorted(self.recorded_errors), self.explicit_out[-2000:])
        expected_rc = 1 if self.recorded_errors else 0
        self.assertEqual((self.default_rc, self.explicit_rc), (expected_rc, expected_rc))

    def test_pytest_collects_every_discovered_file(self):
        self.assertEqual(sorted(self.expected - self.collected), sorted(self.recorded_errors),
                         'files pytest did not collect (other than recorded collection errors)')

    def test_pytest_collects_nothing_outside_discovered_files(self):
        self.assertEqual(sorted(self.collected - self.expected), [], 'files only pytest collected')

    def test_default_run_and_explicit_list_select_the_same_tests(self):
        self.assertTrue(self.explicit_nodes, 'explicit list collected no tests')
        self.assertEqual(sorted(self.explicit_nodes ^ self.default_nodes), [],
                         'default run and explicit module list differ')


if __name__ == '__main__':
    unittest.main()
