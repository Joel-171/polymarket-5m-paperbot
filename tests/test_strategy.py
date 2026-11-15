import unittest
from strategy import choose_side, paper_buy, settle

class TestStrategy(unittest.TestCase):
    def test_selects_highest_qualifying_side(self):
        self.assertEqual(choose_side(.79,.81,.80).side,"DOWN")
    def test_no_trade_below_threshold(self):
        self.assertIsNone(choose_side(.79,.78,.80))
    def test_full_cash_allocation(self):
        result=paper_buy(100,.80,1.0,0.0)
        self.assertAlmostEqual(result["shares"],125)
    def test_binary_settlement(self):
        self.assertEqual(settle(125,"DOWN","DOWN"),125)
        self.assertEqual(settle(125,"DOWN","UP"),0)

if __name__=="__main__":
    unittest.main()
