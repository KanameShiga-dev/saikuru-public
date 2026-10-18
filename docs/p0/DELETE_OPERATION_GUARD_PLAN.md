# 削除・破壊操作ガード：統合確認と受入試験

状態：Claude の `f0a2c2c` / `dec1e36` / `aa83086` を `p0/integration` にローカル cherry-pick 済み。Codex のレビューで見つけた `outbound_exceptions` の迂回は、`delete` / `dynamic` を例外対象外にする修正と回帰テストで塞いだ。2026-10-19に関連32件（`test_security_guard`、`tests.test_run_time_limit`、`tests.test_security_runtime_synthetic`）が成功。全テストスイート・E1通し・本番反映は未実施。pushなし。

## 目的

`automatic_operations=true` でも、既知の削除・破壊操作をBashの通常承認経路で自動許可せず、人の確認に回す。`classify()` の文字列判定は回避可能なため、この試験だけを根拠に「削除全般を防止」とは扱わない。

## 推奨する試験場所

- `test_security_guard.py` の `OutboundGuardTests`：文字列分類と既存の偽陽性回帰を確認。
- `test_security_guard.py` の `EngineGuardTests`：`automatic_operations=true` の合成Engineで、実際の `Context.approve()` 経路が削除を承認待ちにし、通常コマンドを自動許可することを確認。管理者例外が `delete` / `dynamic` の強制確認を解除しないことも確認。実コマンドは実行しない。
- 実行対象は `classify()` と合成Engineだけ。削除コマンド、Provider、OSコマンド、実プロジェクトのファイルには触れない。

## 1. 直接コマンドの検出（manual）

次の表記ゆれを `('delete', label, 'manual')` に分類する。試験値は文字列としてのみ渡す。

| 分類 | 試験ケース例 |
|---|---|
| POSIX削除 | `rm -rf ./build`、`rm -fr ./build`、`unlink file`、`find . -type f -delete`、`xargs rm` |
| PowerShell | `Remove-Item -Recurse -Force .\\build`、別名 `rm` / `rmdir` / `del` / `erase` / `rd` |
| cmd | `del /s /q .\\build`、`erase /s /q .\\build`、`rmdir /s /q .\\build`、`rd /s /q .\\build` |
| Gitのデータ損失 | `git reset --hard`、`git clean -fdx`、`git restore .`、`git checkout -- .`、`git stash drop`、`git worktree remove` |
| Python削除API | `shutil.rmtree(...)`、`os.remove(...)`、`os.unlink(...)`、`Path.unlink()`、`Path.rmdir()` |
| 一括ミラー・同期 | `robocopy /MIR`、`rsync --delete` |

組合せ試験として、大小文字、空白、引用符、先行する `sudo`、`cmd /c`、`powershell -Command`、コマンド連結（`;` / `&&` / pipeline）を加える。解析器が未対応の構文を見つけた場合は自動許可せずmanualへ倒すか、未対応として制限を明記する。

## 2. 実際の承認経路

合成Engineを `automatic_operations=true` にし、Bashから直接削除例を `tool_request()` に渡す。確認項目：

1. `allow=true` を即時返さず、承認要求を作る。
2. 承認payloadに `force_manual=true`、`guard.category='delete'`、利用者向けの削除説明が付く。
3. 画面で利用者が拒否すれば、コマンドは実行されず拒否結果になる。
4. `automatic_operations=false` でも通常の人の承認経路になる。
5. 認証情報を含むコマンドは引き続き `deny` され、`delete: manual` によって拒否が承認待ちへ緩和されない。

## 3. 例外・優先順位

- `outbound_exceptions` は他のmanualカテゴリを完全一致で例外化できる。`delete` と `dynamic` は例外判定へ渡さず、人の確認を維持する。追加した合成Engineテストで例外設定があっても両カテゴリに `force_manual` が付くことを確認する（未実行）。
- 機密・認証情報の `deny` は削除カテゴリより優先し、常に拒否する。
- ネットワーク・公開等と削除が同じコマンドにある場合も、強い方の強制確認が消えないことを確認する。
- Codex native command approvalは `Context.approve()` が既に `force_manual` にする別経路。今回のengine試験はClaude等のBash `tool_request()` を通し、native承認だけを試して終わらない。

## 4. 偽陽性・既知の迂回

既存の検索語除外と通常作業の動作を保つため、次の非実行コマンドは従来どおり `None` となることを確認する。

- `rg -n 'rm -rf|Remove-Item' docs/` のような引用検索語。
- `git status --short`、`python -m unittest -v`、通常のローカル起動。

コマンド内の引用文字列やプログラム引数を構文解析しないため、文字列を表示するだけの `python -c "print('rm')"`、`echo "git reset --hard"`、説明文に `Remove-Item` を含む出力も `delete` に分類されることを確認した。これは既知の偽陽性で、日常作業の承認増加につながる。受入時には実際の一般作業コマンドで発生頻度を記録し、文字列ルールを無条件に広げず、構造化実行器へ移行する範囲を決める。

次のケースは単純なコマンド文字列分類では確実に判定できない。分類器の既知の限界として、各々をprobeし、`None` の場合はengineの自動承認経路に流れることを記録する。これらを人の確認必須にするのが受入条件なら、追加の設計（未解析Bashをmanualにする、許可リスト実行器、OS隔離等）が必要。

- `python cleanup.py` 等、削除APIを含むスクリプトファイル。
- Pythonの別名import、`getattr`、`eval`、文字列結合、Base64等の動的実行。
- PowerShell変数、関数、独自エイリアス、`-EncodedCommand`。
- シェル変数・関数・ラッパー・`xargs`・スクリプト経由。
- `Write` / `Edit` によるファイル内容の空化・置換。これはBash分類とは別で、特に `job.auto_execute=true` の直接許可経路を別に評価する。

確認済み：`automatic_operations=true` の `Context.approve()` を通す合成Engine試験、`automatic_operations=false` で削除・通常Bashの両方が承認待ちになる経路、削除・dynamicの管理者例外無効化、通常コマンドの従来どおりの自動許可。未確認：`Write` / `Edit` による空化・置換を含む挙動試験、実provider/runtimeでの実行結果。コード経路上、承認済み `job.auto_execute` はプロジェクト内のWrite/Editを直接許可し、変更前ファイルを退避するが、内容の空化・置換を分類するチェックはない。これはBash guardの範囲外であり、許可済み作業範囲の意味と復旧可能性を別途評価する。

## 完了条件

- 既知の直接削除・破壊コマンドが自動許可されず、`automatic_operations` のtrue/false双方で期待どおりの承認フローになる。
- 管理者例外で削除のmanual判定を解除できない。
- 既存のcredential deny、外向き通信のmanual、通常の読取・テスト・ローカル起動に回帰がない。
- 間接実行のprobe結果を報告し、文字列ガードの対象外を明示する。probeで見つかった迂回を防止対象に含めるなら、regex追加だけで完了にせず実行境界の案を選ぶ。
- 実装の影響範囲（本番設定での承認件数増加を含む）とrollbackを利用者に提示する。
