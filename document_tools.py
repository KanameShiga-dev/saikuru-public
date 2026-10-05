"""Bounded document output broker; source is always read-only."""
import hashlib
import json
import os
import re
from pathlib import Path
import sys
import subprocess
import tempfile
import shutil
import urllib.request
from consultation_read_tools import execute, TOOLS

DOCUMENTS = {'.md', '.html', '.txt', '.svg', '.pptx', '.pdf', '.mp4'}

def output_path(root, name):
    relative = Path(name)
    if relative.is_absolute() or any(p in ('.', '..') or p.startswith('.') for p in relative.parts):
        raise ValueError('資料の相対パスを指定してください。')
    if relative.suffix.lower() not in DOCUMENTS:
        raise ValueError('資料はmd/html/txt/svg/pptx/pdf/mp4のみです。')
    path = (root / relative).resolve()
    path.relative_to(root)
    return path

def document_operation(root, name, args, writable):
    path = output_path(root, args.get('path', ''))
    if name == 'read_document':
        if path.suffix in ('.pptx','.pdf','.mp4'):
            return {'path':str(path.relative_to(root)), 'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(), 'note':'バイナリ成果物です。内容の視聴・表示確認は別途必要です。'}
        if path.stat().st_size > 200000: raise ValueError('資料が大きすぎます。')
        data = path.read_bytes()
        return {'content': data.decode('utf-8')[:16000], 'sha256': hashlib.sha256(data).hexdigest()}
    if name != 'write_document' or not writable: raise ValueError('資料変更は禁止です。')
    content = args.get('content')
    if path.suffix in ('.pptx','.pdf','.mp4'):raise ValueError('バイナリ成果物はgenerate_mediaツールを使ってください。')
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

def media_environment():
    from team_document_capabilities import runtime
    python,ffmpeg=runtime()
    result={'renderer':'PowerPoint/PDF/slide MP4','ffmpeg':bool(ffmpeg),'voicevox_available':False,'speakers':[]}
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://127.0.0.1:50021/speakers',timeout=3) as response:
            speakers=json.loads(response.read(1000000))
        result['speakers']=[{'name':s['name'],'style':style['name'],'id':style['id']} for s in speakers for style in s['styles']]
        result['voicevox_available']=True
    except (OSError,ValueError,KeyError):pass
    return result

def generate_media(root,args,writable,job_id):
    if not writable:raise ValueError('制作はbuilderのみです。')
    path=output_path(root,args.get('path',''))
    if path.suffix not in ('.pptx','.pdf','.mp4') or path.exists():raise ValueError('新しいPPTX・PDF・MP4の名前を指定してください。既存成果物は上書きしません。')
    slides=args.get('slides')
    if not isinstance(slides,list) or not 1<=len(slides)<=40:raise ValueError('ページ・章は1〜40件です。')
    for slide in slides:
        if not isinstance(slide,dict) or set(slide)-{'title','body','narration','duration','asset_path','scene'}:raise ValueError('制作データの形式が不正です。')
        for key,limit in [('title',50),('body',1200),('narration',1500)]:
            value=slide.get(key,'')
            if not isinstance(value,str) or len(value)>limit:raise ValueError('ページの文字数上限を超えました。')
        duration=slide.get('duration',8)
        if type(duration) not in (int,float) or not 1<=duration<=180:raise ValueError('章の長さは1〜180秒です。')
    from team_document_capabilities import require_supported,runtime
    require_supported(path.suffix[1:]);python,ffmpeg=runtime()
    path.parent.mkdir(parents=True,exist_ok=True);path=output_path(root,args['path'])
    with tempfile.TemporaryDirectory(prefix='saikuru-output-',dir=path.parent) as folder:
        assets=[];render_args=dict(args,slides=[dict(s) for s in slides])
        for index,slide in enumerate(render_args['slides']):
            if not slide.get('asset_path'):continue
            name=Path(slide['asset_path'])
            if name.is_absolute() or '..' in name.parts:raise ValueError('素材は保存先内の相対パスを指定してください。')
            asset=(root/name).resolve(strict=True)
            if not asset.is_relative_to(root.resolve()) or not asset.is_file() or asset.suffix.lower() not in {'.png','.jpg','.jpeg','.mp4'}:raise ValueError('素材は保存先内のPNG・JPEG・MP4のみです。')
            if asset.stat().st_size>100*1024*1024:raise ValueError('素材は100MB以内です。')
            scene=slide.get('scene','実画面')
            if scene not in {'実画面','モデル設定','台帳','履歴'}:raise ValueError('素材のsceneは実画面・モデル設定・台帳・履歴です。')
            if len(slide.get('body',''))>140:raise ValueError('実画面を使う章の本文は140字以内にしてください。')
            copied=Path(folder)/('asset-'+str(index)+asset.suffix.lower());shutil.copyfile(asset,copied)
            slide['asset_path']=str(copied)
            assets.append({'path':str(asset.relative_to(root.resolve())),'sha256':hashlib.sha256(copied.read_bytes()).hexdigest(),'scene':scene,'slide':index+1})
        spec=Path(folder)/'input.json';generated=Path(folder)/('result'+path.suffix)
        spec.write_text(json.dumps(render_args,ensure_ascii=False),encoding='utf-8')
        result=subprocess.run([python,'-X','utf8',str(Path(__file__).with_name('document_media_worker.py')),str(spec),str(generated),ffmpeg or ''],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=1500,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode or not generated.is_file():raise ValueError('成果物の生成に失敗しました。VOICEVOX・話者・文字量・制作環境を確認してください。完成扱いにはしません。')
        if generated.stat().st_size>500*1024*1024:raise ValueError('成果物は500MB以内です。')
        with path.open('xb') as dest,generated.open('rb') as src:shutil.copyfileobj(src,dest)
    record={'path':str(path.relative_to(root)),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size,'narration':all(bool(s.get('narration','').strip()) for s in slides),'visual_review':'未確認'}
    record['assets']=assets
    if job_id:
        manifest=root/('.saikuru-output-'+job_id+'.json')
        if manifest.is_symlink():raise ValueError('成果物管理ファイルにリンクは使えません。')
        records=json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else []
        records.append(record);manifest.write_text(json.dumps(records,ensure_ascii=False),encoding='utf-8')
    return record

def main():
    source, output = (Path(p).resolve(strict=True) for p in sys.argv[1:3])
    writable = '--write' in sys.argv[3:]
    job_id=sys.argv[sys.argv.index('--job-id')+1] if '--job-id' in sys.argv else ''
    if job_id and not re.fullmatch('[a-f0-9]{32}',job_id):raise ValueError('Invalid job ID')
    if source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError('読み取り元と保存先は分離してください。')
    tools = list(TOOLS) + [{'name': 'read_document', 'description': 'Read a document in the approved output folder.',
        'inputSchema': {'type':'object','properties':{'path':{'type':'string'}},'required':['path'],'additionalProperties':False}}]
    tools.append({'name':'media_environment','description':'Check local movie renderer and list available VOICEVOX speaker names/style IDs. No settings changes.', 'inputSchema':{'type':'object','properties':{},'additionalProperties':False}})
    if writable:
        tools.append({'name':'generate_media','description':'Generate editable PPTX, slide PDF, or MP4 from structured slides. VOICEVOX narration uses the local engine and explicit speaker_id. Existing output is never overwritten. Return paths and hashes; viewing/listening remains unverified.',
            'inputSchema':{'type':'object','properties':{'path':{'type':'string'},'speaker_id':{'type':'integer'},'slides':{'type':'array','minItems':1,'maxItems':40,'items':{'type':'object','properties':{'title':{'type':'string'},'body':{'type':'string'},'narration':{'type':'string'},'duration':{'type':'number'},'asset_path':{'type':'string','description':'Relative PNG/JPEG/MP4 path in approved output folder; integrated in this slide'},'scene':{'type':'string','enum':['実画面','モデル設定','台帳','履歴']}},'required':['title','body'],'additionalProperties':False}}},'required':['path','slides'],'additionalProperties':False}})
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
                value=(media_environment() if name=='media_environment' else generate_media(output,args,writable,job_id) if name=='generate_media' else document_operation(output,name,args,writable) if name in ('read_document','write_document') else execute(source,name,args))
                result={'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}]}
            else: raise ValueError('Unsupported operation')
            reply={'jsonrpc':'2.0','id':msg['id'],'result':result}
        except Exception as exc:
            reply={'jsonrpc':'2.0','id':msg.get('id'),'error':{'code':-32602,'message':str(exc)}}
        print(json.dumps(reply,ensure_ascii=False),flush=True)

if __name__=='__main__': main()
