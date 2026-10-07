"""One narrow interface for two providers; no dashboard decisions in model output."""
import json
import os
import queue
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
        try:
            authentication = subprocess.run(ctx.command + ['auth', 'status'], capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=15,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if json.loads(authentication.stdout).get('loggedIn') is not True:
                raise ProviderError('Claude Code CLIは未ログイン、または認証期限切れです。Claude CLIの auth login でログインを完了してから再試行してください。モデル実行は開始していません。', 'claude_auth')
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise ProviderError('Claude Code CLIの認証状態を確認できません。CLIの起動とログインを確認してください。', 'claude_auth') from exc
        ctx.check()
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        env['AGENT_TEAM_RUN_TOKEN'] = ctx.token
        env['AGENT_TEAM_ENDPOINT'] = ctx.endpoint
        env['AGENT_TEAM_TASK'] = ctx.task['id']
        hook = Path(__file__).with_name('approval_hook.py')
        settings = {'hooks': {'PreToolUse': [{'matcher': '*', 'hooks': [
            {'type': 'command', 'command': sys.executable, 'args': [str(hook)],
             'timeout': ctx.approval_timeout + 30}]}]}}
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


ADAPTERS = {'codex': CodexAdapter, 'claude': ClaudeAdapter}
