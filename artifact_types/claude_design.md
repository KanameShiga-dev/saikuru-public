# Claude Design（キャンバス）のデータファイル要点（采来用の手元まとめ）

claude.ai の Design 型 Artifact は、`project/` 以下の自前ファイルだけを中身として表示する。
采来では制作担当が作業フォルダに `project/...` を作り、受け入れ後に利用者の承認のもとで claude.ai へ送る。

## 作るファイル
1. `project/canvas.json`（目次）
```json
{"v":3,"createdOnFiles":{"v":1,"at":"<今のISO時刻>"},"title":"<キャンバス名>",
 "launch":{"view":"canvas"},"pages":[],
 "boards":{"Main.dc.html":{"x":0,"y":0,"w":880,"h":1245,"title":"<アートボード名>"}},
 "order":["Main.dc.html"],"notes":{},"designSystems":[]}
```
- `boards` はアートボード1枚につき1件。キーは `project/` からのパス。`x`,`y`,`w`,`h` はキャンバス上の枠（px、w/h は 40〜8000）。横に並べるときは間を80px、行の間は120px。
- 最初のアートボードは `Main.dc.html` にする。`order` は同じパスを奥から手前の順に並べる。
- `designSystems` は空のまま（デザインシステムの組み込みは送信時に行う）。

2. `project/Main.dc.html`（アートボード1枚＝1ファイル、ファイル全体を書く）
```html
<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>（名前）</title>
<script src="./support.js"></script>
</head>
<body>
<x-dc>
<helmet>
<style>
body{margin:0}
</style>
</helmet>
<div style="width: 880px; height: 1245px; box-sizing: border-box; padding: 64px; display: flex; flex-direction: column; gap: 24px; background: #f6f3ee; color: #1f2a33; font-family: 'Noto Sans JP', sans-serif">
  <h1 style="margin: 0; font-size: 56px; color: {{accent}}">見出し</h1>
  <p style="margin: 0">本文</p>
</div>
</x-dc>
<script type="text/x-dc" data-dc-script data-props='{"accent":{"editor":"color","default":"#2f5d7c"},"$preview":{"width":880,"height":1245}}'>
class Component extends DCLogic {
renderVals() {
return { accent: this.props.accent ?? '#2f5d7c' };
}
}
</script>
</body>
</html>
```

## 守ること（守らないと黙って表示が崩れる）
- head の `<script src="./support.js"></script>` はそのまま必ず書く。
- すべての要素を閉じ、属性は必ず引用符で囲む。
- ポスター・SNS画像・印刷物はルート要素を `w`×`h` の固定サイズにし、`$preview` も同じ値にする。
- 見た目の指定は要素の `style="…"` に書く（`<helmet><style>` は body の余白やリンク色だけ）。
- 並びは flex か grid と `gap` で組む。
- `{{名前}}` は `renderVals()` が返す値の参照だけ（式は書けない）。読者が読む文言はそのままマークアップに書く。
- `<script type="text/x-dc" data-dc-script>` のブロックは必ず入れる（`class Component extends DCLogic`、import なし）。
- 画面はスクリプトで組み立てない（innerHTML・appendChild 禁止）。
- 外部読み込みは Google Fonts の css2 の `<link>`（`<helmet>` 内）だけ。画像は用意できないので、必要なら文字入りの枠（プレースホルダー）にする。`<iframe>`・`<object>`・`<embed>` は使わない。
- アイコンは線画のインライン SVG。絵文字は使わない。

## デザインの心得
- 埋め草・作った数字・ダミー文は使わない。足りない情報は `[開催日]` のようなプレースホルダーにする。
- 書体は1〜3種類、地の色1つ、アクセントは0〜2色。グラデーションの背景、左端に太線のカード、絵文字といった「AIっぽい」表現は避ける。
- 文字のコントラストは 4.5:1 以上（24px以上の文字は 3:1 以上）。区別が必要な色は明るさでも差をつける。
- 印刷物は本文 12pt 以上。はみ出して切れるより、縦に長くなるほうがよい。
- 制作の理由や案の説明はアートボードに書かず、完了報告に書く。
