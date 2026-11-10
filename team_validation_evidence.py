"""Hash the inputs to a validation run and evaluate conservative reuse.

No command is executed here. Missing/ambiguous snapshots fail closed. A change
to an existing README or docs text file can be ignored only after checking that
unchanged project code does not name it or broadly enumerate project files.
"""

import ast
import fnmatch
import hashlib
import hmac
import importlib.metadata
import json
import os
import platform
import re
import secrets
import shlex
import sys
from pathlib import Path


SKIP_DIRECTORIES = {'.git'}
# 2026-11-05 (M09 live run, user decision): the Claude builder's TEMP points here (team_adapters.scoped_test_temp)
# and the CLI rewrites its own cache files in it after every run, so counting it expired every evidence.
SCRATCH_DIRECTORIES = {'.local/test-tmp'}
SKIP_SUFFIXES = set()
MAX_FILES = 5000
MAX_BYTES = 256_000_000
SAFE_STDLIB_IMPORTS = {
    'abc', 'bisect', 'collections', 'collections.abc', 'csv', 'dataclasses',
    'decimal', 'enum', 'fnmatch', 'fractions', 'functools', 'hashlib', 'hmac', 'heapq',
    'io', 'itertools', 'json', 'math', 'numbers', 'operator', 'os', 'pathlib',
    'platform', 're', 'statistics', 'string', 'sys', 'tempfile', 'textwrap',
    '__future__', 'os.path', 'time', 'typing', 'unittest', 'unittest.mock',
    'secrets', 'importlib', 'importlib.metadata',
}



_ENVIRONMENT_HMAC_KEY = secrets.token_bytes(32)
ENVIRONMENT_KEY_FILE = 'evidence-environment.key'


def use_data_directory_key(directory):
    """Keep the environment fingerprint key in the data folder, so evidence outlives a restart.

    A key made anew by every process expired every evidence whose tests start another process
    (environment_scope 'all'), although nothing had changed; after a restart the request could then
    not be confirmed or received. The key only hides the environment values behind the fingerprint.
    It stays on this PC: the data folder is outside Git, and it must not be copied into exports.
    """
    global _ENVIRONMENT_HMAC_KEY
    path = Path(directory) / ENVIRONMENT_KEY_FILE
    try:
        with open(path, 'xb') as handle:
            handle.write(secrets.token_bytes(32))
    except FileExistsError:
        pass
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError('検証証跡の鍵ファイルを読み取れません: ' + ENVIRONMENT_KEY_FILE)
    _ENVIRONMENT_HMAC_KEY = key
_INLINE_ENV_ASSIGNMENT = re.compile(r"(?im)(?:^|[;&|]\s*|\benv\s+)(?:\$env:)?[A-Z_][A-Z0-9_]*\s*=\s*\S+")


def _project_module_exists(root, module):
    if not module:
        return False
    candidate = root.joinpath(*module.split('.'))
    return candidate.with_suffix('.py').is_file() or (candidate / '__init__.py').is_file()


def _environment_dependencies(project, excluded=None):
    root = Path(project).resolve()
    names, all_values = set(), False
    stdlib = getattr(sys, 'stdlib_module_names', set())
    for path in sorted(root.rglob('*.py')):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or any(part in SKIP_DIRECTORIES or part == '__pycache__' for part in Path(relative).parts):
            continue
        if excluded and (relative == excluded or relative.startswith(excluded.rstrip('/') + '/')):
            continue
        try:
            if path.stat().st_size > 2_000_000:
                all_values = True
                continue
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=relative)
        except (OSError, UnicodeError, SyntaxError, ValueError):
            all_values = True
            continue
        os_aliases, getenv_aliases, environ_aliases = set(), set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split('.', 1)[0]
                    if alias.name == 'os':
                        os_aliases.add(alias.asname or 'os')
                    elif top in {'subprocess', 'multiprocessing'}:
                        all_values = True
                    elif top not in stdlib and not _project_module_exists(root, alias.name):
                        all_values = True
            elif isinstance(node, ast.ImportFrom):
                module, top = node.module or '', (node.module or '').split('.', 1)[0]
                if module == 'os':
                    for alias in node.names:
                        if alias.name == 'getenv':
                            getenv_aliases.add(alias.asname or alias.name)
                        elif alias.name == 'environ':
                            environ_aliases.add(alias.asname or alias.name)
                elif node.level:
                    if module and not _project_module_exists(root, module):
                        all_values = True
                elif top in {'subprocess', 'multiprocessing'}:
                    all_values = True
                elif top not in stdlib and not _project_module_exists(root, module):
                    all_values = True
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in getenv_aliases:
                    arg = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg == 'key'), None)
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        names.add(arg.value)
                    else:
                        all_values = True
                elif isinstance(func, ast.Attribute):
                    owner = func.value
                    os_owner = isinstance(owner, ast.Name) and owner.id in os_aliases
                    env_owner = ((isinstance(owner, ast.Attribute) and owner.attr == 'environ'
                                  and isinstance(owner.value, ast.Name) and owner.value.id in os_aliases)
                                 or (isinstance(owner, ast.Name) and owner.id in environ_aliases))
                    if os_owner and func.attr == 'getenv':
                        arg = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg == 'key'), None)
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            names.add(arg.value)
                        else:
                            all_values = True
                    elif os_owner and (func.attr in {'system', 'popen', 'putenv', 'unsetenv'} or func.attr.startswith('exec')):
                        all_values = True
                    elif env_owner and func.attr in {'get', 'setdefault', 'pop'}:
                        arg = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg == 'key'), None)
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            names.add(arg.value)
                        else:
                            all_values = True
                    elif env_owner:
                        all_values = True
                if isinstance(func, ast.Name) and func.id in {'eval', 'exec', '__import__'}:
                    all_values = True
                if isinstance(func, ast.Name) and func.id == 'getattr' and node.args and isinstance(node.args[0], ast.Name) and node.args[0].id in os_aliases:
                    all_values = True
            elif isinstance(node, ast.Subscript):
                owner = node.value
                is_env = ((isinstance(owner, ast.Attribute) and owner.attr == 'environ'
                           and isinstance(owner.value, ast.Name) and owner.value.id in os_aliases)
                          or (isinstance(owner, ast.Name) and owner.id in environ_aliases))
                if is_env:
                    if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                        names.add(node.slice.value)
                    else:
                        all_values = True
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in os_aliases and node.attr == 'environ':
                parent = parents.get(node)
                if not ((isinstance(parent, ast.Subscript) and parent.value is node)
                        or (isinstance(parent, ast.Attribute) and parent.value is node and parent.attr in {'get', 'setdefault', 'pop'})):
                    all_values = True
            elif isinstance(node, ast.Name) and node.id in environ_aliases:
                parent = parents.get(node)
                if not ((isinstance(parent, ast.Subscript) and parent.value is node)
                        or (isinstance(parent, ast.Attribute) and parent.value is node and parent.attr in {'get', 'setdefault', 'pop'})):
                    all_values = True
    if all_values:
        return 'all', ()
    return ('selected', tuple(sorted(names))) if names else ('none', ())


def _environment(dependencies=None):
    try:
        packages = sorted((str(dist.metadata.get('Name', '')).casefold(), str(dist.version))
                          for dist in importlib.metadata.distributions())
    except Exception as exc:
        raise ValueError('実行環境の依存パッケージ情報を確認できません。') from exc
    values = {'platform': platform.platform(), 'python': sys.version,
              'executable': sys.executable, 'virtual_env': os.environ.get('VIRTUAL_ENV', ''),
              'pythonpath': os.environ.get('PYTHONPATH', ''), 'path': os.environ.get('PATH', ''),
              'python_packages': packages}
    scope, names = dependencies or ('none', ())
    if scope == 'all':
        selected = sorted(os.environ.items(), key=lambda item: item[0].casefold())
    elif scope == 'selected':
        selected = [(name, os.environ.get(name)) for name in names]
    elif scope == 'none':
        selected = []
    else:
        raise ValueError('環境変数の依存範囲を確認できません。')
    values['environment_scope'] = scope
    if scope == 'none':
        # No application environment values are in scope, so a process-local key
        # must not make otherwise reusable evidence expire after a restart.
        values['environment_values_hmac'] = ''
    else:
        payload = json.dumps(selected, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        values['environment_values_hmac'] = hmac.new(_ENVIRONMENT_HMAC_KEY, payload, hashlib.sha256).hexdigest()
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode('utf-8')).hexdigest()

def _digest(files, environment, excluded, validation_commands, environment_scope):
    body = json.dumps({'files': files, 'environment': environment, 'excluded': excluded,
                       'validation_commands': validation_commands, 'environment_scope': environment_scope},
                      sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(body).hexdigest()


def snapshot(project, exclude_directory=None, validation_commands=None):
    """Capture a bounded, deterministic version of project inputs and test commands."""
    root = Path(project).resolve()
    validation_commands = [] if validation_commands is None else validation_commands
    if (not isinstance(validation_commands, list) or len(validation_commands) > 3
            or any(not isinstance(command, str) or not command.strip() or len(command) > 500
                   for command in validation_commands)):
        raise ValueError('検証コマンド情報を確認できません。')
    validation_commands = [command.strip() for command in validation_commands]
    if any(_INLINE_ENV_ASSIGNMENT.search(command) for command in validation_commands):
        raise ValueError('環境変数を値付きで含むコマンドは証跡へ保存できません。')
    if not root.is_dir():
        raise ValueError('検証対象のプロジェクトが存在しません。')
    excluded = None
    if exclude_directory is not None:
        candidate = Path(exclude_directory)
        candidate = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        if candidate.is_relative_to(root):
            excluded = candidate.relative_to(root).as_posix()
    files = {}
    total = 0
    for base, directories, names in os.walk(root, followlinks=False):
        current = Path(base)
        directories[:] = sorted(name for name in directories
                                if name not in SKIP_DIRECTORIES
                                and (current / name).relative_to(root).as_posix() not in SCRATCH_DIRECTORIES
                                and (excluded is None or (current / name).relative_to(root).as_posix() != excluded))
        if any((current / name).is_symlink() for name in directories):
            raise ValueError('検証対象にシンボリックリンクが含まれます。')
        for name in sorted(names):
            path = current / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise ValueError('検証対象にシンボリックリンクが含まれます。')
            if path.suffix.casefold() in SKIP_SUFFIXES:
                continue
            before = path.stat()
            total += before.st_size
            if len(files) >= MAX_FILES or total > MAX_BYTES:
                raise ValueError('検証対象が追跡上限を超えました。証跡を再利用できません。')
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('検証対象が記録中に変更されました。')
    environment_scope, environment_names = _environment_dependencies(root, excluded)
    environment = _environment((environment_scope, environment_names))
    return {'files': files, 'environment': environment, 'environment_scope': environment_scope,
            'excluded': excluded, 'validation_commands': validation_commands,
            'digest': _digest(files, environment, excluded, validation_commands, environment_scope)}


def _document_candidate(path):
    return path == 'README.md' or (path.startswith('docs/') and
                                   path.casefold().endswith(('.md', '.txt')))


def _path_matches_changed(path, changed):
    value = str(path).casefold().replace('\\', '/').lstrip('./')
    return any(fnmatch.fnmatchcase(candidate.casefold().replace('\\', '/'), value)
               or fnmatch.fnmatchcase(Path(candidate).name.casefold(), value)
               for candidate in changed)


def _python_might_read_changed_documents(root, relative, body, changed):
    """Conservatively reject reuse if Python can read changed files or dependency scope is unknown."""
    if '.git' in body.casefold():
        return True
    try:
        tree = ast.parse(body, filename=relative)
    except (SyntaxError, ValueError):
        return True

    module_aliases = {}
    imported_names = {}
    alias_collision = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split('.')[0]
                if local in module_aliases and module_aliases[local] != alias.name:
                    alias_collision = True
                module_aliases[local] = alias.name
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                local = alias.asname or alias.name
                origin = (node.module or '', alias.name)
                if local in imported_names and imported_names[local] != origin:
                    alias_collision = True
                imported_names[local] = origin

    if alias_collision:
        return True

    def local_module(module):
        if not module:
            return False
        path = root.joinpath(*module.split('.'))
        return path.with_suffix('.py').is_file() or (path / '__init__.py').is_file()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name
                if module not in SAFE_STDLIB_IMPORTS and not local_module(module):
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                return True
            module = node.module or ''
            if module not in SAFE_STDLIB_IMPORTS and not local_module(module):
                return True

    glob_names = {name for name, (module, original) in imported_names.items()
                  if module == 'glob' and original in ('glob', 'iglob')}
    directory_names = {name for name, (module, original) in imported_names.items()
                       if module == 'os' and original in ('listdir', 'scandir', 'walk', 'fwalk')}
    open_names = {'open'} | {name for name, (module, original) in imported_names.items()
                            if module in ('builtins', 'io', 'os') and original == 'open'}
    file_status_names = {'stat', 'lstat', 'exists', 'is_file', 'is_dir', 'is_symlink',
                         'samefile', 'getsize', 'getmtime', 'getctime', 'getatime',
                         'access', 'readlink'}
    file_status_aliases = {name for name, (module, original) in imported_names.items()
                           if module in ('os', 'os.path') and original in file_status_names}
    process_names = {name for name, (module, original) in imported_names.items()
                     if module in ('subprocess', 'os') and original in
                     ('run', 'Popen', 'call', 'check_call', 'check_output', 'getoutput',
                      'getstatusoutput', 'system', 'popen', 'spawnv', 'spawnve', 'execv', 'execve')}
    dynamic_names = {'__import__', 'eval', 'exec', 'getattr', 'globals', 'locals', 'vars'}
    dynamic_names |= {name for name, (module, original) in imported_names.items()
                      if module in ('builtins', 'importlib') and original in dynamic_names}

    def dotted(node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = dotted(node.value)
            return f'{prefix}.{node.attr}' if prefix else node.attr
        return ''

    def literal_path(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if (isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))
                and (dotted(node.func).split('.')[-1] == 'Path') and node.args
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            return node.args[0].value
        return None

    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in glob_names | directory_names | open_names:
            parent = parents.get(node)
            if not (isinstance(parent, ast.Call) and parent.func is node):
                return True
        if isinstance(node, ast.Attribute) and node.attr in (
                'glob', 'iglob', 'rglob', 'iterdir', 'listdir', 'scandir', 'walk',
                'fwalk', 'read_text', 'read_bytes', 'open', 'stat', 'lstat', 'exists',
                'is_file', 'is_dir', 'is_symlink', 'samefile', 'getsize', 'getmtime',
                'getctime', 'getatime', 'access', 'readlink'):
            parent = parents.get(node)
            if not (isinstance(parent, ast.Call) and parent.func is node):
                return True
        if not isinstance(node, ast.Call):
            continue
        function = dotted(node.func)
        parts = function.split('.')
        root_name = parts[0] if parts else ''
        module = module_aliases.get(root_name, '')
        name = parts[-1] if parts else ''

        if function in dynamic_names or (isinstance(node.func, ast.Name) and node.func.id in dynamic_names):
            return True
        if module == 'importlib' or name in ('import_module', 'spec_from_file_location'):
            return True
        if name in ('system', 'popen', 'spawnv', 'spawnve', 'execv', 'execve') and module in ('os', 'subprocess'):
            return True
        if module == 'subprocess' or (isinstance(node.func, ast.Name) and node.func.id in process_names):
            return True
        if isinstance(node.func, ast.Name) and node.func.id in directory_names:
            return True
        if ((isinstance(node.func, ast.Name) and node.func.id in file_status_aliases)
                or (name in file_status_names and module in ('os', 'os.path'))):
            paths = [literal_path(arg) for arg in node.args]
            if not paths or any(path is None or _path_matches_changed(path, changed) for path in paths):
                return True
        if name in ('listdir', 'scandir', 'walk', 'fwalk', 'iterdir'):
            return True

        is_glob = (name in ('glob', 'iglob', 'rglob')
                   or (isinstance(node.func, ast.Name) and node.func.id in glob_names))
        if is_glob:
            pattern = node.args[0] if node.args else None
            if not isinstance(pattern, ast.Constant) or not isinstance(pattern.value, str):
                return True
            if _path_matches_changed(pattern.value, changed):
                return True
            continue

        is_open = ((isinstance(node.func, ast.Name) and node.func.id in open_names)
                   or (name == 'open' and module in ('builtins', 'io', 'os')))
        if is_open:
            path = literal_path(node.args[0]) if node.args else None
            if path is None or _path_matches_changed(path, changed):
                return True

        if isinstance(node.func, ast.Attribute) and name in (
                'read_text', 'read_bytes', 'open', 'stat', 'lstat', 'exists', 'is_file',
                'is_dir', 'is_symlink', 'samefile', 'readlink'):
            path = literal_path(node.func.value)
            if path is None or _path_matches_changed(path, changed):
                return True
    return False


def _safe_unittest_command(root, command):
    """Allow reuse only for a plain Python unittest command over tracked project files."""
    try:
        tokens = [token.strip('"\'') for token in shlex.split(command, posix=False)]
    except ValueError:
        return False
    if not tokens:
        return False
    executable = Path(tokens[0].replace('\\', '/')).name.casefold()
    if executable not in ('python', 'python.exe', 'py', 'py.exe'):
        return False
    if any(any(mark in token for mark in ('|', '&', ';', '>', '<', '$(', '`')) for token in tokens):
        return False
    try:
        module_index = tokens.index('-m', 1)
    except ValueError:
        return False
    if module_index + 1 >= len(tokens) or tokens[module_index + 1] != 'unittest':
        return False
    safe_flags = {'-B', '-E', '-I', '-O', '-OO', '-P', '-s', '-S', '-u', '-v', '-q', '-x'}
    index = 1
    while index < module_index:
        token = tokens[index]
        if token in safe_flags:
            index += 1
        elif token in ('-X', '-W') and index + 1 < module_index:
            index += 2
        else:
            return False
    args = tokens[module_index + 2:]
    if args and args[0] == 'discover':
        start = root
        i = 1
        while i < len(args):
            token = args[i]
            if token in ('-s', '--start-directory', '-t', '--top-level-directory'):
                if i + 1 >= len(args):
                    return False
                candidate = (root / args[i + 1]).resolve()
                if not candidate.is_relative_to(root) or not candidate.is_dir():
                    return False
                if token in ('-s', '--start-directory'):
                    start = candidate
                i += 2
            elif token in ('-p', '--pattern'):
                if i + 1 >= len(args) or not args[i + 1].casefold().endswith('.py'):
                    return False
                i += 2
            elif token in ('-k', '--failfast', '-f', '--catch', '-c', '--buffer', '-b', '-v', '--verbose', '-q', '--quiet'):
                if token == '-k':
                    if i + 1 >= len(args):
                        return False
                    i += 2
                else:
                    i += 1
            else:
                return False
        return start.is_relative_to(root)

    i = 0
    while i < len(args):
        token = args[i]
        if token in ('-v', '--verbose', '-q', '--quiet', '-f', '--failfast', '-c', '--catch', '-b', '--buffer', '--locals'):
            i += 1
            continue
        if token in ('-k',):
            if i + 1 >= len(args):
                return False
            i += 2
            continue
        if token.startswith('-'):
            return False
        module = token.split(':', 1)[0].replace('.', '/')
        candidate = root.joinpath(*module.split('/'))
        if not (candidate.with_suffix('.py').is_file() or (candidate / '__init__.py').is_file()):
            return False
        i += 1
    return True


def _independent_documents(root, changed, files, validation_commands):
    if any(not _document_candidate(path) for path in changed):
        return False
    names = {part.casefold() for path in changed for part in (path, Path(path).name)}
    if any(path.startswith('docs/') for path in changed):
        names.add('docs')
    if not validation_commands or any(not _safe_unittest_command(root, command)
                                      for command in validation_commands):
        return False
    # Reuse is limited to Python projects whose tracked code can be parsed;
    # unknown languages and configuration inputs are not proven independent.
    for relative in files:
        if _document_candidate(relative):
            continue
        suffix = Path(relative).suffix.casefold()
        if suffix != '.py':
            return False
        path = root / relative
        if path.stat().st_size > 2_000_000:
            return False
        try:
            body = path.read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            return False
        if any(name in body.casefold() for name in names):
            return False
        if _python_might_read_changed_documents(root, relative, body, changed):
            return False
    return True


def validity(project, recorded, validation_commands=None):
    """Return (valid, reason, reused_documents) for a persisted snapshot."""
    if not isinstance(recorded, dict) or not isinstance(recorded.get('files'), dict) \
            or not isinstance(recorded.get('environment'), str) \
            or recorded.get('environment_scope') not in {'none', 'selected', 'all'}:
        return False, '検証証跡が欠損または破損しています。', False
    files = recorded['files']
    saved_commands = recorded.get('validation_commands')
    if (not isinstance(saved_commands, list) or len(saved_commands) > 3
            or any(not isinstance(command, str) or not command.strip() or len(command) > 500
                   for command in saved_commands)):
        return False, '検証コマンドを含む証跡が欠損または不正です。', False
    expected_commands = [command.strip() for command in saved_commands]
    if validation_commands is not None:
        if (not isinstance(validation_commands, list) or
                [str(command).strip() for command in validation_commands] != expected_commands):
            return False, '検証コマンドが記録時から変わりました。', False
    if any(not isinstance(name, str) or not isinstance(digest, str)
           or not re.fullmatch('[0-9a-f]{64}', digest) for name, digest in files.items()):
        return False, '検証証跡のファイル情報が不正です。', False
    if recorded.get('digest') != _digest(files, recorded['environment'], recorded.get('excluded'),
                                         expected_commands, recorded['environment_scope']):
        return False, '検証証跡の整合性が崩れています。', False
    excluded = recorded.get('excluded')
    if excluded is not None and (not isinstance(excluded, str)
                                 or not (Path(project).resolve() / excluded / 'team.sqlite3').is_file()):
        return False, '検証対象の除外設定が不正です。', False
    try:
        current = snapshot(project, recorded.get('excluded'), expected_commands)
    except (OSError, ValueError) as exc:
        return False, str(exc), False
    if (current['environment'] != recorded['environment']
            or current['environment_scope'] != recorded['environment_scope']):
        return False, '検証実行時から実行環境または環境依存範囲が変わりました。', False
    if current['excluded'] != recorded.get('excluded') or set(current['files']) != set(files):
        return False, '検証対象のファイルが追加または削除されました。', False
    changed = [name for name in files if current['files'][name] != files[name]]
    if not changed:
        return True, '検証対象の版が一致します。', False
    if _independent_documents(Path(project).resolve(), changed, files, expected_commands):
        return True, '独立した説明資料だけが変更されました。', True
    return False, 'コード・テスト・設定・依存資料の変更、または資料の独立性を確認できません。', False
