# -*- coding: utf-8 -*-
"""Persistence coverage for background scheduled analysis run status/events."""

from __future__ import annotations

import unittest

from src.storage import DatabaseManager


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


if __name__ == "__main__":
    unittest.main()
