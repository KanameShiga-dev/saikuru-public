"""Apply the 2026-10-03 UI redesign documentation after history_update.py.

Run order after regenerating the manual:
  build_manual.py -> manual_revision.py -> measurement_update.py ->
  limited_routing_update.py -> history_update.py -> ui_redesign_update.py
No application data, network access or publishing is performed.
"""
from pathlib import Path
import base64
import hashlib
import json

import history_update

ROOT = Path(__file__).resolve().parent
# Screenshots taken before the redesign (kept so already-generated HTML can swap embedded images).
OLD_ASSETS = ROOT.parents[1] / 'data' / 'backups' / 'manual-ui-redesign-20261003-214203' / 'assets'

UI_SECTION = '''<!--ui-redesign-20261003:start--><h3 id="ui-redesign-20261003">画面の見方（2026-10-03 UI改善）</h3>
<p>作業ボード・プロジェクト台帳・プロジェクト履歴・操作テストの上部に、共通のヘッダーを表示します。人が対応すべき判断を見落とさないことを優先した配置です。</p>
<div class="tablewrap"><table><thead><tr><th>場所</th><th>表示・操作</th></tr></thead><tbody>
<tr><td>タブ</td><td>「作業ボード」「プロジェクト台帳」「プロジェクト履歴」「操作テスト」で画面を移動します。表示中の画面は強調されます。</td></tr>
<tr><td>利用枠チップ</td><td>「Codex 残り○％」「Claude 残り○％」を常に表示します。押すと「モデル・利用枠の情報」を開きます。取得できない値は「不明」、古い値は「（前回値）」と表示します。残りが少ない場合は点の色が変わります。</td></tr>
<tr><td>接続状態・新規着手</td><td>「● ローカル接続中」または「新規着手を一時停止中」を表示し、隣のボタンで新規着手の一時停止・再開を切り替えます。</td></tr>
<tr><td>「＋ 依頼を追加」</td><td>依頼の入力画面を開きます（操作は従来どおり）。</td></tr>
<tr><td>「⋯」メニュー</td><td>チーム設定（役割とモデル）、モデル・利用枠の情報、スマホから接続、通知（PC通知・音声通知・声を試聴）、ローカルDBをバックアップ、<b>全作業を中止</b>をまとめています。全作業を中止は区切り線の下に分けています。Escキーで閉じます。</td></tr>
</tbody></table></div>
<p><b>判断待ちのバナー：</b>判断・確認が必要な作業は、作業ボードの最上部に「あなたの判断が必要：○件」として、判断が済むまで表示され続けます。各行の「内容を確認」で対象のカードを開きます。以前の画面右下に浮かぶ通知は廃止しました。PC通知と音声通知は、新しい判断が発生したときに従来どおり通知します。判断待ちのカードがある間は「あなたの判断」の列も強調されます。</p>
<p><b>自動修正の上限で止まった依頼：</b>レビューの指摘が残ったまま自動修正の上限（既定2回）に達した依頼は、最後のレビュー工程が「担当作業終了」のまま依頼だけが停止します。以前はこのカードが「終了した作業」に表示され、通知も出ませんでした。現在は「判断が必要（自動修正の上限）」として「あなたの判断」とバナーに表示し、PC通知・音声通知の対象にもなります。カードを開き、修正範囲を指定して再依頼するか、依頼の扱い（中止など）を判断してください。表示の判定だけを変更しており、作業の処理は変わりません。</p>
<p><b>表示：</b>色はOS・ブラウザのライト／ダーク設定に合わせて切り替わります。状態は色だけでなく記号（▶ 実行中、！ 判断待ち、✓ 終了など）と名前で示します。閉じている「モデル・利用枠の情報」「スマホから接続」は、利用枠チップと「⋯」メニューから開きます。</p><!--ui-redesign-20261003:end-->
'''

LEDGER_NOTE = ('カードのプロジェクト名を押すと、一覧の右側のパネルに詳細が開きます（ダイアログではありません）。'
               'パネルは上から「改修・指摘事項の相談」「管理項目と台帳に保存」「確認したメタ情報・親子情報」「管理操作」の順です。'
               'フォルダの移動・アーカイブ・削除は、パネル下部の赤枠「管理操作」にまとめています。'
               '保存後も詳細は開いたままで、最新の内容に更新されます。×で詳細を閉じます。'
               '一覧のカードには、主要な管理項目（目的・管理担当・起動方法・検証方法・完了条件）のうち確認済みの数と、未確認の項目数を表示します。')

REPLACEMENTS = [
    ('ボード上部の「チーム設定」で', 'ヘッダー右端の「⋯」メニュー →「チーム設定（役割とモデル）」で'),
    ('ボード上部でPC通知・音声通知を有効にします。', 'ヘッダー右端の「⋯」メニューの「通知」で、PC通知・音声通知を有効にします。'),
    ('PCボードの「スマホから接続」を開き', 'PCボードのヘッダー右端の「⋯」メニュー →「スマホから接続」を開き'),
    ('通知を閉じても判断待ち状態は続きます。', '判断待ちは、判断が済むまで作業ボード最上部のバナーに表示され続けます。'),
    ('ボードの「プロジェクト台帳」から開きます。', 'ヘッダーのタブ「プロジェクト台帳」から開きます。'),
    ('カードのプロジェクト名を押すと詳細が開きます。', LEDGER_NOTE),
    ('「全作業を中止」は作業を停止する操作です。', '「全作業を中止」（ヘッダーの「⋯」メニューの区切り線の下）は作業を停止する操作です。'),
    ('作業ボードの「ローカルDBをバックアップ」', '作業ボードの「⋯」メニューの「ローカルDBをバックアップ」'),
]
CHANGELOG = ('<p>2026-10-03：UI改善を反映。共通ヘッダー（タブ・利用枠チップ・「⋯」メニュー）、判断待ちのバナー、'
             '自動修正の上限で止まった依頼の表示、台帳の常設パネルと管理操作の分離、履歴の2列表示・集計・統合した記録の説明を追加し、'
             '画面の画像19点を新しい画面で撮り直しました（撮影用の仮データ）。</p>')
SOURCES = '<section id="sources"><h2>付録G 改訂履歴・参照資料</h2>'


def swap_images(source):
    if not OLD_ASSETS.is_dir():
        return source, 0
    swapped = 0
    for old in sorted(OLD_ASSETS.glob('*.jpg')):
        new = ROOT / 'assets' / old.name
        if not new.exists():
            continue
        before, after = base64.b64encode(old.read_bytes()).decode(), base64.b64encode(new.read_bytes()).decode()
        if before != after and before in source:
            swapped += source.count(before)
            source = source.replace(before, after)
    return source, swapped


def apply(path):
    history_update.apply(path)  # rewrites the history section and re-embeds 16-18 from assets/
    source = path.read_text(encoding='utf-8')
    source, swapped = swap_images(source)
    for old, new in REPLACEMENTS:
        if new not in source:
            assert source.count(old) == 1, old
            source = source.replace(old, new)
    start, end = '<!--ui-redesign-20261003:start-->', '<!--ui-redesign-20261003:end-->'
    if start in source:
        begin = source.index(start)
        source = source[:begin] + '\n' + source[source.index(end, begin) + len(end):].lstrip('\n')
    head = '<section id="board"><h2>3 作業ボード</h2>'
    assert source.count(head) == 1
    source = source.replace(head, head + UI_SECTION, 1)
    assert source.count(SOURCES) == 1
    if CHANGELOG not in source:
        source = source.replace(SOURCES, SOURCES + CHANGELOG, 1)
    path.write_text(source, encoding='utf-8')
    return swapped


def manifest():
    data = json.loads((ROOT / 'screenshots.json').read_text(encoding='utf-8'))
    labels = {item['file']: item['screen'] for item in data['screenshots']}
    data['as_of'] = '2026-10-03'
    data['environment'] = 'real 采来 — サイクル — UI after the 2026-10-03 redesign; isolated synthetic fixtures; no task execution'
    data['screenshots'] = [{'file': 'assets/' + p.name, 'screen': labels.get('assets/' + p.name, p.stem),
                            'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted((ROOT / 'assets').glob('*.jpg'))]
    (ROOT / 'screenshots.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    for name in ('index.html', 'agent-team-manual.html'):
        print(name, 'swapped embedded images:', apply(ROOT / name))
    manifest()
