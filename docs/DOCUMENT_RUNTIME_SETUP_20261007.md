# 資料制作環境の導入記録（2026-10-07）

## 実施内容

利用者のインストール依頼に基づき、パッチ指定の pypdf 6.19.0 / pypdfium2 5.14.0 を導入しました。

資料用Pythonを data/document-runtime-20261007/Scripts/python.exe に変更。既存CodexランタイムのPython 3.12.14から --system-site-packages の仮想環境を作成し、既存の資料ライブラリを利用しながら追加2種類だけ専用環境にインストールしました。共用Python側のパッケージは変更していません。元のPython環境も必要な構成です。

## インストール元と手順

1. 既存資料用Pythonで python -m venv --system-site-packages data/document-runtime-20261007 を実行。
2. 専用Pythonの pip --isolated download --index-url https://pypi.org/simple --only-binary=:all: --no-deps で指定版のwheelを data/install-cache-20261007/ に取得。
3. PyPIの指定版メタデータにあるSHA256とwheelを照合。
4. pip --isolated install --no-index --find-links data/install-cache-20261007 --no-deps pypdf==6.19.0 pypdfium2==5.14.0 でローカルから導入。
5. 作業・相談0件を確認し、data/attachment-runtime.json のpythonだけ切り替え。他の設定は保持。

## 確認した環境

| 用途 | 版・状態 |
|---|---|
| PDF作成 | reportlab 4.4.9 |
| PowerPoint | python-pptx 1.0.2 |
| 画像 | Pillow 12.3.0 |
| Word | python-docx 1.2.0 |
| Excel | openpyxl 3.1.5 |
| PDF文字・書式抽出 | pypdf 6.19.0（今回導入） |
| PDF描画・配色抽出 | pypdfium2 5.14.0（今回導入） |
| 動画 | 既存FFmpegを検出、libx264/AACあり |
| 日本語フォント | 既存meiryo.ttcあり |
| ナレーション | 既存VOICEVOXが稼働、50021番の話者API応答あり |

7種類のPythonモジュールの読み込みを確認。追加のGUIアプリ導入・権限変更・外部公開は行っていません。Artifact用データは既存のCLIでローカル制作するため、新しいアプリや外部アカウントを追加していません。

実資料のレンダリング、PDF抽出、実モデル一巡は未実施です。資料・添付ツールは処理のたびに設定を読むため、今回の設定切替にサーバー再起動は不要です。

## 復旧

変更前設定は data/backups/document-runtime-20261007/attachment-runtime.json に保存。作業と添付検査が動いていない状態で、この設定をdata/attachment-runtime.jsonへ戻せば元のPythonを使います。専用環境やwheelは控えとして残せます。依頼DB・認証情報を巻き戻す必要はありません。

wheelの照合結果は data/install-cache-20261007/verified-wheels.json、環境の記録は同フォルダのenvironment-report.json。data配下は公開対象外です。
