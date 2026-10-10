> 添付パッチの提供元資料です。実施済み・導入先の設定に関する記載は、このPCでの完了を意味しません。現在の統合内容と確認範囲は [2026-10-09統合記録](CHANGE_REQUEST_MERGE_20261009.md) を参照してください。資料レビュー緩和は既定で無効です。

# 「依頼を追加」からの資料作成（2026-10-07）

最初から資料作成の依頼もあるため、作業ボードの「依頼を追加」で資料作成を選べるようにした（これまでは台帳の「相談」からだけ）。

## 画面
- 「依頼の種類」：開発・作業（標準。これまでと同じ）／資料作成。
- 資料作成を選ぶと「成果物の形式」（PPTX・DOCX・XLSX・PDF・MP4・HTML・MD・TXT・SVG・Claude Design／Slides／Design System）が必須になる。
- 開発・作業のまま資料らしい依頼文を出したときの確認は残し、文言を「依頼の種類を資料作成に」の案内に変えた。

## 動き
- 対象プロジェクトのフォルダを、読み取り元と保存先の両方にする（相談からの依頼は、計画に書かれた保存先を使う。こちらは変えていない）。
- 依頼文の前に付ける条件（資料作成のみ・コマンド禁止・公開の範囲・必須成果物形式）は、相談と共通の `team_document_capabilities.document_goal()` で作る。相談側の文面は変わらない。
- 形式の制作環境（ライブラリ・フォント・FFmpeg）は受付時に `require_supported()` で確認し、足りなければ依頼を始めない。
- 台帳登録とハーネス化（prepare_new_request）、スキルの追加、自動で進める設定は、これまでどおり。

## 変更したファイル
- `team_document_capabilities.py`：`document_goal()` を追加。
- `team_consultation.py`：`document_goal()` を使うように整理（文面は同じ）。
- `server.py`：`/api/jobs` で `kind: documentation` と `document_format` を受け取る。
- `web/index.html`・`web/app.js`：依頼の種類・成果物の形式の欄。
- テスト：`test_document_goal.py`（2件）。

変更前のファイル：`data/backups/new-request-document-20261007/`
