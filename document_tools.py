"""Bounded document output broker; source is always read-only."""
import hashlib
import json
import os
import re
from pathlib import Path
import sys
from consultation_read_tools import execute, TOOLS

DOCUMENTS = {'.md', '.html', '.txt', '.svg'}

def output_path(root, name):
    relative = Path(name)
    if relative.is_absolute() or any(p in ('.', '..') or p.startswith('.') for p in relative.parts):
        raise ValueError('資料の相対パスを指定してください。')
    if relative.suffix.lower() not in DOCUMENTS:
        raise ValueError('資料はmd/html/txt/svgのみです。')
    path = (root / relative).resolve()
    path.relative_to(root)
    return path

def document_operation(root, name, args, writable):
    path = output_path(root, args.get('path', ''))
    if name == 'read_document':
        if path.stat().st_size > 200000: raise ValueError('資料が大きすぎます。')
        data = path.read_bytes()
        return {'content': data.decode('utf-8')[:16000], 'sha256': hashlib.sha256(data).hexdigest()}
    if name != 'write_document' or not writable: raise ValueError('資料変更は禁止です。')
    content = args.get('content')
    if not isinstance(content, str) or len(content.encode('utf-8')) > 200000:
        raise ValueError('資料は200KB以内です。')
    if path.suffix.lower() in ('.html','.svg') and re.search(r'<script\b|\bon\w+\s*=|javascript:',content,re.I):
        raise ValueError('資料に実行用スクリプトは含められません。')
    manifest = root / '.saikuru-document-manifest.json'
    if manifest.is_symlink() or manifest.with_suffix('.tmp').is_symlink():
        raise ValueError('資料管理ファイルのリンクは使えません。')
    records = json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else {}
    key = str(path.relative_to(root))
    if path.exists():
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if records.get(key) != actual or args.get('expected_sha256') != actual:
            raise ValueError('既存資料・外部変更を上書きしません。新しい名前を指定してください。')
    path.parent.mkdir(parents=True, exist_ok=True)
    # Resolve again after mkdir; reject an escaped parent.
    path = output_path(root, args['path'])
    with path.open('w' if path.exists() else 'x', encoding='utf-8') as f: f.write(content)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    records[key] = digest
    temp = manifest.with_suffix('.tmp')
    temp.write_text(json.dumps(records), encoding='utf-8'); os.replace(temp, manifest)
    return {'path': key, 'sha256': digest}

def main():
    source, output = (Path(p).resolve(strict=True) for p in sys.argv[1:3])
    writable = '--write' in sys.argv[3:]
    if source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError('読み取り元と保存先は分離してください。')
    tools = list(TOOLS) + [{'name': 'read_document', 'description': 'Read a document in the approved output folder.',
        'inputSchema': {'type':'object','properties':{'path':{'type':'string'}},'required':['path'],'additionalProperties':False}}]
    if writable:
        tools.append({'name':'write_document','description':'Create documents only in the approved output folder. Updating a generated document requires its SHA256.',
            'inputSchema':{'type':'object','properties':{'path':{'type':'string'},'content':{'type':'string'},'expected_sha256':{'type':'string'}},'required':['path','content'],'additionalProperties':False}})
    sys.stdin.reconfigure(encoding='utf-8');sys.stdout.reconfigure(encoding='utf-8')
    calls = 0
    for raw in sys.stdin:
        msg = {}
        try:
            if len(raw)>1500000: raise ValueError('Request too large')
            msg=json.loads(raw)
            if 'id' not in msg: continue
            method=msg.get('method'); params=msg.get('params',{})
            if method=='initialize': result={'protocolVersion':params.get('protocolVersion','2024-11-05'),'capabilities':{'tools':{}},'serverInfo':{'name':'document-scope','version':'1.0'}}
            elif method=='ping': result={}
            elif method=='tools/list': result={'tools':tools}
            elif method=='tools/call':
                calls+=1
                if calls>100: raise ValueError('Tool limit')
                name=params.get('name');args=params.get('arguments',{})
                value=(document_operation(output,name,args,writable) if name in ('read_document','write_document') else execute(source,name,args))
                result={'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}]}
            else: raise ValueError('Unsupported operation')
            reply={'jsonrpc':'2.0','id':msg['id'],'result':result}
        except Exception as exc:
            reply={'jsonrpc':'2.0','id':msg.get('id'),'error':{'code':-32602,'message':str(exc)}}
        print(json.dumps(reply,ensure_ascii=False),flush=True)

if __name__=='__main__': main()
