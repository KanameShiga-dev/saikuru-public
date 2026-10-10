"""MCP server that lets GitHub Copilot work as the builder of a development job (2026-10-09, user decision).

Copilot CLI has no hook that asks 采来 before a tool runs, so its own edit/shell tools stay denied. Instead it gets
these tools. Reading uses the bounded read broker (consultation_read_tools). Every write, edit and command is first
sent to 采来's /worker/tool as Write / Edit / Bash, exactly like a Claude builder's PreToolUse hook, so the same
checks apply (project boundary, protected files, outbound guard, automatic or human approval, backups). Only when
采来 allows it does this server perform the operation. Anything unexpected denies.

Usage: python copilot_work_tools.py <project> <token-file>
The token file holds {"endpoint", "task_id", "token"}; it is read once at start (the token is never on a command line).
"""
import json
import os
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import consultation_read_tools as reader

OUTPUT_LIMIT = 30000
TOOLS = reader.TOOLS + [
    {'name': 'write_file', 'description': 'Create or overwrite a UTF-8 text file in the project (project-relative path). '
                                          '采来 checks and may ask the user before it is written.',
     'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}},
                     'required': ['path', 'content'], 'additionalProperties': False}},
    {'name': 'edit_file', 'description': 'Replace an exact text in a project file. old_string must match once unless '
                                         'replace_all is true. 采来 checks and may ask the user before it is changed.',
     'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'old_string': {'type': 'string'},
                                                      'new_string': {'type': 'string'}, 'replace_all': {'type': 'boolean'}},
                     'required': ['path', 'old_string', 'new_string'], 'additionalProperties': False}},
    {'name': 'run_command', 'description': 'Run one Windows PowerShell command in the project folder (no profile, '
                                           'non-interactive). 采来 checks it first; outbound, publishing and install '
                                           'commands need the user\'s approval, secrets are refused. Returns exit code '
                                           'and output (bounded).',
     'inputSchema': {'type': 'object', 'properties': {'command': {'type': 'string'},
                                                      'timeout_seconds': {'type': 'integer'}},
                     'required': ['command'], 'additionalProperties': False}},
]


class Denied(Exception):
    pass


def target(root, name):
    """Project path for a write/edit (采来 decides on protected files; this only keeps the path inside the project)."""
    path = Path(str(name or ''))
    path = (path if path.is_absolute() else root / path).resolve()
    if not path.is_relative_to(root) or path == root:
        raise Denied('対象プロジェクト外への変更です。')
    return path


def ask(config, tool, data):
    """采来's decision for one operation, as for a Claude builder. Any failure is a denial."""
    payload = json.dumps({'task_id': config['task_id'], 'tool': tool, 'input': data, 'cwd': config['root'],
                          'source': 'copilot'}).encode()
    request = urllib.request.Request(config['endpoint'] + '/worker/tool', data=payload,
                                     headers={'Content-Type': 'application/json',
                                              'Authorization': 'Bearer ' + config['token']})
    try:
        with urllib.request.urlopen(request, timeout=config.get('timeout', 930)) as response:
            answer = json.load(response)
    except Exception as exc:
        raise Denied('采来に確認できないため実行しません。') from exc
    if answer.get('allow') is not True:
        raise Denied(str(answer.get('note') or '采来が許可しませんでした。'))
    return str(answer.get('note') or '')


def report(config, command, value):
    """Send a command's result to 采来 for the reviewers' evidence (best effort; the run itself is not affected)."""
    payload = json.dumps({'task_id': config['task_id'], 'tool': 'Bash', 'command': command,
                          'exit_code': value.get('exit_code'), 'output': value.get('output', ''),
                          'timed_out': value.get('timed_out') is True}).encode()
    request = urllib.request.Request(config['endpoint'] + '/worker/tool-result', data=payload,
                                     headers={'Content-Type': 'application/json',
                                              'Authorization': 'Bearer ' + config['token']})
    try:
        with urllib.request.urlopen(request, timeout=30):
            pass
    except Exception:
        pass


def write_file(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='') as stream:
        stream.write(content)


def edit_file(path, old, new, replace_all):
    if not path.is_file():
        raise Denied('ファイルがありません。')
    with path.open('r', encoding='utf-8', newline='') as stream:
        text = stream.read()
    count = text.count(old) if old else 0
    if count == 0:
        raise Denied('old_string が見つかりません。')
    if count > 1 and not replace_all:
        raise Denied(f'old_string が{count}か所あります。一意になるよう前後を含めるか、replace_all を指定してください。')
    write_file(path, text.replace(old, new) if replace_all else text.replace(old, new, 1))
    return count if replace_all else 1


def run_command(root, command, timeout):
    env = {k: v for k, v in os.environ.items() if not k.startswith('AGENT_TEAM_')}
    # Python children write piped output in the ANSI code page (cp932) by default, which came back garbled through
    # the UTF-8 pipeline below (2026-10-09). Only stdio is changed; files a program reads or writes are not.
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONDONTWRITEBYTECODE'] = '1'  # no __pycache__ left in the project by test runs
    # Windows PowerShell writes redirected output in the console code page (Japanese came back garbled); use UTF-8.
    prefix = '[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;$OutputEncoding=[System.Text.Encoding]::UTF8;'
    process = subprocess.Popen(['powershell', '-NoProfile', '-NonInteractive', '-Command', prefix + command], cwd=root, env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               encoding='utf-8', errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # 2026-10-09: killing only PowerShell left its child (e.g. python) holding the pipes, so the call waited until
        # the child ended by itself. Stop the whole process tree, then collect what was written.
        timed_out = True
        stop_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stdout, stderr = '', ''
    output = (stdout or '') + (('\n[stderr]\n' + stderr) if stderr else '')
    from team_security_audit import mask
    try:
        output = mask(output)
    except (ValueError, OSError):
        output = '［保護設定を確認できないため出力は非表示です］'
    if timed_out:
        return {'exit_code': None, 'timed_out': True, 'output': output[-OUTPUT_LIMIT:]}
    return {'exit_code': process.returncode, 'output': output[-OUTPUT_LIMIT:], 'truncated': len(output) > OUTPUT_LIMIT}


def stop_tree(process):
    """Stop a process and everything it started (only that tree)."""
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=15)
    else:
        process.kill()


class Server:
    def __init__(self, root, config):
        self.root, self.config = root, dict(config, root=str(root))
        self.cancelled, self.lock, self.reads = set(), threading.Lock(), 0

    def send(self, answer):
        with self.lock:
            print(json.dumps(answer, ensure_ascii=False), flush=True)

    def call(self, request_id, name, args):
        """Work tools wait for 采来 (perhaps for the user); a request cancelled meanwhile is never performed."""
        try:
            if name in ('list_files', 'read_file', 'search_files', 'read_document'):
                self.reads += 1
                if self.reads > 200:
                    raise Denied('読み取り呼出上限に達しました。')
                value = reader.execute(self.root, name, args)
            elif name == 'write_file':
                path, content = target(self.root, args.get('path')), args.get('content')
                if not isinstance(content, str):
                    raise Denied('content は文字列です。')
                note = ask(self.config, 'Write', {'file_path': str(path), 'content': content})
                if request_id in self.cancelled:
                    raise Denied('取り消された要求のため実行しません。')
                if target(self.root, args.get('path')) != path or not path.resolve().is_relative_to(self.root):
                    raise Denied('承認待ち中に対象パスが変わったため実行しません。')
                write_file(path, content)
                value = {'path': str(path.relative_to(self.root)), 'written': True, 'note': note}
            elif name == 'edit_file':
                path = target(self.root, args.get('path'))
                old, new = args.get('old_string'), args.get('new_string')
                if not isinstance(old, str) or not isinstance(new, str):
                    raise Denied('old_string・new_string は文字列です。')
                note = ask(self.config, 'Edit', {'file_path': str(path), 'old_string': old, 'new_string': new,
                                                 'replace_all': args.get('replace_all') is True})
                if request_id in self.cancelled:
                    raise Denied('取り消された要求のため実行しません。')
                if target(self.root, args.get('path')) != path or not path.resolve().is_relative_to(self.root):
                    raise Denied('承認待ち中に対象パスが変わったため実行しません。')
                replaced = edit_file(path, old, new, args.get('replace_all') is True)
                value = {'path': str(path.relative_to(self.root)), 'replaced': replaced, 'note': note}
            elif name == 'run_command':
                command = args.get('command')
                if not isinstance(command, str) or not command.strip() or len(command) > 8000:
                    raise Denied('command を指定してください（8000文字まで）。')
                timeout = max(1, min(600, int(args.get('timeout_seconds') or 120)))
                note = ask(self.config, 'Bash', {'command': command, 'shell': 'powershell'})
                if request_id in self.cancelled:
                    raise Denied('取り消された要求のため実行しません。')
                value = dict(run_command(self.root, command, timeout), note=note)
                report(self.config, command, value)
            else:
                raise Denied('この道具はありません。')
            result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}]}
        except Denied as exc:
            result = {'content': [{'type': 'text', 'text': '拒否: ' + str(exc)}], 'isError': True}
        except Exception:
            result = {'content': [{'type': 'text', 'text': '拒否: 対象外のパス・操作、または上限です。'}], 'isError': True}
        self.send({'jsonrpc': '2.0', 'id': request_id, 'result': result})

    def serve(self, stream):
        for raw in stream:
            msg = {}
            try:
                if len(raw) > 2_000_000:
                    raise ValueError('Request too large')
                msg = json.loads(raw)
                method, params = msg.get('method'), msg.get('params') or {}
                if method == 'notifications/cancelled':
                    self.cancelled.add(params.get('requestId'))
                    continue
                if 'id' not in msg:
                    continue
                if method == 'initialize':
                    result = {'protocolVersion': params.get('protocolVersion', '2024-11-05'), 'capabilities': {'tools': {}},
                              'serverInfo': {'name': 'saikuru-project-work', 'version': '1.0'}}
                elif method == 'ping':
                    result = {}
                elif method == 'tools/list':
                    result = {'tools': TOOLS}
                elif method == 'tools/call':
                    # In a thread, so a later cancel notification can still be read while 采来 waits for the user.
                    threading.Thread(target=self.call, args=(msg['id'], params.get('name'), params.get('arguments') or {}),
                                     daemon=True).start()
                    continue
                else:
                    raise ValueError('Unsupported operation')
                self.send({'jsonrpc': '2.0', 'id': msg['id'], 'result': result})
            except Exception:
                self.send({'jsonrpc': '2.0', 'id': msg.get('id'), 'error': {'code': -32602, 'message': '対象外の操作です。'}})


def main():
    root = Path(sys.argv[1]).resolve(strict=True)
    config = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stdin.reconfigure(encoding='utf-8')
    Server(root, config).serve(sys.stdin)


if __name__ == '__main__':
    main()
