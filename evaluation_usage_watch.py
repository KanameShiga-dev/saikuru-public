"""Local, prompt-free cumulative usage snapshots. No model calls."""
import argparse, datetime, json, pathlib, sqlite3, time

def collect(root, database):
    rows=[]
    for p in (root/'decisions').glob('*.json'):
        d=json.loads(p.read_text(encoding='utf-8'))
        u=d.get('normalized_usage',{})
        if not u and d.get('arm')=='ollama':
            raw=d.get('usage',{});u={k:raw[k] for k in ['input_tokens','output_tokens'] if isinstance(raw.get(k),int)}
        rows.append((p.stem,d.get('arm','unknown'),json.dumps(u,sort_keys=True)))
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE IF NOT EXISTS observations (observed TEXT, decision TEXT, provider TEXT, usage TEXT, PRIMARY KEY(observed,decision))')
        stamp=datetime.datetime.now(datetime.timezone.utc).isoformat()
        db.executemany('INSERT INTO observations VALUES (?,?,?,?)',[(stamp,*r) for r in rows])
    return len(rows)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=pathlib.Path,required=True);p.add_argument('--watch',action='store_true');p.add_argument('--interval',type=int,default=300);a=p.parse_args()
    database=a.root/'usage-observations.sqlite3'
    while True:
        print(json.dumps({'records':collect(a.root,database),'cumulative_snapshot':True}),flush=True)
        if not a.watch or (a.root/'usage-watch.stop').exists():break
        time.sleep(max(60,a.interval))
