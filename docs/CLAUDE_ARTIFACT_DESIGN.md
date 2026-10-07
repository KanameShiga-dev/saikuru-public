> 添付パッチの説明資料を現行版へ統合。本文中の試験結果は提供元での記録です。この環境では機能試験・実モデル実行は未実施。現行の反映内容と制限は [統合記録](CHANGE_REQUEST_MERGE_20261007.md) を参照してください。

# ClaudeのArtifact（Design／Slides／Design System）連携（2026-10-07）

采来（サイクル）の資料作成の依頼で、Claude の Artifact（claude.ai 上のデザイン・スライド・デザインシステム）用のデータファイルを作れるようにした。claude.ai への作成・送信は、成果を受け入れた後に、利用者の承認のもとでデスクトップアプリの Claude が行う。

## なぜこの形か（2026-10-07 の調査結果）
- Claude CLI は、デスクトップアプリから起動されたとき（環境変数 `CLAUDE_CODE_ENTRYPOINT=claude-desktop`）だけ `Artifact` の道具を出す。タスクスケジューラから起動した采来の担当には出ない（同じ条件で、この変数の有無だけで結果が変わることを確認した）。
- 変数を付けて「デスクトップアプリから起動された」と名乗らせれば動くが、公式に案内された使い方ではない。利用者の判断で、それはせず「送信はデスクトップから」とした。
- 公式に CLI で使えるようになったら、`team_artifact_guard.review()`（作成は必ず利用者の承認、この依頼で作った Artifact だけに送信、公開前チェック）を有効にする余地を残している。

## 使い方
1. 台帳 → 相談 → 種類「資料作成」→ 成果物の形式で次のどれかを選ぶ
   - Claude Design（画面・チラシ等のデザイン）… `claude_design`
   - Claude Slides（スライド）… `claude_slides`
   - Claude Design System（デザインルール）… `claude_design_system`
2. 計画 → 制作担当（builder）が、保存先の `claude-artifacts\<依頼ID>\project\` にデータファイルを作る。形式の要点は同じフォルダの `_reference\<形式>.md`（元は `artifact_types/`）。
3. 完了時のチェック：必須ファイル（Design：`canvas.json` と `*.dc.html`／Slides：`deck.json` と `slides/*.html`／Design System：`design-system.json` と `README.md`）があること。テキストファイルがすべて公開前チェックを通ること。
4. レビュー → 利用者が成果を受け入れる。
5. デスクトップアプリの Claude に「サンプル07を claude.ai に送って」のように頼む。Claude は送る内容を示して承認を取り、Artifact を作成・送信する。そのあと `/api/jobs/artifact-url`（`{id: 依頼ID, url, note}`）で URL を記録する。URL は作業ボードのカード「ClaudeのArtifact」欄に表示される。

## 決めたこと（利用者：2026-10-07）
- 社内の資料を claude.ai の Artifact に載せてよい（非公開で作成。共有するかは利用者が判断する）。
- Design・Design System・Slides の3種類を使う。
- 既存の PPTX 作成（`build_pptx`）は残し、Artifact は追加の選択肢とする。
- 采来の担当は送信しない。送信はデスクトップから行う。

## 安全のための仕組み
| 項目 | 内容 |
|---|---|
| 担当の道具 | 制作担当に Read・Write・Edit・Glob を追加。書けるのは `claude-artifacts\<依頼ID>\project\` の json/html/css/js/md/txt/svg だけ。読めるのは保存先の中だけ（機密ファイルは不可）。Bash は渡さない |
| Artifact の道具 | 担当が呼んでも拒否する（`artifact_blocked` を記録）。ArtifactComments・ArtifactData・DesignSync も拒否 |
| 公開前チェック | 認証情報、12桁の番号、メール・電話番号、社内IP・社内ホスト名、PC内のパス、`data/websearch-policy.json` の社内固有名詞・ドメイン、このPCのユーザー名など。該当すると完了にしない |
| 記録 | `artifact_staging_write`（作業フォルダへの書き込み）、`artifact_published`（URLの記録）、`artifact_blocked`。毎回のセキュリティレビューの操作記録にも入る |
| 依頼文 | `claude_*` の依頼だけ「公開は、利用者が承認した非公開 Artifact への送信だけ」と書く。それ以外の資料作成は従来どおり「公開は禁止」 |



## 確認範囲

公開版は構文・配信の確認のみ。実モデルでの制作と外部への送信は未確認です。
