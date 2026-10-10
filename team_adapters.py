"""One narrow interface for two providers; no dashboard decisions in model output."""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tempfile
import uuid
from pathlib import Path


class Cancelled(Exception):
    pass


class ProviderError(Exception):
    def __init__(self, message, code='provider'):
        super().__init__(message)
        self.code = code


class Process:
    def __init__(self, command, cwd, env=None):
        self.proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding='utf-8', errors='replace', bufsize=1,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.lines = queue.Queue(maxsize=128)
        self.stderr = []
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        threading.Thread(target=self._errors, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            if len(line) < 512_000:
                self.lines.put(line)
        self.lines.put(None)

    def _errors(self):
        for line in self.proc.stderr:
            self.stderr.append(line[:1000])
            self.stderr = self.stderr[-10:]

    def send(self, value):
        self.proc.stdin.write(json.dumps(value, ensure_ascii=False) + '\n')
        self.proc.stdin.flush()

    def receive(self, ctx):
        while True:
            ctx.check()
            try:
                line = self.lines.get(timeout=.25)
                if line is None:
                    raise ProviderError('AI接続が終了しました。CLIの認証・モデル設定・起動条件を確認してください。')
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
            except queue.Empty:
                continue

    def close(self):
        if self.proc.poll() is None:
            if os.name == 'nt':
                # Only the process created by this adapter and its descendants.
                subprocess.run(['taskkill', '/PID', str(self.proc.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=15)
            else:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def decode_result(text):
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ProviderError('結果が指定のJSON形式ではありません。自動では次のタスクへ進めません。') from exc
    if not isinstance(value, dict):
        raise ProviderError('結果はJSONオブジェクトである必要があります。')
    return value


class CodexAdapter:
    def run(self, ctx, prompt, schema):
        profile = ctx.task['profile']
        command = ctx.command + ['app-server', '--listen', 'stdio://',
                                 '-c', 'service_tier="default"', '--disable', 'fast_mode',
                                 '--disable', 'multi_agent', '--disable', 'multi_agent_v2',
                                 '-c', 'model_reasoning_effort="' + profile['effort'] + '"']
        if not ctx.engine.config.get('computer_use_allowed', False):
            command += ['--disable', 'computer_use']
        if getattr(ctx, 'text_only', False):
            command += ['--disable', 'shell_tool', '--disable', 'unified_exec',
                        '-c', 'web_search="disabled"', '-c', 'mcp_servers={}',
                        '--disable', 'enable_mcp_apps', '--disable', 'standalone_web_search']
            if getattr(ctx, 'consultation_research', False):
                command += [
                            '-c', 'mcp_servers.project_read.command=' + json.dumps(ctx.read_mcp['command']),
                            '-c', 'mcp_servers.project_read.args=' + json.dumps(ctx.read_mcp['args'])]
        # Native Codex search has no pre-query approval hook. Use the guarded MCP.
        command += ['-c','web_search="disabled"','--disable','standalone_web_search']
        if not getattr(ctx,'text_only',False) and getattr(ctx,'read_mcp',None):
            command += ['-c','mcp_servers.project_read.command='+json.dumps(ctx.read_mcp['command']),
                        '-c','mcp_servers.project_read.args='+json.dumps(ctx.read_mcp['args'])]
        process = Process(command, ctx.project)
        counter = 0
        file_changes = {}

        def request(method, params):
            nonlocal counter
            counter += 1
            process.send({'id': counter, 'method': method, 'params': params})
            while True:
                msg = process.receive(ctx)
                if msg.get('id') == counter and 'method' not in msg:
                    if 'error' in msg:
                        raise ProviderError(str(msg['error'].get('message', 'Codex接続エラー'))[:1500])
                    return msg.get('result', {})
                handle(msg)

        def handle(msg):
            method = msg.get('method', '')
            params = msg.get('params') or {}
            if 'id' in msg and method:
                if method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval'):
                    payload = {'source': 'codex', 'operation': method, 'details': params}
                    if method == 'item/fileChange/requestApproval':
                        key = (params.get('threadId'), params.get('turnId'), params.get('itemId'))
                        payload['fileChanges'] = file_changes.pop(key, [])
                    answer = ctx.approve(payload)
                    process.send({'id': msg['id'], 'result': {'decision': 'accept' if answer['allow'] else 'decline'}})
                elif method == 'item/tool/requestUserInput':
                    answer = ctx.approve({'source': 'codex', 'operation': 'question', 'details': params})
                    answers = answer.get('answers', {})
                    process.send({'id': msg['id'], 'result': {'answers': answers if answer['allow'] else {}}})
                elif method == 'mcpServer/elicitation/request':
                    from team_elicitation import reply_to_elicitation
                    try:
                        response = reply_to_elicitation(ctx, params)
                    except ProviderError as exc:
                        process.send({'id': msg['id'], 'result': {'action': 'decline', 'content': None}})
                        ctx.event(str(exc))
                        raise
                    process.send({'id': msg['id'], 'result': response})
                else:
                    process.send({'id': msg['id'], 'error': {'code': -32601, 'message': 'Unsupported request; permission not granted'}})
                    ctx.event('未対応の要求を拒否: ' + method)
            elif method == 'item/started':
                item = params.get('item', {})
                if item.get('type') == 'fileChange':
                    key = (params.get('threadId'), params.get('turnId'), item.get('id'))
                    if all(key) and len(file_changes) < 100:
                        file_changes[key] = item.get('changes', [])
                if item.get('type') != 'reasoning':
                    ctx.event('処理: ' + item.get('type', 'item'))
            elif method == 'item/completed':
                item = params.get('item', {})
                file_changes.pop((params.get('threadId'), params.get('turnId'), item.get('id')), None)
                if item.get('type') == 'commandExecution' and hasattr(ctx, 'command_result'):
                    # 2026-10-09 the command's result as Codex reports it, for the reviewers' evidence.
                    command = item.get('command')
                    ctx.command_result(' '.join(map(str, command)) if isinstance(command, list) else str(command or ''),
                                       item.get('exitCode') if isinstance(item.get('exitCode'), int) else None,
                                       str(item.get('aggregatedOutput') or ''))
            elif method == 'thread/tokenUsage/updated':
                ctx.record_usage((params.get('tokenUsage') or {}).get('total', {}))
            return method, params

        try:
            request('initialize', {'clientInfo': {'name': 'agent_team_local', 'title': '采来 — サイクル —', 'version': '0.1.0'}})
            process.send({'method': 'initialized', 'params': {}})
            result = request('thread/start', {'model': profile['model'], 'cwd': ctx.project,
                             'approvalPolicy': 'untrusted',
                             'sandbox': ctx.agent_definition.sandbox,
                             'developerInstructions': ctx.agent_system_instructions, 'ephemeral': True})
            thread_id = result['thread']['id']
            ctx.agent_started(thread_id, 'Codex app-server / 独立thread')
            ctx.event('Codexへ接続しました。')
            if os.name == 'nt' and not getattr(ctx, 'text_only', False):
                # Check the existing sandbox before spending a model turn. No files or network are changed.
                try:
                    probe = request('command/exec', {'command': ['cmd.exe', '/d', '/c', 'echo AGENT_TEAM_NATIVE_OK'],
                        'cwd': ctx.project, 'sandboxPolicy': {'type': 'readOnly', 'networkAccess': False},
                        'timeoutMs': 10000})
                    if probe.get('exitCode') != 0 or probe.get('stdout', '').strip() != 'AGENT_TEAM_NATIVE_OK':
                        raise ProviderError('CodexのWindowsサンドボックスでコマンドを起動できません。setup refresh・実行環境の読み取り権限を復旧してから再試行してください。モデル実行は開始していません。', 'codex_sandbox')
                except ProviderError as exc:
                    raise ProviderError('CodexのWindowsサンドボックスの起動確認に失敗しました。モデル実行は開始していません。詳細: ' + str(exc)[:800], 'codex_sandbox') from exc
            from team_attachments import provider_input
            request('turn/start', {'threadId': thread_id, 'input': provider_input(ctx, prompt, 'codex'),
                                  'effort': profile['effort'], 'outputSchema': schema})
            final = ''
            while True:
                msg = process.receive(ctx)
                method, params = handle(msg)
                if method == 'item/completed':
                    item = params.get('item', {})
                    if item.get('type') == 'agentMessage':
                        final = item.get('text', '')
                if method == 'turn/completed':
                    turn = params.get('turn', {})
                    if turn.get('status') != 'completed':
                        raise ProviderError('Codexのターンが完了しませんでした: ' + str(turn.get('error') or turn.get('status'))[:1500])
                    return decode_result(final)
        finally:
            process.close()


class ClaudeAdapter:
    def run(self, ctx, prompt, schema):
        profile = ctx.task['profile']
        # The status check sometimes answers slowly (seen 2026-10-08: >15 s once, 0.7-3.4 s normally). A single slow
        # answer must not fail the task, so wait longer and ask once more before giving up. Logged-out stays fatal.
        authentication = None
        for attempt in range(2):
            ctx.check()
            try:
                authentication = subprocess.run(ctx.command + ['auth', 'status', '--json'], capture_output=True, text=True,
                    encoding='utf-8', errors='replace', timeout=30,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                break
            except subprocess.TimeoutExpired as exc:
                if attempt:
                    raise ProviderError('Claude Code CLIのログイン確認に応答がありませんでした（30秒×2回）。一時的な遅れの可能性があります。'
                                        '少し待ってから再試行してください。モデル実行は開始していません。', 'claude_auth') from exc
                ctx.event('Claude Code CLIのログイン確認に時間がかかっています。もう一度確認します。')
            except OSError as exc:
                raise ProviderError('Claude Code CLIを起動できず、ログインを確認できません。CLIの導入状態を確認してください。'
                                    'モデル実行は開始していません。', 'claude_auth') from exc
        try:
            logged_in = json.loads(authentication.stdout).get('loggedIn') is True
        except (ValueError, AttributeError) as exc:
            raise ProviderError('Claude Code CLIのログイン確認の結果を読み取れませんでした。CLIの更新状態を確認してから再試行してください。'
                                'モデル実行は開始していません。', 'claude_auth') from exc
        if not logged_in:
            raise ProviderError('Claude Code CLIは未ログイン、または認証期限切れです。Claude CLIの auth login でログインを完了してから再試行してください。モデル実行は開始していません。', 'claude_auth')
        ctx.check()
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONDONTWRITEBYTECODE'] = '1'  # same as copilot_work_tools: no __pycache__ left in the project
        env['AGENT_TEAM_RUN_TOKEN'] = ctx.token
        env['AGENT_TEAM_ENDPOINT'] = ctx.endpoint
        env['AGENT_TEAM_TASK'] = ctx.task['id']
        hook = Path(__file__).with_name('approval_hook.py')
        settings = {'hooks': {'PreToolUse': [{'matcher': '*', 'hooks': [
            {'type': 'command', 'command': sys.executable, 'args': [str(hook)],
             'timeout': ctx.approval_timeout + 30}]}],
            # 2026-10-09 Bash results (exit code + output) go to 采来 as evidence for the reviewers. A command that
            # exits non-zero fires PostToolUseFailure instead of PostToolUse, so both are registered.
            **{event: [{'matcher': 'Bash', 'hooks': [
                {'type': 'command', 'command': sys.executable, 'args': [str(Path(__file__).with_name('result_hook.py'))],
                 'timeout': 40}]}] for event in ('PostToolUse', 'PostToolUseFailure')}}}
        definition = ctx.agent_definition
        # Explicit CLI agent definition takes precedence over project-local customizations.
        # Only prompt/tools/description are transferred; hooks/MCP/permission overrides are not.
        agent_dir = tempfile.TemporaryDirectory(prefix='agent-team-agent-')
        agent_file = Path(agent_dir.name) / 'agents.json'
        agent_file.write_text(json.dumps({definition.name: {'description': definition.description,
            'prompt': ctx.agent_system_instructions, 'tools': list(definition.tools) + ['StructuredOutput'], 'model': 'inherit'}},
            ensure_ascii=False), encoding='utf-8')
        session_id = str(uuid.UUID(ctx.agent_run['id']))
        mcp_config = {'mcpServers': {'project_read': ctx.read_mcp}} if getattr(ctx, 'consultation_research', False) else {'mcpServers': {}}
        command = ctx.command + ['-p', '--restricted', '--output-format', 'stream-json', '--verbose',
                  '--agents', str(agent_file), '--agent', definition.name, '--session-id', session_id,
                  '--strict-mcp-config', '--mcp-config', json.dumps(mcp_config),
                  '--permission-mode', 'default', '--permission-prompts', 'host',
                  '--tools', ','.join(definition.tools + ('StructuredOutput',)), '--disallowed-tools', 'Agent,Task',
                  '--model', profile['model'], '--effort', profile['effort'],
                  '--settings', json.dumps(settings), '--json-schema', json.dumps(schema),
                  '--no-session-persistence']
        if getattr(ctx, 'attachment_ids', []):
            command += ['--input-format', 'stream-json']
        for folder in getattr(ctx, 'skill_dirs', []):
            # Enabled skills of this project: readable by the file tools (采来 still denies writes there).
            command += ['--add-dir', folder]
        process = None
        try:
            process = Process(command, ctx.project, env)
            if getattr(ctx, 'attachment_ids', []):
                from team_attachments import provider_input
                process.send({'type': 'user', 'message': {'role': 'user',
                    'content': provider_input(ctx, prompt, 'claude')}, 'parent_tool_use_id': None})
            else:
                process.proc.stdin.write(prompt)
            process.proc.stdin.close()
            artifact_calls = {}
            while True:
                msg = process.receive(ctx)
                kind = msg.get('type')
                if kind == 'system' and msg.get('subtype') == 'init':
                    if msg.get('session_id') != session_id:
                        raise ProviderError('Claude共通Agentの実行セッションが一致しません。')
                    available_agents = msg.get('agents')
                    if isinstance(available_agents, list) and definition.name not in available_agents:
                        raise ProviderError('Claude共通AgentをCLIへ登録できませんでした: ' + definition.name)
                    ctx.agent_started(session_id, 'Claude Code / --agent ' + definition.name)
                    ctx.event('Claude Codeへ接続しました。モデル: ' + str(msg.get('model', profile['model'])))
                    if 'Artifact' in definition.tools:
                        ctx.event('Artifactの道具: ' + ('使用可' if 'Artifact' in (msg.get('tools') or []) else 'CLIが提供していません')
                                  + '（CLIの道具一覧: ' + ', '.join(str(t) for t in (msg.get('tools') or []))[:400] + '）')
                elif kind == 'assistant':
                    if hasattr(ctx, 'observe_message'):
                        ctx.observe_message(msg.get('message', {}))
                    for block in msg.get('message', {}).get('content', []):
                        if block.get('type') == 'tool_use':
                            ctx.event('処理: ' + str(block.get('name', 'tool')))
                            if block.get('name') == 'Artifact':
                                artifact_calls[block.get('id')] = block.get('input') or {}
                elif kind == 'user' and artifact_calls and hasattr(ctx, 'artifact_result'):
                    content = msg.get('message', {}).get('content', [])
                    for block in content if isinstance(content, list) else []:
                        if block.get('type') == 'tool_result' and block.get('tool_use_id') in artifact_calls:
                            body = block.get('content')
                            text = body if isinstance(body, str) else '\n'.join(
                                str(part.get('text', '')) for part in body or [] if isinstance(part, dict))
                            ctx.artifact_result(artifact_calls.pop(block.get('tool_use_id')), text)
                elif kind == 'result':
                    if hasattr(ctx, 'observe_message'):
                        ctx.record_usage(msg.get('usage', {}), msg)
                    else:
                        ctx.record_usage(msg.get('usage', {}))
                    if not ctx.agent_run.get('session_id'):
                        raise ProviderError('Claude共通Agentの起動確認を受け取れませんでした。')
                    if msg.get('is_error') or msg.get('subtype') != 'success':
                        raise ProviderError('Claude Code: ' + str(msg.get('result') or msg.get('errors') or msg.get('subtype'))[:1500])
                    output = msg.get('structured_output')
                    return output if isinstance(output, dict) else decode_result(msg.get('result', ''))
        finally:
            if process is not None:
                process.close()
            agent_dir.cleanup()


TOKEN_KEYS = ('inputTokens', 'outputTokens', 'cacheReadTokens', 'cacheWriteTokens', 'reasoningTokens')


def copilot_tokens(path):
    """Token counts summed over the models in a Copilot CLI usage file (--usage-output-file), or None.
    The file is deleted after reading (it holds only counts and durations)."""
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    finally:
        try:
            Path(path).unlink()
        except OSError:
            pass
    totals = dict.fromkeys(TOKEN_KEYS, 0)
    for metrics in (data.get('modelMetrics') or {}).values():
        usage = (metrics or {}).get('usage') or {}
        for key in TOKEN_KEYS:
            if isinstance(usage.get(key), (int, float)):
                totals[key] += int(usage[key])
    return totals if any(totals.values()) else None


def forget_copilot_session(session_id):
    """Remove the conversation Copilot CLI saved for one run of 采来 (2026-10-09). Claude runs with
    --no-session-persistence; Copilot has no such option and kept every conversation (instructions and project
    contents) under ~/.copilot/session-state. Only the folder and lock of this run's own session id are removed."""
    try:
        parsed = uuid.UUID(str(session_id))
        if str(parsed) != str(session_id):
            return False
    except ValueError:
        return False
    home = Path(os.environ.get('COPILOT_HOME') or (Path.home() / '.copilot'))
    state_root = home / 'session-state'
    if state_root.is_symlink() or getattr(state_root, 'is_junction', lambda: False)() or not state_root.resolve().is_relative_to(home.resolve()):
        return False
    removed = False
    for path in (home / 'session-state' / session_id, home / 'session-state' / '.session-operation-locks' / (session_id + '.lock')):
        try:
            if path.is_symlink() or getattr(path, 'is_junction', lambda: False)() or not path.resolve().is_relative_to(state_root.resolve()):
                continue
            if path.is_dir():
                shutil.rmtree(path)
                removed = True
            elif path.is_file():
                path.unlink()
                removed = True
        except OSError:
            pass
    return removed


COPILOT_READ_TOOLS = ('list_files', 'read_file', 'search_files', 'read_document', 'media_environment')
# Document builder: 采来's document tools write only inside the output folder and run no commands (document_tools).
COPILOT_DOCUMENT_TOOLS = ('write_document', 'generate_media', 'generate_office', 'learn_design')
# Development builder: copilot_work_tools; writes, edits and commands are decided by 采来 before they run.
COPILOT_WORK_TOOLS = ('list_files', 'read_file', 'search_files', 'read_document', 'write_file', 'edit_file', 'run_command')


# Report fields whose absence means the same as empty ("no question", "no update"). The only fields that may be
# left out; the engine fills them. Every field that carries information must be present, whatever the provider.
EMPTY_WHEN_ABSENT = {'question': '', 'questions': [], 'context_updates': []}


def schema_errors(value, schema, path=''):
    """Where a value does not match our JSON schema subset (object/array/string/boolean/number, required, enum, items).
    Extra keys are not errors (they carry no loss); missing keys and wrong types are. At the top level the
    EMPTY_WHEN_ABSENT fields may be absent or null."""
    if not schema:
        return []
    if not path and isinstance(value, dict):
        value = {k: v for k, v in value.items() if not (k in EMPTY_WHEN_ABSENT and v is None)}
        value = dict({k: e for k, e in EMPTY_WHEN_ABSENT.items() if k in (schema.get('properties') or {})}, **value)
    name = path or '結果'
    kind = schema.get('type')
    types = {'object': dict, 'array': list, 'string': str, 'boolean': bool}
    if kind in types and (not isinstance(value, types[kind]) or (kind != 'boolean' and isinstance(value, bool))):
        return [f'{name}（{kind}ではない）']
    if kind in ('number', 'integer') and (isinstance(value, bool) or not isinstance(value, (int, float))):
        return [f'{name}（数値ではない）']
    if 'enum' in schema and value not in schema['enum']:
        return [f'{name}（{"/".join(map(str, schema["enum"]))} のいずれでもない）']
    errors = []
    if kind == 'object':
        for key in schema.get('required', []):
            if key not in value:
                errors.append(f'{path + "." if path else ""}{key}（欠けている）')
        for key, sub in (schema.get('properties') or {}).items():
            if key in value:
                errors += schema_errors(value[key], sub, (path + '.' if path else '') + key)
    elif kind == 'array' and schema.get('items'):
        for index, item in enumerate(value):
            errors += schema_errors(item, schema['items'], f'{name}[{index}]')
    return errors


def copilot_answer(text, schema=None):
    """Copilot has no output-schema option, so the model may add a preface, a closing remark or a code fence around
    the JSON (2026-10-09: a security review failed only because of that). Take the JSON object from the text: the
    whole text if it is one, otherwise the object in the text that best matches the schema (only the schema's keys,
    the most of them; the last one on a tie). Missing keys are checked later by the engine as before."""
    text = (text or '').strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    try:
        return decode_result(text)
    except ProviderError:
        pass
    keys = set((schema or {}).get('properties') or [])
    decoder, best, best_score, index = json.JSONDecoder(), None, 0, text.find('{')
    while index >= 0:
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index = text.find('{', index + 1)
            continue
        if isinstance(value, dict) and value:
            score = len(keys & set(value)) if keys else len(value)
            if (not keys or set(value) <= keys) and score >= best_score and score > 0:
                best, best_score = value, score
        index = text.find('{', end)
    if best is None:
        raise ProviderError('結果が指定のJSON形式ではありません。自動では次のタスクへ進めません。')
    return best


class CopilotAdapter:
    """GitHub Copilot CLI in prompt mode. It has no hook that asks 采来 before a tool runs, so it runs fail-closed:
    every built-in tool is unavailable and only 采来's own tools (MCP) may be called.
    - planning / review / consultation: the read-only project broker;
    - builder of a document job: the document tools (write only inside the output folder, no commands);
    - builder of a development job (2026-10-09): copilot_work_tools, whose writes, edits and commands are each
      decided by 采来's /worker/tool first (the same checks and approvals as a Claude builder)."""

    _spent = (None, None)  # (credits, premium) reported by the last _call, for a resumed session's baseline

    def run(self, ctx, prompt, schema):
        from team_config import COPILOT_MODELS
        profile = ctx.task['profile']
        definition = ctx.agent_definition
        role, document = ctx.task.get('role'), getattr(ctx, 'document_scope', False)
        if getattr(ctx, 'attachment_ids', []):
            raise ProviderError('GitHub Copilotは添付ファイル付きの作業に未対応です。CodexまたはClaudeへ切り替えてください。'
                                'モデル実行は開始していません。', 'copilot_scope')
        if getattr(ctx, 'artifact_format', None):
            raise ProviderError('GitHub CopilotはClaudeのArtifact形式の資料に未対応です（Claudeの道具を使うため）。'
                                'Claudeへ切り替えてください。モデル実行は開始していません。', 'copilot_scope')
        work = role == 'builder' and not document and not getattr(ctx, 'text_only', False)
        if not work and (not getattr(ctx, 'text_only', False) or definition.sandbox != 'read-only'):
            raise ProviderError('GitHub Copilotは計画・レビュー・相談・実装（資料作成と開発）の担当に使えます。'
                                'この作業はCodexまたはClaudeへ切り替えてください。モデル実行は開始していません。', 'copilot_scope')
        # Empty working folder: the CLI reports its cwd to the model and must not see the project directly.
        workdir = tempfile.TemporaryDirectory(prefix='agent-team-copilot-')
        allowed, extra = [], ''
        if work:
            # 采来's run token goes to the tool server in a file of this temporary folder, never on a command line.
            token_file = Path(workdir.name) / 'work-token.json'
            token_file.write_text(json.dumps({'endpoint': ctx.endpoint, 'task_id': ctx.task['id'], 'token': ctx.token,
                                              'timeout': ctx.approval_timeout + 30}), encoding='utf-8')
            allowed = ['project_work-' + name for name in COPILOT_WORK_TOOLS]
            server = {'type': 'local', 'command': sys.executable, 'args': [
                '-X', 'utf8', str(Path(__file__).with_name('copilot_work_tools.py')), str(ctx.project), str(token_file)],
                'tools': ['*']}
            mcp = ['--additional-mcp-config', json.dumps({'mcpServers': {'project_work': server}}), '--allow-tool=project_work']
            extra = ('\n\n---\nこの担当で使える道具は project_work の list_files・read_file・search_files・read_document・'
                     'write_file・edit_file・run_command だけです。ファイルの変更は write_file・edit_file、コマンドは run_command'
                     '（Windows PowerShell、プロジェクトのフォルダで実行）で行ってください。変更とコマンドは実行前に采来が確認し、'
                     '拒否されたら理由に従ってください（拒否を回避する別の方法を試さない）。')
        else:
            permitted = COPILOT_READ_TOOLS + (COPILOT_DOCUMENT_TOOLS if document and role == 'builder' else ())
            for name in definition.tools:
                if not name.startswith('mcp__project_read__'):
                    continue  # Web search/fetch and file tools of other CLIs are not given to Copilot.
                short = name.removeprefix('mcp__project_read__')
                if short not in permitted:
                    workdir.cleanup()
                    raise ProviderError('GitHub Copilotにはこの道具を渡せません: ' + short, 'copilot_scope')
                allowed.append('project_read-' + short)
            read_mcp = getattr(ctx, 'read_mcp', None) if getattr(ctx, 'consultation_research', False) else None
            if allowed and not read_mcp:
                allowed = []
            mcp = ['--additional-mcp-config', json.dumps({'mcpServers': {'project_read': {
                'type': 'local', 'command': read_mcp['command'], 'args': read_mcp['args'], 'tools': ['*']}}}),
                '--allow-tool=project_read'] if allowed else []
        common = ['--model', profile['model'], '--output-format=json',
                  '--deny-tool=shell', '--deny-tool=write', '--deny-tool=url',
                  '--deny-tool=read', '--deny-tool=memory', '--disable-builtin-mcps',
                  '--no-ask-user', '--no-custom-instructions', '--no-auto-update']
        if COPILOT_MODELS.get(profile['model'], ('', True))[1]:
            common += ['--reasoning-effort', profile['effort']]
        # One named session, so a format repair can continue the same conversation (the model keeps what it read).
        session_id = str(uuid.uuid4())
        command = ctx.command + common + ['--session-id', session_id, '--available-tools=' + (','.join(allowed) or '__none__')] + mcp
        text = (ctx.agent_system_instructions + extra + '\n\n---\n\n' + prompt
                + '\n\n---\n最終回答は、次のJSONスキーマに適合するJSONオブジェクトだけを出力してください。'
                  'すべての項目を省略せずに含めてください（該当がなければ空文字・空の配列）。'
                  '前置き・説明文・コードブロックは付けないでください。\n' + json.dumps(schema, ensure_ascii=False))
        env = os.environ.copy()
        env['COPILOT_AUTO_UPDATE'] = 'false'
        try:
            ctx.agent_started('copilot-' + session_id, 'GitHub Copilot CLI / prompt mode（'
                              + ('采来の確認付きの実装' if work else '資料作成' if document and role == 'builder' else '読み取り専用') + '）')
            ctx.event('GitHub Copilotへ接続しました。モデル: ' + profile['model'])
            final = self._call(ctx, command, text, workdir.name, env, allowed)
            try:
                result, errors = copilot_answer(final, schema), None
                errors = schema_errors(result, schema)
            except ProviderError:
                result, errors = None, ['結果（JSONとして読めない）']
            if not errors:
                return result
            # 2026-10-09 (user decision): the report must carry the same items whatever the model. Copilot has no
            # output-schema option, so a missing or malformed item is not filled in by 采来: the same model, in the
            # same conversation and without tools, is asked once to output its own report again in full.
            ctx.event('報告の項目が指定と違うため、同じモデルに形だけ直させます：' + '、'.join(errors[:8])[:400])
            repair = ctx.command + common + ['--resume=' + session_id, '--available-tools=__none__']
            ask = ('直前の最終回答は、指定のJSONスキーマに合っていません（' + '、'.join(errors[:20]) + '）。'
                   '調べ直したり内容を変えたりせず、ここまでの作業で確認した事実に基づいて、同じ報告を'
                   '次のスキーマに合うJSONオブジェクトだけで出力し直してください。すべての項目を含め、'
                   '確認していないことは未確認と書いてください。前置き・説明文・コードブロックは付けないでください。\n'
                   + json.dumps(schema, ensure_ascii=False))
            first_errors = errors
            final = self._call(ctx, repair, ask, workdir.name, env, [], baseline=self._spent)
            try:
                result = copilot_answer(final, schema)
                errors = schema_errors(result, schema)
            except ProviderError:
                errors = ['結果（JSONとして読めない）']
            if errors:
                raise ProviderError('担当の報告に必要な項目がそろいませんでした（最初の報告：' + '、'.join(first_errors[:8])[:300]
                                    + '／形の修正後：' + '、'.join(errors[:8])[:300]
                                    + '）。形の修正を1回頼みましたが直りませんでした。担当・モデルを切り替えてください。', 'format')
            ctx.event('報告の形を直しました（同じモデル・同じ会話、道具なし）。')
            return result
        finally:
            workdir.cleanup()
            forget_copilot_session(session_id)

    def _call(self, ctx, command, text, cwd, env, allowed, baseline=None):
        """One Copilot CLI run: send the prompt, watch the tools, return the final message text.
        baseline: (credits, premium) already recorded for this session. A resumed session may report totals that
        include the earlier run; a value not below the baseline is treated as such a total and only the increase
        is recorded, a smaller value as the resumed run's own use."""
        process = None
        final, credits, premium = '', None, None
        self._spent = (None, None)
        # Token counts are only in the CLI's usage file, written as the process ends (2026-10-09).
        usage_file = Path(cwd) / ('usage-' + uuid.uuid4().hex + '.json')
        command = list(command) + ['--usage-output-file', str(usage_file)]
        tokens = None
        try:
            process = Process(command, cwd, env)
            process.proc.stdin.write(text)
            process.proc.stdin.close()
            while True:
                try:
                    msg = process.receive(ctx)
                except ProviderError as exc:
                    detail = ''.join(process.stderr).strip()
                    raise ProviderError('GitHub Copilot: ' + (detail[-800:] or str(exc))) from exc
                kind = msg.get('type')
                data = msg.get('data') or {}
                if kind == 'tool.execution_start':
                    tool = str(data.get('toolName', ''))
                    if tool not in allowed:
                        raise ProviderError('GitHub Copilotが許可していない道具を呼び出したため停止しました: ' + tool[:100])
                    ctx.event('処理: ' + tool)
                    if hasattr(ctx, 'count_tool'):
                        ctx.count_tool(tool)  # agent_run.tool_calls, as for Claude
                elif kind == 'assistant.message' and data.get('content'):
                    final = data['content']
                elif kind == 'session.usage_checkpoint':
                    if isinstance(data.get('totalNanoAiu'), (int, float)):
                        credits = data['totalNanoAiu'] / 1e9
                    if isinstance(data.get('totalPremiumRequests'), (int, float)):
                        premium = data['totalPremiumRequests']
                elif kind == 'result':
                    usage = msg.get('usage') or {}
                    if isinstance(usage.get('premiumRequests'), (int, float)):
                        premium = usage['premiumRequests']
                    if msg.get('exitCode') != 0:
                        raise ProviderError('GitHub Copilotの実行が完了しませんでした（終了コード ' + str(msg.get('exitCode')) + '）。')
                    return final
        finally:
            if process is not None:
                try:
                    process.proc.wait(timeout=15)  # let the CLI write its usage file before it is stopped
                except subprocess.TimeoutExpired:
                    pass
                process.close()
            tokens = copilot_tokens(usage_file)
            self._spent = (credits, premium, tokens)
            if baseline:
                base_credits, base_premium = baseline[:2]
                if credits is not None and base_credits is not None and credits >= base_credits:
                    credits -= base_credits
                if premium and base_premium and premium >= base_premium:
                    premium -= base_premium
                base_tokens = baseline[2] if len(baseline) > 2 else None
                if tokens and base_tokens and all(tokens.get(k, 0) >= base_tokens.get(k, 0) for k in tokens):
                    tokens = {k: tokens[k] - base_tokens.get(k, 0) for k in tokens}
            if credits is not None or premium or tokens:
                # Count consumption also for failed/cancelled runs (the credits were spent).
                from team_usage import record_copilot_usage
                record_copilot_usage(credits, premium)
                if hasattr(ctx, 'add_copilot_usage'):
                    ctx.add_copilot_usage(credits, premium, tokens)  # per task, for comparing models


ADAPTERS = {'codex': CodexAdapter, 'claude': ClaudeAdapter, 'copilot': CopilotAdapter}
