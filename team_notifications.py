"""Opt-in local Windows notifications with durable, private summaries."""
import os
import subprocess
import threading
import time
from team_worktime import business_segments, DEFAULT


def items(store):
    jobs = {j['id']:j for j in store.all('job')}
    tasks = store.all('task'); out = {}
    ended = {'cancelled', 'accepted', 'accepted_with_pending_checks'}
    for a in store.all('approval'):
        if a['status'] == 'pending' and jobs.get(a['job_id'], {}).get('status') not in ended:
            out['approval:'+a['id']] = 'completion' if a['kind']=='completion' else 'decision'
    for task in tasks:
        if jobs.get(task['job_id'], {}).get('status') not in ended and task['status'] in {'failed','blocked','interrupted'}:
            out[f"task:{task['id']}:{task.get('attempt',0)}:{task['status']}"] = 'decision'
    for job in jobs.values():
        if job['status'] == 'blocked':
            review = [t for t in tasks if t['job_id']==job['id'] and t['status']=='succeeded' and (t.get('result') or {}).get('status')=='needs_changes']
            if review:
                out['review:'+review[-1]['id']] = 'decision'
        elif job['status'] in {'accepted','accepted_with_pending_checks'}:
            out['accepted:'+job['id']] = 'accepted'
    return out


def windows_summary(counts, preview=False):
    if os.name != 'nt':
        raise OSError('Windows以外では常駐PC通知に対応していません。')
    # No request text, project path, user name, or model content enters this process.
    message = '判断待ち {decision}件、成果確認 {completion}件、成果受領 {accepted}件。作業ボードで確認してください。'.format(**counts)
    if preview:
        message = '常駐PC通知の表示テストです。依頼の開始や承認は行っていません。'
    script = ("Add-Type -AssemblyName System.Windows.Forms; "
              "$sairaiIcon=New-Object System.Windows.Forms.NotifyIcon; "
              "$sairaiIcon.Icon=[System.Drawing.SystemIcons]::Information; "
              "$sairaiIcon.Visible=$true; "
              "$sairaiIcon.ShowBalloonTip(8000,'采来 — サイクル —','"+message+"',[System.Windows.Forms.ToolTipIcon]::Info); "
              "Start-Sleep -Seconds 9; $sairaiIcon.Dispose()")
    import base64
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-WindowStyle','Hidden','-EncodedCommand',encoded],
                   timeout=20, check=True, capture_output=True,
                   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))


class Notifications:
    def __init__(self, app, deliver=windows_summary):
        self.app, self.deliver = app, deliver
        self.error = ''; self.last_attempt = 0
        self.thread = None

    def tick(self, current=None):
        current = time.time() if current is None else current
        store = self.app.store
        active = items(store)
        with store.atomic():
            existing = {n['key']:n for n in store.all('notification')}
            for key, kind in active.items():
                if key not in existing:
                    # Do not replay old accepted requests when first enabled.
                    job = next((j for j in store.all('job') if key=='accepted:'+j['id']), None)
                    status = 'ignored' if job and job['updated_at'] < current-60 else 'pending'
                    store.put('notification', {'id':'notification:'+key,'key':key,'category':kind,'status':status,'created_at':current})
            for key, n in existing.items():
                if key not in active and n['status']=='pending':
                    store.update(n['id'], status='obsolete')
        settings = self.app.config.get('notifications', {})
        if not settings.get('windows_enabled'):
            return
        cfg = self.app.config.get('business_hours', DEFAULT)
        if settings.get('business_hours_only', True) and not business_segments(current,current+1,cfg):
            return
        pending = [n for n in store.all('notification') if n['status']=='pending']
        if not pending or current-self.last_attempt < 60:
            return
        self.last_attempt = current
        counts = {k:sum(n['category']==k for n in pending) for k in ('decision','completion','accepted')}
        try:
            self.deliver(counts)
            with store.atomic():
                for n in pending:store.update(n['id'],status='delivered',delivered_at=current)
            self.error = ''
        except (OSError, subprocess.SubprocessError):
            self.error = 'PC通知を送信できません。ログイン中のWindowsセッションと通知設定を確認してください。画面内の通知は利用できます。'

    def snapshot(self):
        return dict(self.app.config.get('notifications', {'windows_enabled':False,'business_hours_only':True}),
                    error=self.error,pending=sum(n['status']=='pending' for n in self.app.store.all('notification')))

    def start(self):
        def loop():
            while not self.app.engine.shutdown.is_set():
                try:self.tick()
                except Exception:self.error='常駐通知の確認に失敗しました。画面内の通知を確認してください。'
                self.app.engine.shutdown.wait(15)
        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def stop(self):
        if self.thread:
            self.thread.join(timeout=22)
