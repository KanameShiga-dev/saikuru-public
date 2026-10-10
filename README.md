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


## 判断待ち・通知・台帳のタブ（2026-10-04）

履歴で勤務時間内・時間外・システム停止を分けて集計し、試行別トークンを確認できます。停止カードから判断材料と操作先を確認し、台帳詳細は5タブで編集できます。常駐PC通知は初期無効で、勤務時間内に件数だけを送信します。

[計測の定義・操作・制約](docs/DECISION_WAIT_METRICS.md)。実モデルの完了一巡、OS通知の実表示・音声、スマホ実機は未確認。Teams等の外部通知は未実装です。

## 参考ファイルの添付（2026-10-04）
新規依頼と台帳相談で画像・PDF・Office・テキスト／ソースを参考添付できます。送信時にローカルで抽出・OCRし、指示の偽装や検査不能なファイルを拒否します。文書は抽出文字、画像は正規化したPNGとして担当へ引き継ぎます。4ファイル・各5MB・合計15MBまで。既存の作業範囲と承認条件は維持します。

[対応形式・導入環境・拒否・保存・限界](docs/ATTACHMENT_SAFETY.md)、[初期画像実装の記録](docs/IMAGE_ATTACHMENTS.md)。専用Pythonの依存はrequirements-attachments.txt、OCRはWindows日本語・英語が必要です。個人環境のdata設定・添付・DBは公開対象外です。実ファイルのOCR、拒否・誤検知、AI送信の一巡は未確認です。検査通過は安全の保証ではありません。

## 指示ファイルの容量・不足警告（2026-10-04）
台帳にAGENTS.md系/CLAUDE.md系の不足警告とフィルタを追加。「どちらか不足」「両方なし」「AGENTS.md系なし」「CLAUDE.md系なし」「両方あり」「未確認」を既存の検索・分類と組み合わせます。台帳表示時にファイル名の存在を確認するため、旧台帳の再調査は不要です。自動生成・編集・作業停止は行いません。

容量・行数・階層合計候補も診断します。Codexの既定合計32 KiB、Claude Codeの推奨200行未満とCLAUDE.mdの4 MiB上限を区別し、采来独自の容量注意も明示します。未確認の範囲や実効設定を断定しません。[調査根拠・操作・コンバートによる作成・限界](docs/INSTRUCTION_SIZE.md)。実画面のフィルタ・警告操作、境界値、実際のAI読み込み範囲は未確認です。

- [実画面の必須要件・資料制作と回答時ファイル選択](docs/MEDIA_REQUIREMENT_FIX_20261005.md)

- [共通Agent定義（Codex / Claude Code）](shared-agents/README.md)


## 2026-10-06 更新

確認指摘から修正・再確認への連携、Word/Excel作成、工程途中の成果物判定、回答添付欄などを改善しました。

- [確認指摘の修正と再確認](docs/CONFIRMATION_REPAIR_20261006.md)
- [変更依頼の統合と未確認事項](docs/CHANGE_REQUEST_INTEGRATION_20261006.md)
- [Word・Excelの作成・編集](docs/OFFICE_DOCUMENTS.md)

制作用依存は `requirements-documents.txt` を参照してください。


## 企業の経験とスキル

- [企業記憶の自動蓄積](docs/ENTERPRISE_MEMORY.md)
- [経験からスキル候補へ](docs/EXPERIENCE_SKILL_CANDIDATES.md)
- [正式化・一覧・共有・版管理](docs/PROJECT_SKILL_RELEASE.md)

Claude利用枠は15分間隔で取得し、再起動後も次回取得時刻を保持します。実操作とモデル利用の一巡確認は未実施です。


## 資料制作・スキル管理の追加（2026-10-07）

PDFのデザイン抽出、PPTX本文・表・ノート読み取り、Artifact用のローカルデータ制作、外部スキルの取り込み・適用管理、失敗カードの案内を追加。全依頼のセキュリティレビュー、検索出口制御、拒否理由からの修正依頼も反映。

- [統合内容と確認範囲](docs/CHANGE_REQUEST_MERGE_20261007.md)
- [依存環境の導入記録](docs/DOCUMENT_RUNTIME_SETUP_20261007.md)
- [外部スキルの取り込み](docs/SKILL_IMPORT.md)

新規PCでは専用のPython仮想環境に requirements-documents.txt を導入し、data/attachment-runtime.json のpythonへ実行ファイルの絶対パスを設定してください。MP4はFFmpegと日本語フォント、音声付きの場合は起動中のVOICEVOXが必要です。ローカル設定・Python環境・認証情報・DBは公開物に含めません。


## 2026-10-10 更新

Copilot実装担当・コマンド結果の引き継ぎ・資料プレビュー等を更新しました。公開対象と未解消の制限は[更新記録](docs/RELEASE_20261010.md)を参照してください。評価実行用資材は今回の公開に含みません。
