# 残作業②〜⑤の実装と確認

作業開始 2026-10-03／最終確認 2026-10-04。公開・pushは今回実施していません。

## Completed
- ② 保存済みCLI利用量47試行（失敗9試行、再試行のある工程19件）を独立集計と照合。不一致0件。試行別サービス・モデル・状態・利用量を履歴に追加。同じ実行IDの重複は最新版を採用。未取得とモデル未記録は推測しない。
- ③ 状態変化を原子的に記録し、勤務時間内・時間外・システム停止を分離。承認の期限切れから再試行までの待ちを結合。期間・種類別の中央値／90パーセンタイル／最大値、履歴詳細と時系列、認証済み集計APIを実装。過去8依頼は計測開始前。
- ④ 判断待ちカードに停止理由・判断材料・操作先を表示。修正範囲指定、復旧確認、補足・差し戻し、中止の確認、保留の導線を追加。AIによる矛盾調査の意見は承認と区別。既存APIの承認・再開条件を維持。
- ⑤ 台帳詳細を基本情報／実行・検証／ハーネス／調査・関連／管理操作に分け、分類編集を基本情報へ移動。タブ切替時の未保存入力を保持。常駐Windows通知は初期無効。勤務時間外は次の勤務開始後にまとめて件数を通知し、配信済みと解消済みを記録。
- 履歴検索0件で以前の詳細が残る不具合を修正。既存テストの失敗3件を現行Provider方針に追随し、Windowsで残るテスト用DBのファイルロックを解消。

## Files and environment changed
team_worktime.py、team_waits.py、team_notifications.pyを追加。team_store.py、team_config.py、team_history.py、team_engine.py、server.py、mobile_gateway.py、web/app.js、web/index.html、web/history.*、web/ledger.*を更新。計測文書、README、HANDOFF、ローカルマニュアル2版と生成用history_update.pyを更新。
テストは tests/test_history_metrics.py を追加し、tests/test_ui_automation.py と test_team.py を修正。
本体8790を作業0件・相談0件で正常停止・再起動。状態の前後一致、配信JS3ファイルのディスク一致を確認。常駐通知は無効のまま。確認用8842サーバーは停止。

## Delegation
追加Agentは使用せず、同じ作業内で実施。

## Validation performed
- `python -X utf8 -m unittest tests.test_history_metrics tests.test_ui_automation tests.test_ui_automation_http test_team`：53件成功。期待するHTTPエラーの応答オブジェクトに既存のResourceWarningが出るが、失敗は0。
- JS3ファイルのnode --check、Python10ファイルのAST解析：成功。画面3版とマニュアルの重複IDは0、マニュアル2版の内容一致。
- 勤務時間、休日、週末、逆転、計算範囲外、重複排除、トランザクションのロールバック、期限切れ→停止→再試行、システム停止、失敗後の再試行、計画承認レコードの前後関係、期間指定、API認証、通知の保留・重複防止・旧レコードを検証。
- 隔離サンプルで台帳タブ切替、入力保持、保存、修正上限の通知→修正再依頼→待機列、新規依頼→計画承認、台帳相談→計画プロンプト→依頼作成→台帳由来の履歴を確認。実モデルの代わりに模擬応答を使用。
- 幅390pxで履歴・台帳の横はみ出し0、画面エラー0。端末実機ではない。
- Windows通知のテスト送信ヘルパーは正常終了。表示そのものは人による確認が必要。
- 稼働API `/api/project-history`、`/api/decision-wait-stats`、`/api/notifications` は正常応答。

## Human-centered considerations
勤務時間外の待ちを捨てず、継続中・未計測・計算範囲外を明示。色以外に文字と斜線を使用。タブは矢印／Home／Endで移動可能。保留で依頼は停止したまま。修正再依頼は最新成果物と修正範囲の明示チェックを要求し、送信するまで開始しない。

## Security and privacy
認証とX-Agent-Team-UIのPOST条件を維持。通知本文は件数のみで、依頼名・パス・本文・認証情報を含めない。外部送信を追加していない。新しい計測はモデル呼び出し・承認・実行の可否を変えない。個人ログイン・人物ごとの権限は今回の範囲外。

## Residual risks and unverified items
CLIの生プロトコル全件は保存していないため、②は保存済み報告との照合。CLI未報告の利用量やモデル提供側の会計は確認できない。利用枠はアカウント共通であり、他作業の消費と区別できない。金額表示は追加していない。
実モデルによる作業完了の一巡、スマホ実機、OS通知の実表示・音声・サインアウト後は未確認。外部通知（Teams等）は接続先・送信範囲が未指定で未実装。休日自動判定・人物別勤務時間・勤務設定の編集画面も未実装。計測開始前の待ち時間は復元しない。

## Recovery or rollback
変更前ソースとSQLiteバックアップを data/backups/remaining-20261003 に保存（SHA256SUMS付き）。正常停止し、必要な既存ファイルだけ退避版に戻して起動する。追加したtransitions・metrics_metaとnotificationオブジェクトは旧コードから参照されず、残っていても旧動作を妨げない。新しい依頼や記録が生じた後にDBを丸ごと戻すと新しい記録が失われるため、通常はソースだけ戻す。公開用Gitコピーは今回更新していない。

## Evidence
- data/ui-check-20261003/TOKEN_AUDIT.md：数値のみの照合記録。
- data/ui-check-20261003/wait-metrics.png：隔離サンプルの履歴・勤務時間集計・待ちの時系列。
- data/ui-check-20261003/ledger-tabs.png：隔離サンプルの台帳タブ。
- マニュアル内 decision-wait-manual：新機能の操作説明（旧スクショは保持）。
