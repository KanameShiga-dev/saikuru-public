"""Summarize real experiment evidence without equating cache/characters with savings."""
import argparse
import hashlib
import json
import statistics
from pathlib import Path


def write_report(root):
    rows=json.loads((root/'results.json').read_text(encoding='utf-8'))
    lines=['# 判断Provider比較：予備測定', '',
           '同一初期状態、同一モデル・工程の3条件。1組だけでは削減効果を確定しない。', '',
           '| 条件 | 状態 | 固定受入 | 入力 | キャッシュ入力 | 出力 | Agent呼出 | 秒 |',
           '|---|---|---|---:|---:|---:|---:|---:|']
    for row in rows:
        def display(value):
            return 'UNKNOWN' if value is None else str(value)
        lines.append('| '+' | '.join(display(row.get(k)) for k in (
            'arm','status','independent_acceptance','input_tokens','cached_input_tokens',
            'output_tokens','agent_calls','elapsed_seconds'))+' |')
    lines += ['', '## 判断結果', '']
    for row in rows:
        decisions=row.get('decisions',[])
        lines.append('- '+row['arm']+': '+str(len(decisions))+'回。'+
                     '候補差異 '+str(sum(d.get('differs') is True for d in decisions))+'回、'+
                     '実採用差異 '+str(sum(d.get('effective') != d.get('baseline') for d in decisions))+'回、'+
                     'エラー '+str(sum(d.get('status')!='ok' for d in decisions))+'回。')
        permissions=row.get('permission_decisions',[])
        lines.append('  - 操作判断（参考）: '+str(len(permissions))+'回、許可推奨 '+
                     str(sum(d.get('effective')=='ALLOW' for d in permissions))+'回、拒否・停止推奨 '+
                     str(sum(d.get('effective')!='ALLOW' for d in permissions))+'回。固定範囲による実行許可 '+
                     str(sum(d.get('execution_allowed') is True for d in permissions))+'回。')
        usage=[d.get('usage',{}) for d in decisions+permissions if d.get('provider')=='ollama']
        known=usage and all('input_tokens' in u and 'output_tokens' in u for u in usage)
        lines.append('  - Ollama入力/出力: '+
                     (str(sum(u['input_tokens'] for u in usage))+'/'+str(sum(u['output_tokens'] for u in usage)) if known else 'UNKNOWN / 未実行')+'。開発CLIとは別集計。')
    lines += ['', '## 判定の範囲', '',
              '- 不合格・未完了・判断エラーがある条件を、成功条件と同品質の削減効果として比較しない。',
              '- 総入力にはキャッシュ入力が含まれる。キャッシュ入力を再加算しない。',
              '- 呼出回数や利用量の差にはモデルの揺れ・キャッシュ・修正の差が含まれる。',
              '- 今回のAgent選択範囲は同一役割・権限の候補のみ。モデル切替による削減効果は対象外。',
              '- 実機ブラウザの操作・表示品質はNOT TESTED。',
              '- 初期調整で除外した試行にも利用量が発生している。results.json以外の調整試行は削減率の分母には入れないが、総計測コストとして別記する。',
              '', '## 証拠', '',
              '- results.json SHA256: '+hashlib.sha256((root/'results.json').read_bytes()).hexdigest(),
              '- manifest.json：初期ファイルのSHA256、モデル、Provider、判断予算。',
              '- *-external-acceptance.txt：固定受入テスト。',
              '- *-project-acceptance.txt：各実装のテスト。',
              '- state/team.sqlite3：全担当の状態・利用量・判断記録・承認境界イベント。', '']
    path=root/'REPORT.md'
    path.write_text('\n'.join(lines),encoding='utf-8')
    return path


def aggregate(roots, destination):
    records=[]
    manifests=[]
    for root in roots:
        manifests.append(json.loads((root/'manifest.json').read_text(encoding='utf-8')))
        for row in json.loads((root/'results.json').read_text(encoding='utf-8')):
            records.append(dict(row,source_run=root.name))
    signatures=[(m['seed_hashes'],m['model'],m['effort'],m['decision_timeout_ms']) for m in manifests]
    if signatures and any(s!=signatures[0] for s in signatures):
        raise ValueError('初期状態またはモデル条件が異なる実験は集約しません。')
    lines=['# 判断Provider効果の比較', '',
           '| 条件 | 回数 | 受入合格 | 入力中央値 | キャッシュ入力中央値 | 出力中央値 | 秒中央値 |',
           '|---|---:|---:|---:|---:|---:|---:|']
    for arm in ('A-disabled','B-shadow','C-adopted'):
        rows=[r for r in records if r['arm']==arm]
        def med(key):
            vals=[r.get(key) for r in rows]
            return round(statistics.median(vals),1) if vals and all(isinstance(v,(int,float)) for v in vals) else 'UNKNOWN'
        lines.append('| '+arm+' | '+str(len(rows))+' | '+str(sum(r.get('independent_acceptance') is True for r in rows))+' | '+
                     ' | '.join(str(med(k)) for k in ('input_tokens','cached_input_tokens','output_tokens','elapsed_seconds'))+' |')
    adopted=[d for r in records if r['arm']=='C-adopted' for d in r.get('decisions',[])]
    changed=sum(d.get('effective')!=d.get('baseline') for d in adopted)
    complete=all(len([r for r in records if r['arm']==arm and r.get('independent_acceptance') is True])>=3
                 for arm in ('A-disabled','B-shadow','C-adopted'))
    lines+=['', '## 結論の範囲', '',
            '- 実採用条件で既存ルールから変わったAgent選択: '+str(changed)+' / '+str(len(adopted))+'回。',
            '- 同品質の各条件3回以上: '+str(complete)+'。',
            '- '+('今回の課題ではAgent選択の変更がなかったため、開発CLIの利用量差をAgent切替の削減効果とは結論しない。' if changed==0 else
                    '選択の変化は観測されたが、利用量・修正回数・品質・順序の揺れを併せて評価する。'),
            '- 総入力にキャッシュ入力を再加算しない。料金・文字数をトークン数に換算していない。',
            '- Ollamaは同一役割の選択範囲のみ。モデルや工程を変更する最適化の効果は対象外。',
            '- 実機UIはNOT TESTED。計測前の調整試行は別途記録し、条件比較から除外する。', '',
            '## 証拠', '']
    for root in roots:
        lines.append('- '+root.name+'/results.json SHA256 '+hashlib.sha256((root/'results.json').read_bytes()).hexdigest())
    destination.mkdir(parents=True,exist_ok=True)
    (destination/'aggregate.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
    path=destination/'REPORT.md'
    path.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return path


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('root',type=Path,nargs='+')
    parser.add_argument('--aggregate-to',type=Path)
    args=parser.parse_args()
    if args.aggregate_to:
        print(aggregate([r.resolve() for r in args.root],args.aggregate_to.resolve()))
    elif len(args.root)==1:
        print(write_report(args.root[0].resolve()))
    else:
        parser.error('複数の実験には--aggregate-toを指定してください。')
