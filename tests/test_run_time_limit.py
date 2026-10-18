"""The task run-time limit counts working time only. Time spent waiting for a person's decision is not counted.

2026-10-18: a builder waiting for tool approvals was stopped by the 30-minute limit although nothing was running.
Synthetic fake clock and context; no provider, database or network.
"""
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import team_engine
from team_engine import Context, ProviderError


def fake_context(started, waited=0.0, wait_began=None, limit=1800):
    return SimpleNamespace(
        cancel=threading.Event(), started=started, waited=waited, wait_began=wait_began,
        engine=SimpleNamespace(shutdown=threading.Event(), config={'task_timeout_seconds': limit}))


class RunTimeLimitTests(unittest.TestCase):
    def check(self, ctx, at):
        with mock.patch.object(team_engine, 'now', return_value=at):
            Context.check(ctx)

    def test_working_time_over_the_limit_still_stops(self):
        with self.assertRaises(ProviderError):
            self.check(fake_context(started=0), at=1801)

    def test_working_time_within_the_limit_continues(self):
        self.check(fake_context(started=0), at=1799)

    def test_finished_waits_are_not_counted(self):
        # 40 minutes in total, 25 of them waiting for approvals: 15 minutes of work.
        self.check(fake_context(started=0, waited=1500), at=2400)

    def test_a_wait_in_progress_is_not_counted(self):
        # Waiting since t=600 and now t=3000: only the first 600 seconds were work.
        self.check(fake_context(started=0, wait_began=600), at=3000)

    def test_work_after_a_long_wait_still_counts(self):
        # 10 min work, 25 min wait, then 21 more minutes of work: 31 minutes of work in total.
        with self.assertRaises(ProviderError):
            self.check(fake_context(started=0, waited=1500), at=1500 + 600 + 1260)


if __name__ == '__main__':
    unittest.main()
