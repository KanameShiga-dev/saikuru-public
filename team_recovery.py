"""Local recovery guidance. Never execute commands supplied by a model report."""
import json
import re
import subprocess
import time
from pathlib import Path


def desktop_connection_state():
    """Read only the current session state, without account names or screen data."""
    import os
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes
    session = wintypes.DWORD()
    api = ctypes.WinDLL('wtsapi32', use_last_error=True)
    query = api.WTSQuerySessionInformationW
    query.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
                      ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    query.restype = wintypes.BOOL
    api.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    buffer, size = ctypes.c_void_p(), wintypes.DWORD()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        return None
    if not query(None, session.value, 8, ctypes.byref(buffer), ctypes.byref(size)):
        return None
    try:
        return ctypes.cast(buffer, ctypes.POINTER(ctypes.c_int)).contents.value if size.value >= 4 else None
    finally:
        api.WTSFreeMemory(buffer)


def handoff_report_advice(task):
    recovery = task.get('recovery') or {}
    if recovery.get('code') != 'handoff_rejected':
        return None
    if task.get('role') not in ('builder', 'researcher') or '未完了項目の引き継ぎには利用者の範囲指定' not in recovery.get('message', ''):
        return None
    summary = recovery.get('reported_summary', '')
    if not re.search(r'工程[0-9０-９]+.{0,18}((?<!未)完了|検証を終え(?!てい(?:ない|ません)))|担当.{0,12}(?<!未)完了', summary):
        return None
    return {'attempt': task['attempt'], 'checked_at': time.time(), 'evidence': [],
        'code': 'task_completion', 'title': '担当工程の完了を報告する回答案',
        'explanation': '担当は今回の工程を終えたと報告していますが、未完了作業の移管を意味するhandoffを返したため、統括が拒否しました。報告された証拠の合格を統括が確認済みという意味ではありません。',
        'next_step': '移管欄は使わず、下の回答案で担当工程の完了条件と証拠を確認して再報告してください。',
        'draft': '今回の担当工程が完了しており、残る作業が元の計画どおり後続工程の担当範囲なら、今回の結果をstatus=doneで報告してください。'
            '\n既存の報告書と検証証拠を確認し、変更がなければ退避・テストを重複実行しないでください。'
            '\n依頼全体が未完了であることと、今回の担当工程が完了したことを区別してください。今回の担当工程自体に未完了項目がある場合のみ、具体的に報告してください。',
        'can_retry': True}


def advice(task, project):
    completed = handoff_report_advice(task)
    if completed:
        return completed
    summary = task.get('summary', '')
    result = task.get('result') or {}
    report = summary + '\n' + json.dumps(result, ensure_ascii=False)
    answer = {'attempt': task['attempt'], 'checked_at': time.time(), 'evidence': [],
              'code': 'unknown', 'title': '停止理由の確認が必要です',
              'explanation': summary or '具体的な停止理由は報告されていません。',
              'next_step': '担当へ、失敗した操作とエラー全文の報告を依頼してください。',
              'draft': '失敗した実行ファイル・操作、作業ディレクトリ、終了コード、エラー全文を報告してください。秘密情報は含めないでください。同じ復旧待ちの案内だけを繰り返さないでください。',
              'can_retry': True}
    if re.search(r"hit your session limit", summary, re.I):
        reset = re.search(r'resets\s+(.+)', summary, re.I)
        answer.update(code='quota', title='Claudeの5時間枠を使い切りました',
            explanation='Claude CLIが利用枠の上限を報告しています。' + ('リセット案内: '+reset[1] if reset else ''),
            next_step='Codexに切り替えるか、利用枠の回復を確認して再試行してください。', draft='', can_retry=False)
    elif re.search(r'Failed to authenticate|OAuth.*expired|authentication', summary, re.I):
        answer.update(code='auth', title='ログイン状態の復旧が必要です',
            next_step='対象CLIへ再ログインし、成功後に再試行してください。', draft='', can_retry=False)
    elif '引き継ぎ文脈が大きすぎ' in summary:
        answer.update(code='context_size', title='引き継ぎ情報が一度に渡せる量を超えました',
            explanation='ゲームの実行エラーではなく、担当へ渡す引き継ぎ情報のサイズ上限で停止しました。履歴DBは保持されています。情報を索引付きの分割ファイルにして順番に読み取る方式へ変更しました。',
            next_step='下の回答案で再開してください。最新の利用者判断と現在の事実を先に確認し、必要な報告を分割して読み取ります。',
            draft='引き継ぎ文脈の分割読込に対応しました。索引の最新の利用者判断・現在の事実は全partを読み、関連する担当報告を確認して未完了部分から続行してください。Computer Useは使わず、元のデータと証拠を保持し、完了済みの処理を重複実行しないでください。',
            can_retry=True)
    elif re.search(r'Python', report, re.I) and re.search(r'起動失敗|起動でき|プロセス作成失敗|Unable to create process', report, re.I):
        root = Path(project).resolve()
        names = ['.venv']
        for name in re.findall(r'(?<![\w])\.tmp_[A-Za-z0-9_-]*venv[A-Za-z0-9_-]*', report):
            if name not in names:
                names.append(name)
        for name in names[:4]:
            path = root / name / 'Scripts' / 'python.exe'
            item = {'file': str(path), 'ok': False}
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                item['result'] = 'プロジェクト外への参照のため確認しません。'
            elif not path.is_file():
                item['result'] = '実行ファイルが見つかりません。'
            else:
                try:
                    proc = subprocess.run([str(path), '--version'], cwd=root, shell=False,
                        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    version = (proc.stdout.strip() or proc.stderr.strip())
                    item.update(ok=proc.returncode == 0 and bool(re.fullmatch(r'Python \d+\.\d+\.\d+\S*', version)),
                                exit_code=proc.returncode)
                    # Keep version or a fixed description, never arbitrary child output.
                    item['result'] = version if item['ok'] else '起動確認に失敗しました。'
                except (OSError, subprocess.TimeoutExpired) as exc:
                    item['result'] = '起動できませんでした（'+type(exc).__name__+'）。'
            answer['evidence'].append(item)
        ready = all(item['ok'] for item in answer['evidence'])
        answer.update(code='python', title='テスト用Pythonの起動確認',
            explanation='担当はPython起動失敗で停止しました。今回の確認は統括サービス側です。担当側の実行環境の復旧はまだ確認できていません。',
            next_step='移管欄は使わず、下の回答案を再試行の入力欄へ入れて再開してください。' if ready else
                '統括側でも起動確認に失敗しました。Python環境を復旧してから再確認してください。工程3への移管では解消しません。',
            can_retry=ready,
            draft=('統括サービス側では、対象仮想環境のpython.exe --versionが成功しました。確認結果: '
                   + ' / '.join(item['file']+' = '+item['result'] for item in answer['evidence'])
                   + '\n担当側でも起動を再確認し、成功したら現在の工程の残作業を続行してください。既存の退避は重複実行しないでください。'
                   + '\n再び失敗する場合は、使用した実行ファイル、作業ディレクトリ、終了コード、エラー全文を記録して報告してください。同じ復旧待ちの案内だけを繰り返さないでください。') if ready else '')
    elif 'Computer Use was not approved' in report or 'Computer Useの承認' in report:
        session_state = desktop_connection_state()
        disconnected = session_state is not None and session_state != 0
        answer.update(code='computer_use', title='PCの画面接続が必要です' if disconnected else 'ゲーム画面への操作許可を確認してください',
            explanation='担当はゲームのウィンドウ取得をComputer Useの承認チェックで拒否されました。ボードの操作承認とは別のチェックです。'
                + (' 現在、統括のWindows画面セッションも接続されていません。背景処理と実画面操作は条件が異なります。' if disconnected else ''),
            next_step='PCでWindowsにサインインし、画面のロックを解除して対象ゲームを表示してください。その後「原因を確認して回答案を作る」を押し、回答案から一度だけ再開してください。' if disconnected else
                '下の回答案で一度だけ再開してください。再び拒否された場合は担当環境の許可反映が未解消なので、同じ再試行を繰り返さないでください。',
            draft='対象はC:\\Projects\\bravia-New-Games\\builds\\pc-runtime-smoke\\BraveStrategyPcRuntimeSmoke.exeだけです。利用者はこのゲームの画面取得と検証のための操作を承認しています。'
                '既存のゲーム・隔離データ・証拠を保持し、再ビルド・再seedはしないでください。'
                '最初に対象ウィンドウ1件を選択してComputer Useの取得・画面取得を確認し、成功した場合だけ未完了の実操作を続けてください。'
                '拒否や画面取得失敗の場合は一度で停止し、操作名とエラーを報告してください。承認チェックの迂回や別手段の入力は禁止です。',
            can_retry=not disconnected)
        answer['evidence'].append({'file': 'Windows session', 'result': 'disconnected' if disconnected else 'active' if session_state == 0 else 'unconfirmed'})
    elif re.search(r'ACL|PreToolUse|フック.*拒否', summary, re.I):
        answer.update(code='permission', title='実行権限またはフックによる拒否です',
            next_step='権限・フックの設定と拒否理由を確認する必要があります。移管や再試行だけでは解消しません。',
            draft='', can_retry=False)
    elif re.search(r'未完了項目の引き継ぎには利用者の範囲指定', summary):
        answer.update(code='transfer', title='引き継ぎ範囲の登録が必要です',
            next_step='「ボードから復旧する」で、登録済み移管を確認するか、後続工程へ移す必須作業を指定してください。',
            draft='', can_retry=False)
    return answer
