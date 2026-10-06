> 添付パッケージの提供元の仕様・確認記録を基にした資料です。記載の機能確認は提供元の報告で、本環境で再実施した結果ではありません。本環境への適用と未確認事項は CHANGE_REQUEST_INTEGRATION_20261006.md を参照してください。

# 検索・読み取りの自動承認

2026-09-27、頻繁な操作承認を減らす利用者の依頼により追加。

## 対象

Codexから届くPowerShellコマンドのうち、実際のコマンド表示とCLIの解析結果が一致し、PowerShell構文木で静的な読み取りだけと確認できるものを自動承認する。対象は登録プロジェクトの作業ディレクトリ内。

- `rg` の通常のファイル検索・本文検索（一部の明示的なオプションのみ）。
- 明示したプロジェクト内パスに対する `Get-Content` / `Get-ChildItem`。
- `Get-Location`、後段の `Select-Object -First/-Last/-Skip` による出力範囲指定。
- 実行ファイル名を指定した `Get-Command Unity.exe` / `where.exe Unity.exe` 等の場所確認。Get-CommandのErrorAction/CommandType Application、および後段のSelect-Object -ExpandProperty/-PropertyによるSource/Path/Name/FullName/CommandTypeの取り出しに対応。実行ファイルの起動、モジュール検索、ワイルドカードや任意パスの探索は含まない。
- `rg --files | rg "検索語"` など、同じ制限内での検索結果の絞り込み。
- `git status` と `git diff`（`--check`、ファイルを指定した差分、差分の統計を含む）。対応オプションは実装内の許可リストに限定。リポジトリの実体が対象ルートに一致することを確認し、外部diff/textconvドライバ・fsmonitor・環境変数による別リポジトリやコマンド指定は除外する。diffは先にファイル名を確認し、保護対象を含む場合は手動へ戻す。

判定不能・混在コマンド・削除・編集・実行・ネットワーク・権限要求・プロジェクト外・秘密情報やGit内部の明示パス・動的式・リダイレクトは手動承認を継続する。全操作の自動承認ではない。質問や計画承認も変更しない。

ClaudeのRead/Glob/Grepと、利用者が自動実行を指定済みの依頼内編集は既存処理を維持。Claude Bashの自動承認を今回追加してはいない。

## 実装

- `team_auto_read.py`: 小さい許可リスト、パスの実体解決、コマンド表示の照合。不明な形式は手動へ戻す。
- `read_command_ast.ps1`: 入力文字列の構文解析専用。入力コマンドを実行しない。
- `team_engine.py`: 承認待ちを作る前に判定し、許可時は `auto_approved` イベントを記録。

Codexのサンドボックスと承認モード、個人方針、永続的なCLI許可ルールは変更しない。

解析器はWindows PowerShellを-NoProfile/-NonInteractiveで起動し、ローカルの固定解析コードを-Commandへ渡す。対象コマンドは標準入力のデータとしてのみ渡す。.ps1の実行ポリシーは変更しない。存在しないプロジェクト内ファイルへの読み取りも許可する（通常のファイル未検出結果を担当へ返す）。

## 反映と復旧

承認待ちの担当を停止してサービスを正常再起動し、既存変更を保持して同じモデル・同じタスクを再開した。

変更前の `team_engine.py`: `data/backups/auto-read-20260927-223407/`。
DB退避: `data/backup-20260927-223536-7650a7.sqlite3`。
ロールバック時は担当の安全な停止を確認後、変更前のteam_engine.pyへ戻し、サービスを再起動する。新しい補助ファイルは未使用となるため削除不要。DBを過去版に戻す必要はない。

テストスイートは実行していない。未対応の書式は自動化せず手動承認に残す。

追記 2026-09-28 00:08: 実際の承認要求 `Get-Command Unity.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source` が新しい判定で許可になることを確認。要求自体は利用者による承認済みだったため再実行・重複承認していない。実行中担当0件で正常再起動して反映。変更前ファイルは `data/backups/auto-locate-20260928-000753/`、DBは `data/backup-20260928-000823-eaa2de.sqlite3`。巻戻しは安全な停止後にteam_auto_read.pyのみ復元して再起動する。

実運用でattempt 6の読み取り要求がauto_approvedになり、次のコマンドへ進んだことをAPIの履歴で確認。git status --shortは自動許可リスト外のまま、再開時の現状確認として一度だけ承認した。

追記 2026-10-05: 利用者の指示により、Claude Bashの読み取り判定に `ls`（-1aAlhtrSF のみ、再帰なし、プロジェクト内パス）、`python`/`python3`/`py` の `--version`/`-V`、区切り `||`、サブシェルの丸括弧を追加。実例 `(python --version || py --version); ls; cat generate.py` を許可。括弧または `||` を含む場合は `cd` を手動へ戻す（cdの成否・範囲が静的に確定しないため）。版数確認だけのコマンドは読み取り扱いにしない。スクリプト実行・リダイレクト・ヒアドキュメント・プロジェクト外は従来どおり手動。判定関数に22件の文字列を与えて期待どおりを確認（テストスイートは未実行）。変更前は `data/backups/auto-read-ls-20261005-142506/`。巻戻しは実行中担当0件で同ファイルのみ戻して再起動する。

追記 2026-09-27 23:54: 上記Gitの対象外制限を修正。実際に保留中だった `git diff --check -- HANDOFF.md unity/Assets/Scripts/Unity/Boot/M5BattleStateController.cs` が新しい判定を通過し、通常APIから許可して担当が終了するまで確認。続く反映は実行中担当0件で正常再起動した。テストスイートは実行していない。変更前のteam_auto_read.pyは `data/backups/auto-git-20260927-235246/`、DBは `data/backup-20260927-235413-b37b2f.sqlite3`。必要時は担当を停止して同ファイルのみ戻し、正常再起動する。DBの巻戻しは不要。
