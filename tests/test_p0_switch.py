"""p0_contract_enabled: whether a new development request carries the P0 contract.

2026-10-19: every new full-route request became a P0 job, and a P0 job that stops cannot be resumed without a
maintenance credential that a one-person setup does not have. The production folder keeps this off until a way to
resume from the screen exists. Default stays on, so isolated P0 runtimes and the P0 tests behave as before.
Synthetic data only; no provider is started.
"""
import tempfile
import unittest

from team_config import default_config, validate_config
from team_engine import Engine
from team_store import Store


class P0SwitchTests(unittest.TestCase):
    def make_engine(self, **overrides):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = Store(temp.name)
        config = default_config()
        config.update(approved_roots=[temp.name], **overrides)
        engine = Engine(store, config, {'codex': ['fixture'], 'claude': ['fixture']}, 'http://127.0.0.1:1')

        def close():
            engine.shutdown.set()
            engine.active.clear()
            engine.handoff.close()
            store.db.close()
        self.addCleanup(close)
        return engine, temp.name

    def test_the_default_keeps_the_p0_contract_on(self):
        self.assertIs(default_config()['p0_contract_enabled'], True)
        engine, project = self.make_engine()
        job = engine.create_job('sample', 'local goal', project, True)
        self.assertEqual(job.get('p0_contract_version'), 1)
        self.assertIn('goal_contract', job)

    def test_turning_it_off_makes_a_request_a_legacy_job(self):
        engine, project = self.make_engine(p0_contract_enabled=False)
        job = engine.create_job('sample', 'local goal', project, True)
        self.assertNotIn('p0_contract_version', job)
        self.assertNotIn('goal_contract', job)
        self.assertEqual(job['g2_gate_version'], 1)  # the approval gates do not depend on the P0 contract

    def test_a_config_without_the_key_means_on(self):
        config = default_config()
        del config['p0_contract_enabled']
        validate_config(config)
        engine, project = self.make_engine()
        del engine.config['p0_contract_enabled']
        job = engine.create_job('sample', 'local goal', project, True)
        self.assertEqual(job.get('p0_contract_version'), 1)

    def test_a_non_boolean_value_is_refused(self):
        for value in ('false', 0, None, 'no'):
            with self.subTest(value=value):
                config = default_config()
                config['p0_contract_enabled'] = value
                with self.assertRaises(ValueError):
                    validate_config(config)


if __name__ == '__main__':
    unittest.main()
