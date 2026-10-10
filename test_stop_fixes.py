"""2026-10-08 stop-path fixes: web-search allowlist for generic folder names, read_document for read-only roles,
and waiting out provider usage limits instead of failing the job."""
import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import consultation_read_tools
import document_tools
import team_web_guard
from team_adapters import ADAPTERS, ProviderError
from team_config import default_config
from team_engine import Engine
from team_recovery import usage_limit_retry_at
from team_store import Store


class WebGuardAllowlistTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.policy = Path(folder.name) / 'websearch-policy.json'
        patcher = mock.patch.object(team_web_guard, 'POLICY', self.policy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_generic_folder_name_does_not_block_searches(self):
        self.assertEqual(team_web_guard.check_query('Claude Code skills best practices', r'C:\Projects\skills'), (True, ''))

    def test_identifying_project_name_is_still_blocked(self):
        allowed, reason = team_web_guard.check_query('saikuru-workstyle の使い方', r'C:\Projects\saikuru-workstyle')
        self.assertFalse(allowed)
        self.assertIn('固有名', reason)

    def test_ascii_name_matches_whole_word_only(self):
        self.assertTrue(team_web_guard.check_query('knowledgeStores pattern', r'C:\Projects\knowledgeStore')[0])
        self.assertFalse(team_web_guard.check_query('knowledgeStore pattern', r'C:\Projects\knowledgeStore')[0])

    def test_allowed_terms_exempt_only_the_name_check(self):
        self.policy.write_text(json.dumps({'allowed_terms': ['saikuru-workstyle']}), encoding='utf-8')
        self.assertTrue(team_web_guard.check_query('saikuru-workstyle の使い方', r'C:\Projects\saikuru-workstyle')[0])
        # Paths and e-mail addresses are still refused.
        self.assertFalse(team_web_guard.check_query(r'C:\Users\someone\secret.txt', r'C:\Projects\skills')[0])
        self.assertFalse(team_web_guard.check_query('mail taro@example.com', r'C:\Projects\skills')[0])


class ReadDocumentBrokerTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        from PIL import Image
        Image.new('RGB', (300, 200), 'white').save(self.root / 'guide.pdf')
        (self.root / '.env').mkdir()
        Image.new('RGB', (300, 200), 'white').save(self.root / '.env' / 'hidden.pdf')

    def test_reads_project_pdf_as_text(self):
        with mock.patch.object(document_tools, '_ocr_pdf_pages', return_value=['采来の使い方']):
            value = consultation_read_tools.execute(self.root, 'read_document', {'path': 'guide.pdf'})
        self.assertEqual(value['pages'][0]['text'], '采来の使い方')
        self.assertEqual(value['path'], 'guide.pdf')

    def test_lists_documents_and_keeps_exclusions(self):
        listing = consultation_read_tools.execute(self.root, 'list_files', {})
        self.assertEqual(listing['documents'], ['guide.pdf'])
        with self.assertRaises(ValueError):
            consultation_read_tools.execute(self.root, 'read_document', {'path': '.env/hidden.pdf'})
        with self.assertRaises(ValueError):
            consultation_read_tools.execute(self.root, 'read_document', {'path': '../outside.pdf'})

    def test_tool_is_announced(self):
        self.assertIn('read_document', [t['name'] for t in consultation_read_tools.TOOLS])


class UsageLimitParseTest(unittest.TestCase):
    def test_reset_time_plus_five_minutes(self):
        now = datetime(2026, 10, 8, 13, 55).timestamp()
        at = usage_limit_retry_at("Claude Code: You've hit your session limit · resets 3pm (Asia/Tokyo)", now)
        self.assertEqual(datetime.fromtimestamp(at), datetime(2026, 10, 8, 15, 5))

    def test_past_reset_time_means_tomorrow_and_unknown_time_means_30_minutes(self):
        now = datetime(2026, 10, 8, 16, 0).timestamp()
        self.assertEqual(datetime.fromtimestamp(usage_limit_retry_at('hit your session limit · resets 3pm', now)),
                         datetime(2026, 10, 9, 15, 5))
        self.assertEqual(usage_limit_retry_at('usage limit reached', now), now + 1800)

    def test_other_errors_are_not_waited_out(self):
        self.assertIsNone(usage_limit_retry_at('Claude Code CLIの認証状態を確認できません。'))


PLAN = {'summary': '計画', 'tasks': [{'title': '実装', 'instruction': '実装する', 'role': 'builder'}],
        'security_review_required': False}
DONE = {'status': 'done', 'summary': '完了', 'checks': ['確認'], 'question': ''}


class UsageLimitWaitTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        config = default_config()
        config['approved_roots'] = [self.temp.name]
        config['security_review'] = 'planner'
        self.engine = Engine(self.store, config, {'codex': ['fixture'], 'claude': ['fixture']}, 'http://127.0.0.1:1')
        self.saved = ADAPTERS.copy()
        self.limit_hits = 1
        test = self

        class LimitedAdapter:
            def run(s, ctx, prompt, schema):
                if ctx.task['role'] == 'planner':
                    return PLAN
                if ctx.task['role'] == 'builder' and test.limit_hits:
                    test.limit_hits -= 1
                    raise ProviderError("Claude Code: You've hit your session limit · resets 3pm (Asia/Tokyo)")
                return DONE
        ADAPTERS.update(codex=LimitedAdapter, claude=LimitedAdapter)

    def tearDown(self):
        self.engine.shutdown.set()
        deadline = time.time() + 3
        while self.engine.active and time.time() < deadline:
            time.sleep(.01)
        ADAPTERS.update(self.saved)
        self.engine.handoff.close()
        self.store.db.close()
        self.temp.cleanup()

    def builder(self):
        return next((t for t in self.store.all('task') if t['role'] == 'builder'), None)

    def run_until(self, predicate):
        deadline = time.time() + 6
        while not predicate() and time.time() < deadline:
            self.engine.tick()
            time.sleep(.01)
        self.assertTrue(predicate())

    def test_waits_for_reset_then_retries_automatically(self):
        job = self.engine.create_job('sample', 'goal', self.temp.name, True)
        self.run_until(lambda: self.builder() and self.builder().get('failure_code') == 'usage_limit_wait')
        task = self.builder()
        self.assertEqual(task['status'], 'queued')
        self.assertGreater(task['retry_at'], time.time())
        self.assertEqual(self.store.get(job['id'])['status'], 'running')  # the job is not failed
        attempts = task['attempt']
        for _ in range(20):
            self.engine.tick()
            time.sleep(.01)
        self.assertEqual(self.builder()['attempt'], attempts)  # not started before the reset time
        self.store.update(task['id'], retry_at=time.time() - 1)  # the reset time has come
        self.run_until(lambda: self.builder()['status'] == 'succeeded')

    def test_gives_up_after_three_waits(self):
        self.limit_hits = 10
        self.engine.create_job('sample', 'goal', self.temp.name, True)
        waiting = lambda: self.builder() and self.builder()['id'] not in self.engine.active and (
            self.builder()['status'] == 'failed' or (self.builder().get('retry_at') or 0) > time.time())
        for _ in range(5):
            self.run_until(waiting)
            if self.builder()['status'] == 'failed':
                break
            self.store.update(self.builder()['id'], retry_at=time.time() - 1)  # the reset time has come
        self.assertEqual(self.builder()['status'], 'failed')
        self.assertEqual(self.builder()['quota_waits'], 3)


if __name__ == '__main__':
    unittest.main()
