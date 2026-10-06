"""Fail-closed approval of a small set of static, project-local read commands.

Parse PowerShell syntax without executing it. Unrecognized commands remain manual.
This does not modify Codex permissions, sandbox settings, or persistent CLI rules.
"""
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import fnmatch
import subprocess


def _script(details):
    # App Server's command is a display string, not Windows argv serialization.
    # Require exact agreement with its one parsed action, including the whole wrapper.
    actions = details.get('commandActions', [])
    if len(actions) != 1 or not isinstance(actions[0].get('command'), str):
        raise ValueError('ambiguous command')
    script = actions[0]['command']
    match = re.fullmatch(r'"([^"]+)" -Command ([\s\S]+)', details['command'])
    if not match:
        raise ValueError('wrapper')
    quoted = match[2]
    candidates = {'"' + script.replace('\\', '\\\\').replace('"', '\\"') + '"'}
    if "'" not in script:
        candidates.add("'" + script + "'")
    if quoted not in candidates:
        # Display quoting is lossy for regex $ and mixed quotes. Use the exact
        # argv only when App Server repeats it in the proposed decision AND it
        # agrees with the single parsed action and the displayed executable.
        # We only return a one-off accept; never install this proposed policy.
        argv = details.get('proposedExecpolicyAmendment')
        decisions = details.get('availableDecisions') or []
        mirrored = any(isinstance(d, dict) and
            isinstance(d.get('acceptWithExecpolicyAmendment'), dict) and
            d.get('acceptWithExecpolicyAmendment', {}).get('execpolicy_amendment') == argv
            for d in decisions)
        if (not isinstance(argv, list) or len(argv) != 3
                or not all(isinstance(v, str) for v in argv)
                or argv[1] != '-Command' or argv[2] != script
                or Path(argv[0]).resolve() != Path(match[1]).resolve() or not mirrored):
            raise ValueError('wrapper disagrees with action')
    return Path(match[1]).resolve(), script


def _path(value, root):
    # No wildcard, provider, ADS, device path, parent traversal or sensitive path.
    if not value or any(c in value for c in '*?[]$`') or value.startswith(('\\\\', '//')):
        return False
    p = Path(value)
    if ':' in value[2:] or (':' in value and not p.is_absolute()):
        return False
    protected = {'.git', '.codex', '.claude', '.ssh', 'auth.json', '.credentials.json'}
    if any(x.lower() in protected or x.lower().startswith('.env') or x == '..' for x in p.parts):
        return False
    resolved = (p if p.is_absolute() else root / p).resolve()
    return resolved.is_relative_to(root) and not any(
        x.lower() in protected or x.lower().startswith('.env') for x in resolved.relative_to(root).parts)


def _command(parts, root, downstream):
    if not parts:
        return False
    name, args = parts[0].lower(), parts[1:]
    if downstream:
        if name == 'rg':
            return _command(parts, root, False)
        if (name == 'select-object' and len(args) == 2
                and args[0].lower() in ('-expandproperty', '-property')
                and args[1].lower() in ('source', 'path', 'name', 'fullname', 'commandtype')):
            return True
        # Only simple output slicing; expressions and script blocks were rejected by AST.
        return (name == 'select-object' and bool(args) and len(args) % 2 == 0
                and all(args[i].lower() in ('-first', '-last', '-skip') and args[i+1].isdigit()
                        for i in range(0, len(args), 2)))
    if name == 'get-location':
        return not args
    if name in ('get-command', 'where.exe'):
        # Native executable names only. No module discovery, arbitrary paths or recursive search.
        if not args or not re.fullmatch(r'[A-Za-z0-9_.-]+\.exe', args[0], re.IGNORECASE):
            return False
        rest = args[1:]
        if name == 'where.exe':
            return not rest
        return (len(rest) % 2 == 0 and all(
            (rest[i].lower() == '-erroraction' and rest[i+1].lower() in ('silentlycontinue', 'stop', 'continue'))
            or (rest[i].lower() == '-commandtype' and rest[i+1].lower() == 'application')
            for i in range(0, len(rest), 2)))
    if name == 'git':
        return _git_read(args, root)
    if name in ('get-content', 'get-childitem'):
        paths, i = [], 0
        while i < len(args):
            a = args[i].lower()
            if a in ('-path', '-literalpath'):
                i += 1
                if i == len(args): return False
                paths.append(args[i])
            elif a in ('-totalcount', '-tail') and name == 'get-content':
                i += 1
                if i == len(args) or not args[i].isdigit(): return False
            elif a == '-name' and name == 'get-childitem':
                pass
            elif a.startswith('-'):
                return False
            else:
                paths.append(args[i])
            i += 1
        return len(paths) == 1 and _path(paths[0], root)
    if name != 'rg':
        return False
    # No --pre, --hostname-bin, --follow, --hidden, unrestricted search, etc.
    flags = {'-n', '--line-number', '-l', '--files-with-matches', '-i', '--ignore-case',
             '-s', '--case-sensitive', '-F', '--fixed-strings', '--files', '--count', '-c',
             '--no-heading', '--heading', '--no-messages', '--no-config'}
    values = {'-g', '--glob', '-e', '--regexp', '-A', '-B', '-C', '--context',
              '--after-context', '--before-context', '-m', '--max-count', '-t', '--type'}
    positional, has_pattern, i = [], False, 0
    while i < len(args):
        a = args[i]
        if a in values:
            i += 1
            if i == len(args): return False
            v = args[i]
            if a in ('-g', '--glob') and (v.startswith('!') or any(x in v.lower() for x in ('.env', '.git', '.ssh', '.codex', '.claude', 'auth.json', 'credentials'))):
                return False
            if a in ('-e', '--regexp'): has_pattern = True
        elif a in flags:
            pass
        elif a.startswith('-'):
            return False
        else:
            positional.append(a)
        i += 1
    if '--files' not in args and not has_pattern:
        if not positional: return False
        positional.pop(0)  # literal regex, not a filesystem path
    return all(_path(p, root) for p in positional)


def _git_read(args, root):
    """Only diff/status. Reject executable Git customizations and alternate roots."""
    if not args or args[0] not in ('diff', 'status'):
        return False
    options, paths = args[1:], []
    if '--' in options:
        cut = options.index('--')
        options, paths = options[:cut], options[cut+1:]
    allowed = ({'--stat', '--name-only', '--name-status', '--numstat', '--shortstat',
                '--cached', '--staged', '--no-ext-diff', '--no-textconv', '--check',
                '--no-color', '--color=never', '--exit-code', '--quiet'} if args[0] == 'diff'
               else {'--short', '-s', '--branch', '-b', '-sb', '--porcelain', '--porcelain=v1', '--porcelain=v2'})
    if any(o not in allowed for o in options) or any(not _path(p, root) for p in paths):
        return False
    # Environment overrides can change both the target repository and executables.
    if any(k.startswith('GIT_') and k not in ('GIT_OPTIONAL_LOCKS', 'GIT_TERMINAL_PROMPT', 'GIT_PAGER')
           for k in os.environ):
        return False
    if os.environ.get('GIT_PAGER', '') not in ('', 'cat'):
        return False
    git = shutil.which('git')
    if not git:
        return False
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
    def read(arguments, input_text=None):
        return subprocess.run([git, '--no-pager'] + arguments, cwd=root, env=env,
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            input=input_text, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
    top = read(['rev-parse', '--show-toplevel'])
    if top.returncode or Path(top.stdout.strip()).resolve() != root:
        return False
    config = read(['config', '--get-regexp', r'^(diff\..*\.(command|textconv)|diff\.external|core\.fsmonitor)$'])
    if config.returncode not in (0, 1):
        return False
    external_drivers = set()
    for line in config.stdout.splitlines():
        key, _, value = line.partition(' ')
        if key == 'core.fsmonitor' and value.lower() in ('false', '0', 'off', 'no'):
            continue
        if key.startswith('diff.') and key.endswith(('.command', '.textconv')):
            external_drivers.add(key[5:].rsplit('.', 1)[0])
            continue
        if key == 'core.fsmonitor' or (key == 'diff.external' and args[0] == 'diff'):
            return False
    # Unscoped diff can expose tracked secrets: inspect its filenames first.
    if args[0] == 'diff':
        names = read(['diff', '--no-ext-diff', '--no-textconv', '--name-only', '-z']
                     + (['--cached'] if any(x in options for x in ('--cached', '--staged')) else [])
                     + ['--'] + paths)
        if names.returncode or any(not _path(p, root) for p in names.stdout.split('\0') if p):
            return False
        if external_drivers and names.stdout:
            attrs = read(['check-attr', '-z', '--stdin', 'diff'], names.stdout)
            fields = attrs.stdout.rstrip('\0').split('\0')
            if attrs.returncode or len(fields) % 3 or any(v in external_drivers for v in fields[2::3]):
                return False
    return True


def auto_read_reason(payload, project):
    if payload.get('source') == 'claude' and payload.get('operation') == 'Bash':
        return claude_git_read_reason(payload, project)
    if os.name != 'nt' or payload.get('source') != 'codex' or payload.get('operation') != 'item/commandExecution/requestApproval':
        return None
    try:
        details = payload.get('details') or {}
        root = Path(project).resolve()
        if Path(details.get('cwd', '')).resolve() != root:
            return None
        # Any explicit escalation/network/permission request remains manual.
        if any(details.get(k) for k in ('reason', 'networkApprovalContext', 'additionalPermissions')):
            return None
        exe, script = _script(details)
        system_ps = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        bundled_ps = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe'
        if exe not in (system_ps.resolve(), bundled_ps.resolve()):
            return None
        if len(script) > 16000:
            return None
        # rg config can inject processing programs; leave such environments manual.
        if os.environ.get('RIPGREP_CONFIG_PATH'):
            return None
        parser = Path(__file__).with_name('read_command_ast.ps1').read_text(encoding='utf-8-sig')
        result = subprocess.run([str(system_ps), '-NoProfile', '-NonInteractive', '-Command', parser],
                                input=script, capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            return None
        pipelines = json.loads(result.stdout).get('pipelines', [])
        if not pipelines or len(pipelines) > 12:
            return None
        if all(p and all(_command(c, root, i > 0) for i, c in enumerate(p)) for p in pipelines):
            return '対象プロジェクトの検索・読み取りを自動承認（静的構文と対象パスを確認）'
    except (ValueError, TypeError, KeyError, OSError, subprocess.SubprocessError):
        pass
    return None


def claude_git_read_reason(payload, project):
    """Project-local read policy for Claude Bash; no shell evaluation."""
    if os.name != 'nt' or payload.get('source') != 'claude' or payload.get('operation') != 'Bash':
        return None
    try:
        root = Path(project).resolve()
        cwd = payload.get('cwd')
        if not isinstance(cwd, str) or not cwd or not _path(cwd, root) or not Path(cwd).is_dir():
            return None
        script = (payload.get('details') or {}).get('command')
        if not isinstance(script, str) or len(script) > 1000:
            return None
        if _bash_search(script, root, Path(cwd).resolve()):
            return 'プロジェクト内の読み取りを自動承認（検索・行数・Git確認とその組み合わせ）'
    except (ValueError, TypeError, OSError, subprocess.SubprocessError):
        pass
    return None


def _bash_search(script, root, initial_cwd=None):
    """Validate every leaf of a bounded read-only shell sequence without executing it."""
    if any(c in script for c in '$`<>\r\n') or len(script) > 1000:
        return False
    lexer = shlex.shlex(script, posix=True, punctuation_chars=';&|<>()')
    lexer.whitespace_split = True
    lexer.commenters = ''
    tokens = []
    for token in lexer:
        # shlex merges adjacent punctuation (e.g. ");"); split it into known operators only.
        if token and all(c in ';&|<>()' for c in token):
            pieces = re.findall(r'&&|\|\||[;|()]|.', token)
            if any(p not in ('&&', '||', ';', '|', '(', ')') for p in pieces):
                return False
            tokens.extend(pieces)
        else:
            tokens.append(token)
    # Subshell groups and "||" can leave cd state unknown, so cd is refused with them.
    grouped = any(t in ('(', ')', '||') for t in tokens)
    chunks, part, before, depth, closed = [], [], None, 0, False
    for token in tokens:
        if token in ('&&', '||', ';', '|'):
            if not part:
                return False
            chunks.append((before, part, token));part=[];before=token;closed=False
        elif token == '(':
            if part or closed:
                return False
            depth += 1
        elif token == ')':
            if not part or depth == 0:
                return False
            depth -= 1;closed=True
        elif closed:
            return False
        else:
            part.append(token)
    if not part or depth:
        return False
    chunks.append((before, part, None))
    if len(chunks) > 12:
        return False
    cwd = initial_cwd or root
    def path(raw):
        if re.match(r'^/[A-Za-z]/', raw):
            raw = raw[1].upper()+':/'+raw[3:]
        candidate = Path(raw)
        candidate = candidate if candidate.is_absolute() else cwd / candidate
        return candidate.resolve() if not candidate.is_symlink() and _path(str(candidate), root) else None
    searched = False
    for preceding, words, following in chunks:
        name, args = words[0], words[1:]
        if name == 'cd':
            if grouped or preceding == '|' or following not in ('&&', ';') or len(args) != 1:
                return False
            target = path(args[0])
            if not target or not target.is_dir():
                return False
            cwd = target
        elif name == 'git':
            git_args=list(args)
            if '--' in git_args:
                cut=git_args.index('--')
                files=[path(raw) for raw in git_args[cut+1:]]
                if any(p is None for p in files):
                    return False
                git_args=git_args[:cut+1]+[str(p) for p in files]
            current=cwd
            while current!=root:
                if (current/'.git').exists():
                    return False
                current=current.parent
            if preceding == '|' or not _git_read(git_args, root):
                return False
            searched=True
        elif name == 'wc':
            options = [arg for arg in args if arg.startswith('-')]
            files = [arg for arg in args if not arg.startswith('-')]
            if any(not re.fullmatch(r'-[lwcmL]+', arg) for arg in options):
                return False
            if preceding == '|':
                if files:
                    return False
            else:
                if not files or len(files)>20:
                    return False
                for raw in files:
                    target=path(raw)
                    if not target or not target.is_file():
                        return False
                searched=True
        elif name == 'ls':
            # Names only; no recursion so protected folders are not listed.
            if preceding == '|' or any(a.startswith('-') and not re.fullmatch(r'-[1aAlhtrSF]+', a) for a in args):
                return False
            targets = [a for a in args if not a.startswith('-')]
            if len(targets) > 20 or any(not (path(raw) and path(raw).exists()) for raw in targets):
                return False
            searched=True
        elif name in ('python', 'python3', 'py') and args in (['--version'], ['-V']):
            if preceding == '|':
                return False
        elif name in ('cat', 'sed', 'head', 'tail') and preceding != '|':
            file_arg=None
            if name=='cat' and len(args)==1 and not args[0].startswith('-'):
                file_arg=args[0]
            elif name=='sed' and len(args)==3 and args[0]=='-n' and re.fullmatch(r'[1-9][0-9]{0,6}(?:,[1-9][0-9]{0,6})?p',args[1]):
                file_arg=args[2]
            elif name in ('head','tail') and len(args)==3 and args[0]=='-n' and args[1].isdigit() and 0<int(args[1])<=10000:
                file_arg=args[2]
            target=path(file_arg) if file_arg else None
            if not target or not target.is_file():
                return False
            searched=True
        elif name in ('head', 'tail') and preceding == '|':
            if args and not ((len(args)==1 and re.fullmatch(r'-[1-9][0-9]{0,3}',args[0])) or
                             (len(args)==2 and args[0]=='-n' and args[1].isdigit() and 0<int(args[1])<=10000)):
                return False
        elif name == 'grep':
            recursive, includes, positional = False, [], []
            for arg in args:
                if arg.startswith('--include='):
                    glob = arg[len('--include='):]
                    if not re.fullmatch(r'\*\.(?:cs|py|js|ts|tsx|jsx|html|css|md|txt)', glob):
                        return False
                    includes.append(glob)
                elif arg.startswith('-'):
                    if not re.fullmatch(r'-[rnliIvEF]+',arg):
                        return False
                    recursive |= 'r' in arg
                else:
                    positional.append(arg)
            if not positional:
                return False
            if preceding == '|':
                if len(positional)!=1 or recursive or includes:
                    return False
                continue
            if len(positional)<2:
                return False
            for raw in positional[1:]:
                target = path(raw)
                if not target or not target.exists():
                    return False
                if target.is_file():
                    continue
                if not recursive:
                    return False
                # grep -r may enter protected directories or follow a command-line
                # symlink. Check actual scope rather than assuming a glob hides secrets.
                count=0
                for folder, directories, files in os.walk(target, followlinks=False):
                    count += len(directories)+len(files)
                    if count > 30000:
                        return False
                    for entry in directories:
                        child=Path(folder)/entry
                        if child.is_symlink() or not _path(str(child),root):
                            return False
                    for entry in files:
                        child=Path(folder)/entry
                        if (not includes or any(fnmatch.fnmatchcase(entry,glob) for glob in includes)) and (child.is_symlink() or not _path(str(child),root)):
                            return False
            searched=True
        else:
            return False
    return searched
