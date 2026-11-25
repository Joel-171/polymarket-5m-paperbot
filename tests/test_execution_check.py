import unittest
from decimal import Decimal

from execution_check import estimate_buy


class TestExecutionCheck(unittest.TestCase):
    def quote(self, levels, cap="0.82", fee="0.01", budget="10"):
        book = {
            "min_order_size": "5",
            "asks": [{"price": price, "size": size} for price, size in levels],
        }
        return estimate_buy(book, Decimal(budget), Decimal(cap), Decimal(fee))

    def test_uses_best_price_and_includes_fee_in_budget(self):
        quote = self.quote([("0.84", "50"), ("0.82", "12.20")])
        self.assertTrue(quote["fits"])
        self.assertEqual(quote["best_ask"], Decimal("0.82"))
        self.assertEqual(quote["estimated_order_shares"], Decimal("12.17"))
        self.assertEqual(quote["estimated_taker_fee"], Decimal("0.01796"))
        self.assertLessEqual(quote["estimated_total_cost"], Decimal("10"))

    def test_rejects_shallow_best_ask_even_with_deeper_levels(self):
        quote = self.quote([("0.82", "5"), ("0.84", "50")], cap="0.85")
        self.assertFalse(quote["fits"])
        self.assertIn("Insufficient shares", quote["reason"])

    def test_rejects_price_above_cap(self):
        self.assertFalse(self.quote([("0.83", "200")])["fits"])

    def test_rejects_below_order_minimum(self):
        self.assertFalse(self.quote([("0.82", "200")], budget="2")["fits"])

    def test_empty_book_and_fee_free_order(self):
        self.assertFalse(self.quote([])["fits"])
        quote = self.quote([("0.80", "100")], fee="0")
        self.assertTrue(quote["fits"])
        self.assertEqual(quote["estimated_total_cost"], Decimal("10"))

    def test_partial_fill_leaves_cash_and_does_not_chase_price(self):
        book = {
            "min_order_size": "5",
            "asks": [{"price": "0.88", "size": "7"}, {"price": "0.90", "size": "20"}],
        }
        fill = estimate_buy(
            book, Decimal("10"), Decimal("0.88"), Decimal("0"), allow_partial=True,
        )
        self.assertTrue(fill["fits"])
        self.assertEqual(fill["estimated_order_shares"], Decimal("7"))
        self.assertEqual(fill["unused_budget"], Decimal("3.84"))
