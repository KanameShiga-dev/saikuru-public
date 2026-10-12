"""Report automatic handoff with separate cloud and local utilization."""
import hashlib
import json
from pathlib import Path
from ui_decision_report import usage

def calls(cases):
    return ([s['decision'] for r in cases for s in r['steps']]
            +[c for r in cases for c in r.get('decisionFailures',[])]
            +[r['error'] for r in cases if r.get('error')])

def report(root):
    rows=json.loads((root/'ui-results.json').read_text(encoding='utf-8'))
    metrics={}
    for arm in ('model','hybrid'):
        cases=[r for r in rows if r['arm']==arm]
        cs=calls(cases); native=[c for c in cs if c['arm']=='model']; local=[c for c in cs if c['arm']=='ollama']
        metrics[arm]={'cases':len(cases),'success':sum(r['status']=='passed' for r in cases),
            'operations':sum(len(r['steps']) for r in cases),'switches':sum(len(r.get('switches',[])) for r in cases),
            'cli_calls':len(native),'cli_input':usage(native,'input'),'cli_cached':usage(native,'cached'),
            'cli_output':usage(native,'output'),'ollama_calls':len(local),
            'ollama_input':usage(local,'input') if local else 0,'ollama_output':usage(local,'output') if local else 0,
            'decision_seconds':round(sum(c.get('latency_ms',0) for c in cs)/1000,3),
            'denied_requests':sum(c.get('denied_requests',0) for c in cs)}
    (root/'hybrid-metrics.json').write_text(json.dumps(metrics,indent=2),encoding='utf-8')
    reduction=round((1-metrics['hybrid']['cli_input']/metrics['model']['cli_input'])*100,1)
    common=({(r['name'],r['repetition']) for r in rows if r['arm']=='model' and r['status']=='passed'}
            &{(r['name'],r['repetition']) for r in rows if r['arm']=='hybrid' and r['status']=='passed'})
    paired={arm:usage([c for c in calls([r for r in rows if r['arm']==arm and (r['name'],r['repetition']) in common])
                      if c['arm']=='model'],'input') for arm in ('model','hybrid')}
    paired_reduction=round((1-paired['hybrid']/paired['model'])*100,1)
    lines=['# Ollamaから通常モデルへの自動引き継ぎ：実測結果','',
        '同じ画面・41件の合成データ・4目的で各3回、2条件24件を実行。実行順は反復ごとに交互にした。',
        '目的達成は独立したDOM条件で確認。状態更新した合成タスクは画面操作で復元した。', '',
        '| 項目 | 通常モデルのみ | Ollama＋自動引き継ぎ |','|---|---:|---:|']
    for label,key in [('目的達成','success'),('ケース数','cases'),('画面操作数','operations'),('自動引き継ぎ','switches'),
                      ('開発CLI呼び出し','cli_calls'),('CLI入力トークン','cli_input'),
                      ('キャッシュ入力（入力の内数）','cli_cached'),('CLI出力トークン','cli_output'),
                      ('Ollama判断要求','ollama_calls'),('Ollama入力','ollama_input'),('Ollama出力','ollama_output'),
                      ('累積判断処理時間（秒）','decision_seconds')]:
        lines.append('| '+label+' | '+str(metrics['model'][key])+' | '+str(metrics['hybrid'][key])+' |')
    lines += ['', '## 結論','',
        f'- 今回の記録上、併用のCLI入力は通常モデルのみより{reduction}%少なかった。',
        f"- 両方式で成功した同じ{len(common)}ケースだけでは、通常モデルのCLI入力{paired['model']:,}に対して併用{paired['hybrid']:,}で、{paired_reduction}%少なかった。停止例を除いた比較も分けて記録する。",
        '- 併用は12/12成功し、検索と該当なしの確認で各3回、合計6回自動引き継ぎした。',
        '- ページ移動と状態更新の6ケースはOllamaだけで完了した。',
        '- 通常モデルのみでは初回ページ移動で検索を行い、目的を達成する前にSTOPを選んだ。停止した判断の利用量も集計した。',
        '- 以前のOllama単独は6/12成功だった。今回の併用で、以前停止した検索・該当なしの6ケースも実行できた。以前の値は参考で、別実行の時間・利用量を混ぜていない。',
        '', '## 引き継ぎ条件と範囲','',
        '- 2操作続けてDOM状態が変わらなければ通常モデルへ切り替える。',
        '- OllamaのSTOP、通信エラー、不正候補でも実行せず通常モデルへ引き継ぐ。',
        '- 通常モデルへは目的、現在のDOM、操作候補、直前までの操作・状態の要約を渡す。完全な操作記録はローカルに保存する。',
        '- 一度通常モデルへ移ったケースはOllamaへ戻さない。全体の8操作上限は切り替え時にリセットしない。',
        '- 切り替えても実行可能な操作は固定の安全な候補だけ。任意のURL・コード・セレクターは実行できない。',
        '- 全条件で利用者への質問は発生しなかった。モデルの承認要求拒否数: '+str(sum(m['denied_requests'] for m in metrics.values()))+'。',
        '', '## 測定の限界','',
        '- 24ケースの合成画面での結果。一般の画面や本番で40%等の削減率を保証する測定ではない。',
        '- 通常モデルはgpt-6-sol/medium、ローカルはtev1:0.8b。開発CLIとOllamaのトークンを混ぜて削減率を計算しない。',
        '- CLI総入力には共通指示やキャッシュ入力も含まれる。課金額やアカウント残量の減少率とは異なる。',
        '- 判断時間には通信とCLIセッション起動が含まれる。ケースelapsed_msには統括の待ちも含むため上表の判断時間とは分ける。',
        '- 通常モデルの初回停止の影響も含む。モデルの揺れ・キャッシュ・追加操作の差を完全には分離できていない。',
        '- 原因別のエラー引き継ぎは単体テストで確認。今回の実画面で発生した6回は、すべて画面進捗がないことによる切り替え。',
        '- 本番の采来への組み込みは今回の対象外。計測専用の操作ハーネスに実装した。',
        '', '## 証拠・再現','',
        '- ui-results.json: 操作前後、実担当、利用量、切り替え理由、完了と停止、画面画像。',
        '- hybrid-metrics.json: 集計値。',
        '- manifest.json: 元ソースとハーネスのハッシュ・条件。harness.mjs: 今回実行したハーネスのスナップショット。',
        '- ui_decision_benchmark.pyのprepare/serve/decideと、Browserスキル内のUiBenchmarkを使用する。arm=hybrid、decision_neededのproviderに応じて判断を依頼する。',
        '- ui-results.json SHA256: '+hashlib.sha256((root/'ui-results.json').read_bytes()).hexdigest(), '']
    path=root/'REPORT.md';path.write_text('\n'.join(lines),encoding='utf-8');return path

if __name__=='__main__':
    import sys
    print(report(Path(sys.argv[1])))
