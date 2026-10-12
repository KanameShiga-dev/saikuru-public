"""Reader-oriented structure for the public manual; application behavior unchanged."""
import re

def revise(sections, table, shot):
    old={k:b for k,t,b in sections}
    def clean(s):
        s=s.replace('家族の記録','個人情報・認証情報・機密情報').replace('家族の情報','個人情報・機密情報')
        s=s.replace('C:\\Projects\\SharedHarness','&lt;SHARED_HARNESS&gt;').replace('C:\\Projects\\Sairai','&lt;SAIRAI_HOME&gt;').replace('C:\\Projects\\_project_archives','&lt;ARCHIVE_ROOT&gt;').replace('C:\\Projects\\&lt;未使用の復元先&gt;','&lt;RESTORE_DESTINATION&gt;')
        for a,b in [('第6・7章','第4・5章'),('第8章','付録D'),('第6章','第4章'),('第7章','第5章')]:s=s.replace(a,b)
        return s
    old={k:clean(b) for k,b in old.items()}
    glossary=table(['用語','説明'],[
        ['作業ボード','依頼と担当の状態を見て、回答・承認・再試行を行う画面'],
        ['プロジェクト台帳 / Ledger','目的、場所、起動・検証方法、受入条件などを継続管理する情報基盤'],
        ['Harness','AIへ作業範囲・制約・参照資料・完了条件を与える制御枠組み'],
        ['Agent','AIに与える役割・担当。モデルそのものとは別'],
        ['Skill','必要な時に読み込む専門知識・手順・ルール'],
        ['Handoff','別Agentや別セッションへ、実施内容・判断・証拠・残作業を渡す記録'],
        ['State','現在の工程、担当、停止理由、承認待ちなどの作業状態'],
        ['Evidence','実行結果・検証結果・根拠となる記録。AIの報告とは区別する'],
        ['Shadow Mode','候補を実際の処理へ採用せず、記録・比較する評価方式'],
        ['Agent選択層 / Routing Provider','担当Agentの候補を評価する接続先。「あなたの判断」は人の承認・選択を指す']])
    start='''<h3>3分で分かる采来</h3>
<p class="lead">采来はAIモデルそのものではありません。<br>Codex・Claude CodeなどのAI開発ツールを統括し、作業状態・引き継ぎ・検証結果・判断を管理するローカル開発運用基盤です。<br>AIが変わっても、作業を継続できる状態を残すことを目的とします。</p>
<div class="flow" aria-label="基本的な作業の流れ"><span>利用者が依頼</span>→<span>采来が管理・割り振り</span>→<span>調査 → 実装 → 検証 → レビュー</span>→<span>必要な判断を人へ戻す</span>→<span>履歴・証拠・残作業を保存</span>→<span>別モデルでも続行</span></div>
<div class="note"><b>目的から読む</b><p><a href="#quickstart">依頼する：第2章</a> ／ <a href="#recovery">停止から再開：第4章</a> ／ <a href="#settings">モデルを切り替える：第5章</a> ／ <a href="#ledger">プロジェクトを管理：第6章</a> ／ <a href="#architecture">構成を知る：付録A</a></p></div>
<h3>用語集</h3>'''+glossary+'''<h3 id="safety">采来の安全原則</h3><ol>
<li id="safety-1">AIの報告だけで完了扱いしない。</li><li id="safety-2">実行結果・検証結果・証拠を確認する。</li><li id="safety-3">未確認事項はUNKNOWNとして残す。</li><li id="safety-4">削除・公開・外部送信・権限変更は人間が確認する。</li><li id="safety-5">Harnessの作業制約とOSレベルの隔離は別物。</li><li id="safety-6">個人情報・APIキー・Cookie・アクセストークン・顧客情報・社内機密を不用意にAIへ渡さない。</li></ol>
<p>継続性の中心は特定のモデルではなく、Harness・State・Evidence・Ledgerです。人は記録と根拠を確認して次の作業を決めます。</p>'''
    states=table(['機能','分類','現在の状態'],[
        ['担当切替・引き継ぎ履歴','IMPLEMENTED','実装済み。内容と現在のソースの照合は人／Agentの運用'],
        ['共通資料の必要時読み込み','IMPLEMENTED','実装済み'],
        ['再試行時の完全一致重複指示整理','IMPLEMENTED','実装済み。意味の異なる指示は勝手に削除しない'],
        ['プロジェクト台帳','IMPLEMENTED','実装済み。登録だけで開発は開始しない'],
        ['Git差分の利用','OPERATION','推奨運用'],
        ['全タスクのGit差分自動収集・最適化','UNKNOWN','確認できていない'],
        ['OllamaのAgent選択候補評価','IMPLEMENTED','シャドー測定のみ'],
        ['Ollama候補の実処理への採用','NOT TESTED','未実施。UIではシャドー固定で実採用を有効化できない'],
        ['Ollama／Git運用の削減効果','UNMEASURED','未測定'],
        ['Harness生成・補完','IMPLEMENTED','下書き生成・補完。人による確認が必要'],
        ['UNKNOWN・受入条件・最終完了判断','OPERATION','人が確認する'],
        ['スマホ・音声・移動・削除・CLI更新等の今回の検証範囲','NOT TESTED','付録Fで確認する']])
    overview='''<h3>采来とは何か</h3><p>開発依頼を工程と担当へ分け、状態・証拠・引き継ぎをブラウザで管理するツールです。</p>
<h3>何が楽になるか</h3><ul><li>モデル変更や新しいセッションのたびに、実施内容と残作業を説明し直す負担を減らせます。</li><li>停止理由・承認待ち・次の操作をボードで確認できます。</li><li>プロジェクトの情報と検証条件を台帳に集め、開発と整理を続けやすくします。</li></ul>
<h3>できること</h3><ul><li>作業依頼・担当状態・Codex／Claudeへの割り振り</li><li>担当切替・引き継ぎ履歴、作業履歴・検証結果・未確認事項の記録</li><li>プロジェクト台帳、Harness生成・補完、共通Agent／Skillの利用</li><li>停止・質問・承認・再試行、Ollamaの判断候補のシャドー評価</li><li>プロジェクト移動・アーカイブ管理</li></ul>
<h3>できないこと</h3><ul><li>前モデルの内部記憶・会話全体をそのまま移すこと</li><li>台帳登録だけで開発を始めること</li><li>AIのdoneを実機確認済みとして保証すること</li><li>HarnessだけでOSレベルの隔離を行うこと</li><li>Ollamaを開発実行Agentの代替として使うこと</li><li>未測定のトークン削減効果を保証すること</li><li>Computer Useへ自由なGUI操作を許可すること</li></ul>
<p><a href="#quickstart">最初の依頼は第2章へ。</a> 個別の画面操作から読み始められます。</p>
<h3 id="implementation-status">現在の実装状態</h3>'''+states+'''<p>分類の意味とシステム機能／運用の境界は<a href="#limits">第13章</a>、検証範囲は<a href="#verification">付録F</a>を参照してください。</p>'''
    quick=old['quickstart'].replace('計画と結果を確認。必要な承認・質問に回答し、検証の根拠とレビュー結果を読んで完了を判断します。','送信後は作業ボードへ。次に見る場所は下記の案内を参照します。')
    quick+='''<h3>送信後に起きること</h3><ol><li>「計画を開始する」後は、ボードのカードと「最近の動き」で受付と担当状態を確認します。</li><li>開始前は「待機している作業」、担当実行中は「チームが作業中」へ表示されます。</li><li>計画承認や質問などが必要なら「あなたの判断」で対象カードを開きます。<a href="#recovery">第4章</a>へ進んでください。</li><li>担当終了後は「終了した作業」の報告、検証根拠、未検証事項を確認します。依頼全体の最終判断は利用者が行います。</li></ol>
<h3>停止は異常とは限りません</h3><p>人の承認・回答待ちは予定された停止です。利用枠不足・権限不足・外部環境障害・検証不能も、原因を明示して停止する場合があります。状態だけで失敗と決めず、理由と必要な操作を確認します。</p>
<p>AIからdoneと報告されても、検証根拠と未検証事項を確認し、最終的な完了判断は利用者が行います。<a href="#safety-1">安全原則1・2</a>を参照してください。</p>'''
    board=old['board']+shot('01-board','作業ボードの状態と操作')+'''<div class="note"><b>この画面では列・担当状態・停止理由を確認します。</b><p>「新規着手を再開」は全体の新規着手停止だけを解除します。個別カードの質問・失敗・承認待ちは解除しません。</p><p>「全作業を中止」は作業を停止する操作です。「再開」と取り違えないでください。</p><p>終了履歴は自動削除されません。担当終了と依頼全体の完了は別です。doneは利用者の最終確認を代替しません。</p></div>'''
    board=re.sub(r'<p><b>「新規着手を再開」</b>.*?</p>','',board,count=1)
    board=re.sub(r'<p>終了履歴は自動削除しません。.*?</p>','<p>終了履歴は日付と午前／午後のグループを開いて確認します。</p>',board,count=1)
    settings=old['settings']
    roles=re.search(r'<div class="tablewrap">.*?</div>',settings,re.S).group(0)
    settings=re.sub(r'<div class="tablewrap">.*?</div>','<p>共通Agentの役割は<a href="#agents">第8章</a>で確認できます。</p>',settings,count=1,flags=re.S)
    settings+='''<h3>モデルを切り替えて続ける</h3><ol><li>停止・待機中のカードを開き、現在の担当・実施内容・検証結果・停止理由を確認。</li><li>「担当・モデルを切り替える」で利用可能な担当とモデルを選ぶ。</li><li>Handoffに基準、変更ファイル、利用者の判断、証拠、未確認・残作業を残す。</li><li>必要な回答・承認を行い、現在のソースと記録を照合して再開する。</li></ol><p>前モデルの内部記憶の移植ではなく、State・Evidence・Handoffを読むことで継続します。実施済み処理を再実行する前に確認してください。</p>'''
    agents='''<p>Agentは役割、Skillは必要な知識・手順です。担当CLIのモデルと区別して設定・確認します。</p>'''+roles+'''<p>共通資料はメタ情報から候補を選び、必要な本文・関連資料だけ読み込みます。共通Agent名とセッションIDは担当詳細で確認します。プロジェクト固有の目的・範囲・起動方法・完了条件はプロジェクトに残します。</p><p>共通Agent定義を利用しても権限が広がるわけではありません。<a href="#safety-5">安全原則5</a>を参照してください。</p>'''
    git=old['overview'].split('<h3 id="git-tokens">',1)[1].split("<p>利用者は",1)[0]
    git='<h3 id="git-tokens">'+git
    git=re.sub(r'<h3 id="git-tokens">.*?</h3>','<p class="lead">変更箇所と、判断に必要な最小限の文脈へ確認対象を絞ります。</p>',git,count=1)
    git=re.sub(r'<div class="note"><b>実装済み・運用方法・効果を区別</b>.*?</div>','<p>現在の対応状況は<a href="#implementation-status">実装状態表</a>を参照してください。</p>',git,flags=re.S)
    git+='''<h3>推奨する確認順</h3><ol><li>基準コミットを記録</li><li>未コミット・未追跡を確認</li><li>変更ファイルを確認</li><li>関連差分を確認</li><li>依存先・共通処理・設定などを必要な範囲だけ追加確認</li><li>結果・根拠・残作業をHandoffへ記録</li></ol><p>これはシステムによる自動トークン最適化機能ではなく、人／Agentが守る運用です。未追跡・バイナリ・生成物、差分に出ない受入条件も別に確認します。</p>
<h3>効果の測定方法</h3><p>同じ作業・モデル・検証条件で比較します。文字数をトークン数と同一視しません。</p><ul><li>入力トークン、キャッシュ利用量、出力トークン</li><li>再試行回数、完了までのAgent呼び出し回数</li><li>作業結果の品質、人間による修正量</li></ul><p>「文字数が何％減ったのでトークンも同じ割合で減った」とは報告しません。削減効果は<a href="#implementation-status">実装状態表</a>のUNMEASUREDを参照してください。</p>'''
    harness=old['harness']
    harness=re.sub(r'<div class="note"><b>補完候補の確認が必要</b>.*?</div>', '<p>候補の既知の制限は<a href="#limits">第13章</a>を確認します。UNKNOWNや受入条件は<a href="#safety-3">安全原則3</a>に従って確認してください。</p>',harness,flags=re.S)
    archive='''<div class="note danger"><b>⚠ DANGER ZONE — 元フォルダを削除する操作</b><p>采来のアーカイブはバックアップと照合を完了した後、元プロジェクトフォルダを削除します。元データをそのまま残す保管操作ではありません。</p><ul><li>ZIPは暗号化されません。秘密情報を含む場合は保存先を管理します。</li><li>バックアップの照合前に削除しません。</li><li>削除・GitHub公開状態変更の最終判断は人が行います。<a href="#safety-4">安全原則4</a>を参照。</li></ul></div>'''+old['archive']
    archive=re.sub(r'<p>GitHub接続先がある場合、.*?</p>','''<h3>実行中の処理順と確認ポイント</h3><ol><li>対象プロジェクト・元フォルダを確認。</li><li>未コミット・未追跡の変更を確認して保存対象を把握。</li><li>GitHub公開先と公開状態を確認。</li><li>必要に応じて非公開化し、状態を再確認。</li><li>確認したGit remoteを解除。</li><li>接続解除後のフォルダ全体をZIPへ保存。</li><li>ZIPの内容・件数・必要ファイル・SHA-256と元データを照合。</li><li>進行状態を履歴へ記録し、元フォルダを退避・再照合して削除。</li><li>削除完了後に台帳を「アーカイブ済み／削除済み」へ更新。</li><li>アーカイブ完了・ZIP・履歴を確認。</li></ol><p>GitHubリポジトリ自体は残します。GitHub認証・管理権限が必要です。途中で失敗したら、最後に完了したステップと未完了のステップを履歴で確認し、同じ削除を繰り返さないでください。</p>''',archive,count=1,flags=re.S)
    archive=re.sub(r'<div class="note danger"><b>ZIPは暗号化されません</b>.*?</div>','<p>WindowsのACL・所有者・全属性の完全再現は保証しません。復元時に確認します。</p>',archive,flags=re.S)
    matrix=table(['項目','システム機能','人／Agentによる運用'],[
        ['状態・タスク履歴管理','○','－'],['共通資料の必要時読込','○','対象選択・根拠確認'],['Git差分利用','△','○'],['Handoff内容確認','△','○'],['最終完了判断','－','○'],['UNKNOWN確認','－','○'],['Ollama候補評価','○','測定結果の確認'],['Ollama候補の実採用','現状なし','人の判断のみ。UIで候補採用を有効化できない'],['破壊的操作の最終判断','－','○']])
    limits='''<p>機能ごとの現在の状態は<a href="#implementation-status">実装状態表</a>を参照してください。</p><h3>システム機能と運用の境界</h3><p>○：担当する機能／運用あり。△：部分的な支援で運用が必要。－：その側だけでは実施・保証しない。Ollamaの実採用は現状なしです。</p>'''+matrix+'''<h3>分類の凡例</h3>'''+table(['分類','意味'],[
        ['IMPLEMENTED','実装されている機能。全環境での動作保証とは別'],['OPERATION','人／Agentが守る運用'],['MEASURED','実測した結果。条件・根拠とともに記録'],['UNMEASURED','効果量を測定していない'],['UNKNOWN','確認できていない事項'],['NOT TESTED','今回の確認範囲では実行していない']])+'''<h3>既知の制限</h3><ul><li>Harnessの補完では本文のUNKNOWN残存、根拠の重複、ファイルツリーのコマンド誤認、アプリ固有の受入条件不足を確認する必要があります。</li><li>候補コマンドや資料の記載は実行成功の証拠ではありません。</li><li>Agent選択層はシャドー評価です。タスク・Skill・モデル・ループ全経路の自動選択は完成済みと扱いません。</li><li>Decisions APIは未対応。Luna実通信と課金削減の実証は別の検証が必要です。</li></ul><p>安全境界は<a href="#safety">安全原則</a>、今回の確認範囲は<a href="#verification">付録F</a>へ集約しています。</p>'''
    decision=old['decision'].replace('判断層','Agent選択層').replace('判断Provider','Routing Provider')
    decision=decision.replace('<p><b>チーム設定 → Routing Provider</b>', '<p>本文ではAgent選択層／Routing Providerと呼びます。現在のUIでは「判断Provider」と表示されます。</p><p><b>チーム設定 → 判断Provider</b>')
    decision=re.sub(r'<div class="note"><b>現在の動作範囲</b>.*?</div>','<p>現在の対応状況は<a href="#implementation-status">実装状態表</a>を参照してください。Shadow Modeは候補と時間を記録・比較し、実際の担当選択は既存経路を維持する方式です。</p>',decision,flags=re.S)
    decision=decision.replace('シャドー評価の精度と速度は継続測定が必要で、利用枠削減率は未確定です。','評価状態は実装状態表を参照してください。')
    notifications=old['notifications']
    notifications=re.sub(r'<p>スマホ入口は通常HTTPSの.*?</p>', '<p>接続先はローカル設定・ネットワーク状態によって異なります。画面に表示される現在の接続先を使用してください。公開ネットワークからのアクセスを前提としません。</p>',notifications,count=1)
    notifications=notifications.replace('初回の証明書はPCに表示された指紋を照合する。合言葉を入力して接続する。','接続画面の案内に従い、アクセスを認証する。')
    operations=old['operations'].replace('&lt;SAIRAI_HOME&gt;\\manage.py','&lt;SAIRAI_HOME&gt;/manage.py')
    operations+='''<h3>ローカル設定を確認する</h3><p>&lt;SAIRAI_HOME&gt;は采来の設置場所、&lt;PROJECT_ROOT&gt;は対象プロジェクト、&lt;PORT&gt;は利用ポートです。実際の値はローカル設定・管理者向けREADMEで管理し、コマンド例のプレースホルダーを置き換えて実行してください。公開マニュアルの表記は実値ではありません。</p>'''
    architecture=old['architecture'].replace('判断層','Agent選択層').replace('判断Provider','Routing Provider')
    responsibilities=re.search(r'<div class="tablewrap">.*?</div>',architecture,re.S).group(0)
    architecture=re.sub(r'<div class="tablewrap">.*?</div>','<p>ファイルごとの責務は<a href="#storage">付録B</a>を参照してください。</p>',architecture,count=1,flags=re.S)
    storage=old['storage']
    db=re.search(r'<div class="tablewrap">.*?</div>',storage,re.S).group(0)
    storage=re.sub(r'<div class="tablewrap">.*?</div>','<p>保存される情報は<a href="#database">付録C</a>へ。</p>',storage,count=1,flags=re.S)
    storage+='<h3>ファイル責務</h3>'+responsibilities
    source=old['sources'];source=re.sub(r'<details><summary>用語</summary>.*?</details>','',source,flags=re.S)
    verify='''<p>操作説明と、この改訂で確認した範囲を区別するための記録です。利用者向け本文には制作環境の事情を記載しません。</p>'''+table(['対象','確認範囲'],[
        ['画面・資料','実UIのスクリーンショット、HTML表示・検索・構成図。説明用データを使用'],
        ['本改訂','文章・情報設計の変更。機能追加は行わない'],
        ['スマホ実機接続','NOT TESTED：この資料作成では未実施'],
        ['音声実聴','NOT TESTED：未実施'],
        ['削除・実フォルダ移動','NOT TESTED：資料作成のための実行は行わない'],
        ['ツールによるGitHub設定変更','NOT TESTED：プロジェクト削除機能の実行確認は行わない。マニュアル公開作業とは別'],
        ['CLI更新・開発依頼の実行','NOT TESTED：資料作成では未実施'],
        ['トークン削減量','UNMEASURED：比較評価の結果なし']])+'''<p>過去の実行記録を今回の検証結果として再利用しません。実運用の受入確認は対象の環境・作業でEvidenceを収集します。</p>'''
    history='''<p>2026-10-01：利用者向けの操作を前半、内部構成・保守情報を付録へ再配置。用語集・安全原則・実装状態表・機能マトリクスを追加。Git運用の表現、アーカイブのDanger Zoneと実処理順を整理。環境パス・ポートを一般化しました。</p><h3>今後の更新方針</h3><p>事実・運用・推測・未確認を分けます。各説明はIMPLEMENTED / OPERATION / MEASURED / UNMEASURED / UNKNOWN / NOT TESTEDとして扱い、根拠と対象版を示します。画面変更時は操作名・画像・次の操作を合わせて更新します。</p>'''+source
    result=[('start','0 まずここ',start),('overview','1 采来とは',overview),('quickstart','2 最初の依頼',quick),('board','3 作業ボード',board),('recovery','4 判断・承認・再開',old['recovery']),('settings','5 Agent・モデル切替・Handoff',settings),('ledger','6 プロジェクト台帳',old['ledger']+old['detail']),('harness','7 Harness生成・補完',harness),('agents','8 共通Agent・Skill',agents),('git','9 Git差分を利用したコンテキスト削減を狙う運用',git),('move','10 プロジェクト整理',old['move']),('archive','11 アーカイブ・削除・復元',archive),('troubleshooting','12 トラブル対応',old['troubleshooting']),('limits','13 実装状態・既知の制限',limits),('architecture','付録A 全体アーキテクチャ',architecture),('storage','付録B フォルダ・ファイル責務',storage),('database','付録C DB構造',db+'<p>記録の保存とプロジェクト本体の保全は別です。バックアップ・復旧は付録Eを参照してください。</p>'),('decision','付録D Provider・Agent選択層',decision),('operations','付録E CLI・保守・接続手順',old['updates']+notifications+operations),('verification','付録F 検証・撮影条件',verify),('sources','付録G 改訂履歴・参照資料',history)]
    # One sentence below each image: tell the reader where to look.
    captions={'01-board':'列・担当状態・停止理由と、再開／中止の違いを確認します。','02-request':'対象・完了条件・計画後の進め方を確認します。','03-settings':'役割ごとの担当・モデルとProvider設定を確認します。','04-cli':'現在版・更新有無・確認時刻を確認します。','05-recovery':'現在の担当・停止理由・回答先を確認します。','06-ledger':'検索・分類・プロジェクトの管理状況を確認します。','07-project':'セクションと入力有無を確認します。','08-harness':'提案・変換・既存情報補完の入口を確認します。','09-move':'移動先と、実行前の確認画面への操作を確認します。','10-archive':'対象確認・削除・公開先の入力を確認します。','11-nodes':'親子関係と所属Gitを確認します。','12-usage':'利用枠・取得状況・モデル切替の入口を確認します。','13-mobile':'現在の接続案内を確認します。','14-convert':'作成予定と、確認するフォルダ・ファイル移動案を確認します。','15-metadata':'目的・分類・管理状況などの入力欄を確認します。'}
    fixed=[]
    for k,t,b in result:
        for name,caption in captions.items():
            b=re.sub(r'(<figure><a href="assets/'+re.escape(name)+r'\.jpg".*?<figcaption>).*?(</figcaption>)',lambda m:m[1]+'この画面では'+caption+m[2],b,flags=re.S)
        b=b.replace('判断Providerとシャドーモード','Routing Providerとシャドーモード')
        fixed.append((k,t,b))
    from measurement_update import update
    return update(fixed, table)
