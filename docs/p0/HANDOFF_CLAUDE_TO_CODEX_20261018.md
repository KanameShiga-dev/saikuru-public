# Claude → Codex 引き継ぎ（2026-10-18）

Codex の利用枠の制限中（〜23:00）に Claude が引き継いだ作業の記録。Codex の枠が回復したので、ここから戻す。
ブランチ `p0/claude-takeover`（`p0/integration` = `7d9a00d` の上に9コミット）。**統合は未実施。取り込みの可否は Codex と利用者で決める。**

## 0. 訂正（2026-10-19）

- `30cf6c9`（ローカル宛てだけの外部通信の例外）は `p0/claude-takeover` 上で `3430e57` により取り消された。宛先をファイルや設定から読むコードに `http://127.0.0.1` を添えると、承認なしで通る穴が再現された。`p0/integration` には `30cf6c9` 自体を取り込んでいないため、`3430e57` の取り消しパッチは適用しない。現在も通信コマンドは個別承認になる。
- 画面の改善3件（`f9fe396`、`b6b0672`、`c9867b0`）と本引き継ぎ文書は統合済み（Codex側のコミット `d8f78ff`、`a821e46`、`595c709`、`b58d41e`）。
- E1は attempt 6 を実施済み。報告は `needs_changes`（検証証跡の失効、`test_files` 不足）で未完了。保存済み成果物に対し、2026-10-19に `tests/test_lookup.py` の9テストを再実行して合格を確認したが、采来上のレビュー・正式受領には反映していない。追加Provider呼び出しも行っていない。
- 許可リスト方式は `docs/p0/OUTBOUND_ALLOWLIST_DESIGN.md` に設計案を記録。ガード本体の動作変更は未実施。

## 1. 結論（先に）

- E1（約1,000件の商品を ID 検索する架空案件）は、**まだ完走していない**。実装工程 `28134d60`（起動方法と確認結果をまとめる）が累計5回失敗し、`maintenance_required` で停止している。レビュー2件は待機中（Claude sonnet/medium に切替済み。Codex に戻すかは判断事項）。
- 失敗の原因は、モデルでもトークンでもなく、**采来側の不具合3件の連鎖**だった（下の2）。3件とも本ブランチで修正し、テストを付けた。
- 利用者が使えていない根本は、まだ残っている（本番は最新版を含まない／保守担当の認証が1人開発に存在しない／承認の説明が不足）。下の4を参照。

## 2. 見つけた不具合と修正（コミット順）

| コミット | 内容 | 変更ファイル | 検証 |
|---|---|---|---|
| `a008a0a` | 旧テスト `test_claude_failover` を P0 規則（P0 ジョブは自動フェイルオーバーしない）に合わせた | `test_claude_failover.py` | 16件 OK |
| `7620edd` | `failure_code=format` の直後に、終了情報（subtype / stop_reason / terminal_reason / num_turns / 拒否された道具名）を event に残す。内容は含めない | `team_adapters.py` | 構文、関連テスト OK。実走で `terminal_reason=hook_stopped` を捕捉 |
| `76a506e` | ツール出力の送信前ガードが、ローカル URL（127.0.0.1 等）を含むだけで出力を伏せ、セッションを止めていた。ツール出力の経路だけ `loopback_url` を除外（`tool_output_findings`）。秘密・個人情報・プロファイルパスは従来どおり伏せる | `team_egress_guard.py`, `tests/test_team_egress_guard.py` | ガード18件 OK |
| `82f366e` | 復旧中表示が、折りたたみに包まれた承認欄まで隠していた（利用者が承認できない）。判断欄に `data-user-decision` を付け、含む枠は隠さない | `web/app.js` | E1 実画面で承認欄の表示を確認 |
| `30cf6c9` | 外部通信ガード（`network` 規則）が、道具名（urllib 等）に当たると通信先を見ずに manual にしていた。**ローカル宛てだけ**の狭い例外を追加（条件は 3.2） | `team_outbound_guard.py`, `test_security_guard.py` | 手元で13件のすり抜け試験、関連54件 OK |
| `f9fe396`, `b6b0672` | 依頼の一覧を1件1行（名前・状態・「N工程中M件完了」・進捗バー）に変更。状態ごとの配色、成果物の交互背景 | `web/app.js`, `web/style.css`, `docs/p0/UI_ACCEPTANCE.md` | E1 実画面（明/暗） |
| `c9867b0` | 操作の承認（kind=tool）を、コマンド文字列から読み取った事実の表（何をする／場所／通信先／削除語／許可・拒否の結果）で表示。全文と生データは折りたたみ | `web/app.js`, `web/style.css`, `tests/ui/p0/states/p0-states.json`, `docs/p0/UI_ACCEPTANCE.md` | 10種のコマンドと架空状態で画面確認 |
| `bc5aae9` | **実行時間の上限（`task_timeout_seconds`）に、承認待ちの時間を数えていた**。E1 は承認待ちのまま 23:24:22 に上限で停止した。待機時間を記録し、差し引く | `team_engine.py`, `tests/test_run_time_limit.py` | 新規5件 OK。修正前のコードで2件失敗することを確認 |

### 2.1 E1 が失敗した因果（時系列）
1. `format` エラー3回：`PostToolBatch` の送信前ガードが、README / `app/server.py` のローカル URL を含む Read 結果を block → `hook_stopped`（`76a506e` で修正）。
2. 承認が出ても見えない：折りたたみに隠れた（`82f366e`）。承認の説明は生 JSON と汎用文（`c9867b0`）。最初の承認は15分で期限切れ、出し直された。
3. 検索確認（urllib で 127.0.0.1）が「外部との通信」として止まった（`30cf6c9`）。
4. 承認待ちのまま 30分に達し、`provider` の失敗（実行時間の上限）→ `maintenance_required`（`bc5aae9`）。

### 2.2 `30cf6c9` の例外の条件（レビューしてほしい）
Python の HTTP クライアント（urllib / requests / httpx / aiohttp / http.client）か、単純な `curl http://127.0.0.1:...` で、書かれた URL がすべて 127.0.0.1 / localhost / [::1]（終端文字まで検査）。かつ、他の URL・ホスト名・IP・http 以外のスキーム・実行時に決まる通信先（os.environ、getenv、sys.argv、input、eval、base64 等）・プロキシ・他の通信ツール・`curl -L` が無いこと。疑わしければ manual のまま。
限界：通信先を巧妙に組み立てるコードは文字列検査で見抜けない（既存設計と同じ。サンドボックス段階の課題）。`wget` / `Invoke-WebRequest` は対象外。

## 3. 検証の状況

- 追加・関連テストは OK。**既存の失敗**（本ブランチの変更とは無関係。`7d9a00d` でも同じ）：
  - `test_common_agent_runtime` の3件（`test_claude_selects_exact_named_agent_and_cleans_definition`、`test_repair_replaces_security_review_and_requeues_both`、`test_security_review_is_separate_and_acceptance_waits`）
  - `tests.test_quality_engine_hook_synthetic` の4件
  - ルート全体では、以前の計測で約93件の失敗が残っている（うち13件は本ブランチの `a008a0a` で修正済み）。
- **未実施**：E1 の完走。修正3件を載せたサーバーでの再試行は、まだ行っていない（E1 サーバーは起動時のコードを使うので、再起動が要る）。長時間の承認待ちの実時間での確認。小さい画面幅、キーボード操作の再確認、コントラスト比の計測。

## 4. Codex への依頼（優先順）

1. **契約：tool 承認に、サーバー側で作る要約を付ける。** `payload.summary`（何を・どこで・通信先・変更の有無）。UI は正規表現で読み取っているが、複雑なコマンドは「読み取れません」になる。サーバー側にあれば UI はそれを優先して出す。`business_summary` が汎用文のまま（E1 の承認15件中14件が同一文）の改善も同じ。
2. **契約：`p0_progress` に `total`（工程数）と `done`（完了数）を含める。** UI は `completed + (current があれば1) + remaining` で数えているが、current を数に含めるかを推測している。進捗の `current` / `next_action` が再試行中・承認待ちの間 `None` になる点も確認してほしい。
3. **保守担当の設計：** P0 ジョブの再開（`/api/retry`、`/api/tasks/model`）は保守認証（`X-Saikuru-Maintenance`）が必須だが、1人開発の環境に保守担当が存在せず、UI に再開の手段がない。利用者の案：OS の資格情報マネージャーに保守用トークンを自動生成して保存（表示しない）し、UI から「保守として再開する」を出す。サーバーが起動時にトークンを環境変数から取る現行の作りとの整合を設計してほしい。
4. **承認の期限切れ（`approval_timeout_seconds` = 900）：** 期限切れで同じ操作の承認が出し直される。再要求の回数上限、利用者への通知、`recovery_status` と承認待ちの同居の定義（現状は `retrying` のまま残る）を決めてほしい。
5. **`team_outbound_guard` / `team_egress_guard` の例外のレビュー：** ループバック（127.0.0.1 / localhost / [::1]）を道具名の判定から除外しなかった設計意図があれば教えてほしい。2.2 の条件の過不足を見てほしい。
6. **取り込みの判断：** 本ブランチの9コミットを `p0/integration` に取り込むか。取り込むなら、ファイル衝突に注意：`team_engine.py`、`team_adapters.py`、`team_egress_guard.py`、`team_outbound_guard.py` は Codex の担当領域。UI（`web/`）は Claude の担当。

## 5. E1 の再開手順（Codex が引き取る場合）

- 隔離した E1 のランタイム（`saikuru-ide-p0-e2e-20261018/runtime`、ポート 59296）は、本ブランチのコードを載せて**再起動**してから使う。
- 保守認証は、起動するプロセスのメモリ内にだけ持たせる（表示・保存・ログ出力はしない）。利用者が自分の端末でランチャーを1回実行する形で運用していた。
- 再試行は1回ずつ。再失敗したら新規着手を一時停止する。
- レビュー2件（`c9c230b2`、`e8618852`）は Claude sonnet/medium に切替済み。Codex の枠が戻ったので、元の Codex に戻すかは判断事項。
- 古いサーバーが残るとポートを塞ぐ。ランチャーは、ポート使用中なら起動前に中止する作りにした。

## 6. 戻し方・リスク

- 各コミットは独立して `git revert <hash>` で戻せる（`bc5aae9` → `c9867b0` → … の順に新しいものから）。
- 本番（port 8790）には何も反映していない。本番は最新の修正（P0 を含む `7d9a00d` 以降）を含まない。本番への反映（変更34件＋新規43件、本番固有の9件は保持）は、利用者の判断待ち。
- 文字列一致の検査では見つからない固有名がありうる。公開前に、案件名・社内の呼称がないことを確認すること。

## 7. 参照

- 受入の記録：`docs/p0/UI_ACCEPTANCE.md`（3.7：一覧の1行化と動線確認、3.8：操作の承認の表示）
- 契約：`docs/p0/contract.md`
