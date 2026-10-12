"""Synthetic Markdown correction case; no real names or source material."""
import re
from pathlib import Path

BRIEF = ('# 架空図書室の事実\n\n'
         '- 開館：平日 9:00〜17:00\n- 土日：休館\n'
         '- 貸出：3冊まで、14日間\n- 返却：窓口\n- 予約：不可\n')
INITIAL = ('# 架空図書室の利用案内\n\n'
           '平日 10:00〜16:00 に開館。5冊まで7日間借りられます。予約できます。\n')
REQUIRED = ('# 架空図書室の利用案内', '## 開館', '## 貸出', '## 返却と予約',
            '平日 9:00〜17:00', '土日', '休館', '3冊', '14日', '窓口')
FORBIDDEN = ('10:00〜16:00', '5冊', '7日間', '予約できます')
REQUEST = ('架空の図書室の利用案内を新しいMarkdownファイル guide.md に作成してください。'
           'BRIEF.md の事実だけを使い、古い guide-old.md の誤記を訂正した新版にしてください。'
           '見出しは「# 架空図書室の利用案内」「## 開館」「## 貸出」「## 返却と予約」を使い、'
           '平日 9:00〜17:00、土日は休館、貸出3冊まで14日間、返却は窓口、予約不可を明記してください。'
           'guide-old.md は変更せず、作成するファイルは guide.md のみ。'
           '外部送信・公開・削除・インストール・コマンド実行を禁止します。'
           '制作後、評価側が独立採点して EVALUATION_CHECKS.md に記録します。'
           'レビュー担当はその証拠と現行の guide.md を確認してください。追加質問が必要なら停止してください。')


def quality(project):
    try:
        text = (Path(project) / 'guide.md').read_text(encoding='utf-8')
        return (all(term in text for term in REQUIRED)
                and re.search(r'予約(?:は|[：:]\s*)?不可', text) is not None
                and not any(term in text for term in FORBIDDEN))
    except (OSError, UnicodeError):
        return False
