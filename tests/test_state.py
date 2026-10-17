from pathlib import Path
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from attendance.state import StateStore


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "nested" / "state.sqlite3"
        self.store = StateStore(self.path)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)

    def test_state_persists_across_reopen(self) -> None:
        self.store.mark("D123", "1.001", "0012", "pending")
        self.store.mark("D123", "2.001", "1362", "sent", "2.002")
        with StateStore(self.path) as reopened:
            self.assertEqual(reopened.get_status("D123", "2.001"), "sent")
            self.assertEqual(
                reopened.get_pending("D123"),
                [{"ts": "1.001", "code": "0012", "status": "pending", "reply_ts": None}],
            )
            self.assertIsNone(reopened.get_status("D123", "missing"))

    def test_same_message_is_unique_but_channels_are_independent(self) -> None:
        self.store.mark("D123", "1.001", "0012", "pending")
        self.store.mark("D123", "1.001", "0012", "pending")
        self.store.mark("D456", "1.001", "0099", "pending")
        self.assertEqual(len(self.store.get_pending("D123")), 1)
        self.assertEqual(self.store.get_pending("D456")[0]["code"], "0099")

    def test_sent_and_manual_outcomes_cannot_be_overwritten(self) -> None:
        for index, terminal in enumerate(("sent", "manual")):
            ts = str(index)
            self.store.mark("D123", ts, "0012", terminal, "reply.001")
            for next_status in (
                "pending", "uncertain", "retryable", "expired", "sent", "manual"
            ):
                self.store.mark("D123", ts, "9999", next_status, "reply.999")
                self.assertEqual(self.store.get_status("D123", ts), terminal)
            with sqlite3.connect(self.path) as connection:
                stored = connection.execute(
                    "SELECT code, reply_ts FROM replies WHERE channel = ? AND ts = ?",
                    ("D123", ts),
                ).fetchone()
            self.assertEqual(stored, ("0012", "reply.001"))

    def test_pending_transitions_and_uncertain_reconciliation(self) -> None:
        self.store.mark("D123", "1.001", "1362", "pending")
        self.store.mark("D123", "1.001", "1362", "uncertain")
        self.assertEqual(self.store.get_pending("D123")[0]["status"], "uncertain")
        self.store.mark("D123", "1.001", "1362", "sent", "1.002")
        self.assertEqual(self.store.get_pending("D123"), [])
        self.assertEqual(self.store.get_status("D123", "1.001"), "sent")

    def test_definitely_unaccepted_reply_remains_retryable(self) -> None:
        self.store.mark("D123", "1.001", "1362", "pending")
        self.store.mark("D123", "1.001", "1362", "retryable")
        self.assertEqual(self.store.get_pending("D123")[0]["status"], "retryable")
        self.store.mark("D123", "1.001", "1362", "pending")
        self.store.mark("D123", "1.001", "1362", "sent", "1.002")
        self.assertEqual(self.store.get_status("D123", "1.001"), "sent")

    def test_cleanup_removes_only_old_terminal_outcomes(self) -> None:
        day = 24 * 60 * 60
        with patch("attendance.state.time.time", return_value=1_000):
            for status in (
                "pending", "uncertain", "retryable", "sent", "manual", "expired"
            ):
                self.store.mark("D123", status, "1362", status)
        with patch("attendance.state.time.time", return_value=1_000 + 29 * day):
            self.store.mark("D123", "recent", "1362", "sent")
            self.store.mark("D123", "transition", "1362", "pending")
        self.store.cleanup(now=1_000 + 31 * day)
        for status in ("pending", "uncertain", "retryable"):
            self.assertEqual(self.store.get_status("D123", status), status)
        for status in ("sent", "manual", "expired"):
            self.assertIsNone(self.store.get_status("D123", status))
        self.assertEqual(self.store.get_status("D123", "recent"), "sent")

    def test_cleanup_retains_recent_terminal_transition(self) -> None:
        day = 24 * 60 * 60
        with patch("attendance.state.time.time", return_value=1_000):
            self.store.mark("D123", "1.001", "1362", "pending")
        with patch("attendance.state.time.time", return_value=1_000 + 29 * day):
            self.store.mark("D123", "1.001", "1362", "manual", "reply.001")
        self.store.cleanup(now=1_000 + 31 * day)
        self.assertEqual(self.store.get_status("D123", "1.001"), "manual")

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_database_permissions_are_private(self) -> None:
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.path.chmod(0o644)
        with StateStore(self.path):
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_invalid_inputs_fail_before_writing(self) -> None:
        with self.assertRaises(ValueError):
            self.store.mark("D123", "1.001", "1362", "invalid")
        self.assertIsNone(self.store.get_status("D123", "1.001"))
        with self.assertRaises(ValueError):
            self.store.cleanup(now=1_000, retention_days=-1)


if __name__ == "__main__":
    unittest.main()
