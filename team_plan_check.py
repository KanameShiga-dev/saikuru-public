"""Plan check: tasks must fit the tools of the role they are assigned to.

Read-only roles (researcher, reviewer) cannot run commands in a development job, and no role can run commands in
a document job (document tools only). A plan that asks them to (version checks, hash calculation, test runs, ...)
fails at run time after several attempts, so it is sent back to the planner with the reason instead.
"""
import re

# Sentences that only forbid or exclude something ("pip / npm によるインストールは禁止") are not requests to run it.
NEGATION = re.compile(r'禁止|しない|しません|不可|行わない|使わない|使えない|不要|ではなく|避け|含めない')
COMMAND = re.compile(
    r'(?i)(?:\bpython3?\s+(?:-c|-m|[\w./\\:-]+\.py)|\bpy\s+-\d|\bpip\s+(?:install|list|show|freeze)|\bnpm\s+\w+|\bnode\s+(?:--version|-v|[\w./\\-]+\.js)'
    r'|\bgit\s+(?:status|log|diff|show|rev-parse)|Get-(?:FileHash|ChildItem|Content|Item)|\bpytest\b|--version'
    r'|コマンド(?:を|で)(?:実行|使|確認)|スクリプトを実行|テストを実行|ハッシュ(?:値)?(?:を|の)?(?:計算|算出)'
    r'|SHA-?256\s*(?:を|の)?(?:計算|算出))')
READ_ONLY_ROLES = ('researcher', 'reviewer')
ROLE_NAMES = {'planner': '計画担当', 'builder': '作業担当', 'researcher': '調査担当', 'reviewer': 'レビュー担当'}
MAX_REPAIRS = 2


def _requested_commands(text):
    hits = []
    for sentence in re.split(r'[。\n]', str(text or '')):
        if not sentence.strip() or NEGATION.search(sentence):
            continue
        match = COMMAND.search(sentence)
        if match:
            hits.append(match.group(0).strip())
    return hits


def issues(result, document_job):
    """[(task number, role, title, matched text)] for tasks whose role cannot run the commands they ask for."""
    found = []
    for number, task in enumerate(result.get('tasks') or [], 1):
        role = task.get('role')
        if not document_job and role not in READ_ONLY_ROLES:
            continue
        hits = _requested_commands(str(task.get('title', '')) + '\n' + str(task.get('instruction', '')))
        if hits:
            found.append((number, role, str(task.get('title', ''))[:60], hits[0][:40]))
    return found


def repair_note(found, document_job):
    lines = ['計画の点検で、担当の道具に合わない作業が見つかりました。計画を作り直してください。']
    for number, role, title, hit in found:
        lines.append(f'- 作業{number}「{title}」（{ROLE_NAMES.get(role, role)}）：コマンドの実行（{hit}）が必要ですが、この担当はコマンドを使えません。')
    if document_job:
        lines.append('資料作成の依頼では、どの担当もコマンドを使えません。確認は資料用の専用ツール（list_files・read_file・read_document・media_environment など）でできる方法に変えてください。'
                     'ツールで確認できないことは、未確認として報告する項目にしてください。')
    else:
        lines.append('調査担当とレビュー担当は読み取り専用です（ファイルの一覧・内容の読み取り・Web検索だけ）。'
                     'コマンドが必要な確認（版の確認、ハッシュの計算、テストの実行など）は、コマンドを使える作業担当（builder）の作業に含めてください。'
                     'または、読み取りで確認できる方法に変えてください。')
    return '\n'.join(lines)


def role_guide(document_job):
    """Shown to the planner up front so the plan fits the roles' tools in the first place."""
    if document_job:
        return ('\n担当の道具：資料作成の依頼では、どの担当もコマンドを実行できません。作業担当は資料用の専用ツールで作成し、'
                '調査・レビュー担当は専用ツールでの読み取りだけです。コマンドが必要な確認を計画に入れないでください。'
                '資料の本文（PDF・PowerPoint・Word・Excel）はread_documentで読めます。見た目（レイアウト・配色・文字のはみ出し）は'
                '受け入れ時に自動で画像化され利用者が確認するため、見た目だけを確認する作業は計画しないでください。')
    return ('\n担当の道具：調査担当（researcher）とレビュー担当（reviewer）は読み取り専用で、コマンドを実行できません（ファイルの一覧・内容の読み取り・Web検索だけ）。'
            'コマンドが必要な確認（版の確認、ハッシュの計算、テストの実行など）は、作業担当（builder）の作業に含めてください。'
            'PDF・PowerPoint・Word・Excel の本文は read_document で読めます。見た目（レイアウト・配色）は受け入れ時に利用者が確認するため、'
            '見た目だけを確認する作業は計画しないでください。')
