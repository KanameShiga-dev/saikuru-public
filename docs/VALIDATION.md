# 検証記録 — 2026-09-27

## 実行済み

| 確認 | 結果 |
|---|---|
| `python -X utf8 -m unittest -v test_team` | 16件PASS。模擬AIによる制御とHTTPの検査 |
| `node --check web/app.js` | PASS |
| `python -X utf8 probe_live.py codex` | 実モデルの構造化応答を取得。合成入力のみ・ツール呼出し禁止 |
| Claude Code --version | 2.1.281の起動確認 |
| Claude実接続 | StructuredOutputのフック拒否を修正後、claude-sonnet-5から正常な構造化JSON結果を取得（status=done） |
| Claude auth status --json | 本人ログイン後、終了コード0・loggedIn=true。認証・アカウント情報の値は出力しない |
| manage.py start / stop / start | 起動・正常停止・再起動を確認 |
| 通常画面 | 実ブラウザで初期表示とClaude未ログイン表示を確認 |
| 隔離画面の模擬承認 | カードを開く→補足→承認→後続2タスクと履歴を確認 |

16件には自動分担、計画承認、拒否、試行番号不一致、取消、再起動復旧、同一対象の直列化、設定スナップショット、トランザクションの巻戻し、二重起動拒否、編集前退避、保護対象拒否、Origin/未認証拒否、非公開ファイル非配信を含む。

画面検証用は8788番ポートと `.test-output/ui-fixture` を使用。模擬タスクであり実AIの実績ではない。
通常の8787番ポートのDBには混入させていない。

## 確認・修正した問題

- Codex公式例の承認表記を実機CLIが拒否。CLIからJSONスキーマを生成し、`untrusted` / `read-only` / `workspace-write`へ修正後、実接続成功。
- Claudeエラー表示時のCP932 UnicodeEncodeError。プローブの出力をUTF-8化。
- PowerShellスクリプトは実行ポリシーで拒否。ポリシーを変更せずPythonランチャーで起動・停止を確認。
- 結果表示を生JSONから日本語の計画・検証一覧へ変更。操作の詳細は確認可能なまま保持。

## 未実行・未確認

- 実ファイル・コマンドに対するネイティブ承認フックの許可/拒否/取消。結果整形フックと構造化結果取得は修正後に確認済み。
- Codexの本物のコマンド・ファイル承認要求への画面応答。
- 両AIでの実装→レビュー→修正→再レビュー。
- 実CLI実行中の取消・強制終了・PC再起動からの復旧。
- 実プロジェクト、Git、稼働中サービス、実機との連携。
- モデル・CLI更新後の互換性、人による最終受入。

## 画面記録

- `evidence/dashboard.png`: 通常画面。タスク0件、Claude認証待ち。
- `evidence/approval-fixture.png`: 模擬承認の反映。実AIの成果ではない。

この記録は初期版の確認範囲であり、全面採用の完了証明ではない。
