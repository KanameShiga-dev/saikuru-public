# 配備・起動前検査・バックアップ復元・配布候補（M08）

対象: `team.sqlite3` と `handoff.sqlite3` の「DB ペア」。実装は `team_deployment.py`（標準ライブラリのみ）。
このページの手順は、いずれも既存の DB を上書きしない。実際の稼働中 App への適用、実データでの
バックアップ・復元は、利用者の許可（app_operations）を得てから行う。

## 1. 起動前のペア検査

書込みの前に、両 DB を読み取り専用で検査する。1 件でも問題があれば何も書かずに止まる。

| 状態 | 判定 | コード |
|---|---|---|
| 両方なし、または両方がテーブルを持たない空ファイル | 初回として許可 | （許可） |
| 両方が既知のテーブルを持ち、識別子が一致または片側が未設定 | 許可 | （許可） |
| 片方だけが存在・初期化済み | 停止 | `pair_partial` |
| 読めない・`quick_check` が失敗 | 停止 | `db_corrupt` |
| 無関係なテーブルだけを持つ、またはテーブル名が同じでも構造が違う（列の型・NOT NULL・主キー、UNIQUE の有無と列、余分な必須列、CHECK・外部キー・トリガー） | 停止 | `schema_unknown` |
| `schema_version` がこの版より新しい | 停止 | `schema_newer` |
| 両側の `pair_id` が異なる（別ペアの混在） | 停止 | `pair_mismatch` |

`pair_id` と `schema_version` は、各 DB の `pair_identity` テーブルに入る。`Store` / `HandoffDB` が
初期化時に登録し、先に作られた側の識別子を後の側が引き継ぐ。この版より前から使っている DB には
識別子がなく、その場合は互換として扱われる（識別子が無い側の取り違えは検出できない）。

構造の基準は `team_deployment.py` の `TABLE_SPEC` で、`team_store.py` / `team_handoff.py` の CREATE TABLE と
手で揃えている（片方を変えたら他方も変える）。`conflicts.resolution` は後から追加された列なので任意。
過去の版が作った実 DB が基準と一致するかは、実データを読めないため未確認。一致しない場合は
`schema_unknown` で止まるので、起動前に利用者へ連絡する。

読み取りは、`-wal` / `-shm` が無ければ `immutable=1`、あれば `mode=ro` で開く。前者は側ファイルを作らない。
後者は既存の側ファイルを読むだけで、新しく作らない。

### 検査が通る入口

| 入口 | 検査の位置 |
|---|---|
| `server.py`（直接起動、`make_server`） | DataLock・Store・待受ソケットより前 |
| `manage.py start` | Task Scheduler の起動・子プロセスの起動より前。`background.disabled` は削除しない |
| `background_runner.py`（Task Scheduler） | 子の起動ごとに検査。拒否時は `data/server.stderr.log` に記録し、待機を延ばして再試行 |
| `Start-AgentTeam.ps1` | `data/` の作成・`Start-Process` より前 |
| `install_background.ps1` | 資格情報の入力・タスク登録より前 |

PowerShell は `python -I -X utf8 team_deployment.py check --data <data>` を呼ぶ（終了コード 0 = 許可、2 = 停止）。
検査をどの入口でも同じ関数にしているので、条件が入口ごとにずれない。

### 停止したとき

DB を作り直さず、削除もしない。理由コードを控え、利用者がバックアップを確認する。
復元は下の「別フォルダへの復元」で別の場所に作り、内容を確かめてから、置き換えの要否を利用者が決める
（置き換えを行うコマンドは用意していない）。

## 2. ペアのバックアップと別フォルダへの復元

```
python -I -X utf8 team_deployment.py backup  --data <data> --to <新しいフォルダ>
python -I -X utf8 team_deployment.py restore --from <バックアップ> --to <別の新しいフォルダ>
```

- `backup`: サーバーを止めてから実行する。`instance.lock` を取れなければ（稼働中）`in_use` で拒否する。
  出力先が既にあれば `dest_exists`。2 つの DB を SQLite のバックアップ API で複製し、
  `pair-manifest.json`（サイズ・SHA-256・テーブルごとの件数・`pair_id`）を書く。
- `restore`: マニフェストと各ファイルのサイズ・ハッシュを照合し、`<復元先>.restoring` に複製して
  ペア検査・識別子・件数を再確認してから、復元先の名前に改名する。失敗時は作業フォルダを消す。
  復元先が既にあれば `dest_exists`。元の DB・バックアップは変更しない。

稼働中の実データに対する実施は未確認（要許可）。

## 3. 配布候補マニフェスト

```
python -I -X utf8 team_deployment.py manifest --root <ソース> --commit <40桁のコミットID> --out <新しいファイル> <相対パス>...
python -I -X utf8 team_deployment.py verify-manifest --root <ソース> <マニフェスト>
```

- 配布するファイルは**明示した許可リスト**だけ。フォルダの走査や除外による自動選別はしない。
- 次は許可リストに入れるとエラーになる（黙って除外しない）: `data/` などの運用フォルダ、`.git`、
  `*.sqlite3`・`*.log`・`*.key`・`*.pem`・`.env*`、名前に `secret` / `credential` / `password` を含むもの、絶対パス、`..`。
- 出力は決定的（パス順、時刻なし）。マニフェストがあることは、配布・公開の許可を意味しない。
- 名前の検査では、氏名など未登録の固有名は見つけられない。配布前に人が候補を確認する。

## 未確認

- 稼働中 App への反映、実 DB でのバックアップ・復元、実データの保持。
- PowerShell 2 本の実行（コードの位置関係の確認のみ。Task Scheduler への登録は未実施）。
- `-wal` / `-shm` が残った異常終了後の DB に対する検査結果（`mode=ro` で開けない場合は `db_corrupt` として停止する）。
