"""Build the offline Japanese manual. No application data or external requests."""
from pathlib import Path
import html, json, hashlib, base64

ROOT=Path(__file__).resolve().parent
sections=[]
def add(key,title,body): sections.append((key,title,body))
def shot(name,caption):
    return f'<figure><a href="assets/{name}.jpg" target="_blank"><img src="assets/{name}.jpg" alt="{html.escape(caption)}" loading="lazy"></a><figcaption>{caption} ｜ 画像を押すと拡大。</figcaption></figure>'
def table(head,rows):
    return '<div class="tablewrap"><table><thead><tr>'+''.join('<th>'+x+'</th>' for x in head)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+x+'</td>' for x in row)+'</tr>' for row in rows)+'</tbody></table></div>'

add('overview','01 概要とできること', '''
<p class="lead">采来 — サイクル —は、このPC上の開発依頼を、計画・調査・実装・レビューへ分けて進めるローカルの作業管理ツールです。CodexとClaude CodeのCLIを統括し、作業履歴、判断待ち、プロジェクト台帳をブラウザで管理します。</p>
<div class="cards"><div><b>作業ボード</b><p>依頼と担当の状態を確認。停止理由に応じて回答、承認、再試行します。</p></div><div><b>プロジェクト台帳</b><p>目的・分類・起動方法・検証・次の作業を記録。フォルダの整理を支援します。</p></div><div><b>共通ハーネス</b><p>共通AgentとSkillを参照。プロジェクトごとの範囲・資料・完了条件を組み合わせます。</p></div></div>
<h3>このツールの特色：モデルをまたいで開発を続ける</h3>
<ul><li><b>モデルの切り替えと引き継ぎ。</b>役割別にCodex・Claudeを使い分け、停止・待機タスクの担当を変更できます。対象ソースと、DBに記録した実施内容・利用者判断・未完了事項を次の担当が確認します。前モデルの内部記憶や会話全体がそのまま移るという意味ではありません。操作は第6・7章。</li><li><b>Gitの差分を使って引き継ぎ情報を絞る。</b>基準コミット・変更ファイル・必要な差分を確認し、関連する資料と残作業を渡す運用で、全ソースの再提示や説明の繰り返しを減らします。これはGitを活用する運用上の特色です。差分の自動抽出・送信最適化が全経路に実装済みという意味ではありません。詳細は本章の下記。</li><li><b>軽い判断をローカルProviderで測定。</b>OllamaのTev1を判断に使い、開発モデルへ任せる処理を整理する狙いがあります。現在はAgent選択のシャドーモードで、実採用は既存判断を維持します。削減効果は未測定です。詳細は第8章。</li><li><b>共通資料の必要時読み込み。</b>共通Agent・Skillはメタ情報から選び、必要な本文だけ読み込みます。再試行指示の完全一致重複も整理し、過去の指示は履歴へ分けます。</li></ul>
<h3 id="git-tokens">Gitによるトークン削減の考え方と運用</h3>
<p>Git自体がトークンを削減するのではなく、<b>AIが読む情報を変更箇所と必要な文脈に絞る</b>ことで、入力トークンと再調査を抑えることを狙います。Gitの基準点は、モデルを切り替えたときに「どこまで実施したか」を照合する手掛かりにもなります。</p>
<ol><li><b>作業開始時の基準を記録。</b>現在のコミット、ステージ済み・未コミット・未追跡の状態を確認します。利用者の既存変更と今回の変更を区別します。</li><li><b>変更一覧から確認対象を選ぶ。</b>変更ファイル名を先に確認し、今回の作業に関係する差分・関数・関連資料を読みます。未追跡ファイルは通常の差分に出ないため別途確認します。</li><li><b>引き継ぎを短く、根拠付きにする。</b>基準コミット、対象ファイル、変更概要、検証結果、未検証事項、残作業を記録します。次の担当はこの記録と現在のソースを照合し、重複実装を避けて続行します。</li><li><b>必要な文脈を追加で読む。</b>差分だけで判断できない共通処理、依存先、設定、データ構造、受入条件は必要な範囲を追加します。情報を省きすぎて誤修正することを防ぎます。</li></ol>
<details><summary>読み取りコマンドの例（対象プロジェクト内で実行）</summary><pre>git rev-parse HEAD
git status --short
git diff --name-status
git diff --cached --name-status
git diff -- path/to/relevant-file
git diff --cached -- path/to/relevant-file</pre><p>コマンド例は自動実行されません。実際の作業環境・許可を確認してください。差分に秘密情報が含まれる場合はAIへ渡さず、未追跡・バイナリ・生成物の扱いを別に確認します。Git内部の直接読み取り制限を回避する指示ではありません。</p></details>
<div class="note"><b>実装済み・運用方法・効果を区別</b><p>共通資料の必要時読み込み、再試行指示の重複整理、引き継ぎ履歴の保存は実装されています。Git差分を使う手順は推奨運用として説明しており、統括による全タスクの差分自動収集・最適化は確認できていません。トークン削減率や利用枠の節約量は未測定です。比較するときは同じ作業・モデル条件で、入力トークン、キャッシュ、出力、再試行回数と結果の品質を確認します。文字数をそのままトークン数として報告しません。</p></div>
<p>利用者は開発者・管理者です。依頼の範囲と完了条件を決め、AIからの報告と根拠を確認します。台帳への登録だけではAIの作業は開始しません。Computer Useは基本禁止という運用を保持します。</p>'''+shot('01-board','作業ボード：待機・実行・判断・終了の4列'))

add('architecture','02 アーキテクチャ', '''
<h3>モデル・Git・Ollamaを含む全体図</h3>
'''+(ROOT/'architecture-overview.svg').read_text(encoding='utf-8')+'''
<p>図の実線は実装の接続、破線はGit差分を活用する推奨運用です。GitHubへのpushを前提とせず、ローカルGitでも差分を確認できます。Ollamaは現在、Agent選択をシャドー測定する接続です。Codex・Claudeの開発処理をOllamaへ置き換える接続ではありません。</p>
<h3>層ごとの構成</h3>
<div class="architecture" aria-label="采来 — サイクル —の構成図">
<div class="layer"><b>利用者</b><span>PCブラウザ ／ 同じWi-Fiのスマホ</span></div><div class="arrow">↓ 画面操作・回答・明示承認</div>
<div class="layer"><b>Web UI</b><span>web/index.html + app.js ／ web/ledger.html + ledger.js</span></div><div class="arrow">↓ ローカルHTTP API（スマホは認証付きHTTPSゲートウェイ経由）</div>
<div class="layer"><b>Python統括サービス</b><span>server.py → team_engine.py → 状態・依存工程・承認・レビュー・再試行</span></div>
<div class="branches"><div><b>実行アダプター</b><p>Codex app-server<br>Claude Code headless<br>担当ごとの専用セッション</p></div><div><b>共通Agent / Skill</b><p>定義の検証<br>メタ情報で選択<br>必要な本文だけ読み込み</p></div><div><b>判断層</b><p>ローカルOllama<br>有限候補・安全ゲート<br>現在はシャドー運用</p></div></div>
<div class="arrow">↕ 記録・設定・作業対象の限定</div><div class="branches"><div><b>SQLite / config</b><p>依頼・担当・履歴<br>引き継ぎ・台帳<br>役割・モデル・判断設定</p></div><div><b>各プロジェクト</b><p>ソース・正本資料<br>プロジェクト固有の範囲<br>検証結果・ハーネス</p></div><div><b>バックアップ</b><p>DBスナップショット<br>移動・アーカイブ履歴<br>ZIP・補完前の資料</p></div></div></div>
<p>UIと統括はローカルPythonサービス、AIの開発処理は各CLIに分かれます。Codex/Claudeが直接、統括DBや統括プログラムを編集する設計ではありません。共通定義は別プロジェクト <code>C:\\Projects\\SharedHarness</code> に置き、目的・対象範囲・実行方法は各プロジェクトに残します。</p>
<div class="note"><b>セッションの意味</b><p>采来 — サイクル —が担当ごとに独立セッションを起動します。AIが自律的にさらに子Agentを生成して分担する仕組みとは区別してください。モデル名・共通Agent名・セッションIDは担当詳細で確認します。</p></div>
'''+table(['層','主要ファイル','責務'],[
['HTTP/UI','server.py、web/','API、画面配信、認証されたUI操作'],
['統括','team_engine.py','依頼・依存工程・承認・状態遷移・修正ループ'],
['CLI接続','team_adapters.py','Codex / Claudeの実行と結果取り込み'],
['共通定義','team_common_agents.py、team_shared_harness.py','Agent検証、Skill選択、必要な共通資料の読み込み'],
['引き継ぎ','team_handoff.py、team_instructions.py、team_context_pages.py','履歴、重複整理、大きな文脈の分割'],
['復旧','team_recovery.py、team_questions.py','原因分類、回答案、判断フォーム'],
['台帳・整理','team_ledger.py、team_move.py、team_archive.py','プロジェクト情報、移動、ZIP保存と削除'],
['ハーネス変換','team_harness.py、team_harness_fill.py','調査、資料案、確認した構成変更、根拠付き補完'],
['モデル・利用枠','team_models.py、team_usage.py、team_cli_update.py','一覧取得、残量、CLI更新'],
['判断','team_decisions.py、team_ollama_decisions.py、decision_metrics.py','Provider接続、安全ゲート、候補と測定'],
['スマホ','mobile_gateway.py','LAN接続、HTTPS、合言葉・Cookie認証']]))

add('storage','03 フォルダ・DB・設定の構成', '''
<pre>agent-team/
├─ server.py / manage.py       起動・統括API
├─ team_*.py                  各機能
├─ web/                       作業ボード・台帳
├─ data/                      本番設定・DB・ログ・バックアップ
├─ docs/                      機能別の技術資料
│  └─ manual/                 このマニュアルと画像
└─ AGENTS.md / SCOPE.md / PROJECT_CONTEXT.md
ai-harness-common/             共通Agent・Skillの正本（別フォルダ）</pre>
'''+table(['保存先（data/内）','内容','扱い'],[
['config.json','役割・モデル、作業対象、承認、判断Provider','画面保存の対象。設定だけでOS権限が増えることはない'],
['team.sqlite3','依頼・担当・承認などの正本とイベント','直接編集せずボードで操作'],
['handoff.sqlite3','引き継ぎ情報、判断・訂正などの履歴','古い報告を消さず保持'],
['project_ledger.sqlite3','プロジェクト、調査、改訂、移動・アーカイブ履歴','整理後も台帳のIDと履歴を保持'],
['server.stdout.log / server.stderr.log','起動・統括のログ','エラー調査時に対象時刻を絞る'],
['backups/ および各機能のバックアップ','コード・DB・機能別の復旧材料','後続更新を上書きしないよう比較して復旧']])+'''
<p>プロジェクトのZIPは通常 <code>C:\\Projects\\_project_archives\\&lt;アーカイブID&gt;\\project.zip</code> に保存します。</p>
<p>ユーザー領域のAgent・Skillアダプターと共通フォルダの連携は共通ハーネス側の管理対象です。設定・DB・プロジェクトフォルダ・共通定義を別の保存対象として扱ってください。</p>''')

add('quickstart','04 最初の依頼を出す', '''
<ol><li><b>対象を台帳で確認。</b>目的・正本ソース・作業範囲・起動方法・検証方法を整理します。</li><li><b>チーム設定を確認。</b>役割ごとのCLI・モデル・推論設定と、作業対象への登録状態を確認します。</li><li><b>「＋ 依頼を追加」を押す。</b>依頼名、対象プロジェクト、達成したいこと・完了条件を入力します。</li><li><b>進め方を決める。</b>計画を先に確認するなら自動進行を選択しません。自動進行を選ぶ場合も判断待ち・権限・環境障害で停止することがあります。</li><li><b>送信内容とCLI利用枠を確認。</b>秘密情報・家族の記録を含めず、「計画を開始する」を押します。</li><li><b>計画と結果を確認。</b>必要な承認・質問に回答し、検証の根拠とレビュー結果を読んで完了を判断します。</li></ol>
<details><summary>依頼文の記入例</summary><pre>目的：一覧のカードを読みやすくする。
対象：web/ledger.html・ledger.js・ledger.css。
触らないもの：DB形式、削除・アーカイブ処理。
完了条件：検索と詳細表示を維持し、スマホ幅でも文字が読める。
検証：変更差分、画面表示、未検証項目を報告する。
公開・push：行わない。</pre></details>
'''+shot('02-request','依頼追加：対象・目的・自動進行・送信確認'))

add('board','05 ボードの見方と実行ループ', '''
'''+table(['列・状態','意味','利用者の操作'],[
['待機している作業','先行工程・承認・新規着手の再開を待っている','依存する判断・承認と一時停止状態を確認'],
['チームが作業中','担当セッションが実行中','報告・最近の動きで状況を確認。必要なら中止'],
['あなたの判断','質問・承認・失敗などで止まっている','カードを開き、停止理由と回答先を確認'],
['終了した作業','担当が終了・中止等になった履歴','日付＋午前／午後を開いて内容を確認']])+'''
<div class="flow"><span>依頼</span>→<span>計画</span>→<span>調査・実装</span>→<span>検証・レビュー</span>→<span>結果確認</span></div>
<p>レビューで修正が必要なら修正と再レビューへ進みます。修正回数には上限があり、初期設定の <code>max_repairs</code> は2です。上限・情報不足・権限・認証・利用枠・環境障害では人の判断へ戻します。無制限に失敗を繰り返すループではありません。</p>
<p><b>「新規着手を再開」</b>は全体の新規着手一時停止を解除します。個々のカードの質問・失敗まで解除する操作ではありません。判断待ちカードは別に回答・再試行が必要です。<b>「全作業を中止」</b>は停止操作なので、継続したい場合の再開ボタンと取り違えないでください。</p>
<p>終了履歴は自動削除しません。日本時間の日付と午前（00:00〜11:59）／午後（12:00〜23:59）ごとに折りたたみます。担当の終了と依頼全体の完了は別です。報告の <code>done</code> だけで検証済み・実機動作確認済みとは判断しません。</p>''')

add('recovery','06 判断への回答とボードからの復旧', '''
<ol><li>「あなたの判断」のカードを開く。</li><li><b>停止理由と次の操作</b>を読み、確認日時・担当側／統括側・未確認点を分けて確認する。</li><li>必要なら「原因を確認して回答案を作る」。生成した案の内容を確認する。</li><li>「回答案を再試行の入力欄へ入れる」で転記し、<b>回答・補足を入力して再開</b>で送信・再試行する。転記だけでは再開しない。</li><li>同じエラーが出る場合は、失敗した操作・対象パス・終了コード・エラー全文を確認する。同じ復旧待ちの案を繰り返さない。</li></ol>
<p>選択式の質問は各項目の選択肢と補足欄へ回答します。「引き継ぎ情報を登録・訂正する」は事実の記録欄で、質問への回答や担当の再開を行う欄ではありません。</p>
<h3>工程を分割して移管する場合</h3><p>「ボードから復旧する」で後続工程と、そこで必ず実施する範囲・測定時点・保存する証拠を指定します。現在の報告を確認して「移管を保存して再開」を押します。「移管を保存」だけなら停止状態を維持します。利用枠・認証障害は移管で解消しません。</p>
<p>回答に根拠が足りない場合、報告全文・時刻・報告元・根拠・差分・採用後の影響・未確認点を先に確認します。表示される場合は「AIに根拠を確認させる」を使えます。この調査もCLI利用枠を使い、採用は利用者が決めます。</p>
<div class="note"><b>復旧案は合格証明ではありません</b><p>統括側のPython起動成功は担当側の実行成功とは別です。報告が「工程完了」でも、その工程の証拠と後続工程の完了条件を確認します。古い診断結果で自動的に作業を完了扱いにしません。</p></div>
'''+shot('05-recovery','担当詳細：停止理由と回答・再試行'))

add('settings','07 担当・モデル・共通Agentの設定', '''
<p>ボード上部の「チーム設定」で、計画・調査・実装・レビューの担当CLI、モデル、推論設定を選び「モデル設定を保存」します。保存は今後作成するタスクに適用します。既存の停止・待機タスクはそのカードの「担当・モデルを切り替える」から変更します。</p>
<p>「モデル一覧を再取得」でCLIから取得した選択肢を更新します。表示されたモデルが必ず実行可能とは限りません。認証・利用枠・アカウント権限・実行環境は実行時にも確認します。Opus/Sonnet等の別名と、画面に併記された版を確認してください。</p>
'''+table(['役割','共通Agent','作業の性質'],[
['計画・調査','common-explorer','範囲を絞った読み取りと整理'],
['実装','common-implementer','承認範囲の変更'],
['レビュー','common-reviewer','独立した読み取り専用レビュー'],
['セキュリティ関連','common-security-reviewer','必要なセキュリティレビュー'],
['ハーネス監査','common-harness-auditor','経路・検証ゲート・再試行の監査']])+'''
<p>Skillは名称・説明などのメタ情報から必要なものを選び、本文や関連資料は必要時に読み込みます。全資料を毎回プロンプトへ貼り付ける設計を避けます。共通Agentの選択でプロジェクトの権限・完了条件が自動的に広がることはありません。</p>
'''+shot('03-settings','チーム設定と判断Provider'))

add('decision','08 判断Providerとシャドーモード', '''
<p><b>チーム設定 → 判断Provider</b>を展開します。「ローカルOllamaのみ」を選び、導入済みモデルを選択して「判断設定を保存」。次に開始する作業へ適用します。「保存済み設定で接続確認」は保存後の接続を確認します。モデルは自動ダウンロードしません。</p>
<div class="note"><b>現在の動作範囲</b><p>判断層の測定対象はAgent選択です。タスク・Skill・モデル・ループ判断の全経路へ接続された状態ではありません。シャドーモードでは候補・時間・結果を測定し、実際の採用は既存の判断を維持します。画面ではシャドーモードが固定されています。</p></div>
'''+table(['Provider','扱い'],[
['ローカルOllamaのみ','ローカル推論。CLIの開発作業そのものは引き続き利用枠を使う'],
['判断層を停止','判断層を使用せず既存の経路で運用'],
['自動','ローカル優先。クラウド候補を含むため運用方針を確認'],
['Luna API','別途従量課金のAPI。選択だけで認証・課金許可は有効にならない'],
['Decisions API','現状未対応。利用可能なAPIとして扱わない']])+'''
<p>有限候補・安全条件を先に確認し、失敗・未知・時間制限では人の判断等へ戻します。APIやモデルが選んだ候補だけでコマンド実行・削除・公開を許可しません。シャドー評価の精度と速度は継続測定が必要で、利用枠削減率は未確定です。</p>''')

add('updates','09 CLI更新・利用枠', '''
<ol><li>「チーム設定」の「Codex CLIの更新」「Claude Code CLIの更新」を展開する。</li><li>更新有無、現在版、公開版、確認時刻を読む。「今すぐ確認」で再確認する。</li><li>実行中の作業がない状態で、更新ボタンを押す。更新はボタンを押した時だけ行う。</li><li>更新後の版・モデル一覧・利用枠の再取得結果を確認する。</li></ol>
<p>通常は起動時と24時間ごとに更新を確認します。確認失敗は最新版の証明ではありません。利用枠はアカウントの枠で、会話の残りコンテキストとは異なります。残量不明・取得回数制限・認証失効・次回取得時刻を読み、0%と取り違えないでください。</p>
<p>Claudeの5時間枠枯渇は明示した理由を確認し、リセットを待つか、利用可能なCodexへ停止・待機タスクを切り替えます。週間枠と短期枠は別です。残量が回復しても失敗したタスクを自動的に完了扱いにはしません。</p>
'''+shot('04-cli','CLI更新：折りたたみを展開した状態')+shot('12-usage','モデルと利用枠の表示'))

add('notifications','10 通知・音声・スマホ接続', '''
<h3>通知</h3><p>ボード上部でPC通知・音声通知を有効にします。PC通知にはブラウザの許可が必要です。「声を試聴」で音を確認してください。音声はブラウザ／Windowsに利用可能な声を使うため、声質は環境に依存します。通知を閉じても判断待ち状態は続きます。音声が使えない場合は画面の「あなたの判断」を確認します。</p>
<h3>スマホ</h3><ol><li>PCとスマホを同じ自宅Wi-Fiに接続する。</li><li>PCボードの「スマホから接続」を開き、現在表示されている接続先へアクセスする。</li><li>初回の証明書はPCに表示された指紋を照合する。合言葉を入力して接続する。</li><li>ボードが表示されたら対象カードを開き、PCと同じ判断・回答手順で操作する。</li></ol>
<p>スマホ入口は通常HTTPSの8788番、PC入口はループバックHTTPの8787番です。接続先はPCのネットワーク状態で変わるので固定の古いURLを使い続けないでください。外部公開を想定した入口ではありません。</p>
'''+shot('13-mobile','スマホ接続パネルの位置と案内'))

add('ledger','11 プロジェクト台帳', '''
<p>ボードの「プロジェクト台帳」から開きます。名前・パス・目的を検索し、分類で絞り込みます。カードのプロジェクト名を押すと詳細が開きます。</p>
'''+table(['操作','用途','結果'],[
['フォルダを再調査','開発フォルダの存在・候補情報を再収集','管理項目の入力を保持し、調査結果を更新'],
['パスを指定して登録','個別フォルダ／新規プロジェクトを登録','台帳の登録。AIの作業開始とは別'],
['JSONを書き出す','台帳を別用途へ持ち出す','内容を確認し、機密情報の扱いを守る'],
['DBをバックアップ','台帳の状態と履歴を保全','プロジェクト本体のバックアップとは別'],
['分類を編集・保存','用途別分類を整理','台帳の分類変更。フォルダは移動しない']])+'''
<p>「調査範囲・未調査の範囲」で今回のスキャン範囲を確認します。件数は資料・候補・親子プロジェクトを含むため、独立したアプリ数と同じとは限りません。完了・運用中などの管理状況は自動判定しません。</p>
'''+shot('06-ledger','台帳一覧：検索・分類・登録・バックアップ'))

add('detail','12 詳細画面・管理項目・親子関係', '''
<p>各セクションの見出しで開閉します。<b>● 入力あり</b>はデフォルト値以外の入力、<b>○ 入力なし</b>は空欄・デフォルト値、<b>－ 自動情報</b>は調査情報です。入力マークは未保存の値も反映するため、保存済みの証明ではありません。編集後は「台帳に保存」を押します。</p>
'''+table(['セクション','記録すること'],[
['確認したメタ情報','検出された技術情報、Git情報など'],
['親・子プロジェクトの情報','台帳上の親子、所属階層、親Gitとの関係'],
['基本情報','名前、目的・利用者、分類、管理状況、管理担当'],
['ソースと起動方法','正本ソース／配備コピーの区別、依存・起動方法'],
['検証と完了条件','検証方法、受入条件、未検証事項'],
['次の作業と注意事項','優先課題、制約・危険、引き継ぐ内容'],
['ハーネス化提案','提案の下書き、読み取り調査、変換・補完']])+'''
<p>Gitの親子関係と台帳の親子関係は別の情報です。子フォルダを移しても親Gitの履歴が自動的に子へ複製されることはありません。物理削除・統合を別の方法で行った場合、再調査で存在を確認し、管理状況・注意事項へ統合先と実施内容を記録します。</p>
'''+shot('07-project','詳細：セクションと入力状態')+shot('15-metadata','基本情報の入力欄')+shot('11-nodes','親・子プロジェクト情報'))

add('move','13 プロジェクトフォルダの移動', '''
<ol><li>詳細 →「プロジェクトのフォルダを移動」を展開。</li><li>移動先の<b>絶対パス（新しいプロジェクトフォルダ）</b>を入力。親フォルダは作成済み、指定した移動先そのものは未作成にする。空フォルダでも先に作らない。</li><li>「移動の確認画面へ」を押し、移動元・先・Git影響・台帳ノードを確認。</li><li>アプリ、IDE、ビルド、同期を停止。確認チェック後、「この場所へ移動を実行」で実施。</li><li>新しいパスで起動できるか、起動スクリプト・IDE・AI作業対象を確認。</li></ol>
<p>同じボリューム内の移動に対応します。独立したGit本体は履歴・remoteごと移動します。親Gitに所属する子フォルダでは親に削除差分が残り、親Gitの履歴は子へ移りません。外部worktreeはGitの移動処理を使います。サブモジュール等は対象外のため案内を確認します。</p>
<div class="note"><b>自動変更されない参照</b><p>ソース内の絶対パス、IDE、外部スクリプト、既存AI依頼・引き継ぎ、作業許可設定は自動更新しません。台帳のパス更新とアプリの起動修正は別です。GitHub公開設定・remote変更・commit・pushは移動機能で行いません。</p></div>
<p>失敗したら同じ操作を繰り返す前に、移動履歴、元と先の存在、復旧確認待ち状態を確認します。正常移動を戻す場合は同じ機能で元の未使用パスへ戻します。</p>
'''+shot('09-move','移動：入力後に確認画面へ進む'))

add('archive','14 アーカイブ・削除・復元', '''
<h3>アーカイブ／削除</h3><ol><li>詳細 →「プロジェクトのアーカイブ」を開く。</li><li>アーカイブなら「アーカイブ対象を確認」、削除なら「プロジェクトを削除」で確認画面へ進む。</li><li>対象フォルダ、容量・件数、ZIP保存先、検出されたGitHubリポジトリ・remoteを確認。自動検出できない公開先は欄へ1行1件で入力して確認し直す。</li><li>アプリ・IDE・ビルド・同期を停止。削除とGitHub変更の確認を行い実施する。</li><li>進捗・履歴・ZIP・照合結果を確認する。画面を閉じてもPC側の処理は続く。</li></ol>
<p>GitHub接続先がある場合、非公開化を確認してremoteを解除し、接続解除後のフォルダ全体をZIPに保存・全件照合して元フォルダを削除します。GitHubリポジトリ自体は残します。アーカイブも元フォルダを削除するため同じGitHub整理を行います。GitHub CLIの認証・管理権限が必要です。</p>
<p>台帳行と履歴を保持し、ZIPにはGit履歴・未コミット・未追跡・生成物等も含めます。稼働中采来 — サイクル —、親Gitの一部、外部Git作業ツリー、サブモジュール、他の台帳プロジェクトを含む範囲、未完了依頼のある対象等は制限されます。</p>
<div class="note danger"><b>ZIPは暗号化されません</b><p>元フォルダの秘密情報はZIPにも含まれます。共有・外部送信を避け、保存先を管理してください。WindowsのACL・所有者・全属性の完全再現は保証しません。GitHub非公開化は他の閲覧者へ影響します。</p></div>
<h3>失敗時・復元</h3><p>途中で止まったらZIP、manifest、退避フォルダ、履歴を保全します。GitHub公開状態は自動で公開に戻しません。削除中断は自動再開しません。復元はZIPと履歴のSHA-256を照合し、未使用の別フォルダに展開・照合してから戻します。</p>
<pre>python -X utf8 restore_project_archive.py &lt;履歴のアーカイブID&gt; C:\\Projects\\&lt;未使用の復元先&gt;</pre>
<p>このコマンドは采来 — サイクル —のフォルダで実行します。追加ストリームは専用復元ヘルパーを使用します。元データへ重ねて上書きしないでください。GitHub接続・公開設定・ACL・依存関係・アプリ動作は別に確認し、台帳を再調査します。</p>
'''+shot('10-archive','アーカイブ・削除：まず対象確認へ'))

add('harness','15 ハーネス化：提案・変換・既存情報の補完', '''
<h3>A. 台帳から提案する</h3><p>詳細 → 管理項目 → ハーネス化提案 →「提案を生成」。台帳情報からローカルで下書きを作り、確認・編集後「台帳に保存」します。このボタンでは外部モデルや利用枠を使わず、プロジェクト内のファイルも作りません。</p>
<h3>B. 既存／新規プロジェクトを変換する</h3><ol><li>「読み取り専用調査・ファイル案を作成」を押す。</li><li>読み取り範囲・検出結果・未知の項目・作成予定・衝突・既存スキップを確認。各ファイル案を展開して読む。</li><li>「既存ファイルを整理してフォルダ構成を変換」で追加フォルダとファイル移動案を入力・確認。コード配置は推測で自動変更しない。</li><li>対象パスと案を確認してチェックし、「確認したコンバートを実行」。確認案は30分で失効する。</li><li>生成資料・移動内容・参照パス・起動・検証を確認する。生成だけで開発ループの受入完了としない。</li></ol>
<p>現在の案は19ファイル：AGENTS.md、CLAUDE.md、PROJECT_CONTEXT.md、GOAL.md、SCOPE.md、ARCHITECTURE.md、COMMAND_ALLOWLIST.md、SECURITY.md、.harness/配下のREADME・context・workflow・rules・definition-of-doneと6種の作業プロンプト。既存ファイルは上書きしません。フォルダ作成と個別ファイル移動は確認した案だけを適用します。</p>
<h3>C. 既存ハーネスを情報で補完する</h3><p>「既存情報からハーネスを補完」で前後の文面と根拠を比較して適用します。README・package.json・限定したindex.html/sw.js等を読み、出典行と候補を示します。GOAL、ARCHITECTURE、COMMAND_ALLOWLIST、SECURITY、.harness/context、.harness/definition-of-doneが対象です。補完前の原本と適用記録を保存します。</p>
<div class="note"><b>補完候補の確認が必要</b><p>READMEの記載は実行成功の証拠ではありません。起動コマンドは未承認候補、分からない目的・利用者・受入条件はUNKNOWNのままです。本文のUNKNOWN残存、根拠の重複、ファイルツリーをコマンド候補と誤認する可能性、アプリ固有の受入条件不足など、生成品質の点検が必要です。自動で全項目を正しく埋める保証はありません。</p></div>
<p>読み取り・補完はローカルの限定処理です。モデル/APIの呼び出し、依存インストール、プロジェクトのコマンド実行、権限の拡張は行いません。機密データ領域やリンクを除外しますが、包括的な秘密情報スキャナーではありません。</p>
'''+shot('08-harness','ハーネス化提案の入口')+shot('14-convert','変換プレビュー：フォルダ作成とファイル移動を指定'))

add('operations','16 起動・停止・バックアップ', '''
<p>起動前にポートとアプリ識別を確認してください。</p>
<pre>python -X utf8 C:\\Projects\\Sairai\\manage.py status
python -X utf8 C:\\Projects\\Sairai\\manage.py start
python -X utf8 C:\\Projects\\Sairai\\manage.py stop
python -X utf8 C:\\Projects\\Sairai\\manage.py login-claude
python -X utf8 C:\\Projects\\Sairai\\manage.py background-status</pre>
<p>statusは /health のアプリ識別を確認します。startはバックグラウンド起動を使います。Windowsの実行ポリシーを全体変更する必要はありません。停止時は実行中の作業が中断されることを確認します。既存のバックグラウンドタスクがある場合、任意の別ポート指定がそのタスクへ反映されるとは限りません。</p>
<h3>バックアップ</h3><p>作業ボードの「ローカルDBをバックアップ」と台帳の「DBをバックアップ」はそれぞれ対象を確認して使います。DBの保存だけではプロジェクトソース・共通定義・設定・ZIP・証拠がすべて保全されるわけではありません。復旧前に現状を別に保存し、コードを戻す操作とDBを戻す操作を分けます。</p>
<p>過去DBの復元はそれ以後の履歴も戻すため、通常は履歴DBを保持します。機能を戻しても削除済みフォルダが自動復元されることはありません。保存・復元後は起動、記録、対象プロジェクト、残った検証ゲートを確認します。</p>''')

add('troubleshooting','17 困ったときの確認表', table(['症状・理由','確認','次の操作'],[
['ページが開かない／別アプリが出る','采来 — サイクル — /health、ポート使用者、統括ログ','対象サービスを識別。別アプリを勝手に停止しない'],
['待機が進まない','新規着手一時停止、先行工程、質問・承認','全体再開と個別回答を分けて操作'],
['Claudeの5時間枠を使い切った','短期リセット時刻と週間枠','回復後に再試行、または利用可能な担当へ切り替え'],
['OAuth失効／未ログイン','采来 — サイクル —が使う実際のCLIの認証','login-claudeでCLIログインを完了し再確認'],
['利用枠が不明','取得制限・次回取得・認証','待つ／認証復旧。0%と決めつけない'],
['Python起動失敗','実行ファイル、作業ディレクトリ、終了コード','原因確認案を作り、担当側でも再確認'],
['Windowsサンドボックス／権限エラー','失敗した操作・対象・実環境','セキュリティ設定を安易に解除せず具体的な障害を調査'],
['Computer Use／画面取得で停止','基本禁止方針、要求された確認の目的','プログラムで可能な検証へ分割。実画面確認済みと偽らない'],
['引き継ぎ文脈が大きすぎる','文脈上限・分割索引・各断片','履歴を保ち分割読込。未読・未知を切り捨てない'],
['引き継ぎ情報が食い違う','全文・時刻・報告元・根拠・影響','根拠確認を依頼し、採用情報を明示。旧報告は履歴に残す'],
['スマホでForbidden','現在の接続先・合言葉・Cookie・送信元','PC案内から入り直す。認証制約を無効化しない'],
['移動ボタンがエラー','元の存在、親フォルダ、未使用の移動先、Git、ロック','実行前の確認画面を完了。使用アプリを停止'],
['アーカイブ／削除できない','親Git・worktree・子台帳・未完了依頼・権限','制限理由を解消。確認画面と履歴を読み再実行'],
['ハーネスのUNKNOWNが多い','台帳の目的・起動・検証、資料の根拠','入力・補完案を確認。実際の受入条件は人が設定']]))

add('limits','18 安全境界・現状の制限', '''
<ul><li>ローカルの台帳整理・提案生成・変換補完と、AIの開発実行は別です。後者は選んだCLIの利用枠を消費します。</li><li>Ollama判断はローカルですが、Codex/Claudeの実装・レビューまでローカルへ置き換わるわけではありません。</li><li>同じWindowsユーザーで動くため、設定上の作業対象制限をOS全体の隔離と同一視しません。</li><li>削除・公開状態変更・外部送信などは対象と影響を確認します。モデルの推奨だけで承認したことにはしません。</li><li>レビュー・構文確認・ビルド・インストール・実機・人の操作を別の確認段階として報告します。</li><li>判断層はAgent選択のシャドー測定です。全経路の自動選択、Decisions API実利用、Luna実通信、課金削減率は完成・実証済みと扱いません。</li><li>ハーネス資料の生成・補完は下書きです。フォルダ移動後の参照、UNKNOWN、候補コマンド、受入条件を確認します。</li></ul>
<p>記録にはパスや作業内容が含まれます。資料・DB・ZIPを他人へ渡す前に確認し、合言葉、Cookie、APIキー、家族の情報などを公開しないでください。</p>''')

add('sources','19 保守者向けの参照資料', '''
<p>この資料の根拠はローカルの現在のコード、実画面、機能別資料です。過去の検証記録は、その時点の結果として扱います。画面変更時はスクリーンショットと操作名も更新してください。</p>
'''+table(['対象','参照（agent-team/からの相対パス）'],[
['目的・範囲','AGENTS.md、SCOPE.md、PROJECT_CONTEXT.md'],
['運用','docs/OPERATIONS.md、manage.py'],
['共通Agent・Skill','docs/SHARED_HARNESS_20260930.md、team_common_agents.py、team_shared_harness.py'],
['判断層','docs/DECISION_LAYER.md、docs/OLLAMA_DECISION_LAYER.md、docs/DECISION_VERIFICATION_20260930.md'],
['ボード復旧・引き継ぎ','docs/BOARD_RECOVERY.md、docs/HANDOFF_DB.md、docs/DECISION_HELP.md'],
['通知・利用枠・モデル','docs/VOICE_NOTIFICATIONS.md、docs/USAGE_DISPLAY.md、docs/MODEL_SELECTOR.md'],
['台帳・移動・削除','docs/PROJECT_LEDGER.md、docs/PROJECT_MOVE.md、docs/PROJECT_ARCHIVE.md、docs/PROJECT_DELETE.md'],
['補完','docs/HARNESS_EVIDENCE_FILL.md、team_harness.py、team_harness_fill.py'],
['画面','web/index.html、web/app.js、web/ledger.html、web/ledger.js'],
['資料の再作成','docs/manual/build_manual.py、capture_fixture.py、screenshots.json']])+'''
<details><summary>用語</summary><dl><dt>ハーネス</dt><dd>目的・範囲・実行手順・検証・記録・復旧を組み合わせ、開発を継続できるようにする仕組み。</dd><dt>共通Agent</dt><dd>役割・許可・出力形式を定めた担当定義。モデルそのものではない。</dd><dt>Skill</dt><dd>必要な作業で読み込む手順・知識・参照ファイル。</dd><dt>Provider</dt><dd>判断を供給する接続先。開発担当のCLIモデルとは別。</dd><dt>シャドー</dt><dd>候補を測定するが実採用は既存経路を維持する運用。</dd><dt>正本</dt><dd>矛盾が起きたときに確認する、正式な資料・ソース・記録。</dd></dl></details>''')

from manual_revision import revise
sections=revise(sections,table,shot)
css='''
:root{--ink:#153637;--muted:#496668;--line:#cbd9d5;--accent:#146858;--paper:#fff;--bg:#eff4f1}*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:20px}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.85 "Yu Gothic UI","Meiryo",system-ui,sans-serif}a{color:#126253}a:focus-visible,button:focus-visible,input:focus-visible,summary:focus-visible{outline:3px solid #d98626;outline-offset:3px}header{background:#153d37;color:#fff;padding:64px max(28px,calc((100vw - 1280px)/2));}header h1{font-size:42px;line-height:1.35;margin:10px 0}header p{max-width:820px;color:#dce9e3}header .tag{letter-spacing:.15em;color:#b7dbce;font-size:13px}header .meta{display:flex;gap:12px;flex-wrap:wrap;font-size:13px}.meta span{border:1px solid #638b7f;padding:4px 12px;border-radius:20px}.layout{max-width:1340px;margin:0 auto;display:grid;grid-template-columns:260px 1fr;gap:36px;padding:36px 24px}aside{position:sticky;top:16px;align-self:start;max-height:95vh;overflow:auto;font-size:14px}aside h2{font-size:16px}aside a{display:block;text-decoration:none;padding:7px 10px;border-left:2px solid transparent}aside a:hover{background:#dce9e2;border-color:var(--accent)}aside input{width:100%;padding:12px;border:1px solid var(--line);border-radius:8px;font:inherit}button{font:inherit;cursor:pointer;background:var(--accent);color:#fff;border:0;border-radius:8px;padding:8px 16px}article{min-width:0}section{background:white;border:1px solid var(--line);border-radius:12px;padding:32px;margin-bottom:28px;scroll-margin-top:20px}h2{font-size:26px;line-height:1.45;border-bottom:2px solid #dbe8e1;padding-bottom:14px;margin:0 0 22px}h3{font-size:20px;margin:26px 0 12px}p{margin:12px 0}li{margin:10px 0}.lead{font-size:19px}.cards,.branches{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.cards>div,.branches>div{background:#edf4ef;border:1px solid #cbded3;padding:18px;border-radius:8px}.note{background:#edf5f0;border-left:5px solid #22755b;padding:16px 20px;margin:22px 0}.note.danger{background:#fff4e5;border-color:#b76b1c}.note p{margin:8px 0 0}.tablewrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px;margin:18px 0}th{text-align:left;background:#e6eee8}td,th{border:1px solid var(--line);padding:12px;vertical-align:top}td:first-child{font-weight:600;min-width:125px}code{overflow-wrap:anywhere;background:#edf2ef;padding:2px 5px;border-radius:3px;font-size:14px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#163b34;color:#f1f6f3;padding:22px;border-radius:8px;font:14px/1.8 Consolas,monospace}figure{margin:26px 0}figure img{width:100%;height:auto;border:1px solid #b8cbc2;border-radius:8px;display:block}figcaption{font-size:13px;color:var(--muted);padding:10px 0}.layer{background:#164c40;color:white;padding:20px;border-radius:8px;display:flex;flex-direction:column;align-items:center;text-align:center}.layer span{font-size:14px}.arrow{text-align:center;margin:10px;font-size:14px;color:var(--muted)}.flow{display:flex;flex-wrap:wrap;align-items:center;gap:9px;margin:24px 0}.flow span{background:#e4eee7;padding:10px;border-radius:7px}details{margin:16px 0;border:1px solid var(--line);padding:14px;border-radius:8px}summary{cursor:pointer;font-weight:600}dt{font-weight:bold;margin-top:15px}dd{margin-left:0}footer{text-align:center;padding:30px;color:var(--muted);font-size:13px}[hidden]{display:none!important}.controls{display:flex;gap:10px;margin-top:15px}#empty{padding:20px} @media(max-width:900px){header{padding:36px 24px}header h1{font-size:32px}.layout{display:block;padding:20px 12px}aside{position:static;max-height:none;margin-bottom:24px;background:#fff;padding:20px;border-radius:10px}aside nav{columns:2}section{padding:22px}.cards,.branches{grid-template-columns:1fr}}@media print{body{background:white;font-size:11pt}header{padding:20px;background:white;color:#153637}header p,header .tag{color:#153637}.meta span{border-color:#aaa}.layout{display:block;padding:0}aside,.controls{display:none}section{border:0;border-radius:0;padding:10px 0;break-before:page}figure,table,.note{break-inside:avoid}figure img{max-height:180mm;object-fit:contain}h2{font-size:20pt}a{color:inherit;text-decoration:none}details{border:0}footer{padding:10px}}@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
'''
toc='<h3>利用者向け</h3>'+''.join(f'<a href="#{k}">{html.escape(t)}</a>' for k,t,b in sections if not t.startswith('付録'))+'<h3>保守者向け付録</h3>'+''.join(f'<a href="#{k}">{html.escape(t)}</a>' for k,t,b in sections if t.startswith('付録'))
content=''.join(f'<section id="{k}"><h2>{t}</h2>{b}</section>' for k,t,b in sections)
script='''const input=document.querySelector('#find');input.addEventListener('input',()=>{const q=input.value.trim().toLowerCase();let count=0;document.querySelectorAll('article section').forEach(s=>{const visible=!q||s.textContent.toLowerCase().includes(q);s.hidden=!visible;if(visible)count++;document.querySelector('nav a[href="#'+s.id+'"]').hidden=!visible;});document.querySelector('#empty').hidden=count!==0;});window.addEventListener('beforeprint',()=>{document.querySelectorAll('section,nav a').forEach(e=>e.hidden=false);document.querySelectorAll('details').forEach(e=>{e.dataset.wasOpen=e.open?'1':'0';e.open=true;});});window.addEventListener('afterprint',()=>{document.querySelectorAll('details').forEach(e=>e.open=e.dataset.wasOpen==='1');input.dispatchEvent(new Event('input'));});'''
page=f'''<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="采来 — サイクル —の構成、操作、復旧を実画面で説明するローカルHTMLマニュアル"><title>采来 — サイクル —｜構成・操作マニュアル</title><style>{css}</style></head><body><header><div class="tag">LOCAL DEVELOPMENT OPERATIONS</div><h1>采来 — サイクル —<br>構成・操作マニュアル</h1><p>AIに采配を。開発に連続性を。<br>作業ボードと台帳を、実画面から理解するための手引き。</p><div class="meta"><span>2026年10月2日版</span><span>利用者ガイド＋保守者向け付録</span><span>オフライン閲覧対応</span></div></header><div class="layout"><aside aria-label="目次"><h2>目的の操作を探す</h2><label for="find">本文を検索</label><input id="find" type="search" placeholder="例：再開、移動、Ollama"><nav>{toc}</nav></aside><article>{content}<p id="empty" hidden>該当する章はありません。検索語を短くしてください。</p></article></div><footer>采来 — サイクル — • ローカル実装に基づく資料 • 外部フォント・CDN・解析・API呼び出しなし<br>画像を含む assets フォルダと一緒に保管してください。</footer><script>{script}</script></body></html>'''
(ROOT/'index.html').write_text(page,encoding='utf-8')
standalone=page.replace('画像を含む assets フォルダと一緒に保管してください。','スクリーンショットをこのHTMLへ埋め込んでいます。単一ファイルで保管できます。')
for p in (ROOT/'assets').glob('*.jpg'):
    uri='data:image/jpeg;base64,'+base64.b64encode(p.read_bytes()).decode('ascii')
    standalone=standalone.replace('assets/'+p.name,uri)
(ROOT/'agent-team-manual.html').write_text(standalone,encoding='utf-8')
labels={'01-board':'作業ボード','02-request':'依頼追加','03-settings':'チーム設定・判断Provider','04-cli':'CLI更新','05-recovery':'担当詳細・再開','06-ledger':'台帳一覧','07-project':'台帳詳細','08-harness':'ハーネス提案','09-move':'フォルダ移動','10-archive':'アーカイブ・削除','11-nodes':'親子情報','12-usage':'利用枠','13-mobile':'スマホ接続','14-convert':'変換プレビュー','15-metadata':'基本情報'}
manifest={'as_of':'2026-10-01','environment':'real 采来 — サイクル — UI; isolated synthetic fixtures; no task execution','screenshots':[{'file':'assets/'+p.name,'screen':labels.get(p.stem,p.stem),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted((ROOT/'assets').glob('*.jpg'))]}
(ROOT/'screenshots.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
print(f'Created {ROOT / "index.html"}: {len(sections)} chapters; {len(manifest["screenshots"])} screenshots')
