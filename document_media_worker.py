"""Fixed local renderer. Inputs are data, never commands, URLs or executable code."""
import io
import json
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import wave
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def render(spec, target, ffmpeg):
    slides=spec['slides']
    font=Path('C:/Windows/Fonts/meiryo.ttc')
    if not font.is_file(): raise ValueError('日本語フォントが見つかりません。')
    title_font=ImageFont.truetype(str(font),52)
    body_font=ImageFont.truetype(str(font),32)
    with tempfile.TemporaryDirectory(prefix='saikuru-media-',dir=target.parent) as folder:
        temp=Path(folder); images=[]
        for index,slide in enumerate(slides):
            image=Image.new('RGB',(1920,1080),'#f5f8f7');draw=ImageDraw.Draw(image)
            draw.rectangle((0,0,1920,24),fill='#176b5b')
            for line_index,offset in enumerate(range(0,len(slide['title']),30)):
                draw.text((100,80+line_index*65),slide['title'][offset:offset+30],font=title_font,fill='#143d38')
            asset=slide.get('asset_path')
            if asset:
                source=Path(asset)
                if source.suffix=='.mp4':
                    if target.suffix!='.mp4':raise ValueError('操作動画素材の統合はMP4のみです。')
                    source=temp/f'asset-{index}.png'
                    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-nostdin','-i',asset,'-frames:v','1',str(source)],check=True,timeout=30,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                with Image.open(source) as original:
                    visual=original.convert('RGB');visual.thumbnail((1720,620));image.paste(visual,((1920-visual.width)//2,210+(620-visual.height)//2))
                draw.text((100,160),'実画面素材',font=body_font,fill='#176b5b')
            y=850 if asset else 230
            for line in slide['body'].splitlines():
                while line:
                    part=line[:48];line=line[48:]
                    if y>1020:raise ValueError('本文がページに収まりません。ページを分けてください。')
                    draw.text((100,y),part,font=body_font,fill='#203c39');y+=52
                y+=12
            frame=temp/f'{index:03}.png';image.save(frame);images.append(frame)
        if target.suffix=='.pptx':
            from pptx import Presentation
            from pptx.util import Inches, Pt
            prs=Presentation();prs.slide_width=Inches(16);prs.slide_height=Inches(9)
            for slide in slides:
                page=prs.slides.add_slide(prs.slide_layouts[6])
                asset=slide.get('asset_path')
                if asset:
                    with Image.open(asset) as im:aw,ah=im.size
                    scale=min(14.4/aw,5.2/ah)
                    page.shapes.add_picture(asset,Inches((16-aw*scale)/2),Inches(1.8),width=Inches(aw*scale),height=Inches(ah*scale))
                for text,x,y,w,h,size in [(slide['title'],.8,.6,14.4,1,30),(slide['body'],.8,7.1 if asset else 1.9,14.4,1.6 if asset else 6.5,18 if asset else 22)]:
                    box=page.shapes.add_textbox(Inches(x),Inches(y),Inches(w),Inches(h))
                    box.text_frame.word_wrap=True
                    for index,line in enumerate(text.splitlines()):
                        paragraph=box.text_frame.paragraphs[0] if index==0 else box.text_frame.add_paragraph()
                        paragraph.text=line;paragraph.font.name='Meiryo';paragraph.font.size=Pt(size)
            prs.save(target)
        elif target.suffix=='.pdf':
            from reportlab.pdfgen import canvas
            from reportlab.lib.utils import ImageReader
            pdf=canvas.Canvas(str(target),pagesize=(960,540))
            for frame in images:pdf.drawImage(ImageReader(str(frame)),0,0,960,540);pdf.showPage()
            pdf.save()
        else:
            if not ffmpeg:raise ValueError('FFmpegが見つかりません。')
            clips=[]
            # Explicit localhost only. Do not inherit proxy settings for narration.
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for index,(slide,frame) in enumerate(zip(slides,images)):
                narration=slide.get('narration','').strip()
                wav=temp/f'{index:03}.wav'
                if narration:
                    speaker=spec.get('speaker_id')
                    if type(speaker) is not int or not 0<=speaker<=10000:raise ValueError('VOICEVOX話者IDを指定してください。')
                    query=urllib.parse.urlencode({'text':narration,'speaker':speaker})
                    req=urllib.request.Request('http://127.0.0.1:50021/audio_query?'+query,data=b'',method='POST')
                    with opener.open(req,timeout=30) as r:audio_query=r.read(1000000)
                    req=urllib.request.Request('http://127.0.0.1:50021/synthesis?speaker='+str(speaker),data=audio_query,headers={'Content-Type':'application/json'},method='POST')
                    with opener.open(req,timeout=90) as r:raw=r.read(30000001)
                    if len(raw)>30000000:raise ValueError('音声サイズの上限を超えました。')
                    wav.write_bytes(raw)
                    with wave.open(io.BytesIO(raw)) as audio:duration=audio.getnframes()/audio.getframerate()
                else:duration=slide.get('duration',8)
                if not 1<=duration<=180:raise ValueError('章の長さは1〜180秒にしてください。')
                clip=temp/f'{index:03}.mp4';clips.append(clip)
                args=[ffmpeg,'-hide_banner','-loglevel','error','-nostdin','-loop','1','-i',str(frame)]
                asset=slide.get('asset_path')
                moving=bool(asset and Path(asset).suffix=='.mp4')
                if moving:args+=['-i',asset]
                if narration:args+=['-i',str(wav)]
                if moving:
                    args+=['-filter_complex','[1:v]scale=1720:620:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=1720:620:(ow-iw)/2:(oh-ih)/2:color=0xf5f8f7,tpad=stop_mode=clone:stop_duration=180[v];[0:v][v]overlay=100:210[out]','-map','[out]']
                    if narration:args+=['-map','2:a']
                args+=['-t',str(duration),'-r','24','-c:v','libx264','-preset','veryfast','-pix_fmt','yuv420p']
                if narration:args+=['-c:a','aac']
                args+=[str(clip)]
                subprocess.run(args,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=240,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if len({bool(s.get('narration','').strip()) for s in slides})>1:raise ValueError('音声あり・なしの章を混在させないでください。')
            concat=temp/'clips.txt';concat.write_text('\n'.join("file '"+p.name+"'" for p in clips),encoding='utf-8')
            subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-nostdin','-f','concat','-safe','1','-i',str(concat),'-c','copy','-movflags','+faststart',str(target)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=90,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))


if __name__=='__main__':
    render(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')),Path(sys.argv[2]),sys.argv[3])
