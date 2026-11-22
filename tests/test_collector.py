import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import collector


class TestDiscovery(unittest.TestCase):
    def test_looks_up_current_slot_and_ignores_closed_or_missing_markets(self):
        start = 1_800_000_000
        end = datetime.fromtimestamp(start + 300, timezone.utc).isoformat()

        def get(url, timeout):
            symbol = url.rsplit("/", 1)[1].split("-")[0]
            self.assertEqual(url, f"{collector.GAMMA}/events/slug/{symbol}-updown-5m-{start}")
            if symbol == "sol":
                return Mock(status_code=404)
            market = {
                "slug": f"{symbol}-updown-5m-{start}",
                "endDate": end,
                "closed": symbol == "eth",
                "acceptingOrders": symbol != "xrp",
                "outcomes": ["Up", "Down"],
                "clobTokenIds": ["up", "down"],
            }
            return Mock(status_code=200, **{"json.return_value": {"markets": [market]}})

        with (
            patch.object(collector.time, "time", return_value=start + 180),
            patch.object(collector.SESSION, "get", side_effect=get),
        ):
            found = collector.discover()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][1]["slug"], f"btc-updown-5m-{start}")
        self.assertEqual(found[0][2:], ("up", "down", start, start + 300))

    def test_cache_refreshes_at_next_market_window(self):
        start = 1_800_000_000
        markets = [(None, {}, "up", "down", start, start + 300)] * 4
        with (
            patch.object(collector, "_market_cache", {}),
            patch.object(collector, "discover", side_effect=[markets, []]) as discover,
            patch.object(collector.time, "time", side_effect=[start + 180, start + 190, start + 300]),
            patch.object(collector.time, "monotonic", side_effect=[0, 10, 120]),
        ):
            self.assertEqual(collector.discover_cached(), markets)
            self.assertEqual(collector.discover_cached(), markets)
            self.assertEqual(collector.discover_cached(), [])
        self.assertEqual(discover.call_count, 2)

    def test_batch_reads_resting_sell_prices(self):
        response = Mock(**{"json.return_value": {"up": {"BUY": ".81", "SELL": ".82"}, "down": {}}})
        with patch.object(collector.SESSION, "post", return_value=response) as post:
            self.assertEqual(collector.get_asks(["up", "down", "up"]), {"up": .82})
        post.assert_called_once_with(
            f"{collector.CLOB}/prices",
            json=[{"token_id": "up", "side": "SELL"}, {"token_id": "down", "side": "SELL"}],
            timeout=12,
        )
