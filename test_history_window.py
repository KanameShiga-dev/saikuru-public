"""History window (2026-10-13): the screen's state holds running work and work finished within history_days (default 7);
older finished work is read through archive()."""
import time
import unittest
from types import SimpleNamespace

from server import App
from team_config import default_config, validate_config

DAY = 86400


class FakeStore:
    def __init__(self, jobs, tasks, approvals):
        self.rows = {'job': jobs, 'task': tasks, 'approval': approvals}
        self.revision = 0

    def all(self, kind):
        return list(self.rows[kind])

    def events(self):
        return []

    def get(self, object_id, kind=None):
        return next(r for r in self.rows[kind] if r['id'] == object_id)

    def for_job(self, kind, job_id):
        return [r for r in self.rows[kind] if r.get('job_id') == job_id]


def make_app(days=None):
    now = time.time()
    jobs = [{'id': 'old', 'title': '古い貸出の依頼', 'status': 'accepted', 'updated_at': now - 30 * DAY, 'goal': 'g', 'project': 'C:/p'},
            {'id': 'old-cancel', 'title': '別の依頼', 'status': 'cancelled', 'updated_at': now - 10 * DAY, 'goal': 'g', 'project': 'C:/p'},
            {'id': 'new', 'title': '新しい依頼', 'status': 'accepted', 'updated_at': now - DAY, 'goal': 'g', 'project': 'C:/p'},
            {'id': 'running', 'title': '作業中の古い依頼', 'status': 'running', 'updated_at': now - 90 * DAY, 'goal': 'g', 'project': 'C:/p'}]
    tasks = [{'id': 't-' + j['id'], 'job_id': j['id']} for j in jobs]
    approvals = [{'id': 'a-' + j['id'], 'job_id': j['id']} for j in jobs]
    config = {} if days is None else {'history_days': days}
    app = SimpleNamespace(config=config, store=FakeStore(jobs, tasks, approvals), ENDED_JOB_STATUSES=App.ENDED_JOB_STATUSES, slim_task=App.slim_task, SLIM_FIELDS=App.SLIM_FIELDS,
                          health={}, engine=SimpleNamespace(paused=False), usage=SimpleNamespace(snapshot=lambda: {}),
                          cli_update=SimpleNamespace(snapshot=lambda: {}), claude_update=SimpleNamespace(snapshot=lambda: {}),
                          copilot_update=SimpleNamespace(snapshot=lambda: {}), notifications=SimpleNamespace(snapshot=lambda: {}))
    app.history_days = lambda: App.history_days(app)
    return app


class HistoryWindowTest(unittest.TestCase):
    def test_default_hides_finished_work_older_than_a_week_but_keeps_running_work(self):
        app = make_app()
        state = App.state(app)
        self.assertEqual([j['id'] for j in state['jobs']], ['new', 'running'])
        self.assertEqual([t['id'] for t in state['tasks']], ['t-new', 't-running'])
        self.assertEqual([a['id'] for a in state['approvals']], ['a-new', 'a-running'])
        self.assertEqual(state['history'], {'days': 7, 'hidden_jobs': 2})

    def test_setting_widens_the_window(self):
        state = App.state(make_app(days=14))
        self.assertEqual([j['id'] for j in state['jobs']], ['old-cancel', 'new', 'running'])

    def test_invalid_setting_falls_back_to_seven_days(self):
        for days in (0, -1, True, '7', 99999):
            self.assertEqual(App.history_days(make_app(days=days)), 7)

    def test_archive_searches_only_older_finished_work(self):
        app = make_app()
        self.assertEqual([j['id'] for j in App.archive(app)['jobs']], ['old-cancel', 'old'])
        self.assertEqual([j['id'] for j in App.archive(app, '貸出')['jobs']], ['old'])
        self.assertEqual(App.archive(app, '存在しない')['jobs'], [])
        self.assertEqual(len(App.archive(app, limit=1)['jobs']), 1)

    def test_archive_returns_one_finished_job_with_its_tasks(self):
        detail = App.archive(make_app(), job_id='old')
        self.assertEqual((detail['job']['id'], [t['id'] for t in detail['tasks']]), ('old', ['t-old']))
        with self.assertRaisesRegex(ValueError, '終了した'):
            App.archive(make_app(), job_id='running')

    def test_archive_pagination_has_no_duplicates_and_keeps_search(self):
        app = make_app()
        first = App.archive(app, limit=1)
        second = App.archive(app, limit=1, offset=first['next_offset'])
        self.assertEqual(first['total'], 2)
        self.assertEqual([j['id'] for j in first['jobs'] + second['jobs']], ['old-cancel', 'old'])
        self.assertIsNone(second['next_offset'])
        self.assertEqual(App.archive(app, '貸出', offset=1)['jobs'], [])
        with self.assertRaises(ValueError): App.archive(app, 'x' * 201)

    def test_finished_tasks_are_slimmed_and_detail_returns_heavy_fields(self):
        app = make_app()
        heavy = {'result': 'R' * 100, 'instruction': 'I', 'agent_run': {'status': 'completed', 'log': 'x' * 100}}
        for t in app.store.rows['task']:
            t.update(heavy)
        app.task_detail = lambda task_id: App.task_detail(app, task_id)
        tasks = {t['id']: t for t in App.state(app)['tasks']}
        self.assertTrue(tasks['t-new']['slim'])
        self.assertNotIn('result', tasks['t-new'])
        self.assertEqual(tasks['t-new']['agent_run'], {'status': 'completed'})
        self.assertNotIn('slim', tasks['t-running'])
        self.assertEqual(tasks['t-running']['result'], 'R' * 100)
        detail = App.task_detail(app, 't-new')
        self.assertEqual((detail['result'], detail['instruction']), ('R' * 100, 'I'))
        self.assertEqual(detail['agent_run']['log'], 'x' * 100)

    def test_state_is_rebuilt_only_after_a_write(self):
        app = make_app()
        reads = []
        original = app.store.all
        app.store.all = lambda kind: (reads.append(kind), original(kind))[1]
        first = App.state(app)
        count = len(reads)
        self.assertEqual(App.state(app)['jobs'], first['jobs'])
        self.assertEqual(len(reads), count)
        app.store.revision += 1
        App.state(app)
        self.assertGreater(len(reads), count)

    def test_config_validation(self):
        config = default_config()
        validate_config(config)
        for days in (0, 3651, '7', True):
            config['history_days'] = days
            with self.assertRaisesRegex(ValueError, '履歴'):
                validate_config(config)
        config['history_days'] = 30
        validate_config(config)


if __name__ == '__main__':
    unittest.main()
