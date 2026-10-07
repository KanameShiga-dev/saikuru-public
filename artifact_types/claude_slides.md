# Claude Slides（スライド）のデータファイル要点（采来用の手元まとめ）

claude.ai の Slides 型 Artifact は、`project/` 以下の自前ファイルだけを中身として表示する。
采来では制作担当が作業フォルダに `project/...` を作り、受け入れ後に利用者の承認のもとで claude.ai へ送る。

## 作るファイル
1. `project/deck.json`（目次）
```json
{"v":4,"createdOnFiles":{"v":1,"at":"<今のISO時刻>"},"lists":"css","title":"<デッキ名>",
 "order":["cover","agenda"],"sections":{"s1":{"description":"<この章を1文で>","start":"cover"}},
 "faces":{"noto-sans-jp":{"family":"Noto Sans JP","href":"https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;700&display=swap"}},
 "designSystems":[]}
```
- `order` はスライドIDを表示順に並べる。IDは `[A-Za-z0-9_-]{1,64}`。
- `sections` は章立て（キーは任意、`description` は1文、`start` は章の最初のスライドID。最初の章は表紙から始める）。
- `faces` はスライドで指定する書体ごとに1件（最大4件）。キーは書体名を小文字にし、空白を `-` にしたもの。`href` は `https://fonts.googleapis.com/css2?…` だけ。
- `designSystems` は空のまま（組み込みは送信時に行う）。

2. `project/slides/<id>.html`（スライド1枚＝1ファイル）
- 中身はちょうど1つの `<section id="<id>" style="…">` だけ。`<html>`・`<head>`・`<style>`・`<body>` は書かない。ファイル名とIDは同じにする。
- 画面は 1920×1080px 固定。section に `background`（必須）、`font-family`、`color`、配置（`display:flex; flex-direction:column` か `display:grid`、`padding:128px`、`gap` など）を書く。内側は 1664×824px。
- スタイルはすべてインライン。単位は px、色は16進か rgb。クラス・`<style>`・`margin`・`z-index`・`em`・`var()` は使わない。
- 使える要素：`h1` `h2` `h3` `p`（`font-size` は24px以上）、`ul`/`ol` と `li`、`br`、`b` `i` `u` `a` `span`、`div`（入れ子は15段まで）、`table`/`tr`/`th`/`td`、`svg`（`aria-label` 付き、52KB以下、スクリプトなし）、`hr`、`img`（画像は用意できないので使わない）。1枚200要素まで。
- `position:absolute` で要素をスライド上に固定できる（`left`/`top`/`width` など。文字には `width` を付ける）。
- 発表者メモは最後の子要素として `<aside>` に平文で書く（4,000文字まで。デッキを開いた人は誰でも読める）。

例：
```html
<section id="agenda" style="background:#f7f5f0;color:#1d2731;font-family:'Noto Sans JP', sans-serif;padding:128px 128px 160px;display:flex;flex-direction:column;gap:48px">
<h2 style="font-size:72px;font-weight:700;line-height:1.1">本日の流れ</h2>
<div style="display:flex;gap:32px">
<div style="flex:1;display:flex;flex-direction:column;gap:12px;background:#ffffff;padding:40px;border:1px solid #dde2e6;border-radius:16px">
<h3 style="font-size:36px;font-weight:700">1. 紹介</h3>
<p style="font-size:28px">仕組みの概要</p>
</div>
</div>
<p style="position:absolute;left:128px;bottom:64px;width:800px;font-size:24px;color:#5d6b78">2 / 8</p>
<aside>ここで全体の流れを話す。</aside>
</section>
```

## デザインの心得
- 1枚に1つの考え。箇条書きより、表・カードの列・大きな数字・引用にする。見出しの文体はデッキ全体でそろえる。
- 色は16進で、濃い色1・薄い色1・アクセント1〜2。真っ白・真っ黒は避ける。文字のコントラストは 4.5:1 以上（44px以上は 3:1 以上）。
- 文字サイズは4〜5段階、書体は1〜3種類。強調はサイズを増やさず、太さ・色で行う。
- 縦の余白：見出しの高さはおよそ「サイズ×行数×1.1」。収まらないときは文字を縮めず、スライドを分ける。
- 同じ種類のスライドは同じ組み方にする。見出しは常に上の余白の位置に置く。
- ページ番号・出典は下端の1行（24px、`bottom:64px` に固定）。その場合 section は `padding:128px 128px 160px`。
- 作った統計や引用は書かない。足りない情報は `[数値]` のようなプレースホルダーにする。
