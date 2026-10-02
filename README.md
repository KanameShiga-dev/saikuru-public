# 采来 — サイクル —

Codex／Claude Codeとプロジェクトの作業状態・引き継ぎ・検証結果を管理する、Windows向けローカル開発運用基盤です。

最新ソースから作成した公開用スナップショットです。過去のGit履歴、個別環境の運用記録、DB、認証情報、操作画像は含めません。

## マニュアル

[構成・操作マニュアル](https://kanameshiga-dev.github.io/saikuru-manual/)

## 起動

Python 3.11以上、利用するCodex／Claude Code CLI、必要に応じローカルOllamaを準備してください。CLIへのログインと共通Agent定義の導入が必要です。共通Agentが未導入の場合、開発担当の実行は停止します。

```powershell
python -X utf8 manage.py start
python -X utf8 manage.py status
python -X utf8 manage.py stop
```

実際のURLは起動結果を確認してください。調査ルートの既定値は一般例の `C:\Projects` です。導入先に合わせてteam_ledger.pyのROOTとteam_folders.pyのBASEを同じ境界に設定し、復元先の制約と画面の説明も合わせて確認してください。個別アプリのGUI自動承認は無効にしています。

## Ollamaのセットアップ

[公式Windows手順](https://docs.ollama.com/windows)から導入します。この実装のSystem One接続にはOllama 0.35以上と許可モデルを準備してください。

```powershell
ollama --version
ollama pull tev1:0.8b
ollama list
```

チーム設定の判断ProviderでローカルOllamaと導入済みモデルを選択し、保存して接続確認します。許可モデルはtev1:0.8b／tev1:4bです。モデルは自動ダウンロードしません。pullは通信量とディスク容量を使います。

Ollama Windowsアプリが起動していれば別のserveは不要です。未起動の場合は別のターミナルで `ollama serve` を実行します。GPUなしではCPUでの利用を前提に小さいモデルから始め、速度とメモリ負荷を確認してください。

接続先はHTTPループバックのみです。LANへの公開とAPIキー入力は不要です。decision.env.exampleは自動では読み込みません。接続失敗時は起動状態・モデル名・CLIとサーバーのバージョンを確認してください。遅延時はローカル判断を停止して通常モデルで継続できます。

## 運用上の境界と未確認事項

AIの報告だけで完了とは判断しません。削除・公開・外部送信・権限変更は作業範囲と承認を確認してください。CLIへ渡した作業内容は各提供サービスへ送信されます。ローカルDB・ZIP・ログは個人情報を含み得るため公開しないでください。

このスナップショットの新規PCへの導入と一連の実動作は未検証です。汎用的な速度・トークン削減効果を保証しません。ライセンスの許諾条件は未設定です。
