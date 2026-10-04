"""Bounded child-process parser. No network, shell content execution, or AI calls."""
import base64
import csv
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import warnings
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

from attachment_guard import check, MAX_TEXT

IMAGES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp'}
TEXT = {'.txt', '.md', '.markdown', '.log', '.csv', '.json', '.yaml', '.yml', '.py', '.js', '.ts', '.tsx', '.jsx', '.cs', '.java', '.c', '.cpp', '.h', '.hpp', '.css', '.html', '.htm', '.sql', '.xml', '.toml', '.ini', '.cfg', '.conf', '.sh', '.ps1', '.bat', '.cmd', '.rb', '.go', '.rs', '.vue', '.svelte'}
OFFICE = {'.docx': 'word/document.xml', '.xlsx': 'xl/workbook.xml', '.pptx': 'ppt/presentation.xml'}
ALLOWED = set(IMAGES) | TEXT | set(OFFICE) | {'.pdf'}
MAX_BYTES = 5 * 1024 * 1024


class Refused(ValueError):
    pass


def decode(raw):
    if b'\x00' in raw and not raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        raise Refused('バイナリを含むテキストは検査できません。UTF-8へ変換してください。')
    for encoding in (['utf-16'] if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else ['utf-8-sig', 'cp932']):
        try:
            value = raw.decode(encoding)
            if any(ord(c)<32 and c not in '\n\r\t' for c in value):
                raise Refused('制御文字を含むテキストは受け付けません。')
            return value
        except UnicodeError:
            continue
    raise Refused('テキストの文字コードを検査できません。UTF-8へ変換してください。')


class Parser:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sairai-attachment-')
        self.paths = []
        self.parts = []
        self.total = 0

    def add(self, text):
        self.total += len(text) + 1
        if self.total > MAX_TEXT:
            raise Refused('添付の抽出内容が15万文字を超えるため全文を検査できません。分割してください。')
        check(text)
        self.parts.append(text)

    def image(self, raw, keep=False):
        from PIL import Image, ImageOps
        Image.MAX_IMAGE_PIXELS = 16_000_000
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as opened:
                if opened.format not in ('PNG','JPEG','WEBP') or getattr(opened,'n_frames',1) != 1:
                    raise Refused('画像は静止PNG・JPEG・WebPのみです。')
                if opened.width>8192 or opened.height>8192 or opened.width*opened.height>16_000_000:
                    raise Refused('画像が検査可能な寸法を超えています。')
                for key,value in opened.info.items():
                    if isinstance(value, str): self.add(value)
                for value in opened.getexif().values():
                    if isinstance(value, str): self.add(value)
                opened.load()
                image = ImageOps.exif_transpose(opened).convert('RGB')
                # Inspect tiled full-resolution pixels, including text visible on alpha.
                if 'A' in opened.getbands():
                    rgba=ImageOps.exif_transpose(opened).convert('RGBA')
                    base=Image.new('RGB',rgba.size,'white');base.paste(rgba,mask=rgba.getchannel('A'));image=base
                image.info.clear()  # Do not carry EXIF/ICC/text into the normalized PNG.
                self.pixels(image)
                if keep:
                    output=io.BytesIO();image.save(output,format='PNG')
                    if len(output.getvalue())>MAX_BYTES:
                        raise Refused('検査用に正規化した画像が5MBを超えます。画像を小さくしてください。')
                    return {'mime':'image/png','image':base64.b64encode(output.getvalue()).decode('ascii'),
                            'width':image.width,'height':image.height}
        return None

    def pixels(self, image):
        from PIL import Image
        if image.width*image.height>16_000_000:
            raise Refused('描画された画像が検査上限を超えました。')
        for top in range(0,image.height,1700):
            for left in range(0,image.width,1700):
                if len(self.paths)>=40:
                    raise Refused('画像・PDF・Officeの検査領域数が40を超えました。分割してください。')
                region=image.crop((left,top,min(left+1900,image.width),min(top+1900,image.height)))
                name=Path(self.temp.name)/(str(len(self.paths))+'.png');region.convert('RGB').save(name)
                self.paths.append(str(name.resolve()))

    def ocr(self):
        if not self.paths:return
        if os.name != 'nt':raise Refused('画像のローカルOCRが利用できないため添付を拒否しました。')
        exe=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
        completed=subprocess.run([str(exe),'-NoProfile','-NonInteractive','-File',str(Path(__file__).with_name('attachment_ocr.ps1'))],
            input=json.dumps({'paths':self.paths}),capture_output=True,text=True,encoding='utf-8',timeout=100,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:result=json.loads(completed.stdout)
        except ValueError:raise Refused('画像OCRの結果を確認できないため添付を拒否しました。')
        if completed.returncode or result.get('ok') is not True or not isinstance(result.get('texts'),list) or len(result['texts'])!=len(self.paths)*2:
            raise Refused('日本語・英語の画像OCRが利用できないため添付を拒否しました。')
        for text in result['texts']:self.add(text)

    def office(self, raw, extension):
        try:archive=zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:raise Refused('Office文書の形式を確認できません。')
        with archive:
            info=archive.infolist();names=[part.filename for part in info]
            if len(info)>1500 or len(set(names))!=len(names) or sum(p.file_size for p in info)>30*1024*1024:
                raise Refused('Office文書の展開上限を超えたため拒否しました。')
            if OFFICE[extension] not in names or '[Content_Types].xml' not in names:
                raise Refused('拡張子とOffice文書の内容が一致しません。')
            for part in info:
                name=part.filename;low=name.casefold()
                if part.flag_bits&1 or part.file_size>10*1024*1024 or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:
                    raise Refused('暗号化・不正な構造・展開上限のOffice文書を拒否しました。')
                if any(token in low for token in ('vbaproject','embeddings/','activex/','externallinks/','customui/','webextensions/')):
                    raise Refused('マクロ・埋め込みファイル・外部データ接続を含むOffice文書は拒否します。')
                if part.is_dir():continue
                data=archive.read(part)
                if low.endswith(('.xml','.rels')):
                    text=decode(data)
                    if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():raise Refused('外部実体宣言を含む文書は拒否します。')
                    element=ET.fromstring(text)
                    # Includes notes/comments/hidden cells/alt text and relationship destinations.
                    self.add('[Office part '+name+']\n'+' '.join(element.itertext()))
                    if re.search(r'(?i)(?:DDE|WEBSERVICE|RTD|CALL|EXEC|REGISTER)\s*\(', ' '.join(element.itertext())):
                        raise Refused('外部呼び出し・実行を求める数式を含むOffice文書は拒否します。')
                    attributes=' '.join(value for e in element.iter() for value in e.attrib.values())
                    self.add(attributes)
                    check(text)
                    for e in element.iter():
                        if e.tag.endswith('Relationship') and e.get('TargetMode')=='External' and not e.get('Type','').endswith('/hyperlink'):
                            raise Refused('外部参照を読み込むOffice文書は拒否します。')
                elif Path(low).suffix in IMAGES or '/media/' in low:
                    if Path(low).suffix not in IMAGES:raise Refused('検査できない画像・動画・音声を含むOffice文書は拒否します。')
                    self.image(data)
                else:
                    # Unknown binary parts must not silently escape inspection.
                    raise Refused('検査できない構成要素を含むOffice文書は拒否します。PDFまたはテキストへ変換してください。')

    def pdf(self, raw):
        from pypdf import PdfReader
        import pypdfium2 as pdfium
        if not raw.startswith(b'%PDF-'):raise Refused('PDFの形式を確認できません。')
        reader=PdfReader(io.BytesIO(raw),strict=True)
        if reader.is_encrypted:raise Refused('パスワード付きPDFは拒否します。')
        if len(reader.pages)>20:raise Refused('PDFは全文検査のため20ページまでです。分割してください。')
        prohibited={'/JS','/JavaScript','/AA','/OpenAction','/Launch','/SubmitForm','/ImportData','/EmbeddedFiles','/EF','/RichMedia','/XFA','/Sound','/Movie'}
        visited=set()
        def visit(value,depth=0):
            if depth>40 or len(visited)>10000:raise Refused('PDFの構造を検査できる上限を超えました。')
            ident=id(value)
            if ident in visited:return
            visited.add(ident)
            if hasattr(value,'get_object'):value=value.get_object()
            if isinstance(value,dict):
                if prohibited.intersection(value):raise Refused('実行アクション・埋め込みファイルを含むPDFは拒否します。')
                for key,item in value.items():
                    if str(key)=='/S' and str(item) in prohibited:raise Refused('実行アクションを含むPDFは拒否します。')
                    visit(item,depth+1)
            elif isinstance(value,(list,tuple)):
                for item in value:visit(item,depth+1)
            elif isinstance(value,str):check(value)
        visit(reader.trailer)
        for index,page in enumerate(reader.pages):self.add(f'\n[PDF page {index+1}]\n'+(page.extract_text() or ''))
        document=pdfium.PdfDocument(raw)
        try:
            for index in range(len(document)):
                page=document[index];bitmap=None
                try:
                    width,height=page.get_size()
                    if width<=0 or height<=0 or max(width,height)>2500:raise Refused('PDFのページ寸法が検査上限を超えました。')
                    bitmap=page.render(scale=2);self.pixels(bitmap.to_pil())
                finally:
                    if bitmap is not None:bitmap.close()
                    page.close()
        finally:document.close()

    def run(self, raw, extension):
        output={'kind':'text','mime':'text/plain','width':None,'height':None}
        if extension in IMAGES:output.update(self.image(raw,keep=True),kind='image')
        elif extension in OFFICE:self.office(raw,extension)
        elif extension=='.pdf':self.pdf(raw)
        elif extension in TEXT:
            text=decode(raw)
            if extension=='.json':json.loads(text)
            self.add(text)
        else:raise Refused('許可されていない添付ファイル形式です。')
        self.ocr()
        text='\n'.join(self.parts);check(text)
        if output['kind']=='text' and not text.strip():raise Refused('添付内容を抽出できないため拒否しました。')
        output['text']=text
        return output


def main():
    parser=None
    try:
        request=json.loads(sys.stdin.read(7_100_000));raw=base64.b64decode(request['data'],validate=True)
        if not 0<len(raw)<=MAX_BYTES:raise Refused('添付は1ファイル5MB以内です。')
        parser=Parser();output=parser.run(raw,request['extension'])
        print(json.dumps({'ok':True,'prepared':output},ensure_ascii=False))
    except Refused as exc:print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False))
    except ValueError as exc:
        message=str(exc)
        safe=message if message.startswith(('プロンプトインジェクションの疑い','指示を隠す記述','添付の全文を検査','検査できない長いエンコード')) else '添付の内容を解析できないため拒否しました。'
        print(json.dumps({'ok':False,'error':safe},ensure_ascii=False))
    except Exception:print(json.dumps({'ok':False,'error':'添付の全文検査に失敗したため拒否しました。対応する解析環境またはファイル形式を確認してください。'},ensure_ascii=False))
    finally:
        if parser is not None:parser.temp.cleanup()


if __name__=='__main__':main()
