import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import live_test


class LiveTest(unittest.TestCase):
    def test_quote_requires_a_fresh_book_and_room_for_fees(self):
        book = {"asset_id": "token", "timestamp": "1000000", "min_order_size": "5",
                "asks": [{"price": "0.94", "size": "12"}]}
        with (
            patch.object(live_test, "read_order_book", return_value=book) as read,
            patch.object(live_test.time, "time", return_value=1001),
        ):
            args = ("42", "sol-window", "condition", "token", "UP", 1100, Decimal("0.94"))
            self.assertIsNone(live_test.book_quote(*args, Decimal("4.70"), Decimal("0.05")))
            quote = live_test.book_quote(*args, Decimal("5"), Decimal("0.05"))
            self.assertEqual(quote.max_spend, Decimal("4.72"))
            self.assertEqual(quote.estimated_shares, Decimal("5"))
            read.assert_called_with("token", fresh=True)
            book["timestamp"] = "900000"
            self.assertIsNone(live_test.book_quote(*args, Decimal("5"), Decimal("0.05")))

    def test_redeems_confirmed_winner_and_reports_spendable_collateral(self):
        client = Mock()
        quote = live_test.Quote(
            "42", "sol-window", "condition", "token", "UP", 1100, Decimal("0.94"),
            Decimal("4.70"), Decimal("5"), Decimal("0.01410"), Decimal("4.72"), Decimal("0.05"),
        )
        fill = SimpleNamespace(
            id="fill", taker_order_id="order", status="CONFIRMED", size=Decimal(5),
            price=Decimal("0.94"), fee_rate_bps=Decimal("500"),
        )
        client.place_market_order.return_value = SimpleNamespace(
            ok=True, order_id="order", trade_ids=("fill",), status="matched",
        )
        client.list_account_trades.return_value.first_page.return_value.items = [fill]
        client.get_order.return_value = SimpleNamespace(status="MATCHED", size_matched=Decimal(5))
        client.get_balance_allowance.side_effect = [
            SimpleNamespace(balance=balance) for balance in (5285900, 5285900, 10285900)
        ]
        handle = client.redeem_positions.return_value
        handle.transaction_id = "redemption"
        handle.transaction_hash = None
        handle.wait.return_value.transaction_hash = "tx"
        with (
            patch.object(live_test.time, "time", return_value=1001),
            patch.object(live_test.time, "sleep"),
            patch.object(live_test, "outcome_for", return_value="UP"),
            patch.object(live_test, "record") as record,
        ):
            live_test.run_order(client, quote, Decimal(10), None)
        client.place_market_order.assert_called_once_with(
            asset_id="token", side="BUY", amount=Decimal("4.70"), max_spend=Decimal("4.72"),
            max_price=Decimal("0.94"), order_type="FAK",
        )
        client.redeem_positions.assert_called_once_with(condition_id="condition")
        events = {call.args[1]: call.kwargs for call in record.call_args_list}
        self.assertEqual(events["fill_confirmed"]["observed_fee"], Decimal("0.0141"))
        self.assertEqual(events["payout_spendable"]["collateral"], Decimal("10.2859"))

    def test_unconfirmed_trade_cannot_trigger_redemption(self):
        client = Mock()
        quote = SimpleNamespace(token="token", condition_id="condition")
        response = SimpleNamespace(order_id="order", trade_ids=("pending",))
        client.list_account_trades.return_value.first_page.return_value.items = [
            SimpleNamespace(taker_order_id="order", status="MATCHED")
        ]
        self.assertIsNone(live_test.confirmed_fills(client, quote, response))
        client.redeem_positions.assert_not_called()
