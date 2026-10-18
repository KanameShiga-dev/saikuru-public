# 操作承認の自動化：運用者向けガイド

更新日：2026-10-19。これは現在のソース実装に基づく説明であり、特定の稼働環境の設定値を示すものではない。

## 先に確認すること

- `automatic_operations` は `team_config.py` のサーバー設定。ジョブ単位の設定ではなく、その設定を読み込んだサーバープロセスに作用する。
- 既定値は `false`。実行中の環境が現在どちらかは、その環境の実設定を別途確認する。
- `auto_execute` は別のジョブ単位の設定。承認済み計画の後に担当が作業を進める設定で、`automatic_operations` と同じ意味ではない。
- E1で観測された操作承認は19件（ファイル変更9、unittest 3、アプリ起動2、ネットワーク4、その他1）。その記録上、ネットワーク4件は強制承認の対象だった。E1プロセスだけで `automatic_operations=true` にすれば、その他15件は一般の承認経路に達した場合に自動化候補となる。ただし実際の適用可否は個別ガードにも左右される。
- E1だけに適用する場合は、E1専用の隔離サーバーで設定する。通常運用サーバーの設定を切り替えると、そのプロセスが扱う他のジョブにも影響する。

## 動作の早見表

| 操作経路 | `automatic_operations=false` | `automatic_operations=true` |
|---|---|---|
| `Read` / `Glob` / `Grep` | `tool_request` が役割・実体パス・保護対象を検査し、許可範囲内なら直接許可。設定値に依存しない | 同左 |
| Claudeの限定読み取りBash | Windows上で `auto_read_reason` の構文・パス条件に一致すれば直接許可 | 同左 |
| その他の通常tool承認 | 自動化理由が無ければ利用者の承認待ち | `force_manual` 等で除外されず、質問でなければ原則自動許可 |
| Bashのネットワーク・公開・導入等 | outbound guardの判定に応じて強制承認または拒否 | 同左。検知された強制承認を自動化設定では迂回しない（完全一致の管理者例外を除く） |
| credential／采来内部データ読み取りコマンド | 拒否 | 同左。承認待ちにはしない |
| 質問・仕様判断 | 利用者に確認 | 利用者に確認 |
| WebSearch | queryを専用ガードで検査。通過なら直接許可、失敗なら拒否 | 同左。`automatic_operations` の承認ショートカットを通らない |

## `automatic_operations=true` の範囲

`Context.approve()` は、先行する拒否・専用処理を通過した要求について、`force_manual` がなく、`operation == 'question'` でもなく、`questions` が空なら、自動承認して `allow=true` を返す。

したがって、これは「編集・テスト・起動だけを自動承認する」設定ではない。次を含む、一般の承認経路に到達したtool要求に広く適用される。特別なガードが付かなかった未知のtool要求も、この条件を満たせば対象になり得る。

- 作業フォルダ内の `Write` / `Edit` が専用の計画スコープ自動許可に一致せず、通常の承認経路まで進んだ場合
- `auto_read_reason` が扱わないBashやコマンド
- unittest、ローカルサーバーの起動、その他の通常tool要求
- 個別の専用ガードがないtool操作

この設定はOS権限やCLIサンドボックスを拡張しない。ProviderやOS側で拒否される操作を実行可能にする設定でもない。一方、采来側の通常承認を省く範囲は広いため、専用隔離プロセス以外で有効にしない。

### 削除・破壊操作に関する注意

`p0/integration` の現在の作業ツリーには、`delete` と `dynamic` の分類規則が追加されている。削除操作（`rm` / `Remove-Item` / `git reset --hard` / `git clean` / `shutil.rmtree` 等）と、実行内容を隠す一部の表現（PowerShell encoded command、`exec` / `eval` / `compile` / `__import__`、Base64 decode 等）は人の確認へ回す。これはコマンド文字列の分類であり、下記の未対応例を含め、削除・破壊操作全般を保証して防ぐものではない。

分類はプログラム引数や引用文字列の意味までは解析しない。安全な文字列確認でも、`python -c "print('rm')"`、`echo "git reset --hard"`、`Write-Output "Remove-Item is a PowerShell command"` は `delete` 扱いとなり、人の確認へ回ることを確認した。通常処理の説明・検索・生成コマンドで同様の文字列を含む場合も確認が増える可能性がある。

この実装で未対応として報告された例は、上書き（`mv` / `cp` / リダイレクト / `Set-Content` / `open(..., 'w')`）、`ri` 等の別名、変数経由のコマンド名、`getattr`、`python cleanup.py` 等のスクリプト内部である。別名・関数・変数展開、動的実行、難読化も静的分類を迂回し得る。コードレビューで見つかった管理者例外の抜け道は、統合側で `delete` / `dynamic` を `outbound_exceptions` の対象外にする修正と回帰テストを追加した。2026-10-19に関連する32件のガード・実行時間・合成セキュリティテストは成功した。全テストスイートやE1実行環境での通し確認は未実施。

Codex native command approvalは別経路で強制的に人の承認へ回るが、Claude等の `tool_request()` 経由のBashには同じ強制が常に適用されるわけではない。`job.auto_execute=true` による通常の `Write` / `Edit` の直接許可も、このコマンド分類とは別の経路である。

2026-10-19の読み取り確認では、本番として使われる `data/config.json` の保存値は `automatic_operations=true`、`approved_roots` は5件だった。同時点で8790番は待受けていなかったため、これはファイルに保存された設定値の記録であり、稼働中プロセスの実効設定を確認した結果ではない。設定値やサービス状態は利用時に改めて確認する。

`delete: manual` は既知の直接コマンドを人の確認へ回す第一段階であり、削除全般を防ぐ保証にはならない。強い保証が必要なら、Bashの許可リスト化またはOS側の隔離と併用する。統合前後を問わず、適用先のブランチ・稼働プロセス・設定を確認し、下記の限界を踏まえて使う。

## `automatic_operations=false` でも直接許可される範囲

### 読み取りtool

`team_engine.py::tool_request` は `Read` / `Glob` / `Grep` の対象を実体パスに解決し、対象プロジェクト内か、`.env`、`.ssh`、`.credentials.json`、`auth.json`、`.git` 等の保護対象でないかを確認する。許可範囲なら `automatic_operations` にかかわらず直接許可する。Planner / Reviewer は専用読み取りtoolに限定される。

### `auto_read_reason`

`team_auto_read.py` で次の限定経路を確認する。

- Claude Bash：Windowsのみ。作業ディレクトリと参照パスをプロジェクト内に限定し、長さ1,000文字以内、展開やリダイレクト等のない構文を検査する。`ls`、`wc`、`cat`、限定形の `sed` / `head` / `tail` / `grep`、制限付き `git status` / `git diff` と、その組合せの一部を許可する。未知の構文・コマンド・パスは通常の承認へ回す。
- Codex native PowerShell：関数内にはWindows PowerShell/Pwsh、プロジェクト直下のcwd、追加権限なし、RIPGREP_CONFIG_PATHなし、16,000文字以下、AST 12 pipeline以下等に限定した読取解析器がある。解析対象は制限付き `Get-Content` / `Get-ChildItem` / `rg` / Git状態確認など。ただし現在の `Context.approve()` はnative command approvalを先に `force_manual=true` とするため、通常の経路ではこの解析器による自動許可より利用者承認が優先される。

これはシェル全般を安全とみなす規則ではない。許可範囲を静的に確認できない要求は、`automatic_operations=false` なら承認待ちになる。

### `Write` / `Edit` と `auto_execute`

`team_engine.py::tool_request` は、担当の書込権限、プロジェクト内の実体パス、保護対象、分割文脈などを先に検査する。Builderの通常のファイル変更は、次のいずれかに該当すれば操作前バックアップを作って直接許可する。

1. ジョブの `auto_execute=true`。
2. `team_auto_edit.py::approved_scope_edit()` が、現在の祖先Plannerの承認済み計画と `SCOPE.md` の明示範囲、許可ディレクトリ・拡張子、リンク等の条件に一致すると確認。

対象が大きすぎる、通常ファイルでない、バックアップに失敗する等の場合は直接許可せず、通常の承認経路に戻る。`auto_execute=true` の直接許可では、明示範囲のディレクトリ・拡張子チェックまでは行わず、前段のプロジェクト内・保護領域等の検査を適用する。G3で明示した範囲に限定する経路は `approved_scope_edit()` 側。

Claude等の `tool_request()` 経由の `Write` / `Edit` は別実装で、Builder・`job.auto_execute=true`・実体解決したプロジェクト内path・保護path外・既存ファイル10 MB以下等の条件を満たすと、変更前の `before.bin` と `manifest.json` を保存して許可する。入力内容の意味は解析しないため、空内容による上書きもこの経路では拒否されない。確認したソースには `file-backups` を指定して元ファイルを復元するAPIは見当たらず、manifestと控えからの個別復旧が必要になる。対象プロジェクトの作業許可と、内容が常に安全である保証は区別する。

`approved_scope_edit()` の現在の条件は、Builderであること、jobが停止状態でないこと、現在の祖先Plannerのplan承認が1件だけありapprovedであること、`SCOPE.md` の `Root:` が対象プロジェクトと一致すること、`Allowed after human G3 approval:` が1行あること。許可するのは `app/**`、`tests/**`、`docs/**`、`data/**`、`evidence/**`、`.local/test-tmp/**`、または `README.md` の明示指定と、関数内の許可拡張子・リンク・保護パス条件を満たす追加・更新だけ。削除・移動は対象外。

`team_auto_edit.py::auto_edit_reason()` はCodex native file-change向けの限定解析を実装しているが、通常の `Context.approve()` ではnative file-changeを先に `force_manual=true` にする。現行経路でこの関数が自動許可を行うとは扱わず、native変更の承認は画面で確認する。

関数自体の成立条件は、`source='codex'`、`operation='item/fileChange/requestApproval'`、書込可能な担当、jobの `auto_execute=true`、`grantRoot` / `reason` が無いこと、1〜50件の追加・更新（移動なし）、対象がプロジェクト内の非保護パスであること、許可拡張子、サイズ・diff上限、重複なし、変更前ファイルが検査中に変わっていないこと。全変更の前状態をbackupし、hash付きmanifestを書けた場合にだけ理由を返す。

## 強制承認または拒否

### `force_manual` が付く経路

- Codex native command approval：認証情報・采来内部データを扱うコマンドは拒否し、それ以外もnative commandとして利用者の承認へ回す。
- Codex native file-change approval：変更一覧、追加権限、実体パスを検査。不正・対象外・保護対象は拒否し、通過しても利用者承認へ回す。
- Bashのoutbound guardが `network`、`publish`、`install`、`system` を検知し、管理者の完全一致例外に一致しない場合。
- Claude Artifactの作成など、コンテンツがこのPCから外部サービスへ送られる操作。
- `team_elicitation.py` のうち、既定の明示スコープに合わないMCP elicitation。
- 質問（`operation == 'question'` または `questions` を持つ要求）は一般の自動承認ショートカットの対象外。

`force_manual` は、これらの先行処理がサーバー側で付けた場合に効く。単に画面文言に「危険」と書いてあるだけでは強制承認にならない。

### 拒否になる経路

- `team_outbound_guard.py` が認証情報・采来の内部データに触れると判定したコマンド。
- 権限外の役割、プロジェクト外の対象、保護ファイル、追加権限の要求、確認不能なnative payloadなど、各専用処理が明示的に拒否した要求。
- WebSearchのquery guardが機密・個人情報等を検出した検索語。

拒否は「利用者が承認すれば通る」という意味ではない。必要なら担当は別の方法を提示する。

### outbound guard の限界と管理者例外

`team_outbound_guard.py::classify()` はBash等のコマンド文字列を検査する。検知した通信・公開・導入は原則個別承認、認証情報は拒否する。ただし文字列検査は実行時にファイル・設定・環境等から作られる通信先を完全には見抜けない。未検知なら一般の自動承認経路に進む可能性がある。loopback URLの記載だけで例外許可する方式は採用していない。

`data/security-policy.json` の `outbound_exceptions` は、カテゴリとコマンド文字列が完全一致する管理者例外。`automatic_operations=true` の場合、その例外が手動強制を外し、一般自動許可に進むことがある。登録・変更時は対象コマンドを厳密に限定する。

## 通常承認経路を通らない専用処理（bypass / boundary）

この表はセキュリティ機能が無いという意味ではなく、`automatic_operations` の一つの分岐だけでは判断できない経路を示す。

- WebSearch：queryを `team_web_guard` が検査してから直接許可または拒否。通過したqueryは外部検索へ送信される。
- `Read` / `Glob` / `Grep`：`tool_request` が役割・対象パスを検査して直接許可・拒否。
- `project_read` MCP：`team_elicitation.py` が専用broker、担当scope、text-only/research/document条件を照合して、一部の読み取り・文書toolを直接受理する経路がある。
- Artifact操作：`team_artifact_guard` の専用ルール。外部作成は利用者承認。
- MCP elicitation：`team_elicitation` の個別スコープ判定。対象外は通常の利用者承認。
- Provider/CLI自身の権限、OS ACL、サンドボックス、プロセスの実行範囲：この設定とMarkdown文書では変更・強制されない。

## E1での運用判断

E1の確認回数からは、`automatic_operations=true` により通常の操作承認を大きく減らせる可能性がある。一方、この値はサーバープロセス全体に適用され、未知のtool要求も強制フラグがなければ自動許可され得る。E1だけで試す場合は、他のjobを載せない専用runtimeに限定する。通常運用の8790で値を変更しない。

有効化は、隔離runtimeの設定・起動状態を確認したうえで利用者が判断する。ネットワーク・公開・導入・OS設定の検知、認証情報の拒否、質問、G2/G3、正式受領などの利用者判断は、この自動承認で代行しない。

## 参照

- `team_engine.py`：`Context.approve()`、`Engine.tool_request()`
- `team_auto_read.py`：`auto_read_reason()` と限定構文解析
- `team_auto_edit.py`：`approved_scope_edit()`、`auto_edit_reason()`
- `team_outbound_guard.py`：`classify()`、管理者例外
- `team_web_guard.py`：WebSearch query検査
- `team_artifact_guard.py`：Artifactの送信・作成境界
- `team_elicitation.py`：MCP elicitationのスコープ
- `team_config.py`：`automatic_operations` の既定値と型検査
