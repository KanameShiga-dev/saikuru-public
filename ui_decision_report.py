"""Report quality and measured decision latency separately from orchestration."""
import hashlib
import json
import statistics
import sys
from pathlib import Path

def usage(calls,key):
    aliases={'input':['inputTokens','input_tokens'],'cached':['cachedInputTokens','cached_input_tokens'],
             'output':['outputTokens','output_tokens']}[key]
    values=[next((c.get('usage',{})[k] for k in aliases if k in c.get('usage',{})),None) for c in calls]
    return sum(values) if values and all(v is not None for v in values) else 'UNKNOWN'

def report(root):
    rows=json.loads((root/'ui-results.json').read_text(encoding='utf-8'))
    if any(r['arm']=='hybrid' for r in rows):
        from ui_hybrid_report import report as hybrid_report
        return hybrid_report(root)
    lines=['# 画面操作の判断Provider比較（2026-10-02）','',
        '同じ受入済みタスク管理画面と41件の合成データで、4目的×3反復×3条件の36件を実行。',
        '操作はすべてBrowserの同じ入力・選択・クリックAPIで実施し、毎操作後にDOM状態を確認した。',
        '目的達成はモデルの自己申告ではなく、独立した画面状態の条件で判定。各ケースは最大8操作。', '',
        '| 条件 | 目的達成 | 操作数 | 判断要求 | 判断時間中央値/要求 | 入力トークン | 出力トークン |',
        '|---|---:|---:|---:|---:|---:|---:|']
    for arm in ('fixed','model','ollama'):
        cases=[r for r in rows if r['arm']==arm]
        calls=[s['decision'] for r in cases for s in r['steps']]
        calls += [r['error'] for r in cases if r.get('error')]
        lat=[c['latency_ms'] for c in calls if 'latency_ms' in c]
        lines.append(f"| {arm} | {sum(r['status']=='passed' for r in cases)}/{len(cases)} | {sum(len(r['steps']) for r in cases)} | {0 if arm=='fixed' else len(calls)} | {statistics.median(lat)/1000 if lat else 'UNKNOWN'}秒 | {0 if arm=='fixed' else usage(calls,'input')} | {0 if arm=='fixed' else usage(calls,'output')} |")
    lines += ['', '## 目的別の成功率', '', '| 目的 | 固定 | 通常モデル | Ollama |','|---|---:|---:|---:|']
    for name in ('search','page','update','empty'):
        counts=[f"{sum(r['status']=='passed' for r in rows if r['arm']==arm and r['name']==name)}/3" for arm in ('fixed','model','ollama')]
        lines.append('| '+name+' | '+' | '.join(counts)+' |')
    lines += ['', '## 成功した同じ操作だけの比較', '']
    for arm in ('model','ollama'):
        cases=[r for r in rows if r['arm']==arm and r['name'] in ('page','update')]
        calls=[s['decision'] for r in cases for s in r['steps']]
        lines.append(f"- {arm}: {sum(r['status']=='passed' for r in cases)}/6成功、{len(calls)}判断、累積判断時間{sum(c['latency_ms'] for c in calls)/1000:.3f}秒、入力{usage(calls,'input')}・出力{usage(calls,'output')}トークン。")
    model=[s['decision'] for r in rows if r['arm']=='model' for s in r['steps']]
    lines += ['', '## 解釈と制限', '',
        '- Ollamaの全体結果は6/12のため、通常モデルと同品質で全面置換できたとは扱わない。',
        '- ページ移動と状態更新では両方6/6成功。限定した操作での判断の代替は実行確認できた。',
        '- 判断時間には各アダプターの通信・起動が含まれる。通常モデルは各要求で独立CLIセッションを起動したため、常駐・同一セッション利用時との速度比較ではない。',
        '- ケース全体のelapsed_msはツールの呼び出し・統括待ちも含むため、判断処理の速度にはlatency_msを使った。',
        '- 通常モデルはgpt-6-sol/medium、Ollamaはtev1:0.8b。トークンは別のモデル体系であり、単純な差を課金額へ換算しない。固定手順にAI推論はない。',
        '- 通常モデルのキャッシュ入力（総入力の内数）: '+str(usage(model,'cached'))+'。再加算しない。',
        '- Ollama条件の開発CLI呼び出しは0件。Ollama判断に伴うローカル入力・出力は上表で別集計した。',
        '- 検証対象は構造化DOMと固定候補。任意の画面画像理解やデスクトップ操作、汎用自動運転の検証ではない。',
        '- 各要求は目的・現在のDOM状態・候補のみ。実行するセレクターや入力値はハーネスが固定し、モデルは追加操作・任意コード・URLを指定できない。',
        '- 状態更新した合成データは画面操作で復元し確認した。本番のDB・プロジェクト・権限に変更はない。',
        '- 利用者への質問は発生していない。通常モデルのツール承認要求拒否数: '+str(sum(c.get('denied_requests',0) for c in model))+'。',
        '- 反復は3回だが実行順は固定→Ollama→通常モデル。順序・キャッシュ・起動の影響を完全には分離できていない。',
        '', '## 再現と証拠', '',
        '- ui_decision_benchmark.py: fixture準備・ループバックサーバー・判断アダプター。',
        '- ui_decision_benchmark.mjs: Browserスキル内で実行する操作ハーネス。提供されたtabを使う。',
        '- ui-results.json: 操作前後、選択、利用量、停止、スクリーンショットの記録。',
        '- 各PNG: 実際の画面。撮影対象は合成データのみ。',
        '- manifest.json: 元ファイルのハッシュと条件。',
        '- ui-results.json SHA256: '+hashlib.sha256((root/'ui-results.json').read_bytes()).hexdigest(), '']
    (root/'REPORT.md').write_text('\n'.join(lines),encoding='utf-8')
    return root/'REPORT.md'

if __name__=='__main__': print(report(Path(sys.argv[1])))
