# Claude Design System（デザインルール）のデータファイル要点（采来用の手元まとめ）

claude.ai の Design System 型 Artifact は、`project/` 以下の自前ファイルを中身として表示する。
采来では制作担当が作業フォルダに `project/...` を作り、受け入れ後に利用者の承認のもとで claude.ai へ送る。
ロゴ・アイコン・画像・フォントのファイルは、送信時にアップロードが必要なため、今回は扱わない（必要なら完了報告に「未対応」と書く）。

## 作るファイル
1. `project/README.md`（必須）：ブランドの説明書。文章の方針、見た目の基本（色・文字・余白・角丸）、アイコンの考え方を、実例とトークン名を挙げて書く。
2. `project/tokens.json`：トークン一式（必ずファイル全体を書く）
```json
{"name":"<名前>","version":1,
 "color":{"themes":[{"id":"light","name":"Light"},{"id":"dark","name":"Dark"}],
   "tokens":[{"name":"surface-100","value":{"light":"#fbf7f1","dark":"#1d1a17"},"usage":"ページの背景"},
             {"name":"ink","value":{"light":"#2b2118","dark":"#f3ece3"},"usage":"surface-100 の上の文字"}]},
 "type":{"fonts":[],"families":{"sans":"\"Noto Sans JP\", system-ui, sans-serif"},
   "groups":[{"name":"Text","family":"sans","styles":[{"name":"body","fontSize":"15px","lineHeight":"22px","fontWeight":400}]}]},
 "spacing":{"tokens":[{"name":"space-4","value":"16px","usage":"カードの内側の余白"}]},
 "radius":{"tokens":[{"name":"radius-md","value":"8px","usage":"ボタン・カード"}]}}
```
- `type` 以外は必ず `{"tokens":[{"name","value","usage"}, …]}` の「リスト」にする（名前→値の対応表（DTCG形式）は読めない）。
- 名前は `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`（空白と `/` は不可）。全体で重複させない。
- 色の値は16進、`rgb()` `rgba()` `hsl()` `oklch()`、または既存の色トークンの別名 `"{other-token}"`。色名（red、transparent など）・`var()`・`color-mix()` は使わない。
- テーマ別の値が無いトークンは最初のテーマの値を使うので、主となるテーマを先頭にする。
- 長さは `px|rem|em|%` か数値。`lineHeight` は単位なしでもよい。すべてのトークンに `usage` を書く。
3. `project/design-system.json`（目次。最後に作る）
```json
{"v":3,"layout":"files","createdOnFiles":{"v":1,"at":"<今のISO時刻>"},"title":"<名前>","namespace":"<英数字の名前>",
 "libraries":[],"sections":{},"groups":[],"assetGroups":{},"blobs":{},"docs":{"readme":"project/README.md","sections":[]},
 "lastChange":{"by":"Claude","at":"<今のISO時刻>","via":"saikuru","note":"初版"}}
```
- `title` がデザインシステムの名前になる。`sections`・`blobs`・`docs` は上の値のままにする。
- `api/…`、`tokens.css`、`manifest.json` はページが自動で作るので書かない。

## 作り方の心得
- 元になる資料（会社の資料・既存のデザイン・コード）があれば、正確な値を写す。推測で値を作らない。
- 元資料が無ければ、小さな初版にする（1テーマに色6〜10、文字スタイル5〜7、余白4段階、角丸3種、1段落の README、コンポーネントなし）と完了報告に書く。
- 文字のコントラストは、どのテーマでも 4.5:1 以上（24px以上は 3:1 以上）。区別する色は明るさでも差をつける。
- 青紫のグラデーション、絵文字のカード、左端に太線のカードは使わない。
