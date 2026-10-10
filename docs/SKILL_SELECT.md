> 添付パッチの提供元資料です。実施済み・導入先の設定に関する記載は、このPCでの完了を意味しません。現在の統合内容と確認範囲は [2026-10-09統合記録](CHANGE_REQUEST_MERGE_20261009.md) を参照してください。資料レビュー緩和は既定で無効です。

# 依頼時のスキル追加（2026-10-07）

依頼を出すときに、スキルをその依頼のプロジェクトに追加して、候補にできるようにした。
（最初は「選んだスキルだけ使う」方式だったが、利用者の指示で「追加して候補にする」方式に変更。）

## 画面
- 作業ボードの「依頼を追加」と、プロジェクト台帳の「相談」→「この内容で計画を開始」（改修・資料作成の両方）に「スキルをプロジェクトに追加して候補にする」欄を追加。
- スキルの一覧にチェックを入れると、そのスキルを依頼のプロジェクトに追加し、候補として担当に渡す（1依頼10件まで）。
- 依頼文から自動で合わせるスキル（最大5件）は、これまでどおり候補になる。追加したスキルはその前に並ぶ。
- 「この依頼ではスキルを使わない」にチェックを入れると、追加分も自動分も渡さない。
- 一覧は、スキル一覧で有効なスキル（取り込み済みのものは、ファイルが取り込み時のSHA256と一致するもの）。版ごとに1件。表示名で並ぶ。
- 作業の詳細画面に「依頼時にプロジェクトへ追加したスキル（候補）」を表示。

## 動き
- 追加したスキルが依頼のプロジェクトに未適用なら、受付時に適用する（スキル一覧の「適用先」に追加されるのと同じ。依頼後も残る）。これで担当が SKILL.md 全文と参考資料を読める。同梱スクリプトの実行条件（開発の依頼の制作担当だけ、SHA256一致）は変えていない。
- 担当への引き継ぎ（reviewed_project_skills）では、追加したスキルに `added_at_request: true` を付ける。
- 計画担当には、追加されたスキル名と「依頼に合うものを選んで、関係する作業のinstructionにスキル名つきで手順を明記する。使わない場合は理由をsummaryに書く」指示を渡す。使うかどうかは計画担当が決める。
- 記録は job の `skill_selection`（mode＝auto/none・ids・names）。履歴に `skills_selected` イベント。
- スキルは手順の提供であり、道具の権限は増えない（これまでと同じ）。

## 変更したファイル
- `team_handoff.py`：`job_skills()`（選択に応じて渡すスキル）、`selectable_skills()`（画面用の一覧）。文脈の `reviewed_project_skills` を `job_skills()` に変更。
- `team_engine.py`：`create_job(skill_mode, skill_ids)`、`_skill_selection()`（検証）、計画担当への指示。
- `team_consultation.py`：相談からの依頼でも選択を渡す。
- `server.py`：`/api/jobs` で `skill_mode`・`skill_ids` を受け取る。`GET /api/skills?for=request` で選択用の一覧。`/skill-picker.js` を配信。
- `web/skill-picker.js`（新規）、`web/index.html`・`web/app.js`、`web/ledger.html`・`web/ledger-consultation.js`、`web/style.css`・`web/ledger.css`。
- テスト：`test_skill_select.py`（6件）。既存の test_team・test_skill_import・test_skill_match・test_plan_check も合格。

変更前のファイル：`data/backups/skill-select-20261007/`

# スキルの表示名の変更（2026-10-07）

- スキル一覧の各カードを開くと「表示名」欄がある。「表示名を保存」で変更、「元の名前に戻す」で既定に戻す（60文字まで、`< > `` ` と指示文のような語は不可）。
- 変わるのは**表示名だけ**。内部の名前（取り込みスキルのフォルダ名、読み取り・スクリプト実行の判定、デスクトップのスキル置き場）は変わらない。カードに「内部の名前」を表示。
- 表示名は、スキル一覧・依頼のスキル選択・作業詳細の「依頼時に選んだスキル」・担当への引き継ぎ（`display_name`）で使う。名前（内部）ごとに保存するので、版が変わっても引き継がれる。
- 既定の表示名：取り込みスキルは内部の名前、経験から作ったスキル（`experience-…`）は説明の冒頭（40文字）。
- 保存先：handoff DB の `skill_titles`（name・title・updated_at）。API：`POST /api/skills/rename {version_id, title}`。履歴に `skill_renamed`。
- 変更前のファイル：`data/backups/skill-rename-20261007/`（`web/skills.css` は末尾に `.rename-box` を追記しただけ）。
