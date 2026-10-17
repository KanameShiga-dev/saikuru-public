# Claude ⇔ GitHub Copilot の自動の切り替え（2026-10-15）

利用者の判断による変更です。認証の失敗や利用上限で業務を止めないため、担当が動けなくなったら、もう一方のサービスで続けます。

## いつ切り替えるか
| 止まった理由 | 切り替えの向き |
|---|---|
| Claude のログインが切れた | Claude → Copilot |
| Claude の利用上限 | Claude → Copilot |
| Copilot の利用上限・クレジット切れ | Copilot → Claude |
| Copilot のログインが切れた | Copilot → Claude |

## 切り替える条件（すべて満たすときだけ）
1. プロジェクトのデータ分類（`DATA_CLASSIFICATION.md` の「Project default」）が **Public（公開）か Internal（社内）**。Confidential（機密）・Restricted（極秘）・不明なら、別のサービスへは自動で送らない。
2. 切り替え先が、チーム設定で有効なプロバイダである。
3. 切り替え先がその担当に対応している（Copilot は計画・実装・レビューだけ。調査担当は切り替えない）。
4. 切り替え先が今使える。Claude はログイン済みで待機中でないこと、どちらも測った利用枠・クレジットが5%以上残っていること。
5. 直前に切り替えてきた元のサービスに、まだ使えないうちに戻らない（行ったり来たりしない）。

## モデルの対応
- Claude → Copilot：同じ Claude の系統を Copilot で使う（opus → claude-opus-5.5、sonnet → claude-sonnet-5.5、haiku → claude-haiku-4.5）。
- Copilot → Claude：Copilot で Claude の系統を使っていればその系統、ほかのモデル（GPT など）なら sonnet。
- エフォートはそのまま。

## 切り替えられないとき
- Claude のログイン切れ・利用上限：これまでどおり待機し、回復したら自動で再開する。
- Copilot のログイン切れ：停止する（`copilot login` のあと再試行）。
- どちらも、依頼ごとに1回「切り替えなかった理由」を記録に残す（`model_failover_skipped`）。急ぐ場合は、作業ボードの担当・モデルの切り替えから手で選べる。

## 元に戻すとき
- 切り替えた担当のうち、**まだ始まっていない担当**は、元のサービスが使えるようになったら元のモデルに戻す（`model_restored`）。実行中・終わった担当はそのまま。
- Claude のログイン切れ：ログインが戻ったら戻す。利用上限：測った利用枠が回復するか、示された回復の時刻を過ぎたら戻す。Copilot のログイン切れ：采来からはログインの回復を確かめられないため、Claude のまま続ける。

## 記録と知らせ
- 切り替えたとき：`model_failover` の記録と、PC の通知（PC 通知がオンのとき）。
- 記録には、理由・担当・切り替え先のモデル・データ分類を残す。

## 実装
- `team_engine.py`：`_failover`・`_provider_ready`・`_failover_ready`、作業の割り振り（tick）からの呼び出し、`_restore_recovered_models`。
- `team_config.py`：`failover_profile`、`FAILOVER_DATA_CLASSES`。
- `team_security_templates.py`：`project_data_class`。
- `team_recovery.py`：Copilot のクレジット切れ（`USAGE_LIMIT`）とログイン切れ（`COPILOT_AUTH`）の判定。
- テスト：`test_claude_failover.py`。

## 確かめていないこと
- Copilot のクレジット切れ・ログイン切れの実際のメッセージ（想定の文言で判定している）。実機で起きたら、文言を確かめて判定を直す。
