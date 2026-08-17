# -*- coding: utf-8 -*-
"""Persistence coverage for background scheduled analysis run status/events."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from src.storage import DatabaseManager, ScheduledRunStatus



class ScheduledRunStoreTestCase(unittest.TestCase):
    db: DatabaseManager

    def setUp(self) -> None:
        DatabaseManager.reset_instance()
        self.db = DatabaseManager(db_url="sqlite:///:memory:")

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()

    def test_status_upsert_and_active_listing(self) -> None:
        self.assertTrue(
            self.db.save_scheduled_run_status("run-1", "running", stock_count=5)
        )
        self.assertTrue(
            self.db.save_scheduled_run_status("run-2", "running", stock_count=3)
        )

        active = self.db.list_active_scheduled_runs()
        self.assertEqual([row["run_id"] for row in active], ["run-2", "run-1"])

        self.assertTrue(
            self.db.save_scheduled_run_status(
                "run-1", "completed", stock_count=5, completed_count=4
            )
        )
        row = self.db.get_scheduled_run_status("run-1")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["completed_count"], 4)

        active = self.db.list_active_scheduled_runs()
        self.assertEqual([row["run_id"] for row in active], ["run-2"])

    def test_reconcile_stale_running_rows_preserves_fresh_boundary_and_terminal_rows(self) -> None:
        now = datetime(2026, 8, 14, 12, 0, 0)
        for run_id in ("stale", "boundary", "fresh", "completed"):
            self.assertTrue(
                self.db.save_scheduled_run_status(run_id, "running", stock_count=4)
            )

        with self.db.get_session() as session:
            stale = session.get(ScheduledRunStatus, "stale")
            boundary = session.get(ScheduledRunStatus, "boundary")
            fresh = session.get(ScheduledRunStatus, "fresh")
            completed = session.get(ScheduledRunStatus, "completed")
            assert stale is not None
            assert boundary is not None
            assert fresh is not None
            assert completed is not None
            stale.started_at = now - timedelta(minutes=121)
            stale.last_activity_at = now - timedelta(minutes=121)
            boundary.started_at = now - timedelta(minutes=120)
            boundary.last_activity_at = now - timedelta(minutes=120)
            fresh.started_at = now - timedelta(minutes=30)
            fresh.last_activity_at = now - timedelta(minutes=30)
            completed.status = "completed"
            completed.started_at = now - timedelta(minutes=300)
            completed.last_activity_at = now - timedelta(minutes=300)
            completed.finished_at = now - timedelta(minutes=299)
            session.commit()

        reconciled = self.db.reconcile_stale_scheduled_runs(
            max_age_minutes=120,
            now=now,
        )

        self.assertEqual(reconciled, 1)
        stale_status = self.db.get_scheduled_run_status("stale")
        boundary_status = self.db.get_scheduled_run_status("boundary")
        fresh_status = self.db.get_scheduled_run_status("fresh")
        completed_status = self.db.get_scheduled_run_status("completed")
        assert stale_status is not None
        assert boundary_status is not None
        assert fresh_status is not None
        assert completed_status is not None
        self.assertEqual(stale_status["status"], "failed")
        self.assertEqual(stale_status["finished_at"], now.isoformat())
        self.assertEqual(
            stale_status["error"],
            "Scheduled run exceeded the 120-minute maximum age and was reconciled at scheduler startup.",
        )
        self.assertEqual(boundary_status["status"], "running")
        self.assertIsNone(boundary_status["finished_at"])
        self.assertEqual(fresh_status["status"], "running")
        self.assertIsNone(fresh_status["finished_at"])
        self.assertEqual(completed_status["status"], "completed")
        self.assertEqual(
            completed_status["finished_at"],
            (now - timedelta(minutes=299)).isoformat(),
        )

    def test_reconcile_orphaned_run_started_before_process_boundary(self) -> None:
        now = datetime(2026, 8, 14, 12, 0, 0)
        process_boundary = now - timedelta(minutes=5)
        for run_id in ("orphan", "current"):
            self.assertTrue(
                self.db.save_scheduled_run_status(run_id, "running", stock_count=4)
            )

        with self.db.get_session() as session:
            orphan = session.get(ScheduledRunStatus, "orphan")
            current = session.get(ScheduledRunStatus, "current")
            assert orphan is not None
            assert current is not None
            orphan.started_at = now - timedelta(minutes=10)
            orphan.last_activity_at = now - timedelta(minutes=1)
            current.started_at = now - timedelta(minutes=1)
            current.last_activity_at = now - timedelta(minutes=1)
            session.commit()

        reconciled = self.db.reconcile_stale_scheduled_runs(
            max_age_minutes=120,
            now=now,
            started_before=process_boundary,
        )

        self.assertEqual(reconciled, 1)
        orphan_status = self.db.get_scheduled_run_status("orphan")
        current_status = self.db.get_scheduled_run_status("current")
        assert orphan_status is not None
        assert current_status is not None
        self.assertEqual(orphan_status["status"], "failed")
        self.assertIn("orphaned", orphan_status["error"])
        self.assertEqual(current_status["status"], "running")
        self.assertIsNone(current_status["finished_at"])

    def test_status_and_event_writes_bump_heartbeat(self) -> None:
        self.assertTrue(
            self.db.save_scheduled_run_status("run-1", "running", stock_count=1)
        )
        row = self.db.get_scheduled_run_status("run-1")
        assert row is not None
        self.assertIsNotNone(row["last_activity_at"])
        initial_heartbeat = row["last_activity_at"]

        self.assertTrue(
            self.db.save_scheduled_run_event(
                "run-1", stock_code="600519", event_index=1, event={"kind": "provider"}
            )
        )
        row = self.db.get_scheduled_run_status("run-1")
        assert row is not None
        self.assertIsNotNone(row["last_activity_at"])
        assert initial_heartbeat is not None
        self.assertGreaterEqual(row["last_activity_at"], initial_heartbeat)

    def test_events_roundtrip_and_since_filter(self) -> None:
        self.db.save_scheduled_run_status("run-1", "running", stock_count=1)
        self.assertTrue(
            self.db.save_scheduled_run_event(
                "run-1", stock_code="600519", event_index=1, event={"kind": "provider", "status": "success"}
            )
        )
        self.assertTrue(
            self.db.save_scheduled_run_event(
                "run-1", stock_code="600519", event_index=2, event={"kind": "model", "status": "success"}
            )
        )

        events = self.db.get_scheduled_run_events("run-1")
        self.assertEqual([row["event_index"] for row in events], [1, 2])
        self.assertEqual(events[0]["event"]["kind"], "provider")

        since = self.db.get_scheduled_run_events("run-1", since_event_index=1)
        self.assertEqual([row["event_index"] for row in since], [2])

        scoped = self.db.get_scheduled_run_events("run-1", stock_code="600519")
        self.assertEqual(len(scoped), 2)


    def test_cancel_request_is_persisted_and_running_heartbeats_cannot_clear_it(self) -> None:
        self.db.save_scheduled_run_status("run-1", "running", stock_count=3)

        requested = self.db.request_scheduled_run_cancellation("run-1")

        self.assertIsNotNone(requested)
        assert requested is not None
        self.assertEqual(requested["status"], "cancel_requested")
        self.assertTrue(self.db.is_scheduled_run_cancel_requested("run-1"))
        self.db.save_scheduled_run_status("run-1", "running", stock_count=3, completed_count=1)
        current = self.db.get_scheduled_run_status("run-1")
        assert current is not None
        self.assertEqual(current["status"], "cancel_requested")
        self.assertEqual(current["completed_count"], 1)
        self.assertEqual([row["run_id"] for row in self.db.list_active_scheduled_runs()], ["run-1"])

    def test_cancel_request_for_missing_run_returns_none(self) -> None:
        self.assertIsNone(self.db.request_scheduled_run_cancellation("missing"))

if __name__ == "__main__":
    unittest.main()
