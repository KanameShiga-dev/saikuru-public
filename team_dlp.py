"""Shared detectors for the Input Guard and the Tool (outbound) Guard.

Security by Default: these checks are always on. Exceptions are only the ones an administrator writes in
data/security-policy.json (outside every project, so an agent or a project file cannot add one).
Findings carry category labels only; matched values are never logged or returned.
"""
import json
import re

from team_config import ROOT

POLICY = ROOT / 'data' / 'security-policy.json'
DEFAULT_POLICY = {
    '_note': '管理者が明示的に許可する例外だけを書く。既定はすべて空（安全機能は最初からON）。',
    # Outbound commands an administrator allows to run under automatic approval: [{"category": "install", "contains": "pip install --user reportlab", "reason": "..."}]
    'outbound_exceptions': [],
    # Company, customer, product and internal system names that must not leave this PC (also used by the Web search check)
    'confidential_terms': [],
}

SECRET = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b|\b(?:ghp|gho|github_pat|sk|xox[abpr])[-_][A-Za-z0-9_-]{16,}\b'
                    r'|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.|(?:api[_ -]?key|access[_ -]?token|secret|password|passwd|パスワード)\s*[:=＝]', re.I)
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
PHONE = re.compile(r'(?<!\d)(?:0\d{1,4}-\d{1,4}-\d{3,4}|0[789]0\d{8}|\+81[- ]?\d{1,4}[- ]?\d{1,4}[- ]?\d{3,4})(?!\d)')
PRIVATE_IP = re.compile(r'(?<![\d.])(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?![\d.])')
INTERNAL_HOST = re.compile(r'\b[\w-]+(?:\.[\w-]+)*\.(?:local|lan|corp|internal|intra|intranet|localdomain)\b', re.I)
LOCAL_PATH = re.compile(r'(?:\b[A-Za-z]:[\\/]|\\\\[\w.$-]+\\|(?:^|\s)~[\\/]|/(?:home|Users)/[\w.-]+)')
MY_NUMBER = re.compile(r'(?<!\d)\d{4}[- ]?\d{4}[- ]?\d{4}(?!\d)')
CARD = re.compile(r'(?<!\d)(?:\d[ -]?){13,16}(?!\d)')


def load_policy():
    """Administrator policy. Created with empty exceptions on first use; a broken file fails closed (no exceptions)."""
    if not POLICY.exists():
        POLICY.parent.mkdir(parents=True, exist_ok=True)
        POLICY.write_text(json.dumps(DEFAULT_POLICY, ensure_ascii=False, indent=2), encoding='utf-8')
    if POLICY.stat().st_size > 200_000:
        raise ValueError('security-policy.json が大きすぎます。')
    data = json.loads(POLICY.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError('security-policy.json の形式が不正です。')
    policy = dict(DEFAULT_POLICY, **data)
    if not isinstance(policy['outbound_exceptions'], list) or not isinstance(policy['confidential_terms'], list):
        raise ValueError('security-policy.json の形式が不正です。')
    return policy


def _luhn(digits):
    total, odd = 0, True
    for ch in reversed(digits):
        n = int(ch)
        if not odd:
            n = n * 2 - 9 if n > 4 else n * 2
        total, odd = total + n, not odd
    return total % 10 == 0


def has_card_number(text):
    for match in CARD.finditer(text):
        digits = re.sub(r'\D', '', match.group(0))
        if 13 <= len(digits) <= 16 and _luhn(digits):
            return True
    return False


def confidential_terms():
    """Policy terms from both the security policy and the Web search policy (shared list)."""
    terms = []
    try:
        terms += [str(t) for t in load_policy()['confidential_terms']]
    except (OSError, ValueError) as exc:
        raise ValueError('情報保護ポリシーを読み取れないため送信を停止しました。') from exc
    try:
        from team_web_guard import load_policy as web_policy
        terms += [str(t) for t in web_policy()['blocked_terms']]
    except (OSError, ValueError) as exc:
        raise ValueError('情報保護ポリシーを読み取れないため送信を停止しました。') from exc
    return [t.strip() for t in terms if t and t.strip()]


def scan_input(text):
    """Input Guard: what a person is about to send to the model.

    Returns {'block': [labels], 'confirm': [labels]}. Secrets are blocked (they are never needed by the AI);
    personal data and confidential names need the sender's explicit confirmation."""
    text = str(text or '')
    block, confirm = [], []
    if SECRET.search(text):
        block.append('認証情報・秘密情報らしき文字列（パスワード・APIキー・トークン・秘密鍵）')
    if EMAIL.search(text):
        confirm.append('メールアドレス')
    if PHONE.search(text):
        confirm.append('電話番号')
    if MY_NUMBER.search(text):
        confirm.append('12桁の番号（個人番号の可能性）')
    if has_card_number(text):
        confirm.append('カード番号の可能性がある数字')
    lowered = text.casefold()
    if any(t.casefold() in lowered for t in confidential_terms()):
        confirm.append('社内・顧客の固有名詞（ポリシー指定）')
    return {'block': block, 'confirm': confirm}


def require_clean_input(text, confirmed=False, where='入力'):
    """Raise ValueError when the Input Guard stops the text. The message starts with a fixed tag so the screen
    can ask the person to confirm and send again (only for 'confirm' findings, never for secrets)."""
    found = scan_input(text)
    if found['block']:
        raise ValueError(where + 'に' + '、'.join(found['block']) + 'が含まれています。AIへ送る前に削除してください'
                         '（AIの作業に認証情報は不要です。必要な場合は資格情報マネージャー等に置き、名前だけ書いてください）。')
    if found['confirm'] and not confirmed:
        raise ValueError('[INPUT_GUARD_CONFIRM] ' + where + 'に' + '、'.join(found['confirm']) + 'が含まれています。'
                         'AIの作業に本当に必要か確認してください。不要なら削除・伏せ字（例：山田→A氏、メール→[メール]）にしてください。'
                         '必要な場合だけ、確認のうえ送信してください。')
    return found
