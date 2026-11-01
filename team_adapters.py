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

from claude_hook_settings import build_egress_hook_settings


class Cancelled(Exception):
    pass


# Protocol bookkeeping has no tool effect. Keep unknown/new item types observed
# so recovery cannot silently classify a new capability as file-only.
CODEX_NON_TOOL_ITEMS = frozenset({'userMessage', 'agentMessage', 'plan', 'reasoning',
    'contextCompaction', 'sleep', 'enteredReviewMode', 'exitedReviewMode'})


class ProviderError(Exception):
    def __init__(self, message, code='provider', *, model_call_state='unknown'):
        super().__init__(message)
        self.code = code
        self.model_call_state = model_call_state if model_call_state in ('not_started', 'started') else 'unknown'


def _set_scoped_test_temp(ctx, env):
    job = ctx.engine.store.get(ctx.task['job_id'], 'job')
    if ctx.task.get('role') != 'builder':
        return
    from team_test_environment import TestEnvironmentError, prepare_project_test_temp
    try:
        test_temp, _ = prepare_project_test_temp(ctx.project)
    except TestEnvironmentError:
        raise ProviderError('指定テストの一時領域が許可範囲内にありません。テストは開始していません。',
                            'test_temp_scope', model_call_state='not_started') from None
    # Scope TEMP/TMP to the registered project for this Claude builder process and
    # its test child process. This does not change the Windows or user environment.
    env['TEMP'] = str(test_temp.resolve())
    env['TMP'] = str(test_temp.resolve())
    env['TMPDIR'] = str(test_temp.resolve())


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
        raise ProviderError('結果が指定のJSON形式ではありません。自動では次のタスクへ進めません。',
                            'format') from exc
    if not isinstance(value, dict):
        raise ProviderError('結果はJSONオブジェクトである必要があります。', 'format')
    return value


def report_format_instruction(schema, include_schema=False):
    """The same final-report contract for all provider prompts."""
    instruction = ('\n\n---\n最終報告は指定されたJSONスキーマに合うJSONオブジェクトだけで返してください。'
                   '必須項目をすべて含め、値がない文字列は空文字、配列は空配列、'
                   '真偽値は事実に合わせて true または false にしてください。'
                   '未確認のことを完了とせず、前置き・説明文・コードブロックは付けないでください。')
    properties = schema.get('properties') or {}
    if 'tasks' in properties:
        instruction += (' summaryには計画の目的と範囲を、tasksには後続が実行できる担当種別・作業内容を記してください。'
                        '未確定事項はquestionsに、引き継ぐ事実はcontext_updatesに根拠とともに記してください。')
    elif 'status' in properties:
        instruction += (' statusは実際の結果、summaryは実施内容と到達点、checksは確認項目ごとの結果と根拠、'
                        'pending_checksは必要だが未確認の項目を記してください。'
                        'stage_completeは担当工程が完了した場合だけtrueにしてください。'
                        '停止や差し戻しにはblocking_reasonsで必須条件と支障を示し、'
                        '判断が必要ならquestionまたはquestionsに具体的に記してください。')
        if 'artifacts' in properties:
            instruction += ' artifactsには作成・変更した成果物の相対パスを記してください。'
        if 'test_files' in properties:
            instruction += ' test_filesとtest_change_reasonにはテストの場所と変更理由を記してください。'
    return instruction + ('\nJSONスキーマ:\n' + json.dumps(schema, ensure_ascii=False)
                          if include_schema else '')


def decode_text_report(text, schema):
    """Accept only a complete schema-compatible JSON object from final text."""
    try:
        result = copilot_answer(text, schema)
    except ProviderError as exc:
        raise ProviderError('完了報告をJSONとして回収できませんでした。作業の成果は保持しています。',
                            'format') from exc
    errors = schema_errors(result, schema)
    if errors:
        raise ProviderError('完了報告に必要な項目がありません（' + '、'.join(errors[:8])[:400]
                            + '）。作業の成果は保持しています。', 'format')
    return result


def allow_automatic_format_retry(ctx):
    """P0 requires a new user decision before another paid model turn."""
    job = ctx.engine.store.get(ctx.task['job_id'], 'job')
    return job.get('p0_contract_version') != 1


def decode_claude_result(message, schema, assistant_text=''):
    """Recover a complete report when Claude omits structured_output.

    The CLI can put its JSON report in result, sometimes inside a code fence or
    short explanation. Only a full schema match is accepted; prose and partial
    objects must never be promoted to a successful task result.
    """
    output = message.get('structured_output')
    if isinstance(output, dict) and not schema_errors(output, schema):
        return output
    last_error = None
    for text in (message.get('result', ''), assistant_text):
        if not text:
            continue
        try:
            return decode_text_report(text, schema)
        except ProviderError as exc:
            last_error = exc
    raise last_error or ProviderError('Claudeの完了報告を受信できませんでした。作業の成果は保持しています。',
                                     'format')


def observe_codex_context(ctx, method, params):
    """Optional observational hook; no permission, execution or cancellation."""
    callback = getattr(ctx, 'record_context_observation', None)
    if not callable(callback) or not isinstance(params, dict):
        return
    if method == 'thread/tokenUsage/updated':
        callback(params.get('tokenUsage'), params.get('threadId'))
    elif method in ('item/started', 'item/completed') and (params.get('item') or {}).get('type') == 'contextCompaction':
        callback({}, params.get('threadId'), compacted=True)


class CodexAdapter:
    def run(self, ctx, prompt, schema):
        from team_egress_guard import require_safe_egress
        require_safe_egress({'prompt': prompt, 'agent_instructions': ctx.agent_system_instructions,
                             'agent_description': ctx.agent_definition.description}, 'codex')
        profile = ctx.task['profile']
        command = ctx.command + ['app-server', '--listen', 'stdio://',
                                 '-c', 'service_tier="default"', '--disable', 'fast_mode',
                                 '--disable', 'multi_agent', '--disable', 'multi_agent_v2',
                                 '-c', 'model_reasoning_effort="' + profile['effort'] + '"']
        if not ctx.engine.config.get('computer_use_allowed', False):
            # The desktop user's browser MCP/notify executable may be running
            # and locked. It is not part of a non-GUI project worker's runtime.
            # Override only this child; retain its existing Windows sandbox.
            command += ['--disable', 'computer_use',
                        '-c', 'mcp_servers.node_repl.enabled=false',
                        '-c', 'mcp_servers.node_repl.command=' + json.dumps(sys.executable),
                        '-c', 'notify=[]']
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
        ctx.check()
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
            observe_codex_context(ctx, method, params)
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
                if item.get('type') not in CODEX_NON_TOOL_ITEMS and hasattr(ctx, 'count_tool'):
                    ctx.count_tool(item.get('type') or 'unknownItem')
                if item.get('type') == 'commandExecution' and hasattr(ctx, 'command_started'):
                    command = item.get('command')
                    ctx.command_started(' '.join(map(str, command)) if isinstance(command, list)
                                        else str(command or ''), item.get('id'))
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
                                       str(item.get('aggregatedOutput') or ''), operation_id=item.get('id'))
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
                        raise ProviderError('CodexのWindowsサンドボックスでコマンドを起動できません。setup refresh・実行環境の読み取り権限を復旧してから再試行してください。モデル実行は開始していません。', 'codex_sandbox', model_call_state='not_started')
                except ProviderError as exc:
                    raise ProviderError('CodexのWindowsサンドボックスの起動確認に失敗しました。モデル実行は開始していません。詳細: ' + str(exc)[:800], 'codex_sandbox', model_call_state='not_started') from exc
            from team_attachments import provider_input
            prompt += report_format_instruction(schema)
            from team_usage_metrics import observe_prompt
            observe_prompt(ctx,prompt)
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
                    return decode_text_report(final, schema)
        finally:
            process.close()


class ClaudeAdapter:
    def run(self, ctx, prompt, schema):
        from team_egress_guard import require_safe_egress
        require_safe_egress({'prompt': prompt, 'agent_instructions': ctx.agent_system_instructions,
                             'agent_description': ctx.agent_definition.description}, 'claude')
        profile = ctx.task['profile']
        # 2026-10-12 (plan A): with the long-lived token stored by the user, the regular login is not checked; the
        # token goes to this CLI process only (team_claude_auth). A token that no longer works fails the run with a
        # sign-in error, and the engine then holds Claude tasks (plan C).
        guard = getattr(getattr(ctx, 'engine', None), 'claude_auth', None)
        stored_token = guard is not None and guard.configured()
        # 2026-10-15: inside 采来 the regular login is judged from its file, not by `claude auth status` (an extra CLI
        # start that may refresh the shared login while other workers do the same). The status command remains only
        # when the file cannot be read, or outside 采来's engine.
        file_state = guard.login_state()['logged_in'] if guard is not None and not stored_token else None
        if file_state is False:
            raise ProviderError('Claude Code CLIは未ログイン、または認証期限切れです。長期の鍵を入れるか、Claude にログインし直してください。'
                                'モデル実行は開始していません。', 'claude_auth')
        # The status check sometimes answers slowly (seen 2026-10-08: >15 s once, 0.7-3.4 s normally). A single slow
        # answer must not fail the task, so wait longer and ask once more before giving up. Logged-out stays fatal.
        authentication = None
        for attempt in range(0 if stored_token or file_state else 2):
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
            logged_in = stored_token or file_state or json.loads(authentication.stdout).get('loggedIn') is True
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
        _set_scoped_test_temp(ctx, env)
        if stored_token:
            # An API key in the environment would take precedence over the token (Claude Code's documented order).
            env.pop('ANTHROPIC_API_KEY', None)
            env = guard.env(env)
        hook = Path(__file__).with_name('approval_hook.py')
        result_hook = Path(__file__).with_name('result_hook.py')
        egress_settings = build_egress_hook_settings(
            sys.executable, [str(result_hook)], [str(result_hook)],
            [str(Path(__file__).with_name('egress_batch_hook.py'))])
        settings = egress_settings
        settings['hooks']['PreToolUse'] = [{'matcher': '*', 'hooks': [
            {'type': 'command', 'command': sys.executable, 'args': [str(hook)],
             'timeout': ctx.approval_timeout + 30}]}]
        try:
            from claude_hook_settings import validate_egress_hook_settings
            validate_egress_hook_settings(settings)
            missing_hook_scripts = [path for path in
                (hook, result_hook, Path(__file__).with_name('egress_batch_hook.py'))
                if not path.is_file()]
            if missing_hook_scripts:
                raise ValueError('egress_hook_script_missing')
        except (AttributeError, TypeError, ValueError) as exc:
            raise ProviderError('Claude Codeの送信ガード設定を確認できないため、モデル起動を停止しました。',
                                'claude_hooks') from exc
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
        prompt += report_format_instruction(schema)
        from team_usage_metrics import observe_prompt
        observe_prompt(ctx,prompt)
        # Images (user attachments or reference images drawn by 采来) need the structured input.
        structured = bool(getattr(ctx, 'attachment_ids', []) or getattr(ctx, 'reference_images', None))
        if structured:
            command += ['--input-format', 'stream-json']
        for folder in getattr(ctx, 'skill_dirs', []):
            # Enabled skills of this project: readable by the file tools (采来 still denies writes there).
            command += ['--add-dir', folder]
        process = None
        try:
            ctx.check()
            process = Process(command, ctx.project, env)
            if structured:
                from team_attachments import provider_input
                process.send({'type': 'user', 'message': {'role': 'user',
                    'content': provider_input(ctx, prompt, 'claude')}, 'parent_tool_use_id': None})
            else:
                process.proc.stdin.write(prompt)
            process.proc.stdin.close()
            artifact_calls = {}
            last_assistant_text = ''
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
                    text_blocks = [block.get('text', '') for block in msg.get('message', {}).get('content', [])
                                   if isinstance(block, dict) and block.get('type') == 'text']
                    if text_blocks:
                        last_assistant_text = '\n'.join(text_blocks)
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
                    try:
                        return decode_claude_result(msg, schema, last_assistant_text)
                    except ProviderError as exc:
                        if exc.code == 'format':
                            ctx.event('報告形式を回収できませんでした（構造化結果='
                                      + ('有' if isinstance(msg.get('structured_output'), dict) else '無')
                                      + '、結果の長さ=' + str(len(str(msg.get('result') or '')))
                                      + '、最終本文の長さ=' + str(len(last_assistant_text)) + '）。')
                            denied = msg.get('permission_denials')
                            ctx.event('終了情報（内容は含まない）: subtype=' + str(msg.get('subtype'))[:40]
                                      + ' stop_reason=' + str(msg.get('stop_reason'))[:40]
                                      + ' terminal_reason=' + str(msg.get('terminal_reason'))[:40]
                                      + ' num_turns=' + str(msg.get('num_turns'))[:10]
                                      + ' 拒否された道具='
                                      + (','.join(str(d.get('tool_name', '?'))[:30] for d in denied[:8] if isinstance(d, dict))
                                         if isinstance(denied, list) and denied else 'なし'))
                        raise
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
    whole text if it is one, otherwise the object that best matches the schema. A complete match wins; extra keys
    are allowed by the schema gate. Missing keys are checked later by the engine as before."""
    text = (text or '').strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    try:
        return decode_result(text)
    except ProviderError:
        pass
    keys = set((schema or {}).get('properties') or [])
    decoder, best, best_score, index = json.JSONDecoder(), None, (-1, 0), text.find('{')
    while index >= 0:
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index = text.find('{', index + 1)
            continue
        if isinstance(value, dict) and value:
            known = len(keys & set(value)) if keys else len(value)
            score = (int(bool(schema) and not schema_errors(value, schema)), known)
            if known and score >= best_score:
                best, best_score = value, score
        index = text.find('{', end)
    if best is None:
        raise ProviderError('結果が指定のJSON形式ではありません。自動では次のタスクへ進めません。',
                            'format')
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
        if getattr(ctx, 'reference_images', None):
            # Never expected: the optimiser keeps text for Copilot. Refuse rather than drop the replaced reference.
            raise ProviderError('GitHub Copilotには参考画像を送れません（参考情報の画像化の判定の誤り）。モデル実行は開始していません。',
                                'copilot_scope')
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
                + report_format_instruction(schema, include_schema=True))
        env = os.environ.copy()
        env['COPILOT_AUTO_UPDATE'] = 'false'
        try:
            ctx.agent_started('copilot-' + session_id, 'GitHub Copilot CLI / prompt mode（'
                              + ('采来の確認付きの実装' if work else '資料作成' if document and role == 'builder' else '読み取り専用') + '）')
            ctx.event('GitHub Copilotへ接続しました。モデル: ' + profile['model'])
            from team_usage_metrics import observe_prompt
            observe_prompt(ctx,text,includes_system=True)
            final = self._call(ctx, command, text, workdir.name, env, allowed)
            try:
                result, errors = copilot_answer(final, schema), None
                errors = schema_errors(result, schema)
            except ProviderError:
                result, errors = None, ['結果（JSONとして読めない）']
            if not errors:
                return result
            if not allow_automatic_format_retry(ctx):
                raise ProviderError('報告の形式を回収できませんでした（' + '、'.join(errors[:8])[:300]
                                    + '）。成果は保持しました。追加のモデル実行は利用者の判断を待ちます。', 'format')
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
            observe_prompt(ctx,ask,includes_system=True,kind='format_repair')
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
        from team_egress_guard import require_safe_egress
        require_safe_egress(text, 'copilot')
        process = None
        final, credits, premium = '', None, None
        session_error = ''
        self._spent = (None, None)
        # Token counts are only in the CLI's usage file, written as the process ends (2026-10-09).
        usage_file = Path(cwd) / ('usage-' + uuid.uuid4().hex + '.json')
        command = list(command) + ['--usage-output-file', str(usage_file)]
        tokens = None
        try:
            ctx.check()
            process = Process(command, cwd, env)
            process.proc.stdin.write(text)
            process.proc.stdin.close()
            while True:
                try:
                    msg = process.receive(ctx)
                except ProviderError as exc:
                    detail = ''.join(process.stderr).strip()
                    reason = ' '.join(part for part in (session_error, detail[-800:]) if part)
                    raise ProviderError('GitHub Copilot: ' + (reason or str(exc))) from exc
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
                elif kind == 'session.error':
                    # 2026-10-19: the reason (e.g. "You have exceeded your monthly quota", errorCode quota_exceeded)
                    # must reach the engine, or a usage limit / sign-in loss looks like a plain failure and the
                    # Claude <-> Copilot failover never starts.
                    session_error = ' '.join(str(data.get(k) or '') for k in ('message', 'errorCode', 'errorType')).strip()[:500]
                    if data.get('errorCode') == 'quota_exceeded' or 'monthly quota' in str(data.get('message', '')):
                        from team_usage import mark_copilot_exhausted
                        mark_copilot_exhausted()   # the screen shows 0% (采来 counts only its own runs otherwise)
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
                        raise ProviderError('GitHub Copilotの実行が完了しませんでした（終了コード ' + str(msg.get('exitCode')) + '）。'
                                            + ('理由: ' + session_error if session_error else ''))
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


class GeminiAdapter:
    """Gemini CLI headless, as an emergency reserve on the free API tier (2026-10-13, user decision).
    Fail-closed like Copilot: a private CLI home whose policy denies every tool except 采来's read-only broker
    (planner/reviewer/consultation only). The API key comes from the Credential Manager into this one child's
    environment only. Free-tier input may be used for Google's training, so nothing that the Input Guard flags is sent.
    Each model has a daily request cap: an exhausted or overloaded model is skipped for the next one."""

    QUOTA = ('exhausted your daily quota', 'resource_exhausted', 'terminalquotaerror', 'quota exceeded')
    BUSY = ('high demand', 'unavailable', '"code": 503', 'status 503')
    READ_TOOLS = ('list_files', 'read_file', 'search_files', 'read_document')

    def run(self, ctx, prompt, schema):
        from team_config import GEMINI_KEY_TARGET, GEMINI_MODELS, GEMINI_ROLES
        from team_claude_auth import read_stored_token
        from team_dlp import scan_input
        profile, role = ctx.task['profile'], ctx.task.get('role')
        if (getattr(ctx, 'attachment_ids', []) or getattr(ctx, 'reference_images', None)
                or getattr(ctx, 'artifact_format', None) or getattr(ctx, 'document_scope', False)):
            raise ProviderError('Geminiは添付ファイル・画像・資料作成の作業に未対応です。CodexまたはClaudeへ切り替えてください。'
                                'モデル実行は開始していません。', 'gemini_scope')
        if role not in GEMINI_ROLES or not getattr(ctx, 'text_only', False) or ctx.agent_definition.sandbox != 'read-only':
            raise ProviderError('Geminiは計画・レビュー・相談（非常用）の担当にだけ使えます。この作業はCodexまたはClaudeへ'
                                '切り替えてください。モデル実行は開始していません。', 'gemini_scope')
        sent = ctx.agent_system_instructions + '\n\n' + prompt
        found = scan_input(sent)
        if found['block'] or found['confirm']:
            raise ProviderError('Geminiの無料枠は入力が学習に使われる可能性があるため、次を含む内容は送りません: '
                                + '、'.join(found['block'] + found['confirm']) + '。CodexまたはClaudeへ切り替えてください。'
                                'モデル実行は開始していません。', 'gemini_scope')
        key = read_stored_token(GEMINI_KEY_TARGET)
        if not key:
            raise ProviderError('GeminiのAPIキーが資格情報マネージャー（' + GEMINI_KEY_TARGET + '）にありません。'
                                'モデル実行は開始していません。', 'gemini_auth')
        read_mcp = getattr(ctx, 'read_mcp', None) if getattr(ctx, 'consultation_research', False) else None
        models = [profile['model']] + [m for m in GEMINI_MODELS if m != profile['model']]
        text = (ctx.agent_system_instructions
                + ('\n\n---\n使える道具は project_read の list_files・read_file・search_files・read_document（読み取り専用）だけです。'
                   '回数に上限があるため、必要な読み取りだけにしてください。' if read_mcp else '')
                + '\n\n---\n\n' + prompt
                + report_format_instruction(schema, include_schema=True))
        with tempfile.TemporaryDirectory(prefix='agent-team-gemini-') as root:
            home, work = Path(root) / 'home', Path(root) / 'work'
            (home / '.gemini' / 'policies').mkdir(parents=True)
            work.mkdir()
            self._configure(home, read_mcp)
            env = {k: v for k, v in os.environ.items() if k not in ('GOOGLE_API_KEY', 'GEMINI_API_KEY')}
            env.update({'GEMINI_API_KEY': key, 'GEMINI_CLI_HOME': str(home), 'GEMINI_CLI_TRUST_WORKSPACE': 'true'})
            ctx.agent_started('gemini-' + uuid.uuid4().hex[:12], 'Gemini CLI / 非常用（読み取り専用）')
            skipped = []
            for model in models:
                try:
                    ctx.event('Geminiへ接続しました。モデル: ' + model)
                    from team_usage_metrics import observe_prompt
                    observe_prompt(ctx,text,includes_system=True)
                    answer = self._call(ctx, model, text, work, env)
                except ProviderError as exc:
                    if exc.code in ('gemini_quota', 'gemini_busy'):
                        skipped.append(model + '（' + ('1日の上限' if exc.code == 'gemini_quota' else '混雑') + '）')
                        ctx.event('Geminiの ' + model + ' は使えません（' + str(exc)[:200] + '）。次のモデルを試します。')
                        continue
                    raise
                try:
                    result = copilot_answer(answer, schema)
                    errors = schema_errors(result, schema)
                except ProviderError:
                    result, errors = None, ['結果（JSONとして読めない）']
                if not errors:
                    return result
                if not allow_automatic_format_retry(ctx):
                    raise ProviderError('報告の形式を回収できませんでした（' + '、'.join(errors[:8])[:300]
                                        + '）。成果は保持しました。追加のモデル実行は利用者の判断を待ちます。', 'format')
                ctx.event('報告の項目が指定と違うため、同じモデルに形だけ直させます：' + '、'.join(errors[:8])[:400])
                ask = (text + '\n\n---\n直前の回答は、指定のJSONスキーマに合っていません（' + '、'.join(errors[:20])
                       + '）。直前の回答:\n' + (answer or '')[:20000] + '\n\n調べ直さず、同じ内容を指定のスキーマに合うJSONオブジェクト'
                       'だけで出力し直してください。確認していないことは未確認と書いてください。')
                observe_prompt(ctx,ask,includes_system=True,kind='format_repair')
                answer = self._call(ctx, model, ask, work, env, repair=True)
                try:
                    result = copilot_answer(answer, schema)
                    errors = schema_errors(result, schema)
                except ProviderError:
                    errors = ['結果（JSONとして読めない）']
                if errors:
                    raise ProviderError('担当の報告に必要な項目がそろいませんでした（' + '、'.join(errors[:8])[:300]
                                        + '）。形の修正を1回頼みましたが直りませんでした。担当・モデルを切り替えてください。', 'format')
                ctx.event('報告の形を直しました（同じモデル、道具なし）。')
                return result
            raise ProviderError('Geminiの無料枠は今日は使えません: ' + '、'.join(skipped)
                                + '。非常用のため、CodexまたはClaudeへ切り替えるか、上限のリセット後にやり直してください。', 'gemini_quota')

    def _configure(self, home, read_mcp):
        settings = {'security': {'auth': {'selectedType': 'gemini-api-key'}}, 'general': {'enableAutoUpdate': False},
                    'telemetry': {'enabled': False}, 'privacy': {'usageStatisticsEnabled': False},
                    'tools': {'exclude': ['run_shell_command', 'write_file', 'replace', 'read_file', 'list_directory', 'glob',
                                          'search_file_content', 'grep_search', 'google_web_search', 'web_fetch',
                                          'read_many_files', 'save_memory', 'write_todos', 'activate_skill',
                                          'invoke_agent', 'enter_plan_mode']}}
        # Deny everything, then allow only the broker's read tools (a higher priority wins).
        rules = '[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 100\n'
        if read_mcp:
            settings['mcpServers'] = {'project_read': {'command': read_mcp['command'], 'args': read_mcp['args']}}
            settings['mcp'] = {'allowed': ['project_read']}
            rules += ('\n[[rule]]\nmcpName = "project_read"\ntoolName = ' + json.dumps(list(self.READ_TOOLS))
                      + '\ndecision = "allow"\npriority = 200\n')
        (home / '.gemini' / 'settings.json').write_text(json.dumps(settings), encoding='utf-8')
        (home / '.gemini' / 'policies' / 'agent-team.toml').write_text(rules, encoding='utf-8')

    def _call(self, ctx, model, text, cwd, env, repair=False):
        from team_egress_guard import require_safe_egress
        require_safe_egress(text, 'gemini')
        command = ctx.command + ['-p', '標準入力の指示に従って回答してください。', '-m', model, '-o', 'json', '--skip-trust']
        process, out = None, []
        try:
            ctx.check()
            process = Process(command, cwd, env)
            process.proc.stdin.write(text)
            process.proc.stdin.close()
            while True:
                ctx.check()
                try:
                    line = process.lines.get(timeout=.25)
                except queue.Empty:
                    continue
                if line is None:
                    break
                out.append(line)
            process.proc.wait(timeout=30)
            raw, detail = ''.join(out), ''.join(process.stderr)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {}
            lowered = (detail + raw).lower()
            if process.proc.returncode != 0 or data.get('error') or not data.get('response'):
                if any(s in lowered for s in self.QUOTA) or process.proc.returncode == 429:
                    raise ProviderError('Gemini ' + model + ': 無料枠の上限', 'gemini_quota')
                if any(s in lowered for s in self.BUSY):
                    raise ProviderError('Gemini ' + model + ': 混雑', 'gemini_busy')
                raise ProviderError('Geminiの実行が完了しませんでした（終了コード ' + str(process.proc.returncode) + '）: '
                                    + (str(data.get('error', ''))[:300] or detail[-300:]), 'gemini')
            self._usage(ctx, data)
            return data['response']
        except subprocess.TimeoutExpired as exc:
            raise ProviderError('Geminiの終了待ちが時間切れになりました。') from exc
        finally:
            if process is not None:
                process.close()

    @staticmethod
    def _usage(ctx, data):
        """Token counts per model, added to this run's record (a guide: the free tier is not billed)."""
        run = getattr(ctx, 'agent_run', None)
        if not isinstance(run, dict):
            return
        total = run.setdefault('gemini_tokens', {})
        for stats in ((data.get('stats') or {}).get('models') or {}).values():
            for key, value in (stats.get('tokens') or {}).items():
                if isinstance(value, (int, float)):
                    total[key] = total.get(key, 0) + value
            requests = (stats.get('api') or {}).get('totalRequests')
            if isinstance(requests, (int, float)):
                run['gemini_requests'] = run.get('gemini_requests', 0) + requests


ADAPTERS = {'codex': CodexAdapter, 'claude': ClaudeAdapter, 'copilot': CopilotAdapter, 'gemini': GeminiAdapter}
