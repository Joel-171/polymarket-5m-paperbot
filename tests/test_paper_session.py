import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import paper_session
import service


class TestPaperSession(unittest.TestCase):
    start = 1_800_000_000

    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.multiple(
            service, DATA=Path(directory), DB_PATH=Path(directory) / "state.db",
            ALLOWED_USER_ID="123",
        ))
        self.send = self.enterContext(patch.object(service, "send"))
        with patch.object(service.time, "time", return_value=self.start):
            service.command("123", "123", "/overnight vault")

    def scan(self, index, now, end):
        market = {"id": str(index), "slug": f"market-{index}"}
        markets = [(None, market, "up", "down", now - 60, end)]
        with (
            patch.object(service.time, "time", return_value=now),
            patch.object(service, "discover", return_value=markets),
            patch.object(service, "get_book", side_effect=[(None, .8, 100), (None, .2, 100)]),
        ):
            service.scan()

    def finish(self):
        with patch.object(service.time, "time", return_value=self.start + paper_session.DURATION):
            service.close_session_if_due()

    def test_full_six_hour_run_records_two_cash_outs(self):
        self.assertEqual(service.cash(), 10)
        with patch.object(service, "outcome_for", return_value="UP"):
            for index in range(72):
                now = self.start + 300 * index
                self.scan(index, now, now + 90)
                service.settle_pending()
                if index in (30, 61):
                    self.assertEqual(service.cash(), 10)
        self.assertAlmostEqual(service.cash(), 93.13225746)
        self.finish()
        self.finish()
        with service.db() as conn:
            self.assertEqual(paper_session.wallet_summary(conn), "vault: $20,174.84 (2 transfers)")
            self.assertIn("Trades: 72 | Wins: 72 | Losses: 0", paper_session.latest(conn)["report"])
            self.assertFalse(paper_session.enabled(conn))
        reports_sent = [
            call for call in self.send.call_args_list if "paper session complete" in call.args[1]
        ]
        self.assertEqual(len(reports_sent), 1)

    def test_cutoff_waits_for_pending_settlement_and_blocks_new_trades(self):
        cutoff = self.start + paper_session.DURATION
        self.scan(1, cutoff - 30, cutoff + 60)
        self.finish()
        self.assertIsNotNone(service.pending())
        with service.db() as conn:
            self.assertIsNone(paper_session.latest(conn)["report"])
            self.assertFalse(paper_session.enabled(conn))
        with patch.object(service, "outcome_for", return_value="DOWN"):
            service.settle_pending()
        self.finish()
        self.scan(2, cutoff + 10, cutoff + 90)
        self.assertEqual(service.cash(), 0)
        self.assertIsNone(service.pending())
        service.command("123", "123", "/report")
        self.assertIn("Trades: 1 | Wins: 0 | Losses: 1", self.send.call_args.args[1])

    def test_time_is_checked_again_after_market_requests(self):
        cutoff = self.start + paper_session.DURATION
        now = cutoff - 1
        market = {"id": "late"}
        with (
            patch.object(service.time, "time", side_effect=[now, cutoff]),
            patch.object(service, "discover", return_value=[(None, market, "up", "down", now, cutoff + 60)]),
            patch.object(service, "get_book", side_effect=[(None, .8, 100), (None, .2, 100)]),
        ):
            service.scan()
        self.assertIsNone(service.pending())
        self.assertEqual(service.cash(), 10)

    def test_repeat_settlement_does_not_duplicate_transfer(self):
        service.put("cash", 8_000)
        self.scan(1, self.start, self.start + 90)
        trade = service.pending()
        with (
            patch.object(service, "pending", return_value=trade),
            patch.object(service, "outcome_for", return_value="UP"),
        ):
            service.settle_pending()
            service.settle_pending()
        self.assertEqual(service.cash(), 10)
        with service.db() as conn:
            self.assertEqual(paper_session.wallet_summary(conn), "vault: $9,990.00 (1 transfers)")

    def test_second_entry_in_same_window_is_skipped(self):
        self.scan(1, self.start, self.start + 90)
        with patch.object(service, "outcome_for", return_value="UP"):
            service.settle_pending()
        self.scan(2, self.start + 30, self.start + 90)
        self.assertIsNone(service.pending())
        self.assertEqual(service.cash(), 12.5)

    def test_repeated_start_preserves_deadline_and_existing_balance(self):
        service.put("cash", 15)
        with patch.object(service.time, "time", return_value=self.start + 600):
            service.command("123", "123", "/overnight different-wallet")
        with service.db() as conn:
            session = paper_session.latest(conn)
        self.assertEqual(session["ends_at"], self.start + paper_session.DURATION)
        self.assertEqual(session["wallet"], "vault")
        self.assertEqual(service.cash(), 15)
