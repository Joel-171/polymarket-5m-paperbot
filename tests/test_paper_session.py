import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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
        self.enterContext(patch.object(service.stop_event, "wait", return_value=False))
        with patch.object(service.time, "time", return_value=self.start):
            service.command("123", "123", "/overnight vault")

    def scan(self, index, now, end, size="1000000", fee="0"):
        market = {
            "id": str(index), "slug": f"market-{index}",
            "feesEnabled": fee != "0", "feeSchedule": {"exponent": 1, "rate": fee},
        }
        markets = [(None, market, "up", "down", now - 60, end)]
        with (
            patch.object(service.time, "time", return_value=now),
            patch.object(service, "discover_cached", return_value=markets),
            patch.object(service, "get_asks", return_value={"up": .8, "down": .2}),
            patch.object(service, "read_order_book", return_value={
                "asks": [{"price": "0.80", "size": size}], "timestamp": str(now * 1000),
                "min_order_size": "5", "hash": "snapshot",
            }),
        ):
            service.scan()

    def finish(self):
        with patch.object(service.time, "time", return_value=self.start + paper_session.DURATION):
            service.close_session_if_due()

    def test_full_six_hour_run_records_two_cash_outs(self):
        self.assertEqual(service.cash(), 10)
        with patch.object(service, "outcome_for", return_value="UP"):
            for index in range(72):
                now = self.start + 300 * index + 210
                self.scan(index, now, now + 90)
                service.settle_pending()
                if index in (30, 61):
                    self.assertEqual(service.cash(), 10)
        self.assertAlmostEqual(service.cash(), 93.114)
        self.finish()
        self.finish()
        with service.db() as conn:
            self.assertEqual(paper_session.wallet_summary(conn), "vault: $20,170.33 (2 transfers)")
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
        market = {"id": "late", "feesEnabled": False}
        with (
            patch.object(service.time, "time", side_effect=[now, cutoff]),
            patch.object(service, "discover_cached", return_value=[(None, market, "up", "down", now, cutoff + 60)]),
            patch.object(service, "get_asks", return_value={"up": .8, "down": .2}),
            patch.object(service, "read_order_book", return_value={
                "asks": [{"price": "0.80", "size": "100"}], "timestamp": str(now * 1000),
                "min_order_size": "5",
            }),
        ):
            service.scan()
        self.assertIsNone(service.pending())
        self.assertEqual(service.cash(), 10)

    def test_repeat_settlement_does_not_duplicate_transfer(self):
        service.put("cash", 8_000)
        self.scan(1, self.start + 210, self.start + 300)
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
        self.scan(1, self.start + 210, self.start + 300)
        with patch.object(service, "outcome_for", return_value="UP"):
            service.settle_pending()
        self.scan(2, self.start + 240, self.start + 300)
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

    def test_before_entry_window_uses_no_market_data(self):
        with (
            patch.object(service.time, "time", return_value=self.start + 100),
            patch.object(service, "discover_cached") as discover,
        ):
            service.scan()
        discover.assert_not_called()

    def test_scan_skips_subminimum_book_then_records_partial_fill(self):
        self.scan(1, self.start + 210, self.start + 300, size="4", fee="0.05")
        self.assertIsNone(service.pending())
        self.assertEqual(service.cash(), 10)
        self.scan(1, self.start + 220, self.start + 300, size="7", fee="0.05")
        trade = service.pending()
        self.assertEqual(trade["shares"], 7)
        self.assertGreater(service.cash(), 4)
        self.assertAlmostEqual(trade["cash_spent"] + service.cash(), 10)
        with service.db() as conn:
            audit = conn.execute("SELECT * FROM paper_execution WHERE trade_id=?", (trade["id"],)).fetchone()
        self.assertEqual(audit["available_shares"], 7)
        self.assertEqual(audit["book_hash"], "snapshot")
        self.assertGreater(audit["estimated_fee"], 0)

    def sell(self, price, size, fee="0"):
        now = self.start + 260
        with (
            patch.object(service.time, "time", return_value=now),
            patch.object(service, "market_for", return_value={
                "acceptingOrders": True, "outcomes": ["Up", "Down"],
                "clobTokenIds": ["up", "down"], "feesEnabled": fee != "0",
                "feeSchedule": {"exponent": 1, "rate": fee},
            }),
            patch.object(service, "read_order_book", return_value={
                "bids": [{"price": price, "size": size}],
                "timestamp": str(now * 1000), "min_order_size": "5",
            }),
        ):
            service.command("123", "123", "/sell")

    def test_sell_checks_fees_and_partial_sale_settles_once(self):
        self.scan(1, self.start + 210, self.start + 300, fee="0.05")
        self.sell("0.805", "7", fee="0.05")
        self.assertIn("profitable bid", self.send.call_args.args[1])
        cash_before = service.cash()
        self.sell("0.90", "7", fee="0.05")
        self.assertIn("7.00 shares", self.send.call_args.args[1])
        self.assertAlmostEqual(service.cash(), cash_before + 6.3 - .0315)
        self.sell("0.90", "7", fee="0.05")
        self.assertIn("snapshot was already used", self.send.call_args.args[1])
        self.assertAlmostEqual(service.cash(), cash_before + 6.3 - .0315)
        with patch.object(service, "outcome_for", return_value="DOWN"):
            service.settle_pending()
            service.settle_pending()
        with service.db() as conn:
            trade = conn.execute("SELECT * FROM trades").fetchone()
        self.assertAlmostEqual(trade["pnl"], service.cash() - 10)
        self.assertIsNone(service.pending())

    def test_full_sale_ends_position_and_blocks_reentry_in_same_window(self):
        self.scan(1, self.start + 210, self.start + 300)
        self.sell("0.90", "15")
        self.assertIsNone(service.pending())
        self.assertEqual(service.cash(), 11.25)
        self.scan(2, self.start + 270, self.start + 300)
        self.assertIsNone(service.pending())
        with (
            patch.object(service.time, "time", return_value=self.start + 301),
            patch.object(service, "outcome_for", return_value="UP"),
        ):
            service.verify_early_sale()
        self.assertIn("Holding: $+2.50", self.send.call_args.args[1])
        self.assertEqual(service.cash(), 11.25)

    def test_endsession_reports_now_if_flat_and_blocks_resume(self):
        now = self.start + 100
        with patch.object(service.time, "time", return_value=now):
            service.command("123", "123", "/endsession")
            self.assertIn("Paper session ended.", self.send.call_args.args[1])
            service.command("123", "123", "/resume")
        self.assertIn("window ended", self.send.call_args.args[1])
        self.assertFalse(service.get("active"))

    def test_endsession_timeout_sends_provisional_then_final_report(self):
        now = self.start + 240
        self.scan(1, self.start + 210, self.start + 300)
        with patch.object(service.time, "time", return_value=now):
            service.command("123", "123", "/endsession")
        self.assertIn("when the open position settles", self.send.call_args.args[1])
        with patch.object(service.time, "time", return_value=now + paper_session.SETTLEMENT_TIMEOUT):
            service.close_session_if_due()
            self.assertIn("Unsettled position at cost: $10.00", self.send.call_args.args[1])
            calls = self.send.call_count
            service.close_session_if_due()
            self.assertEqual(self.send.call_count, calls)
            with patch.object(service, "outcome_for", return_value="UP"):
                service.settle_pending()
            service.close_session_if_due()
        self.assertIn("Net P&L: $+2.50", self.send.call_args.args[1])
        self.assertIn("100.0% vs 80.0% break-even", self.send.call_args.args[1])

    def test_saved_position_settles_when_result_is_automatic_without_uma_status(self):
        self.scan(1, self.start + 210, self.start + 300)
        result = {
            "id": "1",
            "closed": True, "automaticallyResolved": True, "umaResolutionStatus": None,
            "outcomes": '["Up", "Down"]', "outcomePrices": '["1", "0"]',
        }
        response = Mock(status_code=200, **{"json.return_value": result})
        with (
            patch.object(service.SESSION, "get", return_value=response) as get,
            patch.object(service.time, "time_ns", return_value=123),
        ):
            service.settle_pending()
        get.assert_called_once_with(
            f"{service.GAMMA}/markets/slug/market-1", params={"_": 123},
            headers={"Cache-Control": "no-cache"}, timeout=12,
        )
        self.assertIsNone(service.pending())
        self.assertEqual(service.cash(), 12.5)
        self.assertIn("SETTLED: WIN", self.send.call_args.args[1])

    def test_slug_lookup_falls_back_to_id_on_missing_slug_and_rejects_wrong_market(self):
        resolved = {"id": "2", "closed": True, "automaticallyResolved": True,
                    "outcomes": ["Up", "Down"], "outcomePrices": ["1", "0"]}
        with (
            patch.object(service.SESSION, "get", side_effect=[
                Mock(status_code=200, **{"json.return_value": resolved}), Mock(status_code=404),
            ]) as get,
            patch.object(service, "market_for", return_value={**resolved, "id": "1"}) as by_id,
            patch.object(service.time, "time_ns", side_effect=[123, 456]),
        ):
            self.assertIsNone(service.outcome_for("1", "market-1"))
            self.assertEqual(service.outcome_for("1", "market-1"), "UP")
        self.assertEqual([call.kwargs["params"] for call in get.call_args_list], [{"_": 123}, {"_": 456}])
        by_id.assert_called_once_with("1")

    def test_does_not_treat_closed_or_indicative_prices_as_final(self):
        market = {"closed": True, "outcomes": ["Up", "Down"], "outcomePrices": ["0", "1"]}
        with patch.object(service, "market_for", return_value=market):
            self.assertIsNone(service.outcome_for("1"))
            market["umaResolutionStatus"] = "resolved"
            self.assertEqual(service.outcome_for("1"), "DOWN")
            market["outcomePrices"] = ["0.01", "0.99"]
            self.assertIsNone(service.outcome_for("1"))
