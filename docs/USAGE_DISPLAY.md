# 利用枠の残量表示 — 2026-09-27

## Completed
- サービスカードに「残り○％（週間残り○％）」を表示。使用率は100から引き、0〜100％に収める。
- 設定モデル、利用枠名、短期の期間、取得時刻、リセット予定も表示。アカウント共通枠をモデル別の独立した枠として扱わない。
- APIがモデル別の週間枠を返す場合、その枠を別行で表示する。取得できない枠は不明。
- 5分間隔で取得。失敗時は前回取得値と注記を表示し、15分〜1時間に間隔を延ばす。リセット時刻経過後は再取得まで不明にする。

## Files and environment changed
- `team_usage.py`: 読取専用の利用枠取得・正規化・バックグラウンド更新。
- `server.py`: 取得処理の起動と画面向け状態への追加。
- `web/app.js`, `web/style.css`: 残量表示。
- `HANDOFF.md`: 継続情報。稼働タスク0件・待機0件の状態でローカルサーバー再起動。

## Delegation
- なし。

## Validation performed
- 稼働中サーバーの実データ取得を確認。取得時点: Codex 短期残り66％・週間95％、Claude 短期91％・週間84％。これらは取得時点の値で固定値ではない。
- テストの追加・実行およびブラウザの画面検証は未実施。

## Human-centered considerations
- 両方とも残量に統一し、週間は括弧書き。色だけに頼らず数値・状態・取得時刻を表示。
- バックグラウンドで取得し、作業一覧の読み込みを利用枠API待ちで止めない。
- トークン実数や会話コンテキスト残量ではなく、プランの利用上限に対する割合。

## Security and privacy
- CodexはCLI App Serverの `account/rateLimits/read` のみ使用。モデルへの依頼や利用枠リセットは行わない。
- Claudeは既存CLI認証をメモリ内で読み、固定HTTPS宛先 `https://api.anthropic.com/api/oauth/usage` に読取要求する。リダイレクトを拒否。認証情報・生のAPI応答を画面、ログ、バックアップに出さない。認証情報のコピー・更新もしない。
- 取得結果は必要な割合・枠名・時刻だけに絞り、メモリに保持する。

## Residual risks and unverified items
- ClaudeのOAuth利用枠APIは安定した公開APIとしての保証がない。仕様変更・認証期限・取得制限により不明になる可能性がある。エラーを握りつぶして100％と表示しない。
- Claudeの公式status lineには利用枠データがあるが、対話セッションのAPI応答後に提供される方式。今回は常駐ダッシュボードからの読取取得を使用。
- 5分以内の他セッションでの消費には遅延がある。数値から操作が許可されると推定せず、自動振分け判断には使用しない。
- 既存のClaude構造化結果取得の問題は別件として残る。

## Recovery or rollback
- 変更前の既存4ファイルは `data/backups/usage-20260927-110402/` に退避済み。
- サーバーを正常停止後、同バックアップの `server.py`, `app.js`, `style.css`, `HANDOFF.md` を元の配置へ復元して起動すれば従来表示へ戻せる。新規 `team_usage.py` は復元後には参照されない。タスクDBは変更していない。

## 参考
- [Codex App Server / Rate limits](https://learn.chatgpt.com/docs/app-server): `usedPercent`, `windowDurationMins`, `rateLimitsByLimitId`。
- [Claude公式status line](https://code.claude.com/docs/en/statusline): 短期と週間の使用率の意味。
- [Claude Code issue #31637](https://github.com/anthropics/claude-code/issues/31637): 利用枠エンドポイントと取得制限に関する利用者報告。公開APIの安定性を保証する資料ではない。
