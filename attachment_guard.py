"""Deterministic screening of untrusted attachment text; never executes the input."""
import base64
import html
import re
import unicodedata
from urllib.parse import unquote

VERSION = 'attachment-guard-20261004-1'
MAX_TEXT = 150_000
PATTERNS = [
    ('上位指示の無視・上書き', r'(?:ignore|disregard|forget|override|bypass).{0,60}(?:previous|prior|above|system|developer|safety|security).{0,40}(?:instructions?|prompts?|rules?|polic(?:y|ies)|restrictions?)'),
    ('上位指示の無視・上書き', r'(?:上記|以上|以前|前の|これまで|システム|開発者|安全|セキュリティ).{0,30}(?:指示|命令|ルール|制約|ポリシー).{0,25}(?:無視|破棄|上書き|解除|従わな)'),
    ('AIへの役割・権限の偽装', r'(?:you\s+are\s+now|act\s+as|enter|enable).{0,40}(?:unrestricted|jailbreak|developer\s+mode|DAN\b|no\s+restrictions)'),
    ('AIへの役割・権限の偽装', r'(?:あなた|AI|モデル).{0,20}(?:無制限|制限なし|開発者モード|制約を解除)'),
    ('承認の偽装・迂回', r'(?:without|skip|bypass|ignore|disable).{0,35}(?:approval|confirmation|authorization|permission\s+checks?)'),
    ('承認の偽装・迂回', r'(?:承認|許可|確認).{0,20}(?:不要|省略|迂回|飛ば|済みとして|なしで実行)'),
    ('秘密情報の取得・外部送信', r'(?:reveal|leak|exfiltrate|upload|send|print|extract).{0,80}(?:system\s+prompt|api[ _-]?keys?|access[ _-]?tokens?|passwords?|credentials?|private[ _-]?keys?|secrets?)'),
    ('秘密情報の取得・外部送信', r'(?:APIキー|パスワード|認証情報|秘密鍵|アクセストークン|システムプロンプト).{0,50}(?:送信|公開|漏洩|開示|表示|アップロード|取得して)'),
    ('利用者への隠蔽・結果の偽装', r'(?:do\s+not\s+(?:tell|inform|disclose)|hide.{0,20}from).{0,25}(?:user|human|operator)'),
    ('利用者への隠蔽・結果の偽装', r'(?:利用者|ユーザー|依頼者).{0,20}(?:知らせず|隠して|報告せず)|(?:未実施|失敗).{0,15}(?:成功|完了|確認済み).{0,10}(?:報告|扱)'),
    ('指示ロールの偽装', r'<\|(?:im_start|im_end|system|developer|assistant|endoftext)[^>]*\|>|\[INST\]|<<SYS>>|(?:^|\n)\s*(?:SYSTEM|DEVELOPER)\s*:'),
]


def normalized(text):
    for _ in range(2):
        text = html.unescape(unquote(text))
    text = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m[1],16)), text)
    text = unicodedata.normalize('NFKC', text)
    text = ''.join(c for c in text if unicodedata.category(c) != 'Cf')
    # Apply after decoding so escaped look-alikes cannot bypass normalization.
    text = text.translate(str.maketrans('аесорхуіΑΒΕΙΚΜΝΟΡΤΧ', 'aecopxyiABEIKMNOPTX'))
    return text


def check(text):
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        raise ValueError('添付の全文を検査できる上限（15万文字）を超えています。分割してください。')
    variants = [normalized(text)]
    # Decode bounded opaque text before inspection. No eval or external lookup.
    candidates=re.findall(r'(?<![\w])[A-Za-z0-9+/]{32,}={0,2}(?![\w])', text)
    if len(candidates)>64:
        raise ValueError('検査できない長いエンコード文字列を含むため拒否しました。')
    for token in candidates:
        if len(token) > 90_000:
            raise ValueError('検査できない長いエンコード文字列を含むため拒否しました。')
        try:
            decoded = base64.b64decode(token + '='*((-len(token))%4), validate=True).decode('utf-8')
            if decoded and sum(c.isprintable() or c.isspace() for c in decoded) / len(decoded) > .95:
                variants.append(normalized(decoded))
        except (ValueError, UnicodeError):
            pass
    hex_candidates=re.findall(r'(?:0x)?\b[0-9a-fA-F]{32,}\b',text)
    escaped_candidates=re.findall(r'(?:\\x[0-9a-fA-F]{2}){8,}',text)
    if len(hex_candidates)+len(escaped_candidates)>64:
        raise ValueError('検査できない長いエンコード文字列を含むため拒否しました。')
    for token in hex_candidates+escaped_candidates:
        if len(token)>90_000:
            raise ValueError('検査できない長いエンコード文字列を含むため拒否しました。')
        try:
            decoded=bytes.fromhex(token.removeprefix('0x').replace('\\x','')).decode('utf-8')
            if decoded and all(c.isprintable() or c.isspace() for c in decoded):
                variants.append(normalized(decoded))
        except (ValueError,UnicodeError):
            pass
    for value in variants:
        for reason, pattern in PATTERNS:
            if re.search(pattern, value, flags=re.I|re.S):
                raise ValueError('プロンプトインジェクションの疑い（'+reason+'）があるため添付を拒否しました。該当する指示を除いた資料を添付してください。')
        compact = re.sub(r'[^\w\u3040-\u30ff\u3400-\u9fff]', '', value).casefold()
        for reason,pattern in PATTERNS[:-1]:
            if re.search(pattern,compact,flags=re.I|re.S):
                raise ValueError('プロンプトインジェクションの疑い（'+reason+'）があるため添付を拒否しました。')
        if any(marker in compact for marker in ('ignoreallpreviousinstructions','ignorepreviousinstructions',
               'disregardallpreviousinstructions','bypassapproval','revealthe systemprompt'.replace(' ',''),
               'youarenowunrestricted','actasanunrestrictedai',
               '上記の指示を無視','以前の指示を無視','システムの指示を無視','承認を迂回')):
            raise ValueError('指示を隠す記述にプロンプトインジェクションの疑いがあるため添付を拒否しました。')
