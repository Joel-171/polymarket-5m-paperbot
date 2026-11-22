import tempfile
import unittest
from pathlib import Path
import service

class TestPersistentState(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.old_db,self.old_data,self.old_cash=service.DB_PATH,service.DATA,service.INITIAL_CASH
        service.DATA=Path(self.tmp.name)
        service.DB_PATH=Path(self.tmp.name)/"test.sqlite3"
        service.INITIAL_CASH=25.0
    def tearDown(self):
        service.DB_PATH,service.DATA,service.INITIAL_CASH=self.old_db,self.old_data,self.old_cash
        self.tmp.cleanup()
    def test_cash_persists(self):
        self.assertEqual(service.cash(),25.0)
        service.put("cash",12.5)
        self.assertEqual(service.cash(),12.5)
    def test_defaults(self):
        self.assertEqual(service.get("threshold"),.80)
        self.assertEqual(service.get("entry_window"),120)
        self.assertEqual(service.get("poll_seconds"),10.0)

    def test_old_poll_value_uses_lower_data_minimum(self):
        service.put("poll_seconds", 2)
        self.assertEqual(service.quote_interval(), 10)

if __name__=="__main__":
    unittest.main()
