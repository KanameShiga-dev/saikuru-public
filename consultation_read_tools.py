"""Bounded, project-scoped read-only MCP tools. No command or write tool."""
import json
import hashlib
from itertools import islice
import os
from pathlib import Path
import re
import sys
import time
import uuid

def receipt(root, path, start, count, reused=False):
    stat = path.stat()
    return {'id':uuid.uuid4().hex[:12], 'path':str(path.relative_to(root)),
            'start_line':start, 'line_count':count, 'checked_at':time.time(),
            'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'mtime_ns':stat.st_mtime_ns, 'size':stat.st_size, 'reused':reused}

def bootstrap(root, text, previous=None):
    """Host-owned investigation before a model can answer; bounded source excerpts."""
    report = {'status':'unavailable', 'files':[], 'evidence':[], 'errors':[], 'snippets':[]}
    try:
        root = Path(root).resolve(strict=True)
        candidates = list(islice(files(root),400))
        report['files'] = [str(p.relative_to(root)) for p in candidates]
        preferred = ('readme.md','project_context.md','architecture.md','package.json','pyproject.toml')
        selected = [(p,1) for p in candidates if p.name.casefold() in preferred][:4]
        terms = list(dict.fromkeys(re.findall(r'[A-Za-z][A-Za-z0-9_-]{2,60}',text)))[:6]
        scanned = 0
        for path in candidates:
            if not terms:break
            if len(selected)>=7:break
            scanned += path.stat().st_size
            if scanned>2000000:break
            if any(p==path for p,_ in selected):continue
            for index,line in enumerate(path.read_text(encoding='utf-8-sig',errors='replace').splitlines()):
                if any(term.casefold() in line.casefold() for term in terms):
                    selected.append((path,max(1,index-15)));break
        if not selected:
            selected=[(p,1) for p in candidates[:3]]
        budget=32000
        for path,start in selected:
            try:
                safe_path(root,str(path.relative_to(root)))
                cached=next((v for v in (previous or {}).get('snippets',[])
                             if v['evidence']['path']==str(path.relative_to(root))
                             and v['evidence']['start_line']==start),None)
                reuse=bool(cached and cached['evidence']['sha256']==hashlib.sha256(path.read_bytes()).hexdigest())
                value=cached['content'] if reuse else execute(root,'read_file',{'path':str(path.relative_to(root)),'start_line':start,'line_count':100})['content']
                if budget<=0:break
                value=value[:budget];budget-=len(value)
                evidence=receipt(root,path,start,100,reuse)
                report['snippets'].append({'evidence':evidence,'content':value})
                report['evidence'].append(evidence)
            except (OSError,ValueError):
                report['errors'].append('候補ファイルを読み取れませんでした。')
        report['status']='reviewed' if report['evidence'] else 'unavailable'
        if not report['evidence']:report['errors'].append('対象範囲に読み取れる資料・コードがありません。')
    except (OSError,ValueError):
        report['errors'].append('対象フォルダを読み取れません。移動・削除・アクセス権を確認してください。')
    return report

BLOCKED = {'.git', '.env', '.ssh', '.aws', '.azure', '.codex', '.claude', 'node_modules', '__pycache__',
           'data', 'backups', 'logs', 'recordings', 'records', 'profiles', 'certs',
           'engines', '.venv', '.offline_stage', 'credentials', 'secrets', 'dist', 'build'}
TEXT = {'.py', '.js', '.ts', '.tsx', '.jsx', '.html', '.css', '.md', '.json', '.toml',
        '.yaml', '.yml', '.txt', '.cs', '.xml', '.ps1', '.sh', '.sql', '.ini', '.cfg'}

def safe_path(root, name):
    path = (root / name).resolve()
    if path.name.casefold() == 'config.yaml':
        raise ValueError('実設定は読み取り対象外です。設定テンプレートを指定してください。')
    relative = path.relative_to(root)
    for part in relative.parts:
        lowered = part.casefold()
        if lowered in BLOCKED or re.search(r'(^\.env|credential|secret|token|cookie|private.key)', lowered):
            raise ValueError('認証情報・個人データ・生成物は読み取り対象外です。')
    if path.suffix.casefold() not in TEXT:
        raise ValueError('対応するテキストファイルを指定してください。')
    if not path.is_file() or path.stat().st_size > 200000:
        raise ValueError('ファイルがないか、読み取りサイズ上限を超えています。')
    return path

DOCUMENTS = {'.pdf', '.pptx', '.docx', '.xlsx'}


def safe_document(root, name):
    """2026-10-08: planning/review roles of development jobs could not read PDF/PowerPoint/Word/Excel at all and
    stopped to ask the user. Same exclusions as text files; documents are read as text only (nothing executed)."""
    path = (root / name).resolve()
    relative = path.relative_to(root)
    for part in relative.parts:
        lowered = part.casefold()
        if lowered in BLOCKED or re.search(r'(^\.env|credential|secret|token|cookie|private.key)', lowered):
            raise ValueError('認証情報・個人データ・生成物は読み取り対象外です。')
    if path.suffix.casefold() not in DOCUMENTS:
        raise ValueError('PDF・PowerPoint・Word・Excel を指定してください。')
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 50_000_000:
        raise ValueError('ファイルがないか、読み取りサイズ上限を超えています。')
    return path


def read_document(root, name):
    import document_tools
    path = safe_document(root, name)
    suffix = path.suffix.casefold()
    if suffix == '.pdf':
        value = document_tools.pdf_text(path)
    elif suffix == '.pptx':
        value = document_tools.pptx_text(path)
    else:
        value = document_tools.office_text(path)
    return dict(value, path=str(path.relative_to(root)), bytes=path.stat().st_size)


def document_files(root, limit=100):
    found = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d.casefold() not in BLOCKED and not (Path(directory)/d).is_symlink()]
        for name in names:
            if Path(name).suffix.casefold() in DOCUMENTS:
                try:
                    found.append(str(safe_document(root, str((Path(directory)/name).relative_to(root))).relative_to(root)))
                except (ValueError, OSError):
                    continue
                if len(found) >= limit:
                    return found
    return found


def files(root):
    scanned = 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d.casefold() not in BLOCKED and not (Path(directory)/d).is_symlink()]
        for name in names:
            scanned += 1
            if scanned > 5000:
                return
            candidate = Path(directory)/name
            try:
                path = safe_path(root, str(candidate.relative_to(root)))
                yield path
            except (ValueError, OSError):
                continue

def execute(root, name, args):
    if name == 'list_files':
        output=[]
        for path in files(root):
            output.append(str(path.relative_to(root)))
            if len(output) >= 400: break
        return {'files': output, 'limit': 400, 'documents': document_files(root)}
    if name == 'read_document':
        return read_document(root, args.get('path', ''))
    if name == 'read_file':
        path = safe_path(root, args.get('path', ''))
        start = max(1, min(100000, int(args.get('start_line', 1))))
        count = max(1, min(150, int(args.get('line_count', 100))))
        lines = path.read_text(encoding='utf-8-sig', errors='replace').splitlines()
        return {'path': str(path.relative_to(root)), 'total_lines': len(lines),
                'content': '\n'.join(f'{i+1}: {line}' for i,line in enumerate(lines) if start-1 <= i < start-1+count)[:16000]}
    if name == 'search_files':
        query=args.get('query', '')
        if not isinstance(query,str) or not 1 <= len(query) <= 120: raise ValueError('検索語を指定してください。')
        output=[]
        scanned_bytes = 0
        for path in files(root):
            scanned_bytes += path.stat().st_size
            if scanned_bytes > 2000000:
                return {'matches':output,'limit':40,'truncated':True}
            for index,line in enumerate(path.read_text(encoding='utf-8-sig',errors='replace').splitlines()):
                if query.casefold() in line.casefold():
                    output.append({'path':str(path.relative_to(root)), 'line':index+1,'text':line[:250]})
                    if len(output)>=40:return {'matches':output,'limit':40}
        return {'matches':output,'limit':40}
    if name == 'web_search':
        from team_web_guard import search
        return search(args.get('query',''),root)
    raise ValueError('読み取り以外の操作は許可されません。')

TOOLS = [
 {'name':'web_search','description':'Search public technical information through a fail-closed egress check. Never include internal names, paths, personal data, secrets or pasted documents. Returns bounded Bing search snippets, never fetches pages.', 'inputSchema':{'type':'object','properties':{'query':{'type':'string','maxLength':200}},'required':['query'],'additionalProperties':False}},
 {'name':'list_files','description':'List bounded non-sensitive project text files.', 'inputSchema':{'type':'object','properties':{},'additionalProperties':False}},
 {'name':'read_file','description':'Read a project-relative text file, bounded lines; never writes.', 'inputSchema':{'type':'object','properties':{'path':{'type':'string'},'start_line':{'type':'integer'},'line_count':{'type':'integer'}},'required':['path'],'additionalProperties':False}},
 {'name':'search_files','description':'Search a literal term in project text files.', 'inputSchema':{'type':'object','properties':{'query':{'type':'string'}},'required':['query'],'additionalProperties':False}},
 {'name':'read_document','description':'Read a project PDF/PPTX/DOCX/XLSX as text (PDF pages; picture-only pages via local OCR; PPTX slides, tables, notes). list_files returns their paths under documents. Visual layout is checked by the user at acceptance.', 'inputSchema':{'type':'object','properties':{'path':{'type':'string'}},'required':['path'],'additionalProperties':False}},
]

def main():
    root=Path(sys.argv[1]).resolve(strict=True)
    sys.stdout.reconfigure(encoding='utf-8');sys.stdin.reconfigure(encoding='utf-8')
    calls=0
    for raw in sys.stdin:
        msg={}
        try:
            if len(raw)>100000:raise ValueError('Request too large')
            msg=json.loads(raw)
            if 'id' not in msg:continue
            method=msg.get('method');params=msg.get('params',{})
            if method=='initialize':result={'protocolVersion':params.get('protocolVersion','2024-11-05'),'capabilities':{'tools':{}},'serverInfo':{'name':'consultation-project-read','version':'1.0'}}
            elif method=='ping':result={}
            elif method=='tools/list':result={'tools':TOOLS}
            elif method=='tools/call':
                calls+=1
                if calls>60:raise ValueError('読み取り呼出上限に達しました。')
                value=execute(root,params.get('name'),params.get('arguments',{}))
                if len(sys.argv)>2 and params.get('name')=='read_file':
                    args=params.get('arguments',{})
                    entry=receipt(root,safe_path(root,args['path']),max(1,min(100000,int(args.get('start_line',1)))),max(1,min(150,int(args.get('line_count',100)))))
                    with Path(sys.argv[2]).open('a',encoding='utf-8') as audit:
                        audit.write(json.dumps(entry,ensure_ascii=False)+'\n')
                    value['evidence_id']=entry['id']
                result={'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}]}
            else:raise ValueError('Unsupported read-only operation')
            answer={'jsonrpc':'2.0','id':msg['id'],'result':result}
        except Exception:
            answer={'jsonrpc':'2.0','id':msg.get('id'),'error':{'code':-32602,'message':'対象外のパス・操作、または読み取り上限です。'}}
        print(json.dumps(answer,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
