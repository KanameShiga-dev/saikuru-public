# モデル選択 — 2026-09-27

## Completed
- 4役割のモデル入力を選択式へ変更。担当の切替でモデル候補を切替。
- 設定画面を開くと両CLIから取得（5分キャッシュ）。「モデル一覧を再取得」はキャッシュ更新、連打は10秒間抑止。
- 一覧が取得できない場合は理由と再取得ボタンを表示。未掲載の現在設定を勝手に変更せず、選び直すまで保存不可。
- サーバーでもモデル候補・推論設定を検証。CodexのAstraとlow/mediumに対応しない候補は除外。

## Files and environment changed
- 新規team_models.py、変更server.py・web/app.js・web/index.html・HANDOFF.md。
- 作業中・待機中タスクがないことを確認してサーバーを正常再起動。

## Delegation
- なし。

## Validation performed
- 読取専用の実モデル一覧を取得: Codexはgpt-6-sol、gpt-6-luna、gpt-5.6-sol、gpt-5.6-terra、gpt-5.6-luna、gpt-5.5。
- Claudeはdefault、opus、claude-fable-5-1[1m]、sonnet、haiku。将来の名称は固定せず応答を使用。
- サーバーの再起動成功を確認。テスト追加・実行、ブラウザの操作確認は未実施。

## Human-centered considerations
- 読み込み中・取得失敗・候補にない既存設定を区別。担当切替時は利用者がモデルを選択するまで未選択にする。
- モデル表示名と実際に設定される識別子を併記。

## Security and privacy
- Codex App Serverのmodel/list、ClaudeのSDK制御初期化のみ。推論用のユーザーメッセージを送信しない。
- Claudeはrestricted・ツールなし・MCP空・非永続の接続で取得。認証情報はCLIが管理。
- 既存の個人方針・グローバル設定は変更しない。

## Residual risks and unverified items
- CLIが提示する選択候補であり、全候補の推論成功を保証しない。利用枠・アカウント権限等は実行時に判定される。
- Codexの推論設定はカタログと個人方針の共通範囲。Claudeは既存のlow/medium方針を継続し、個々のモデルでの推論は未確認。
- 実画面での選択・担当切替・保存の一連操作は未確認。

## Recovery or rollback
- 変更前はdata/backups/model-select-20260927-111447/へ退避済み。
- 正常停止後、server.py・web/index.html・web/app.js・HANDOFF.mdを元の配置へ復元し起動。新規team_models.pyは参照されなくなる。

## 参考
- [Codex App Server model/list](https://learn.chatgpt.com/docs/app-server)
- [Claude Agent SDKの制御初期化実装](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/query.py)
