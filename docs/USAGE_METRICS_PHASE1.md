> 添付パッチの提供元資料です。実施済み・導入先の設定に関する記載は、このPCでの完了を意味しません。現在の統合内容と確認範囲は [2026-10-09統合記録](CHANGE_REQUEST_MERGE_20261009.md) を参照してください。資料レビュー緩和は既定で無効です。

# AI利用量削減 改修提案：取り込み状況と Phase 1（計測基盤）

対象：「采来 改修提案書：AI利用量削減のための経験再利用・実績連動型最適化機構」（2026-10-08）
方針：提案書 §13 に従い、既存の実装を調べたうえで **Phase 1（計測だけ）を先に実装**。AIの動き・権限・承認は変えていない。既存機能とぶつかる点は「4. 相談事項」にまとめ、決まるまで実装しない。

## 1. 既存実装の確認結果（提案書 §2 の再確認）

| 項目 | 実際の状態 |
|---|---|
| 試行ごとの記録 | `task.agent_run`（現在の試行）＋`agent_run_history`（過去30試行）。試行単位の追跡は既に可能 |
| トークン | Claude：最終結果メッセージの `usage` だけを記録。**結果が届かない失敗・中止では記録なし**（0と区別できない）。Codex：累積値を上書き（二重計上はなし） |
| ツール呼び出し回数・ターン数・所要時間 | 未記録（イベントログに「処理: ツール名」が残るだけ） |
| 経験・Skillの提示 | 文脈に入れているが、何を提示したかの記録なし。経験は**関連度0でも最大12件**を常に入れている（提案書の懸念どおり） |
| Skillの読み込み | イベント `skill_read` のみ（試行との対応なし） |
| 利用枠 | `team_usage.py`（%の窓）。試行の開始・終了時の値を `quota_before/after` に保存。トークンとは別指標で、混同はしていない |
| 判断Provider | `team_decisions.py`（有限選択の振り分け、Ollama をシャドーで運用中）、`decision_metrics.py`（判断の集計）。**提案書 Phase 3 の SHADOW と同じ考え方が既にある** |
| 版の区別 | 采来はGitではないため、ソースのコミットSHAは無い |

## 2. Phase 1 で実装したもの（計測のみ）

| 提案書の項目 | 実装 |
|---|---|
| 生データと正規化値の分離 | `agent_run.usage`（従来の生データ）、`usage_raw`（ターン数・所要時間・API時間・コスト見積り・モデル別トークン）、`usage_normalized`（入力非キャッシュ・キャッシュ読取・キャッシュ作成・出力・入力合計） |
| 二重計上しない | Provider の合計値で置き換える（加算しない）。ストリームの同じ呼び出しは id で1回だけ数える |
| 失敗・中止・再試行の消費 | 結果が届かなかった試行は、ストリームで見えた呼び出しごとの値の合計を `partial` として記録 |
| 取得不能は UNKNOWN | 値が無ければ `measurement: unknown`・各値 None。集計は測定できた試行だけを合計し、unknown は件数で示す |
| ツール呼び出し回数 | `agent_run.tool_calls`（ツール名ごとの回数） |
| 使用した経験・Skill | `agent_run.context_refs`：提示した経験のID（キーのハッシュ）、提示したSkillのID・名前・依頼時追加か、各文字数、引き継ぎ文脈全体の文字数。**本文は記録しない** |
| Skillの状態 | ①候補＝既存の skill_candidates ②提示＝context_refs ③読み込み＝`agent_run.skill_reads` ⑤受入＝依頼の受け入れ状態。④「作業へ適用」は自動では判定できないため未測定（担当の自己申告は根拠にしない） |
| 版の区別 | `saikuru_version`（采来の Python ソースのハッシュ12桁。コミットSHAの代わり）、`optimization: {mode: off, policy_version: null}` |
| 人の時間 | 承認の待ち時間（`approval_wait_seconds`）だけ集計。**人の実作業時間は未測定**（承認待ち時間とは別物） |
| 計測レポート | `team_optimization_metrics.py`（読み取り専用。JSON の集計、`--csv` で試行ごとの行） |

変更したファイル：`team_engine.py`（Context：`record_usage` に extra、`observe_message`、`_store_measurement`、`finish_agent` で確定、`agent_run` に版と最適化モード、`context_refs`、`skill_reads`）、`team_adapters.py`（Claude のストリームで `observe_message`、結果で extra を渡す。他の Context は従来どおり）、`team_handoff.py`（`context(..., refs)` で提示内容のIDと文字数を記録）。新規：`team_usage_metrics.py`、`team_optimization_metrics.py`、`test_usage_metrics.py`（6件）。テスト：既存を含め65件合格。

> 控え：`team_handoff.py`・`server.py` は `data/backups/usage-metrics-20261008/`。`team_engine.py`・`team_adapters.py` は控えを取る前に編集したため、上の変更一覧で戻す（ほかの変更は `security-by-default-20261008` の控え以降）。

## 3. 計測レポート（2026-10-08 時点の実測。比較の基準値で、削減効果ではない）

`python -X utf8 team_optimization_metrics.py` の結果（Phase 1 より前の試行も、トークンだけは記録されていた）。

| 区分 | 試行 | 入力合計 | うちキャッシュ読取 | 出力 |
|---|---|---|---|---|
| 全体 | 150（測定150、unknown 0） | 20,756,880 | 15,434,561 | 898,723 |
| opus（計画・レビュー） | 69 | 7,916,935 | 5,568,268 | 286,226 |
| sonnet（実装・調査） | 81 | 12,839,945 | 9,866,293 | 612,497 |
| 失敗した試行 | 5 | 1,282,673 | 1,003,539 | 120,596 |

- 依頼16件。入力の約74%がキャッシュ読取、作業担当（builder）が入力全体の約54%。
- 再試行の多い依頼ほど多く使っている（例：19試行・再試行6回の依頼が最大）。
- Phase 1 より前の試行には、ツール呼び出し・提示した経験/Skill・版が無い（None）。これからの依頼で記録される。
- **実動作での確認は次の依頼から**（今回は依頼を流していない）。

## 4. 相談事項（既存機能とぶつかる点）

| # | 提案書の内容 | ぶつかる既存機能 | 提案 |
|---|---|---|---|
| 1 | Phase 2：経験・Skillの注入をA〜Dで切り替える比較モード | 昨日入れた「依頼時にスキルを追加して候補にする」「スキルを使わない」 | 比較モードは**検証用の依頼にだけ付ける別設定**にし、通常の依頼には出さない。検証用の依頼では依頼時のスキル追加を使わない |
| 2 | Phase 2：コンテキスト予算（関連の低い経験を入れない） | 経験の選び方（今は関連度0でも最大12件） | 全依頼の動きが変わるため、まず SHADOW（入れなかったら何が減るかだけ記録）→確認後に ON |
| 3 | Phase 3：実績連動の最適化（OFF/SHADOW/ON） | 判断Provider（`team_decisions.py`、Ollama シャドー運用中）と同じ役割 | 別の最適化機構を作らず、**判断Providerに「実行方式」の判断を足す**形で既存のシャドーの仕組みを使う。Phase 3 は計測結果を見てから |
| 4 | Phase 3：固定スクリプトでの実行 | Security by Default（送信防御・承認） | 自動の切り替えでも承認条件は緩めない（提案書も同意見）。確認のみ |
| 5 | Phase 2：比較試験の実行 | Team プランの利用枠（実際に消費する） | 課題の数・回数・使ってよい利用枠を決めてから。検証用の課題は実業務と分ける |
| 6 | 人の実作業時間 | 自動では測れない | 受け入れ時に任意で入力する欄を設けるか、未測定のままにするか |

## 5. 決定と Phase 2 の状況（2026-10-08）

相談事項1〜5は提案どおりで決定。

- 1：検証モード（A〜D）は**検証用の依頼にだけ**付ける設定として実装（「依頼を追加」の折りたたみ欄。実業務データを含まない確認が必須、依頼時のスキル追加とは併用不可）。
- 2：文脈の予算は**既定 SHADOW**で実装（選別を記録するだけ。AIに渡す内容は不変）。`data/config.json` の `context_budget` で off／shadow／on。
- 3：Phase 3 は計測結果を見てから。行う場合は判断Provider（team_decisions.py）に実行方式の判断を足す。
- 4：比較試験の利用枠・課題数は未決定（試験は未実施）。
- 5：人の実作業時間は未決定（未測定のまま）。

手順と集計方法：docs/USAGE_EVALUATION.md。変更：`team_handoff.py`（`reuse_budget`、`context` の検証モードと予算）、`team_engine.py`（`create_job` の検証モード、試行の記録に検証モードと予算モード）、`team_usage_metrics.py`（`context_budget`）、`team_optimization_metrics.py`（`evaluation_modes`・`context_budget` の集計、受入1件あたりの利用量）、`server.py`、`web/index.html`・`web/app.js`。テスト：`test_phase2.py`（5件）、全70件合格。控え：`data/backups/phase2-20261008/`。
