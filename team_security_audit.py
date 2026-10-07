"""Evidence for the per-job security review: operation log and a local scan of changed files.

Nothing here is sent anywhere by itself; the summary is appended to the security reviewer's prompt.
Scan findings show file, line and category only (matched values are masked).
"""
import re
import os
from pathlib import Path

from team_web_guard import SECRET, EMAIL, PHONE, PRIVATE_IP, INTERNAL_HOST, load_policy

SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'dist', 'build', '.harness'}
TEXT = {'.py', '.js', '.ts', '.tsx', '.jsx', '.html', '.css', '.md', '.json', '.toml', '.yaml', '.yml', '.txt',
        '.cs', '.xml', '.ps1', '.sh', '.sql', '.ini', '.cfg', '.csv', '.env', '.bat', '.cmd', '.svg'}
OPERATION_EVENTS = ('operation_bash', 'auto_approved', 'websearch_allowed', 'websearch_blocked', 'progress',
                    'artifact_create_approved', 'artifact_create_rejected', 'artifact_created', 'artifact_create_unknown',
                    'artifact_published', 'artifact_blocked', 'artifact_staging_write', 'artifact_read',
                    'skill_script', 'skill_read')


def operation_log(store, job_id, limit=120):
    """Commands, auto-approvals, web searches and edits recorded for this job (oldest first)."""
    lines = []
    for event in reversed(store.events(5000)):
        if event.get('job_id') != job_id or event.get('type') not in OPERATION_EVENTS:
            continue
        message = str(event.get('message', ''))
        if event['type'] == 'progress' and not message.startswith(('依頼範囲内の編集を許可', 'プロジェクト内の読み取りを自動承認', '対象プロジェクトの検索')):
            continue
        from team_web_guard import operation_summary
        safe=message if event['type']=='operation_bash' and message.startswith('操作分類: ') else operation_summary(message)
        lines.append(f"- [{event['type']}] {safe}")
        if len(lines) >= limit:
            lines.append('- …（以降省略）')
            break
    return lines


def _patterns():
    policy = load_policy()
    found = [(SECRET, '認証情報・秘密情報らしき文字列'), (EMAIL, 'メールアドレス'), (PHONE, '電話番号'),
             (PRIVATE_IP, '社内IPアドレス'), (INTERNAL_HOST, '社内ホスト名')]
    for term in policy.get('blocked_terms', []):
        if str(term).strip():
            found.append((re.compile(re.escape(str(term).strip()), re.I), '社内の固有名詞（ポリシー指定）'))
    for domain in policy.get('blocked_domains', []):
        if str(domain).strip():
            found.append((re.compile(re.escape(str(domain).strip().lstrip('.')), re.I), '社内ドメイン（ポリシー指定）'))
    return found


def scan_changed_files(project, since, limit=60):
    """Scan text files (and Word/Excel text) changed in the project since the job started."""
    root = Path(project).resolve()
    findings, scanned, skipped = [], 0, 0
    patterns = _patterns()
    def candidates():
        count=0
        for directory,dirs,names in os.walk(root,followlinks=False):
            dirs[:]=[d for d in dirs if d.lower() not in SKIP_DIRS
                     and not (Path(directory)/d).is_symlink()
                     and (Path(directory)/d).resolve().is_relative_to(root)]
            for name in names:
                count+=1
                if count>10000:return
                yield Path(directory)/name
    for path in candidates():
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):continue
        if path.name.lower() in ('.credentials.json','auth.json','credentials.json') or path.suffix.lower() in ('.pem','.key'):
            skipped+=1;continue
        if scanned>=500:skipped+=1;break
        try:
            if path.stat().st_mtime < since:
                continue
            suffix = path.suffix.lower()
            if suffix in ('.docx', '.xlsx'):
                from document_tools import office_text
                text = office_text(path, limit=200000)['content']
            elif suffix in TEXT or path.name.startswith('.env'):
                if path.stat().st_size > 1_000_000:
                    skipped += 1
                    continue
                text = path.read_text(encoding='utf-8', errors='replace')
            else:
                skipped += 1
                continue
        except Exception:
            skipped += 1
            continue
        scanned += 1
        for number, line in enumerate(text.splitlines(), 1):
            for pattern, label in patterns:
                if pattern.search(line):
                    findings.append(f'- {path.relative_to(root)}:{number} {label}')
                    break
            if len(findings) >= limit:
                return findings,scanned,skipped
    return findings, scanned, skipped


def evidence(store, job):
    """Text block appended to the common-security-reviewer prompt."""
    try:
        ops = operation_log(store, job['id'])
    except Exception as exc:
        ops = [f'- 操作記録を取得できません: {type(exc).__name__}']
    try:
        findings, scanned, skipped = scan_changed_files(job['project'], float(job.get('created_at') or 0))
    except Exception as exc:
        findings, scanned, skipped = [f'- 機械チェックを実行できません: {type(exc).__name__}'], 0, 0
    return ('\n\n【統括が添付する証拠（采来がローカルで作成）】\n'
            '■ この依頼の操作記録（コマンド・自動承認・Web検索・編集）\n' + ('\n'.join(ops) or '- 記録なし') +
            f'\n■ 依頼開始後に作成・変更されたファイルの機械チェック（{scanned}件を確認、PDF等の対象外{skipped}件。値は伏せています）\n'
            + ('\n'.join(findings) or '- 該当なし') +
            '\n機械チェックは規則による候補で、誤検知・見逃しがあります。該当行は読み取って実害の有無を判断してください。'
            '値そのものは報告に書き写さないこと。')
