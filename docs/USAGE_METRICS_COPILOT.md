# 使用量の計測の拡張（Copilot のトークン・クレジット、重みつきの量、1回目の呼び出し）

2026-10-10〜12 の変更です。`team_optimization_metrics.py` の集計（`python -X utf8 team_optimization_metrics.py`）に反映されます。

## Copilot のトークンを集計に入れる（`team_usage_metrics.copilot_normalized`）
- Copilot の作業は、これまで記録（`agent_run.copilot_tokens`）があっても集計では「測定なし」になっていました。
- CLI の利用統計ファイルの値を正規化して集計します。`inputTokens` にはキャッシュの読み込み・書き込みが含まれます（記録 445 件で確認）。
- 記録が無い作業は 0 ではなく「測定なし」のままです。

## クレジットと重みつきの量
- 試行ごとに `copilot_credits` と `copilot_premium_requests` を出し、合計には失敗・中止の試行のクレジットも含めます（消費されているため）。
- `weighted`：料金に近い重みつきの量（キャッシュなし入力 1・読み込み 0.1・書き込み 1.25・出力 5）。提供元の数え方そのものではありません（`team_usage_metrics.WEIGHTS`）。
- 評価モード A〜D の集計に `weighted_per_accepted`（受け入れた成果物 1 件あたり）を加えました。

## 1回目の呼び出しの入力（`agent_run.first_call_usage`）
- Claude の各担当の最初の API 呼び出しの使用量（数値だけ）を残します（`team_engine.Context.observe_message`）。
- 参考情報の画像化の ON／OFF を比べるとき、最初の呼び出しは参考情報以外の中身が同じなので、1回あたりの差を精度よく測れます。
- 集計の `first_call_input`（入力の合計）、`reference_transport`（画像／テキスト）、`token_reduction`（判定のモード）として出ます。

## 確かめたこと
- 実際の記録で、Copilot のクレジットはトークン量にほぼ比例していました（モデルごとの当てはまり R² ≒ 1.0）。トークンを減らすと Copilot の費用も減ります。
