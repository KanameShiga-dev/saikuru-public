# Bash 自動承認の許可リスト設計

更新日：2026-10-19。設計のみ。Bash の実行方式・承認動作は変更していない。

## 目的

`automatic_operations=true` は通常のtool承認を広く自動許可する。これは E1 の実装・テスト・アプリ起動を進めやすくする一方、未知のBash要求や、分類器が見逃した削除・外部通信まで通し得る。利用上限の予測は行わず、モデルとEffortは人が選ぶ。ここで扱うのは、コマンドの実行範囲と承認だけ。

E1の記録では19件のtool承認のうち、Write/Edit 9件、unittest 3件、アプリ起動2件、ネットワーク4件、その他1件だった。削減対象は繰り返し発生する安全な作業であり、ネットワーク・削除・任意コマンドを自動化することではない。

## 推奨方針

1. `automatic_operations=true` をBashの包括許可として使わない。未知・未解析・曖昧な要求は人の承認へ回す。
2. 自動許可するのは、サーバー側の定義に一致する限定された読み取り・テスト・隔離アプリ起動操作だけとする。
3. 可能ならモデルに自由形式Bashを実行させず、采来が提供する構造化操作IDと型付き引数を受け、サーバー側で固定argvを作る。構造化実行器が未実装の経路では、承認を省略しない。
4. 1回の操作許可は、利用者が承認したjob・project root・operation ID・引数条件・回数・期限に束縛する。設定ファイルへの恒久的な任意コマンド例外として保存しない。
5. `delete` / `dynamic` / `credential` / `network` / `publish` / `install` / `system` は、この許可リストから自動許可しない。人の判断または既存の拒否を維持する。

## 許可リスト項目

| 項目 | 必須条件 |
|---|---|
| `operation_id` | サーバーに登録済みの不変ID。自由なコマンド名から作らない |
| 実行ファイル | サーバー設定で解決・固定した絶対パス。shell、script wrapper、PATH任せは許可しない |
| 引数 | 型・長さ・値域を定義。追加引数、未知フラグ、式、置換、shell metacharacterは拒否 |
| 作業ディレクトリ | 実体解決したjobの承認済みproject root内。別root・junction/symlink越境は拒否 |
| 環境 | 継承を最小化し、proxy・認証・実行制御・PATH上書き・言語起動hookを除外または固定 |
| 実行制限 | timeout、出力上限、同時数、最大起動回数を設定。中止・終了は所有processに限定 |
| 承認記録 | 操作名、対象root、実行ファイル、引数の意味、予想される変更、回数、期限を利用者に表示 |
| ログ | command全体や秘密値を無制限に記録しない。操作ID・結果・要約と安全な監査情報を記録 |

## 初期の操作候補

候補は「同一の安全性境界で機械的に制約できるもの」に限定する。具体的な実行ファイル・argv・環境は対象runtimeとOSを確認してから確定する。

| 種別 | 自動許可候補 | 境界 |
|---|---|---|
| 既存の読み取り | `team_auto_read` が完全一致で許可する限定検索・読み取り | 現在の構文・対象path制約を維持。未解析はmanual |
| テスト | G3で承認された受入条件に登録済みの特定test command | 任意 `python -c`、shell連結、追加引数、環境変更は許可しない。固定argvとproject rootで実行 |
| 隔離アプリ起動 | ランタイムが生成した起動operation ID | `127.0.0.1` と割当port、所有process、期限、停止手段に束縛。Provider通信・任意URL・redirectは許可しない |
| ローカルAPI確認 | [OUTBOUND_ALLOWLIST_DESIGN.md](OUTBOUND_ALLOWLIST_DESIGN.md) の構造化operation ID | method/path/queryはサーバー側定義。任意URL・host・port・header/bodyは受け付けない |
| ファイル変更 | 既存の承認済みBuilder/Edit/Write経路 | Bash経由の `mv` / `cp` / redirect / `Set-Content` 等へ置き換えて自動許可しない。変更承認・scope検査を維持 |

任意の `python build.py` や `npm test` を許可するだけでは不十分である。script内容やpackage hooksが変わり得るため、実行ファイル・argvの固定だけで副作用が無いとは言えない。隔離とscript差分・依存hookの確認を別途必要とする。

## 現在の仕組みとの接続

- `Engine.tool_request()` がBash文字列を受け取った直後に、既存のcredential / outbound / delete / dynamic guardを適用する。denyは拒否、manualは常に人の判断へ送る。
- 自動許可候補は専用validatorまたは構造化実行器で判定する。全条件を満たしたときだけ許可し、validator失敗・例外・未知toolはmanualへ倒す。
- `Context.approve()` の包括 `automatic_operations` 分岐は、上記の専用経路で検証済みと明示された要求以外にBashの自動許可を与えない。`force_manual` を消す例外や、文字列がloopbackを含むだけの救済は設けない。
- provider別のtool差異は同じserver-side decision contractへ正規化する。Claude、Codex、Copilotで承認経路を分けて抜け道を作らない。native command approvalの詳細不足はmanual。
- toolの `allow` が実際のコマンド実行を保証・隔離するわけではない。providerが呼び出すshellを使用し続ける場合、静的validatorの限界は残る。可能なら采来が所有する実行器へ移し、providerにはoperation結果だけを返す。

## 実装を分ける段階

1. 現行承認経路をprovider別に図示し、各経路が `automatic_operations`、`force_manual`、outbound例外のどこを通るかを固定する。
2. command文字列を実行しない型付きoperation contractと、server-side allowlist registryを追加する。
3. 読み取り操作を移行し、未知構文・境界外path・別root・環境上書きはmanualになることを確認する。
4. G3承認済みテストだけを固定argvのoperationとして追加する。削除・外部通信・設定変更の文字列を用いたシミュレーションでは、承認要求が残ることを確認する。
5. 隔離アプリ起動とローカルAPI確認を、runtime/process/port/期限束縛のあるoperationとして追加する。
6. providerごとにfixtureで承認・拒否・タイムアウト・中止・不明応答を確認し、実E1は一度に一操作で段階確認する。

## 受入条件

- 既知のoperation IDと型付き引数だけが、登録済みの固定実行条件で動く。
- 未知ID、余分な引数、自由形式command、shell連結、変数・alias・encoded command、外部通信、削除・上書き、別project、期限切れ・別jobの許可は自動実行されない。
- `automatic_operations=true` でも安全分類の外側にあるBashが包括自動許可へ落ちない。
- 管理者例外は、公開・導入等の明示対象だけに限定し、delete/dynamic/credentialを解除しない。
- 操作承認には何を・どこで・何回・どんな副作用の見込みで行うかを示し、通信先や「副作用なし」を根拠なく断定しない。
- Provider別の通常経路、標準UI、隔離E1 runtimeで確認する。静的テストだけを実運用の通し確認とみなさない。
- 書き込み拒否・拒否回答・timeout・runtime終了時に許可が失効し、プロセス・一時DB・成果物の後始末ができる。

## 未決事項

- Codex/Claude/Copilotのtool承認を采来所有の実行器へ統一できるか。
- Windows上で固定argv実行をどのprocess sandboxで行うか、シンボリックリンク・junction・子processの扱い。
- 各test commandに必要な環境変数・依存hook・読み書き範囲をどう宣言し監査するか。
- `automatic_operations` の包括設定を段階的に廃止する互換期間と既存config移行。

本書は設計案であり、実行許可や実装完了を意味しない。実装開始前に、対象の隔離runtime・provider経路・操作候補を確定する。
