> 添付パッケージの提供元の仕様・確認記録を基にした資料です。記載の機能確認は提供元の報告で、本環境で再実施した結果ではありません。本環境への適用と未確認事項は CHANGE_REQUEST_INTEGRATION_20261006.md を参照してください。

# Word・Excelの作成・編集（2026-10-05）

利用者の指示により、資料作成の成果物形式に Word（DOCX）・Excel（XLSX）を追加した。

## 使い方

台帳の相談 →「資料作成のみ」→ 形式で「Word・DOCX（作成・編集）」または「Excel・XLSX（作成・編集）」を選ぶ。計画には builder の `generate_office` と形式名（docx/xlsx）を含む実制作工程が必須（`require_plan`）。完了時は成果物の存在とSHA256を確認する（`require_artifacts`）。

## 資料ツール（document_tools.py）

- `generate_office`（builderのみ）：新しい .docx/.xlsx を作成。
  - Word：`title`、`sections`（見出し・レベル1〜3・段落・箇条書き・表）、`replacements`（既存文の置換）。
  - Excel：`sheets`（シート名・行・見出し行の太字と固定・列幅。既存シートには下に追記）、`updates`（シート・セル番地・値）。数式は可、外部参照・外部呼び出し（URL、`[`、WEBSERVICE等）は拒否。
  - 編集：`base_path` に編集元（保存先または読み取り元フォルダ内の同形式ファイル）を指定し、新しい名前で保存する。元ファイルは変更しない。既存の成果物名は上書きしない。
- `read_document`：Word は見出し・段落・表、Excel はシートとセル（各シート500行・50列まで）をテキストで返す。マクロは実行しない。

上限：節200、表500行×30列、シート20、行5000×100列、セル更新5000、編集元50MB。

## 環境

`python -m pip install --user python-docx openpyxl`（導入済み）。

## 確認

一時フォルダで、新規作成・読み取り・置換と追記による編集（元ファイル不変）・Excelのセル更新と追記と新シート・上書き拒否・外部参照数式の拒否・builder以外の拒否・フォルダ外の編集元拒否・write_documentでのDOCX拒否・計画ゲート・完了ゲートを確認（16項目）。資料ツールのツール一覧に generate_office が出ることを確認。実モデルでの依頼一巡とテストスイートは未実行。

変更前：`data/backups/office-support-20261005-171519/`。巻戻しは実行中担当・相談0件で戻して再起動。
