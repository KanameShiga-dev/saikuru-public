"""W05.05.UI: requests shaped like web/app.js sends them, replayed, stale or tampered.

The real loopback Handler must refuse them without changing state (hiding a control in the
UI is not the guard), and web/app.js must turn every refusal into Japanese text instead of
a raw code. Fictional fixtures only; no Provider, no user data. Display texts are produced
by running workflowErrorText from web/app.js under node; the display tests skip if node
is unavailable.
"""
import copy
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

from tests import test_workflow_api as feature_api
from tests import test_workflow_controls as controls
from tests import test_plan_revision as revision

APP_JS = Path(__file__).resolve().parents[3] / 'web' / 'app.js'
RAW_CODE = re.compile(r'^[a-z_]+$')
JAPANESE = re.compile(r'[぀-ヿ一-鿿]')


def display(results):
    """Text the UI shows for each refusal body: workflowErrorText(result) || result.error."""
    node = shutil.which('node')
    if not node:
        raise unittest.SkipTest('node is required to evaluate web/app.js')
    source = APP_JS.read_text(encoding='utf-8')
    parts = [re.search(r'const WORKFLOW_ERROR_LABELS=\{.*?\n\};', source, re.S),
             re.search(r'const WORKFLOW_ERROR_FAMILIES=\[.*?\]\];', source, re.S),
             re.search(r'function workflowErrorText\(result\)\{.*?\n\}', source, re.S)]
    if not all(parts):
        raise AssertionError('web/app.js no longer defines the refusal display helpers')
    script = '\n'.join(p.group(0) for p in parts) + (
        '\nconst input=JSON.parse(require("fs").readFileSync(0,"utf8"));'
        'process.stdout.write(JSON.stringify(input.map(r=>workflowErrorText(r)||r.error||"")));')
    out = subprocess.run([node, '-e', script], input=json.dumps(results), capture_output=True,
                         text=True, encoding='utf-8', timeout=30, check=True)
    return json.loads(out.stdout)


class RefusalDisplayMixin:
    def assertShownInJapanese(self, refusals):
        for (name, body), text in zip(refusals, display([body for _, body in refusals])):
            with self.subTest(case=name, text=text):
                self.assertTrue(text)
                self.assertIsNone(RAW_CODE.fullmatch(text))
                self.assertRegex(text, JAPANESE)


class FeatureRefusalTests(RefusalDisplayMixin, unittest.TestCase):
    def setUp(self):
        self.api = feature_api.WorkflowApiTests('test_authenticated_prepare_decide_and_duplicate_refusal')
        self.api.setUp(); self.addCleanup(self.api.doCleanups)
        self.f = self.api.f

    def ui_decide(self, review, approval_id, allow):
        # Same fields as featureDecisionForm in web/app.js, which reads the digest from the approval payload.
        digest = self.f.store.get(approval_id, 'approval')['payload']['feature_target_digest']
        self.assertEqual(digest, review['target_digest'])
        return {'approval_id': approval_id, 'allow': allow, 'note': 'Fictional UI note', 'action': 'decide_feature_review',
                'confirmation': {'action': 'feature_review_confirmation', 'target_digest': digest,
                                 'decision': 'approved' if allow else 'rejected'}}

    def test_feature_decision_replay_tamper_and_missing_header_are_refused_and_shown(self):
        review = self.f.ready_feature()
        body = self.ui_decide(review, review['approval_id'], True)
        before = self.f.state()
        refusals = []
        stale = copy.deepcopy(body); stale['confirmation']['target_digest'] = '0' * 64
        for name, value, kwargs in (('stale digest', stale, {}), ('actor', dict(body, actor='end_user'), {}),
                                    ('role', dict(body, role='maintainer'), {}), ('no UI header', body, {'ui': False})):
            status, result = self.api.request('/api/workflow/feature-review/decide', value, **kwargs)
            with self.subTest(case=name):
                self.assertIn(status, (400, 403), result)
            refusals.append((name, result))
        self.assertEqual(self.f.state(), before)
        status, result = self.api.request('/api/workflow/feature-review/decide', body)
        self.assertEqual(status, 200, result)
        after = self.f.state()
        status, replay = self.api.request('/api/workflow/feature-review/decide', body)
        self.assertEqual(status, 400, replay)
        self.assertEqual(self.f.state(), after)
        self.assertEqual(len(self.f.store.job_events(self.f.job_id, 'feature_review_decided')), 1)
        self.assertShownInJapanese(refusals + [('replay', replay)])

    def test_feature_rework_replay_old_revision_tamper_and_legacy_route_are_refused_and_shown(self):
        review = self.f.ready_feature(); self.f.approve(review)
        wp = next(w for w in self.f.engine.workflow_view(self.f.job_id)['work_packages'] if w['id'] == self.f.wp()['id'])
        # Same fields as featureReworkForm in web/app.js.
        body = {'job_id': self.f.job_id, 'work_package_id': wp['id'], 'work_package_revision': wp['state_revision'],
                'note': 'Fictional defect from the UI', 'action': 'rework_job',
                'confirmation': {'action': 'request_feature_rework', 'target_digest': wp['feature_target_digest']}}
        before = self.f.state()
        refusals = []
        for name, value in (('old revision', dict(body, work_package_revision=wp['state_revision'] - 1)),
                            ('tampered digest', dict(body, confirmation=dict(body['confirmation'], target_digest='0' * 64))),
                            ('actor', dict(body, actor='end_user'))):
            status, result = self.api.request('/api/workflow/feature-rework', value)
            with self.subTest(case=name):
                self.assertEqual(status, 400, result)
            refusals.append((name, result))
        status, legacy = self.api.request('/api/jobs/rework', {'id': self.f.job_id, 'note': 'Fictional legacy bypass'})
        self.assertEqual(status, 400, legacy)
        self.assertEqual(self.f.state(), before)
        status, result = self.api.request('/api/workflow/feature-rework', body)
        self.assertEqual(status, 200, result)
        after = self.f.state()
        status, replay = self.api.request('/api/workflow/feature-rework', body)
        self.assertEqual(status, 400, replay)
        self.assertEqual(self.f.state(), after)
        self.assertShownInJapanese(refusals + [('legacy route', legacy), ('replay', replay)])


class HiddenControlRefusalTests(RefusalDisplayMixin, unittest.TestCase):
    """The UI hides retry/confirmation while maintenance is required; the API must still refuse them."""

    def setUp(self):
        self.c = controls.WorkflowControlTests('test_confirm_requires_maintenance_capability_and_never_queues')
        self.c.setUp(); self.addCleanup(self.c.doCleanups)

    def test_hidden_retry_and_confirmation_from_the_browser_session_are_refused(self):
        before = self.c.state()
        refusals = []
        for name, path, body in (('retry', '/api/workflow/control', self.c.body('retry_task')),
                                 ('confirm', '/api/workflow/control', self.c.body('confirm_recovery')),
                                 ('legacy retry', '/api/retry', {'id': self.c.task_id, 'checked_changes': True, 'note': 'x',
                                                                 'workflow_target': self.c.target('retry_task')}),
                                 ('role field', '/api/workflow/control', dict(self.c.body('retry_task'), role='maintainer'))):
            status, result = self.c.request(path, body)
            with self.subTest(case=name):
                self.assertIn(status, (400, 403), result)
            refusals.append((name, result))
        self.assertEqual(self.c.state(), before)
        self.assertShownInJapanese(refusals)


class PlanConfirmationRefusalTests(RefusalDisplayMixin, unittest.TestCase):
    def setUp(self):
        self.r = revision.RevisionTests('test_rejection_does_not_publish_or_create_builders')
        self.r.setUp(); self.addCleanup(self.r.doCleanups)

    def test_wrong_missing_or_replayed_plan_confirmation_is_refused_and_shown(self):
        request = self.r.api_server()
        approval = self.r.approval
        # Same fields as the plan approval button in web/app.js.
        body = {'id': approval['id'], 'allow': True, 'note': 'Fictional UI note',
                'workflow_confirmation': approval['workflow_revision_digest']}
        before = self.r.state()
        refusals = []
        for name, value in (('wrong digest', dict(body, workflow_confirmation='0' * 64)),
                            ('missing confirmation', {k: v for k, v in body.items() if k != 'workflow_confirmation'})):
            status, result = request('/api/decide', value)
            with self.subTest(case=name):
                self.assertEqual(status, 400, result)
            refusals.append((name, result))
        self.assertEqual(self.r.state(), before)
        status, result = request('/api/decide', body)
        self.assertEqual(status, 200, result)
        after = self.r.state()
        status, replay = request('/api/decide', body)
        self.assertEqual(status, 400, replay)
        self.assertEqual(self.r.state(), after)
        self.assertShownInJapanese(refusals + [('replay', replay)])


if __name__ == '__main__':
    unittest.main()
