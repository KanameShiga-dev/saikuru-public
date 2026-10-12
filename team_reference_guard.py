"""Reference guard: secrets in the HANDOFF.md snapshot are never sent to a model (2026-10-10 user decision).

The secret policy ranks above the token reduction and works whether the reduction is ON or OFF. A snapshot with a
credential-like string stops the task before anything is sent, and a person decides how to continue. What was found
is shown only as kind, line and a masked line: the value itself never appears on the screen, in records or in logs.
"""
import hashlib
import json
import re

from team_dlp import SECRET

QUESTION_ID = 'reference_secret'
PLACEHOLDER = '[HANDOFF.md の控えは、認証情報・秘密情報らしき文字列を含むため送っていません（利用者の判断）]'


def _kind(match):
    text = match.group(0)
    if text.startswith('-----BEGIN'):
        return '秘密鍵'
    if re.match(r'AKIA', text):
        return 'AWSのアクセスキーらしき文字列'
    if re.match(r'(?i)(ghp|gho|github_pat)', text):
        return 'GitHubのトークンらしき文字列'
    if re.match(r'(?i)xox', text):
        return 'Slackのトークンらしき文字列'
    if re.match(r'(?i)sk[-_]', text):
        return 'APIキーらしき文字列'
    if text.startswith('eyJ'):
        return 'トークン（JWT）らしき文字列'
    return 'パスワード・キーの記入らしき箇所'


def findings(text):
    """[{'line', 'kind', 'masked'}] with the whole matching line hidden, including its potentially private prefix."""
    result = []
    for number, line in enumerate(str(text or '').splitlines(), 1):
        match = SECRET.search(line)
        if match:
            result.append({'line': number, 'kind': _kind(match), 'masked': '●●●（行全体を伏せ字）'})
    return result


def digest(text):
    return hashlib.sha256(str(text).encode('utf-8')).hexdigest()


def check(references, withheld_hashes):
    """Return (blocked, withheld). blocked: [{'field', 'sha256', 'findings'}] needing a person's decision.
    withheld: [(field, text)] the person chose not to send (only that exact snapshot, by its hash)."""
    blocked, withheld = [], []
    for field, text in references:
        if not isinstance(text, str) or not text or not SECRET.search(text):
            continue
        if digest(text) in (withheld_hashes or []):
            withheld.append((field, text))
        else:
            blocked.append({'field': field, 'sha256': digest(text), 'findings': findings(text)})
    return blocked, withheld


def strip(prompt, text):
    """The prompt with the one JSON-escaped occurrence of the snapshot replaced by the placeholder, or None when it
    is not found exactly once (then the task must not run: the snapshot could not be kept out)."""
    needle = json.dumps(text, ensure_ascii=False)[1:-1]
    if prompt.count(needle) != 1:
        return None
    return prompt.replace(needle, json.dumps(PLACEHOLDER, ensure_ascii=False)[1:-1])


def question(blocked):
    """Approval payload (kind 'question'): what was found, masked, and the two ways to continue. Sending as is is
    not offered: the secret policy is not decided per task."""
    lines = []
    for item in blocked:
        for found in item['findings'][:20]:
            lines.append(f"{found['line']}行目：{found['kind']}　{found['masked']}")
        if len(item['findings']) > 20:
            lines.append(f"ほか {len(item['findings']) - 20} か所")
    summary = ('プロジェクトの HANDOFF.md の控えに、認証情報・秘密情報らしき文字列が見つかりました。秘密情報はAIに送らない方針のため、'
               'この作業はモデルへ何も送らずに止めています。値は表示していません（伏せ字）。\n\n' + '\n'.join(lines)
               + '\n\nHANDOFF.md を開いて該当の行を確認してください。本物の秘密情報なら、HANDOFF.md から削除してから続けてください。'
               '控えを送らない場合も、ファイル自体は担当が読み取りの道具で開けるため、本物の秘密情報を残したまま続けないでください。')
    return {'summary': summary, 'reference_guard': True, 'withhold_hashes': [item['sha256'] for item in blocked],
            'questions': [{'id': QUESTION_ID, 'text': 'HANDOFF.md の控えに秘密情報らしき文字列があります。どう続けますか？',
                           'options': [
                               {'id': 'removed', 'label': 'HANDOFF.md から削除・修正した（もう一度点検してから続ける）',
                                'input_required': False, 'input_label': '補足（任意。値は書かないでください）'},
                               {'id': 'withhold', 'label': '秘密情報ではない（誤検知）。この依頼では控えを送らずに続ける',
                                'input_required': False, 'input_label': '補足（任意。値は書かないでください）'}]}]}
