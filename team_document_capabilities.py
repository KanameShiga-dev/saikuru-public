"""Local renderer capability and per-request deliverable gate."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import re
from pathlib import Path

SUPPORTED = {'md', 'html', 'txt', 'svg', 'pptx', 'pdf', 'mp4', 'docx', 'xlsx'}
MEDIA = {'pptx', 'pdf', 'mp4'}
OFFICE = {'docx', 'xlsx'}
BINARY = MEDIA | OFFICE
ROOT=Path(__file__).resolve().parent

def required_visuals(job):
    """Keep explicit screen requirements separate from the file extension."""
    explicit=job.get('document_media_requirements',{}).get('required_scenes',[])
    if explicit:return list(explicit)
    goal=job.get('goal','')
    if not re.search(r'実画面|実際の画面|画面中心|画面素材|操作動画|撮影素材|スクリーンショット|スクショ',goal):return []
    scenes=[name for name in ('モデル設定','台帳','履歴') if name in goal or (name=='モデル設定' and 'モデル' in goal)]
    return scenes or ['実画面']

def runtime():
    config=ROOT/'data'/'attachment-runtime.json'
    python=json.loads(config.read_text(encoding='utf-8-sig')).get('python') if config.exists() else sys.executable
    if not python or not Path(python).is_file():raise ValueError('資料生成用Pythonが見つかりません。')
    return python,shutil.which('ffmpeg')


def require_supported(format_name, goal=''):
    if format_name not in SUPPORTED:raise ValueError('成果物形式を選択してください。対応はMD・HTML・TXT・SVG・PowerPoint・PDF・MP4・Word・Excelです。')
    if format_name in OFFICE:
        python,_=runtime()
        module={'docx':'docx','xlsx':'openpyxl'}[format_name]
        check=subprocess.run([python,'-c','import '+module],capture_output=True,timeout=15,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if check.returncode:raise ValueError('指定形式の制作ライブラリが利用できません。依頼を開始しません。')
    if format_name in MEDIA:
        python,ffmpeg=runtime()
        module={'pptx':'pptx','pdf':'reportlab','mp4':'PIL'}[format_name]
        check=subprocess.run([python,'-c','import '+module+'; import PIL'],capture_output=True,timeout=15,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if check.returncode:raise ValueError('指定形式の制作ライブラリが利用できません。依頼を開始しません。')
        if not Path('C:/Windows/Fonts/meiryo.ttc').is_file():raise ValueError('日本語フォントが見つかりません。')
        if format_name=='mp4' and not ffmpeg:raise ValueError('FFmpegが見つかりません。動画依頼を開始しません。')
        if format_name=='mp4' and re.search('VOICEVOX|ナレーション|音声',goal,re.I):
            try:
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open('http://127.0.0.1:50021/speakers',timeout=3) as response:
                    if not json.loads(response.read(1000000)):raise ValueError('話者が取得できません。')
            except (OSError,ValueError) as exc:raise ValueError('VOICEVOXが利用できません。音声付き動画は開始しません。ローカルのVOICEVOXを起動してください。') from exc
    return format_name

def require_artifacts(job):
    expected=job.get('document_format')
    if expected not in BINARY:return
    root=Path(job['project']).resolve();manifest=root/('.saikuru-output-'+job['id']+'.json')
    if not manifest.is_file() or manifest.is_symlink():raise ValueError('指定された成果物が未作成です。完了扱いにできません。')
    for record in json.loads(manifest.read_text(encoding='utf-8')):
        path=(root/record['path']).resolve()
        if path.is_relative_to(root) and path.suffix=='.'+expected and path.is_file() and path.stat().st_size>0:
            assets=record.get('assets',[])
            required=required_visuals(job)
            covered={a.get('scene') for a in assets}
            if required and (not assets or any(scene!='実画面' and scene not in covered for scene in required)):continue
            if any(not (root/a['path']).resolve().is_relative_to(root) or not (root/a['path']).is_file() or hashlib.sha256((root/a['path']).read_bytes()).hexdigest()!=a['sha256'] for a in assets):continue
            if expected=='mp4' and re.search('VOICEVOX|ナレーション|音声',job.get('goal',''),re.I) and not record.get('narration'):continue
            if hashlib.sha256(path.read_bytes()).hexdigest()==record['sha256']:return
    raise ValueError('指定成果物が存在しない・変更された、または必須の実画面素材が本編に組み込まれていません。文字スライドだけへの置き換えでは完了できません。')

def require_plan(job,result):
    format_name=job.get('document_format')
    if not format_name:return
    require_supported(format_name,job.get('goal',''))
    if format_name in OFFICE:
        tasks=result.get('tasks',[])
        if not any(t.get('role')=='builder' and 'generate_office' in t.get('instruction','') and format_name in t.get('instruction','').lower() for t in tasks):
            raise ValueError('計画に指定成果物の実制作工程がありません。builderのgenerate_officeで'+format_name+'を作成（既存ファイルの編集はbase_pathを指定して新しい名前で保存）する工程と、完成物の確認工程を計画してください。')
        return
    if format_name in MEDIA:
        tasks=result.get('tasks',[])
        if not any(t.get('role')=='builder' and 'generate_media' in t.get('instruction','') and format_name in t.get('instruction','').lower() for t in tasks):
            raise ValueError('計画に指定成果物の実制作工程がありません。builderのgenerate_mediaで'+format_name+'を生成する工程と、完成物の確認工程を計画してください。台本・手順書だけでは依頼を達成しません。')
        required=required_visuals(job)
        if required and not any(t.get('role')=='builder' and 'generate_media' in t.get('instruction','') and 'asset_path' in t.get('instruction','') and all(scene in t.get('instruction','') for scene in required) for t in tasks):
            raise ValueError('実画面を使う要件が制作計画にありません。asset_pathで必須素材（'+ '・'.join(required)+'）を本編に統合する工程が必要です。素材がない場合はblockedとして受領を待ってください。')

def environment_check(format_name,goal,output):
    require_supported(format_name,goal)
    root=Path(output).resolve(strict=True)
    if not root.is_dir() or not os.access(root,os.W_OK):raise ValueError('資料保存先に書き込めません。')
    checks={'format':format_name,'python_libraries':'available','output_folder':'available','font':'available','checked_at':__import__('time').time()}
    if format_name=='mp4':
        _,ffmpeg=runtime()
        result=subprocess.run([ffmpeg,'-hide_banner','-encoders'],capture_output=True,timeout=15,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode or b'libx264' not in result.stdout or b' aac ' not in result.stdout:raise ValueError('MP4制作に必要なH.264/AACエンコーダーがありません。')
        checks.update(ffmpeg='available',encoders='libx264/aac',voicevox='available' if re.search('VOICEVOX|ナレーション|音声',goal,re.I) else 'not_required')
    return checks
