import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from team_config import default_config, validate_config
from team_engine import Context, Engine
from team_store import Store
from team_review_context import evaluation_mode_for_job, without_own_instruction
from routing_eval_report import summarize


class RoutingEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project = root / 'project'
        self.project.mkdir()
        (self.project / 'calculator.py').write_text('def total(values):\n    return 0\n', encoding='utf-8')
        self.store = Store(root / 'data')
        self.addCleanup(self.store.db.close)
        self.config = default_config()
        self.config['approved_roots'] = [str(self.project)]
        self.config['routing_evaluation'] = {'enabled': True, 'root': str(root)}
        self.engine = Engine(self.store, self.config, {'codex': ['dummy'], 'claude': ['dummy']}, 'http://127.0.0.1:0')
        self.addCleanup(self.engine.handoff.close)

    def create(self, **kwargs):
        settings = dict(title='架空の合計修正', goal='calculator.py の合計を修正する。', project=str(self.project),
                        auto_execute=True, evaluation_mode='A', evaluation_confirmed=True,
                        route='light_eval', light_scope_file='calculator.py',
                        light_acceptance='空配列、正の数、負の数の合計を独立テストで確認する。')
        settings.update(kwargs)
        return self.engine.create_job(**settings)

    def test_light_has_builder_and_both_reviews_without_planner(self):
        job = self.create()
        tasks = [t for t in self.store.all('task') if t['job_id'] == job['id']]
        self.assertEqual([t['role'] for t in tasks], ['builder', 'reviewer', 'reviewer'])
        self.assertEqual(tasks[1]['after'], tasks[0]['id'])
        self.assertEqual(tasks[2]['after'], tasks[1]['id'])
        self.assertEqual(tasks[2]['agent_name'], 'common-security-reviewer')
        self.assertEqual(self.store.get(job['id'])['status'], 'running')
        ctx = Context(self.engine, tasks[0])
        denied = ctx.approve({'source': 'codex', 'operation': 'item/fileChange/requestApproval',
                              'fileChanges': [{'path': 'other.py', 'kind': {'type': 'update'}, 'diff': 'x'}],
                              'details': {}})
        self.assertFalse(denied['allow'])
        denied_command = ctx.approve({'source': 'codex', 'operation': 'item/commandExecution/requestApproval',
                                      'details': {'cwd': str(self.project), 'command': 'python -c "print(1)"'}})
        self.assertFalse(denied_command['allow'])

    def test_full_keeps_planner(self):
        job = self.create(route='full')
        self.assertEqual([t['role'] for t in self.store.all('task') if t['job_id'] == job['id']], ['planner'])
        self.assertEqual(job['status'], 'planning')

    def test_light_is_fail_closed(self):
        invalid = [dict(auto_execute=False), dict(evaluation_mode=None), dict(evaluation_confirmed=False),
                   dict(light_scope_file='../other.py'), dict(light_scope_file='missing.py'),
                   dict(attachment_ids=['some-id']), dict(document_source=str(self.project)),
                   dict(light_acceptance='short')]
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.create(**change)
        self.config['security_review'] = 'planner'
        with self.assertRaises(ValueError):
            self.create()
        self.config['security_review'] = 'always'
        self.config['routing_evaluation']['enabled'] = False
        with self.assertRaises(ValueError):
            self.create()
        self.assertEqual(len(self.store.all('job')), 0)

    def test_config_gate_validates(self):
        validate_config(self.config)
        self.config['review_context_mode'] = 'on'
        validate_config(self.config)
        self.config['routing_evaluation']['enabled'] = False
        with self.assertRaises(ValueError):
            validate_config(self.config)
        self.config['routing_evaluation']['enabled'] = True
        self.config['review_context_mode'] = 'invalid'
        with self.assertRaises(ValueError):
            validate_config(self.config)
        self.config['review_context_mode'] = 'off'
        self.config['routing_evaluation'] = {'enabled': True, 'root': 'relative'}
        with self.assertRaises(ValueError):
            validate_config(self.config)

    def test_review_context_shadow_and_on_remove_only_duplicate_instruction(self):
        job = self.create()
        self.config['review_context_mode'] = 'on'
        self.assertEqual(evaluation_mode_for_job(self.config, job), 'on')
        self.assertEqual(evaluation_mode_for_job(self.config, dict(job, evaluation={})), 'off')
        self.assertEqual(evaluation_mode_for_job(self.config, dict(job, project=str(self.project.parent.parent))), 'off')
        review = next(t for t in self.store.all('task') if t['job_id'] == job['id']
                      and t['role'] == 'reviewer')
        self.engine.sync_handoff(job['id'])
        live_context = Context(self.engine, review)
        on_prompt = self.engine._instructions(live_context)
        self.assertEqual(live_context.context_refs['review_context']['mode'], 'on')
        self.config['review_context_mode'] = 'off'
        off_context = Context(self.engine, review)
        off_prompt = self.engine._instructions(off_context)
        self.config['review_context_mode'] = 'on'
        self.assertGreater(len(off_prompt), len(on_prompt))
        self.assertIn('Security by Default', off_prompt)
        self.assertIn('Security by Default', on_prompt)
        full = json.loads(self.engine.handoff.context(job, review_task_id=review['id'],
                                                      review_instruction=review['instruction']))
        shadow_refs = {}
        shadow = json.loads(self.engine.handoff.context(job, refs=shadow_refs,
                             review_task_id=review['id'], review_instruction=review['instruction'],
                             review_context_mode='shadow'))
        on_refs = {}
        compact = json.loads(self.engine.handoff.context(job, refs=on_refs,
                              review_task_id=review['id'], review_instruction=review['instruction'],
                              review_context_mode='on'))
        self.assertEqual(full, shadow)
        self.assertIn(review['id'], full['latest_task_instructions'])
        self.assertIn(review['id'], compact['latest_task_instructions'])
        self.assertEqual({key: value for key, value in full['latest_task_instructions'][review['id']].items()
                          if key != 'instruction'}, compact['latest_task_instructions'][review['id']])
        self.assertNotIn('instruction', compact['latest_task_instructions'][review['id']])
        self.assertEqual({key: value for key, value in full.items()
                          if key != 'latest_task_instructions'},
                         {key: value for key, value in compact.items()
                          if key != 'latest_task_instructions'})
        self.assertEqual(len(full['latest_task_instructions']),
                         len(compact['latest_task_instructions']))
        self.assertGreater(shadow_refs['review_context']['duplicate_instruction_chars'], 0)
        self.assertLess(on_refs['review_context']['candidate_package_chars'],
                        on_refs['review_context']['full_package_chars'])
        unchanged, removed = without_own_instruction(full, review['id'], 'different instruction')
        self.assertIs(unchanged, full)
        self.assertEqual(removed, 0)

    def test_report_separates_conditional_and_unknown(self):
        jobs = {'a': {'id': 'a', 'route': 'light_eval', 'status': 'accepted', 'evaluation': {'mode': 'A'}},
                'b': {'id': 'b', 'route': 'full', 'status': 'accepted_with_pending_checks', 'evaluation': {'mode': 'A'}}}
        rows = [{'job_id': 'a', 'role': 'builder', 'measurement': 'final', 'input_total': 10,
                 'output': 5, 'provider': 'codex', 'model': 'gpt-6-sol'},
                {'job_id': 'b', 'role': 'planner', 'measurement': 'unknown', 'input_total': None,
                 'output': None, 'provider': 'codex', 'model': 'gpt-6-sol'}]
        with patch('routing_eval_report.attempts', return_value=(rows, jobs, [])):
            result = summarize('unused', {'a': True, 'b': True})
        self.assertEqual(result['routes']['light_eval']['tokens_per_full_success'], 15)
        self.assertEqual(result['routes']['full']['full_success'], 0)
        self.assertIsNone(result['routes']['full']['total_tokens'])


if __name__ == '__main__':
    unittest.main()
