"""Project-scoped, append-only exchange history and explicit fact reconciliation."""
import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path


def canonical(path):
    return str(Path(path).resolve()).casefold()


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class HandoffDB:
    def __init__(self, directory):
        self.path = Path(directory) / 'handoff.sqlite3'
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS records (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL,
                job_id TEXT, task_id TEXT, kind TEXT NOT NULL,
                source_id TEXT NOT NULL UNIQUE, happened_at REAL NOT NULL,
                payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS records_job ON records(job_id,seq);
            CREATE INDEX IF NOT EXISTS records_project ON records(project,seq);
            CREATE TABLE IF NOT EXISTS facts (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL,
                job_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                evidence TEXT NOT NULL, authority TEXT NOT NULL,
                source_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                created_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS facts_current ON facts(job_id,key,status);
            CREATE TABLE IF NOT EXISTS conflicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                key TEXT NOT NULL, old_fact INTEGER NOT NULL, new_fact INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', resolved_at REAL, resolution TEXT);
            CREATE INDEX IF NOT EXISTS conflicts_pending ON conflicts(job_id,status);
        ''')
        if 'resolution' not in [r[1] for r in self.db.execute('PRAGMA table_info(conflicts)')]:
            self.db.execute('ALTER TABLE conflicts ADD COLUMN resolution TEXT')
        self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    def record(self, project, job_id, task_id, kind, source_id, happened_at, payload):
        with self.lock:
            self.db.execute('INSERT OR IGNORE INTO records(project,job_id,task_id,kind,source_id,happened_at,payload) VALUES (?,?,?,?,?,?,?)',
                (canonical(project), job_id, task_id, kind, source_id, happened_at or time.time(),
                 json.dumps(payload, ensure_ascii=False)))
            self.db.commit()

    def fact(self, project, job_id, key, value, evidence, authority, source_id):
        key, value, evidence = str(key).strip()[:160], str(value).strip()[:2000], str(evidence).strip()[:500]
        if not key or not value:
            return
        if authority == 'model_reported' and re.search(r'password|passwd|api.?key|secret|access.?token|cookie|パスワード|秘密鍵|認証コード', key, re.I):
            return
        key = ' '.join(key.casefold().split())
        with self.lock, self.db:
            if self.db.execute('SELECT 1 FROM facts WHERE source_id=?', (source_id,)).fetchone():
                return
            current = self.db.execute("SELECT seq,value,authority FROM facts WHERE job_id=? AND key=? AND status='current' ORDER BY seq DESC LIMIT 1", (job_id,key)).fetchone()
            if authority=='model_reported' and current and current[2]=='model_reported':
                confirmed=self.db.execute("SELECT seq,value,authority FROM facts WHERE project=? AND key=? AND job_id<>? AND status='current' AND authority IN ('user_decision','user_confirmed') ORDER BY seq DESC LIMIT 1",(canonical(project),key,job_id)).fetchone()
                if confirmed and ' '.join(confirmed[1].split())!=' '.join(value.split()):current=confirmed
            if not current and authority == 'model_reported':
                current = self.db.execute("SELECT seq,value,authority FROM facts WHERE project=? AND key=? AND job_id<>? AND status='current' AND authority IN ('user_decision','user_confirmed') ORDER BY seq DESC LIMIT 1",
                    (canonical(project),key,job_id)).fetchone()
            same = current and ' '.join(current[1].split()) == ' '.join(value.split())
            # Within one job, a later AI report about the same key is a progress update
            # (e.g. "not created yet" -> "created"). Supersede it but keep an audit row.
            # Facts the user confirmed or decided still stop for a human decision.
            time_update = bool(current and not same and authority == 'model_reported' and current[2] == 'model_reported')
            superseded = None
            if current and not same and (authority == 'user_decision' or time_update):
                self.db.execute("UPDATE facts SET status='superseded' WHERE seq=?",(current[0],))
                superseded = current[0] if time_update else None
                current = None
            status = 'current' if not current else ('duplicate' if same else 'pending')
            cursor = self.db.execute('INSERT INTO facts(project,job_id,key,value,evidence,authority,source_id,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                (canonical(project),job_id,key,value,evidence,authority,source_id,status,time.time()))
            if status == 'pending':
                self.db.execute('INSERT INTO conflicts(job_id,key,old_fact,new_fact) VALUES (?,?,?,?)',
                    (job_id,key,current[0],cursor.lastrowid))
            elif superseded:
                self.db.execute("INSERT INTO conflicts(job_id,key,old_fact,new_fact,status,resolved_at,resolution) VALUES (?,?,?,?,'resolved',?,?)",
                    (job_id,key,superseded,cursor.lastrowid,time.time(),json.dumps({'choice':'new','auto':'later_model_report'},ensure_ascii=False)))

    def sync(self, job, tasks, approvals):
        project, job_id = job['project'], job['id']
        self.record(project,job_id,None,'goal','goal:'+job_id,job.get('created_at'),
                    {'title':job['title'],'goal':job['goal']})
        for transfer in job.get('scope_transfers', []):
            self.record(project,job_id,transfer['origin_task_id'],'scope_transfer',
                        'scope_transfer:'+transfer['approval_id'],transfer.get('created_at'),
                        dict(transfer, authority='user_decision', verification_waived=False))
        for task in tasks:
            for revision in task.get('instruction_history') or []:
                source = 'instruction_history:' + task['id'] + ':' + digest(json.dumps(revision, ensure_ascii=False, sort_keys=True))
                self.record(project,job_id,task['id'],'instruction_history',source,
                            revision.get('at'),revision)
            if task.get('recovery_advice'):
                item = task['recovery_advice']
                self.record(project,job_id,task['id'],'recovery_advice',
                            'recovery_advice:'+task['id']+':'+str(item['checked_at']),item['checked_at'],item)
            if task.get('recovery'):
                self.record(project,job_id,task['id'],'recovery',
                            'recovery:'+task['id']+':'+str(task.get('attempt',0)),task.get('updated_at'),task['recovery'])
            model_state={'profile':task['profile'],'auto_return':task.get('auto_return'),
                         'history':task.get('model_history',[])}
            self.record(project,job_id,task['id'],'model_assignment',
                        'model_assignment:'+task['id']+':'+digest(json.dumps(model_state,sort_keys=True)),task.get('updated_at'),
                        {'title':task['title'],**model_state})
            if task.get('handoff_note'):
                self.record(project,job_id,task['id'],'scope_handoff',
                            'scope_handoff:'+task['id']+':'+digest(task['handoff_note']),task.get('finished_at'),
                            {'title':task['title'],'note':task['handoff_note'],'status':task['status'],
                             'authority':'user_decision' if any(x.get('task_id')==task['id'] for x in job.get('handoff_scope',[])) else 'model_reported'})
            instruction = task.get('instruction','')
            source = 'instruction:'+task['id']+':'+digest(instruction)
            self.record(project,job_id,task['id'],'instruction',source,task.get('updated_at'),
                        {'title':task['title'],'role':task['role'],'instruction':instruction,'attempt':task.get('attempt',0)})
            result = task.get('result')
            if not isinstance(result,dict):
                continue
            source = 'result:'+task['id']+':'+str(task.get('attempt',0))+':'+digest(json.dumps(result,ensure_ascii=False,sort_keys=True))
            self.record(project,job_id,task['id'],'result',source,task.get('finished_at'),
                        {'title':task['title'],'role':task['role'],'result':result})
            for index, item in enumerate(result.get('context_updates') or []):
                if not isinstance(item,dict):
                    continue
                self.fact(project,job_id,item.get('key',''),item.get('value',''),item.get('evidence',''),
                          'model_reported',source+':fact:'+str(index))
        for approval in approvals:
            if approval['kind'] not in ('question','plan','completion','context_conflict','scope_transfer') or approval['status']=='pending':
                continue
            source = 'decision:'+approval['id']
            payload = {'kind':approval['kind'],'status':approval['status'],
                       'note':approval.get('note',''),'questions':approval.get('payload',{}).get('questions') or [],
                       'answers':approval.get('answers') or {}}
            self.record(project,job_id,approval.get('task_id'),'decision',source,
                        approval.get('decided_at') or approval.get('updated_at'),payload)
            if approval['kind']=='question' and approval['status']=='approved':
                for q in payload['questions']:
                    answer = payload['answers'].get(q.get('id')) or {}
                    option = next((o for o in q.get('options',[]) if o.get('id')==answer.get('option_id')),None)
                    if option:
                        value = option['label'] + (' / '+answer['text'] if answer.get('text') else '')
                        self.fact(project,job_id,q['text'],value,'利用者がダッシュボードで回答',
                                  'user_decision',source+':'+q['id'])
        file = Path(project) / 'HANDOFF.md'
        if file.is_file():
            if file.stat().st_size > 100_000:
                raise ValueError('HANDOFF.mdが100KBを超えています。要点を整理してから引き継いでください。')
            content = file.read_text(encoding='utf-8-sig')
            self.record(project,None,None,'file_snapshot','handoff:'+canonical(project)+':'+str(file.stat().st_mtime_ns)+':'+digest(content),
                        file.stat().st_mtime,{'path':str(file),'modified_at':file.stat().st_mtime,'content':content})

    def pending_conflicts(self, job_id, limit=8):
        with self.lock:
            rows=self.db.execute('''SELECT c.id,c.key,o.value,n.value,o.evidence,n.evidence,o.created_at,n.created_at,o.authority,n.authority FROM conflicts c
                JOIN facts o ON o.seq=c.old_fact JOIN facts n ON n.seq=c.new_fact
                WHERE c.job_id=? AND c.status='pending' ORDER BY c.id LIMIT ?''',(job_id,limit)).fetchall()
            return [{'id':r[0],'key':r[1],'old':r[2],'new':r[3],
                     'old_evidence':r[4],'new_evidence':r[5], 'old_time':r[6], 'new_time':r[7],
                     'old_authority':r[8], 'new_authority':r[9]} for r in rows]

    def resolve(self, job_id, conflict_id, choice, custom=''):
        with self.lock, self.db:
            row=self.db.execute("SELECT old_fact,new_fact,key,status,resolution FROM conflicts WHERE id=? AND job_id=?",(conflict_id,job_id)).fetchone()
            if not row:
                raise ValueError('矛盾の確認項目が見つかりません。')
            old,new,key,status,resolution=row
            chosen=json.dumps({'choice':choice,'custom':custom.strip() if choice=='custom' else ''},ensure_ascii=False)
            if status=='resolved':
                if resolution==chosen:
                    return
                raise ValueError('この矛盾は別の回答で処理済みです。')
            if choice=='old':
                self.db.execute("UPDATE facts SET status='rejected' WHERE seq=?",(new,))
                self.db.execute("UPDATE facts SET authority='user_confirmed' WHERE seq=?",(old,))
            elif choice=='new':
                self.db.execute("UPDATE facts SET status='superseded' WHERE seq=?",(old,))
                self.db.execute("UPDATE facts SET status='current',authority='user_confirmed' WHERE seq=?",(new,))
            elif choice=='custom' and custom.strip():
                source=self.db.execute('SELECT project FROM facts WHERE seq=?',(old,)).fetchone()[0]
                self.db.execute("UPDATE facts SET status='superseded' WHERE seq=?",(old,))
                self.db.execute("UPDATE facts SET status='rejected' WHERE seq=?",(new,))
                self.db.execute('INSERT INTO facts(project,job_id,key,value,evidence,authority,source_id,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                    (source,job_id,key,custom.strip()[:2000],'利用者が矛盾を解消','user_decision',f'conflict:{conflict_id}', 'current',time.time()))
            else:
                raise ValueError('矛盾の解決方法を選んでください。')
            self.db.execute("UPDATE conflicts SET status='resolved',resolved_at=?,resolution=? WHERE id=?",(time.time(),chosen,conflict_id))

    def context(self, job, allowed_files=None):
        job_id=job['id'];project=canonical(job['project'])
        with self.lock:
            facts=[dict(key=r[0],value=r[1],evidence=r[2],authority=r[3]) for r in self.db.execute(
                "SELECT key,value,evidence,authority FROM facts WHERE job_id=? AND status='current' ORDER BY seq",(job_id,))]
            older_facts=[dict(key=r[0],value=r[1],evidence=r[2],job_id=r[3]) for r in self.db.execute(
                "SELECT key,value,evidence,job_id FROM facts WHERE project=? AND job_id<>? AND status='current' AND authority IN ('user_decision','user_confirmed') ORDER BY seq",
                (project,job_id))]
            records=[dict(kind=r[0],task_id=r[1],payload=json.loads(r[2])) for r in self.db.execute(
                "SELECT kind,task_id,payload FROM records WHERE job_id=? ORDER BY seq",(job_id,))]
            file=self.db.execute("SELECT payload FROM records WHERE project=? AND kind='file_snapshot' ORDER BY seq DESC LIMIT 1",(project,)).fetchone()
        project_memory={}
        for fact in older_facts:
            project_memory[fact['key']]=fact
        latest_instructions,latest_reports,decisions={},{},[]
        for record in records:
            if record['kind']=='instruction':
                latest_instructions[record['task_id']]=record['payload']
            elif record['kind']=='result':
                latest_reports[record['task_id']]=record['payload']
            elif record['kind']=='decision':
                decisions.append(record['payload'])
            elif record['kind'] in ('scope_handoff', 'scope_transfer'):
                decisions.append(record['payload'])
        package={'current_facts':facts,'earlier_user_decisions_in_same_project':list(project_memory.values()),
                 'earlier_decisions_note':'前の依頼での判断。現在の依頼に当てはまるか確認し、矛盾したら質問する。',
                 'user_decisions_and_approvals':decisions,
                 'latest_task_instructions':latest_instructions,'latest_task_reports':latest_reports,
                 'history_note':'古い版も専用DBに保持。ここには各タスクの最新版と判断履歴を提示。',
                 'project_handoff_reference':json.loads(file[0]) if file else None}
        text=json.dumps(package,ensure_ascii=False)
        if len(text)>150_000:
            from team_context_pages import write_pages
            project_root = Path(job['project']).resolve()
            snapshot_directory = (project_root / 'builds' / '.agent-team-context').resolve()
            if not snapshot_directory.is_relative_to(project_root):
                raise ValueError('分割文脈の保存先が対象プロジェクト外へ参照されています。')
            manifest, files = write_pages(package, snapshot_directory, job_id)
            if allowed_files is not None:
                allowed_files.extend(files)
            return '引き継ぎ文脈は分割済みです。以下の索引にあるファイルだけを読み取り専用で参照できます。統括コード・DB・APIへのアクセスは禁止。必要な全partを読んでから作業してください。\n' + manifest
        return text

    def history(self, job_id, project, before=None, limit=50):
        with self.lock:
            rows=self.db.execute("SELECT seq,kind,task_id,happened_at,payload FROM records WHERE (job_id=? OR (project=? AND kind='file_snapshot')) AND seq<? ORDER BY seq DESC LIMIT ?",
                (job_id,canonical(project),before or 9223372036854775807,min(max(limit,1),100))).fetchall()
            return [{'seq':r[0],'kind':r[1],'task_id':r[2],'at':r[3],'payload':json.loads(r[4])} for r in rows]

    def recovery_knowledge(self, project, code):
        key = '復旧手順:' + code
        with self.lock:
            row = self.db.execute("SELECT value,evidence,created_at FROM facts WHERE project=? AND key=? AND status='current' AND authority IN ('user_confirmed','user_decision') ORDER BY seq DESC LIMIT 1",
                (canonical(project), key)).fetchone()
        return dict(value=row[0], evidence=row[1], created_at=row[2]) if row else None

    def current_facts(self, job_id):
        with self.lock:
            rows=self.db.execute("SELECT key,value,evidence,authority FROM facts WHERE job_id=? AND status='current' ORDER BY seq",(job_id,)).fetchall()
            return [{'key':r[0],'value':r[1],'evidence':r[2],'authority':r[3]} for r in rows]

    def set_user_fact(self, project, job_id, key, value, evidence):
        key, value, evidence = str(key).strip(), str(value).strip(), str(evidence).strip()
        if not key or not value or len(key)>160 or len(value)>2000 or len(evidence)>500:
            raise ValueError('項目・内容・根拠の長さを確認してください。')
        key = ' '.join(key.casefold().split())
        with self.lock, self.db:
            if self.db.execute("SELECT 1 FROM conflicts WHERE job_id=? AND key=? AND status='pending'",(job_id,key)).fetchone():
                raise ValueError('この項目には未解決の矛盾があります。先に判断欄で回答してください。')
            self.db.execute("UPDATE facts SET status='superseded' WHERE job_id=? AND key=? AND status='current'",(job_id,key))
            source='manual:'+str(time.time_ns())
            self.db.execute('INSERT INTO facts(project,job_id,key,value,evidence,authority,source_id,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                (canonical(project),job_id,key,value,evidence or '利用者が直接登録','user_confirmed',source,'current',time.time()))
            self.db.execute('INSERT INTO records(project,job_id,task_id,kind,source_id,happened_at,payload) VALUES (?,?,?,?,?,?,?)',
                (canonical(project),job_id,None,'manual_fact',source,time.time(),
                 json.dumps({'key':key,'value':value,'evidence':evidence},ensure_ascii=False)))

    def backup(self, target):
        with self.lock, sqlite3.connect(target) as dest:
            self.db.backup(dest)
