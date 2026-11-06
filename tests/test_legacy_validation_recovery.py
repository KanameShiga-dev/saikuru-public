"""A legacy validation stop must expose an explicit, bounded recovery path."""
import json
import io
from email.message import Message
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace

from team_config import default_config
from team_engine import Engine, REPAIR_RULE
from team_store import Store


class LegacyValidationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.db.close)
        config = default_config()
        config.update(p0_contract_enabled=False, approved_roots=[self.temp.name])
        self.engine = Engine(self.store, config, {}, "http://127.0.0.1:1")
        self.addCleanup(self.engine.handoff.close)
        self.addCleanup(self.engine.shutdown.set)
        self.job = self.store.put("job", {
            "id": "legacy", "title": "Synthetic validation", "goal": "Synthetic goal",
            "project": self.temp.name, "status": "blocked", "auto_execute": True,
            "validation_commands": ["python -m unittest"], "security_review_required": True,
        })
        self.task = self.engine.new_task(
            self.job, "Validate", "Synthetic validation", "builder",
            repair=config["max_repairs"])
        self.task = self.store.update(
            self.task["id"], status="succeeded",
            result={"status": "needs_changes", "summary": "Synthetic failing check"},
            finished_at=10)
        self.engine._queue_reviews(self.job, self.task["id"], self.task["repair"])
        for task in self.store.for_job("task", self.job["id"]):
            if task["role"] == "reviewer":
                self.store.update(task["id"], status="cancelled")

    def test_explicit_repair_preserves_history_and_queues_fresh_reviews(self):
        before = self.store.get(self.task["id"])
        cancelled = [t["id"] for t in self.store.for_job("task", self.job["id"])
                     if t["role"] == "reviewer"]
        limit = self.engine.config["max_repairs"]
        result = self.engine.repair_review(self.task["id"], "Only fix the synthetic failing check")
        fix = self.store.get(result["task_id"])
        self.assertEqual(self.store.get(self.task["id"]), before)
        self.assertEqual(self.engine.config["max_repairs"], limit)
        self.assertEqual(fix["repair"], limit + 1)
        self.assertIn("Synthetic failing check", fix["instruction"])
        self.assertTrue(fix["instruction"].endswith(REPAIR_RULE))
        self.assertEqual(fix["status"], "queued")
        self.assertEqual(self.store.get(self.job["id"])["status"], "running")
        self.assertTrue(all(self.store.get(i)["status"] == "cancelled" for i in cancelled))
        reviews = [t for t in self.store.for_job("task", self.job["id"])
                   if t.get("review_cycle") == fix["id"]]
        self.assertEqual({t["agent_name"] for t in reviews},
                         {"common-reviewer", "common-security-reviewer"})
        with self.assertRaises(ValueError):
            self.engine.repair_review(self.task["id"], "Do not duplicate the repair")

    def test_stale_source_is_rejected_without_writes(self):
        newer = self.engine.new_task(self.job, "Newer validation", "Synthetic", "builder",
                                     repair=self.task["repair"] + 1)
        self.store.update(newer["id"], status="succeeded",
                          result={"status": "needs_changes", "summary": "New result"})
        count = len(self.store.for_job("task", self.job["id"]))
        with self.assertRaises(ValueError):
            self.engine.repair_review(self.task["id"], "Stale request")
        self.assertEqual(len(self.store.for_job("task", self.job["id"])), count)

    def test_a_failed_extra_repair_returns_to_an_explicit_decision(self):
        result = self.engine.repair_review(self.task["id"], "One synthetic repair")
        fix = self.store.update(
            result["task_id"], status="succeeded",
            result={"status": "needs_changes", "summary": "Still unresolved"}, finished_at=20)
        for task in self.store.for_job("task", self.job["id"]):
            if task.get("review_cycle") == fix["id"]:
                self.store.update(task["id"], status="cancelled")
        job = self.store.update(self.job["id"], status="blocked")
        self.assertEqual(self.engine.stalled_validation(job)["id"], fix["id"])
        with self.assertRaises(ValueError):
            self.engine.repair_review(self.task["id"], "Cannot reuse the old decision")
        self.assertFalse(any(t["status"] == "queued"
                             for t in self.store.for_job("task", self.job["id"])))

    def test_active_or_pending_work_cannot_be_bypassed(self):
        review = next(t for t in self.store.for_job("task", self.job["id"])
                      if t["role"] == "reviewer")
        for status in ("queued", "running", "awaiting_approval", "failed"):
            with self.subTest(status=status):
                self.store.update(review["id"], status=status)
                self.assertIsNone(self.engine.stalled_validation(self.job))
                with self.assertRaises(ValueError):
                    self.engine.repair_review(self.task["id"], "Synthetic scope")
        self.store.update(review["id"], status="cancelled")
        self.engine.new_approval(self.task, "question", {"summary": "Synthetic question"})
        self.assertIsNone(self.engine.stalled_validation(self.job))

    def test_new_contracts_and_missing_validation_do_not_use_legacy_recovery(self):
        for fields in ({"p0_contract_version": 1}, {"workflow_contract_version": 1},
                       {"validation_commands": []}):
            with self.subTest(fields=fields):
                self.assertIsNone(self.engine.stalled_validation(dict(self.job, **fields)))

    def test_confirmation_scope_is_required(self):
        for note in ("", " ", "x" * 4001):
            with self.subTest(note_length=len(note)):
                with self.assertRaises(ValueError):
                    self.engine.repair_review(self.task["id"], note)

    def test_existing_reviewer_repair_path_still_works(self):
        review = self.engine.new_task(
            self.job, "Review", "Synthetic review", "reviewer", repair=2)
        self.store.update(review["id"], status="succeeded",
                          result={"status": "needs_changes", "summary": "Synthetic review finding"})
        result = self.engine.repair_review(review["id"], "Only fix the review finding")
        fix = self.store.get(result["task_id"])
        self.assertIn("Synthetic review finding", fix["instruction"])
        self.assertEqual(fix["after"], review["id"])
        self.assertEqual(fix["title"], "利用者指定のレビュー指摘を修正する")

    def test_api_requires_authenticated_explicit_confirmation(self):
        from server import Handler

        def request(confirmed, authenticated=True):
            body = json.dumps({"id": self.task["id"], "note": "Synthetic bounded repair",
                               "checked_changes": confirmed}).encode()
            handler = Handler.__new__(Handler)
            handler.server = SimpleNamespace(app=SimpleNamespace(
                engine=self.engine, store=self.store, port=8790,
                origin="http://127.0.0.1:8790", cookie="synthetic-cookie"))
            handler.path = "/api/tasks/repair-review"
            handler.headers = Message()
            for key, value in {
                "Host": "127.0.0.1:8790", "Content-Type": "application/json",
                "Content-Length": str(len(body)), "X-Agent-Team-UI": "1",
                "Cookie": "agent_team=" + ("synthetic-cookie" if authenticated else "wrong"),
            }.items():
                handler.headers[key] = value
            handler.rfile = io.BytesIO(body)
            responses = []
            handler.send = lambda status, value: responses.append((status, value))
            handler.do_POST()
            return responses[0]

        count = len(self.store.for_job("task", self.job["id"]))
        self.assertEqual(request(True, authenticated=False)[0], 403)
        self.assertEqual(request(False)[0], 400)
        self.assertEqual(len(self.store.for_job("task", self.job["id"])), count)
        status, result = request(True)
        self.assertEqual(status, 200)
        self.assertEqual(self.store.get(result["task_id"])["status"], "queued")

    def test_ui_routes_only_the_last_validation_to_user_decisions(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for JavaScript behavior checks")
        script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('web\\app.js','utf8');
const tasks=JSON.parse(process.argv[1]),job=JSON.parse(process.argv[2]);
const context={state:{tasks,jobs:[job],approvals:[],config:{max_repairs:2}},
  pending:id=>context.state.approvals.filter(a=>a.task_id===id&&a.status==='pending'),
  endedJob:()=>false,p0Phase:()=>null,pendingUser:()=>[],maintenanceHeld:()=>false,
  workflowMaintenanceWait:()=>false};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function stalledReview('),
  source.indexOf('const completedFolds')),context);
const target=tasks.find(t=>t.role==='builder');
assert.equal(context.stalledValidation(job).id,target.id);
assert.equal(context.group(target),2);
assert.equal(tasks.filter(t=>context.group(t)===2).length,1);
assert.match(context.stopCause(target,job),/検証指摘.*上限/);
job.status='running';assert.equal(context.stalledValidation(job),null);
job.status='blocked';target.result.status='done';
assert.equal(context.stalledValidation(job),null);
target.result.status='needs_changes';job.p0_contract_version=1;
assert.equal(context.stalledValidation(job),null);
delete job.p0_contract_version;
context.state.approvals.push({task_id:target.id,status:'pending'});
assert.equal(context.stalledValidation(job),null);
"""
        tasks = self.store.for_job("task", self.job["id"])
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [node, "-e", script, json.dumps(tasks), json.dumps(self.job)],
            cwd=root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
