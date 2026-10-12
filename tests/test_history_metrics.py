import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from team_worktime import DEFAULT, split_seconds, validate
from team_waits import collect, statistics
from team_store import Store
from team_history import consumption
from team_notifications import Notifications, items


def ts(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone(timedelta(hours=9))).timestamp()


class WorktimeTests(unittest.TestCase):
    def test_examples(self):
        for a,b,expected in [('2026-10-05T21:00','2026-10-06T09:10',600),
                             ('2026-10-05T21:00','2026-10-06T15:00',21600),
                             ('2026-10-05T17:30','2026-10-06T10:00',5400),
                             ('2026-10-05T19:00','2026-10-05T20:00',0),
                             ('2026-10-02T17:00','2026-10-05T10:00',7200)]:
            with self.subTest(a=a):self.assertEqual(split_seconds(ts(a),ts(b),DEFAULT)[0],expected)
    def test_holiday(self):
        cfg=dict(DEFAULT,holidays=['2026-10-05'])
        self.assertEqual(split_seconds(ts('2026-10-05T10:00'),ts('2026-10-06T10:00'),cfg)[0],3600)
    def test_bounds(self):
        self.assertEqual(split_seconds(100,99,DEFAULT),(0,0))
        with self.assertRaises(ValueError):split_seconds(0,401*86400,DEFAULT)
        for value in [dict(DEFAULT,start='25:00'),dict(DEFAULT,end='08:00'),dict(DEFAULT,workdays=[True]),dict(DEFAULT,holidays=['2026-02-30'])]:
            with self.assertRaises(ValueError):validate(value)


class WaitTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        with patch('team_store.now',return_value=100):self.store=Store(self.temp.name)
        self.addCleanup(self.store.db.close)
        self.at=200
        self.put('job',{'id':'j','created_at':101,'status':'running'})
    def put(self,kind,obj):
        with patch('team_store.now',return_value=self.at):return self.store.put(kind,obj)
    def update(self,id,**values):
        with patch('team_store.now',return_value=self.at):return self.store.update(id,**values)
    def test_no_duplicate_and_atomic_rollback(self):
        self.at=300;self.update('j',status='blocked');self.update('j',status='blocked')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM transitions').fetchone()[0],2)
        with self.assertRaises(RuntimeError):
            with self.store.atomic():self.update('j',status='running');raise RuntimeError()
        self.assertEqual(self.store.get('j')['status'],'blocked')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM transitions').fetchone()[0],2)
    def test_overlap_expiry_retry_one_interval(self):
        self.at=300;self.put('task',{'id':'t','job_id':'j','status':'running'})
        self.put('approval',{'id':'a','job_id':'j','task_id':'t','kind':'tool','status':'pending'})
        self.at=400;self.update('t',status='awaiting_approval')
        self.at=500;self.update('j',status='blocked');self.update('a',status='expired');self.update('t',status='failed')
        self.at=600;self.update('t',status='queued');self.update('j',status='running')
        w=collect(self.store,self.store.get('j'),DEFAULT,700)
        self.assertEqual(len(w['intervals']),1);self.assertEqual(w['intervals'][0]['reason'],'tool')
        self.assertEqual(w['inside_seconds']+w['outside_seconds'],200);self.assertEqual(w['intervals'][0]['closed_at'],600)
    def test_restart_excluded_and_terminal_closes_task(self):
        self.at=300;self.update('j',status='interrupted')
        self.at=400;self.update('j',status='running')
        w=collect(self.store,self.store.get('j'),DEFAULT,500)
        self.assertEqual(w['system_seconds'],100);self.assertEqual(w['outside_seconds'],0)
        self.at=500;self.put('task',{'id':'t','job_id':'j','status':'awaiting_approval'})
        self.at=600;self.update('j',status='cancelled')
        w=collect(self.store,self.store.get('j'),DEFAULT,800)
        self.assertFalse(w['intervals'][-1]['open']);self.assertEqual(w['intervals'][-1]['closed_at'],600)
    def test_old_unknown(self):
        self.update('j',created_at=99,status='blocked')
        self.assertFalse(collect(self.store,self.store.get('j'))['measured'])
    def test_failed_request_waits_for_retry(self):
        self.at=300;self.update('j',status='failed');self.at=500;self.update('j',status='running')
        w=collect(self.store,self.store.get('j'),DEFAULT,600)
        self.assertEqual(w['intervals'][0]['reason'],'task_error')
        self.assertEqual(w['intervals'][0]['closed_at'],500)
    def test_plan_kind_before_approval_record(self):
        self.at=300;self.update('j',status='awaiting_approval')
        self.put('approval',{'id':'a','job_id':'j','task_id':'t','kind':'plan','status':'pending'})
        w=collect(self.store,self.store.get('j'),DEFAULT,400)
        self.assertEqual(w['intervals'][0]['reason'],'plan')
    def test_stats_clips(self):
        self.at=300;self.update('j',status='blocked');self.at=600;self.update('j',status='running')
        d=statistics(self.store,DEFAULT,400,500,700)
        self.assertEqual(d['rows'][0]['max_seconds'],100);self.assertEqual(d['rows'][0]['count'],1)
    def test_service_notifications_deferred_and_deduplicated(self):
        self.put('task',{'id':'t','job_id':'j','status':'failed','attempt':1})
        sent=[];app=SimpleNamespace(store=self.store,config={'business_hours':DEFAULT,'notifications':{'windows_enabled':True,'business_hours_only':True}})
        service=Notifications(app,sent.append)
        service.tick(ts('2026-10-05T21:00'));self.assertEqual(sent,[])
        service.tick(ts('2026-10-06T09:00'));service.tick(ts('2026-10-06T09:10'))
        self.assertEqual(len(sent),1);self.assertEqual(sent[0]['decision'],1)
        other=Notifications(app,sent.append);other.tick(ts('2026-10-06T09:20'));self.assertEqual(len(sent),1)
    def test_notification_handles_legacy_missing_report(self):
        self.put('task',{'id':'t','job_id':'j','status':'succeeded','result':None})
        self.update('j',status='blocked')
        self.assertEqual(items(self.store),{})


class TokenTests(unittest.TestCase):
    def test_failed_retry_model_and_latest_duplicate(self):
        t={'attempt':3,'agent_run_history':[{'id':'1','provider':'codex','model':'a','status':'failed','usage':{'inputTokens':10,'outputTokens':2}},
            {'id':'2','provider':'claude','model':'b','usage':{'input_tokens':20,'cache_read_input_tokens':5,'cache_creation_input_tokens':4,'output_tokens':3}},
            {'id':'3','usage':{'inputTokens':1}}], 'agent_run':{'id':'3','usage':{'inputTokens':8}}}
        c=consumption(t)
        self.assertEqual((c['input'],c['cached'],c['cache_write'],c['output']),(38,5,4,5))
        self.assertEqual(c['expected_runs'],3);self.assertEqual(len(c['runs']),3)
    def test_missing_is_not_zero(self):
        c=consumption({'attempt':2,'agent_run':{'id':'a','status':'failed','usage':{'inputTokens':True}}})
        self.assertIsNone(c['input']);self.assertEqual(c['measured_runs'],0);self.assertEqual(c['expected_runs'],2)


if __name__=='__main__':unittest.main()
