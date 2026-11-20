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
