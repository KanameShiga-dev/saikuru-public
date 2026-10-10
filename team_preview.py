"""Page images of a job's deliverables (PPTX / DOCX / PDF) for quick acceptance on the work board.

PPTX: PowerPoint (COM) exports each slide; DOCX: Word (COM) saves a PDF; PDF: pypdfium2 renders pages.
Office files are screened first (no macros, external links or embedded objects). A PowerPoint that the user
already had open is left running. Images go to data/previews/<job>/<key>/pNNN.png. Nothing is sent anywhere.
"""
import base64
import hashlib
import json
import os
import sys
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from team_config import ROOT

PREVIEWS = ROOT / 'data' / 'previews'
KINDS = {'.pptx', '.docx', '.pdf'}
MAX_PAGES = 40
WIDTH = 960
SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', '.harness', 'claude-artifacts', 'design'}
_lock = threading.Lock()
_running = set()


def _key(path):
    if path.stat().st_size > 50 * 1024 * 1024:
        raise ValueError('プレビューは50MB以下の資料に対応しています。')
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest()[:16]


def deliverables(job, limit=10):
    """Files to preview: the job's generated outputs, else PPTX/DOCX/PDF changed in the project since the job began."""
    root = Path(job['project']).resolve()
    found = []
    manifest = root / ('.saikuru-output-' + job['id'] + '.json')
    if manifest.is_file() and not manifest.is_symlink():
        try:
            for record in json.loads(manifest.read_text(encoding='utf-8')):
                path = (root / record['path']).resolve()
                if path.suffix.lower() in KINDS and path.is_relative_to(root) and path.is_file():
                    found.append(path)
        except (ValueError, KeyError, OSError):
            pass
    if not found:
        since = job.get('created_at', 0)
        seen = 0
        for directory, directories, filenames in os.walk(root, followlinks=False):
            directories[:] = [d for d in directories if d not in SKIP_DIRS and not d.startswith('.')
                and not (Path(directory) / d).is_symlink()
                and not getattr(Path(directory) / d, 'is_junction', lambda: False)()
                and (Path(directory) / d).resolve().is_relative_to(root)]
            seen += len(directories) + len(filenames)
            if seen > 10000:
                break
            for name in filenames:
                path = Path(directory) / name
                if name.startswith(('~$', '.')) or path.is_symlink():
                    continue
                if (path.suffix.lower() in KINDS and path.resolve().is_relative_to(root)
                        and path.is_file() and path.stat().st_size <= 50 * 1024 * 1024
                        and path.stat().st_mtime >= since):
                    found.append(path)
                if len(found) >= limit:
                    break
            if len(found) >= limit:
                break
    return sorted(set(found), key=lambda p: p.stat().st_mtime)[:limit]


def _powershell(script, timeout):
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-EncodedCommand', encoded],
                   timeout=timeout, check=True, capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def _ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def _render_pdf_local(pdf, out):
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument(str(pdf))
    try:
        count = min(len(document), MAX_PAGES)
        for index in range(count):
            page = document[index]
            width = page.get_size()[0]
            page.render(scale=WIDTH / width).to_pil().convert('RGB').save(out / ('p%03d.png' % (index + 1)))
            page.close()
        return count
    finally:
        document.close()


def _render_pdf(pdf, out):
    from team_document_capabilities import runtime
    python, _ = runtime()
    result = subprocess.run([python, '-X', 'utf8', str(Path(__file__).resolve()), '--render-pdf', str(pdf), str(out)],
                            capture_output=True, timeout=120, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('資料用PythonでPDFを画像化できませんでした。PDFと描画環境を確認してください。')
    return int(result.stdout.decode('ascii').strip())


def render(job_id, path):
    """Render one file; returns the record written to meta.json."""
    if not re.fullmatch(r'[a-f0-9]{32}', str(job_id)):
        raise ValueError('依頼IDが不正です。')
    from document_tools import _safe_office_package
    path = Path(path)
    key = _key(path)
    out = PREVIEWS / job_id / key
    meta = out / 'meta.json'
    if meta.is_file():
        return json.loads(meta.read_text(encoding='utf-8'))
    out.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    record = {'file': path.name, 'path': str(path), 'key': key, 'kind': suffix[1:], 'created_at': time.time()}
    try:
        if suffix in ('.pptx', '.docx'):
            _safe_office_package(path)
        if suffix == '.pptx':
            script = (
                "$ErrorActionPreference='Stop';"
                "$was=[bool](Get-Process POWERPNT -ErrorAction SilentlyContinue);"
                "$app=New-Object -ComObject PowerPoint.Application;"
                "$p=$app.Presentations.Open(" + _ps_quote(path) + ",$true,$false,$false);"
                "try{$n=[Math]::Min($p.Slides.Count," + str(MAX_PAGES) + ");"
                "for($i=1;$i -le $n;$i++){$p.Slides.Item($i).Export((Join-Path " + _ps_quote(out) + " ('p{0:D3}.png' -f $i)),'PNG'," + str(WIDTH) + "," + str(WIDTH * 9 // 16) + ")}}"
                "finally{$p.Close();if(-not $was -and $app.Presentations.Count -eq 0){$app.Quit()}}")
            _powershell(script, 240)
            record['pages'] = len(list(out.glob('p*.png')))
        elif suffix == '.docx':
            with tempfile.TemporaryDirectory(prefix='saikuru-preview-') as folder:
                pdf = Path(folder) / 'doc.pdf'
                script = (
                    "$ErrorActionPreference='Stop';"
                    "$was=[bool](Get-Process WINWORD -ErrorAction SilentlyContinue);"
                    "$app=New-Object -ComObject Word.Application;if(-not $was){$app.Visible=$false;$app.DisplayAlerts=0};"
                    "try{$d=$app.Documents.Open(" + _ps_quote(path) + ",$false,$true,$false);"
                    "$d.SaveAs2(" + _ps_quote(pdf) + ",17);$d.Close($false)}finally{if(-not $was -and $app.Documents.Count -eq 0){$app.Quit()}}")
                _powershell(script, 240)
                record['pages'] = _render_pdf(pdf, out)
        elif suffix == '.pdf':
            record['pages'] = _render_pdf(path, out)
        record['status'] = 'done'
    except Exception as exc:
        record.update(status='failed', error=str(exc)[:300] if not isinstance(exc, subprocess.CalledProcessError)
                      else 'Officeでの画像化に失敗しました（Office が使えない、またはファイルが開けない）。')
    meta.write_bytes(json.dumps(record, ensure_ascii=False).encode('utf-8'))
    return record


def start(job):
    """Render the job's deliverables in the background (once per file version)."""
    with _lock:
        if job['id'] in _running:
            return
        _running.add(job['id'])

    def work():
        try:
            for path in deliverables(job):
                render(job['id'], path)
        finally:
            with _lock:
                _running.discard(job['id'])
    threading.Thread(target=work, name='preview-' + job['id'][:8], daemon=True).start()


def snapshot(job):
    """Preview status for the board: rendering flag and per-file pages."""
    files = []
    for path in deliverables(job):
        key = _key(path)
        meta = PREVIEWS / job['id'] / key / 'meta.json'
        if meta.is_file():
            record = json.loads(meta.read_text(encoding='utf-8'))
            files.append({k: record.get(k) for k in ('file', 'key', 'kind', 'pages', 'status', 'error')})
        else:
            files.append({'file': path.name, 'key': key, 'kind': path.suffix[1:].lower(), 'status': 'pending'})
    with _lock:
        rendering = job['id'] in _running
    return {'rendering': rendering, 'files': files}


def image(job_id, key, page):
    if not re.fullmatch(r'[a-f0-9]{32}', job_id) or not re.fullmatch(r'[a-f0-9]{16}', key) or not 1 <= int(page) <= MAX_PAGES:
        raise ValueError('画像の指定が不正です。')
    path = PREVIEWS / job_id / key / ('p%03d.png' % int(page))
    if not path.is_file():
        raise ValueError('画像がありません。')
    return path.read_bytes()


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] != '--render-pdf':
        raise SystemExit('Expected --render-pdf PDF OUTPUT_DIRECTORY')
    print(_render_pdf_local(Path(sys.argv[2]), Path(sys.argv[3])))
