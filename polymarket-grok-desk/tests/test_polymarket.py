import unittest

from grokdesk.feeds.polymarket import PolymarketClient, parse_book, parse_market
from tests.helpers import FakeTransport, fixture


class ParseMarketTest(unittest.TestCase):
    def setUp(self):
        self.raw = fixture("gamma_event_15m.json")[0]["markets"][0]

    def test_fields_from_real_payload(self):
        m = parse_market(self.raw, "15m")
        self.assertTrue(m.slug.startswith("btc-updown-15m-"))
        self.assertEqual(m.end_ts - m.start_ts, 900)
        self.assertEqual(int(m.start_ts), int(m.slug.rsplit("-", 1)[1]))
        self.assertEqual(m.twap_seconds, 60)
        self.assertEqual(m.min_order_size, 5)
        self.assertAlmostEqual(m.fee.rate, 0.07)
        self.assertNotEqual(m.up_token, m.down_token)
        self.assertIsNone(m.resolved_up)  # still open

    def test_resolution(self):
        m = parse_market({**self.raw, "closed": True, "outcomePrices": '["1", "0"]'}, "15m")
        self.assertTrue(m.resolved_up)
        m = parse_market({**self.raw, "closed": True, "outcomePrices": '["0", "1"]'}, "15m")
        self.assertFalse(m.resolved_up)
        m = parse_market({**self.raw, "closed": True, "outcomePrices": '["0.5", "0.5"]'}, "15m")
        self.assertIsNone(m.resolved_up)

    def test_rejects_non_updown(self):
        with self.assertRaises(ValueError):
            parse_market({**self.raw, "outcomes": '["Yes", "No"]'}, "15m")


class BookTest(unittest.TestCase):
    def test_best_levels_despite_api_ordering(self):
        # The CLOB returns best bid/ask LAST; parse_book must sort.
        b = parse_book(fixture("clob_book.json"))
        self.assertEqual(b.best_bid, max(l.price for l in b.bids))
        self.assertEqual(b.best_ask, min(l.price for l in b.asks))
        self.assertLess(b.best_bid, b.best_ask)
        self.assertEqual(b.ask_depth(b.best_ask), b.asks[0].size)
        self.assertEqual(parse_book({"bids": [], "asks": []}).best_ask, None)


class ClientTest(unittest.TestCase):
    def test_live_markets_uses_aligned_slugs(self):
        ev = fixture("gamma_event_15m.json")
        start = int(ev[0]["slug"].rsplit("-", 1)[1])
        t = FakeTransport().on("events?slug=", [])
        t.on(f"events?slug=btc-updown-15m-{start}", ev)
        c = PolymarketClient(t)
        got = c.live_updown_markets("btc", ["15m", "5m"], start + 123)
        self.assertEqual([m.slug for m in got], [ev[0]["slug"]])
        urls = [u for _, u, _, _ in t.calls]
        self.assertIn(f"https://gamma-api.polymarket.com/events?slug=btc-updown-5m-{start + 0}", urls)


if __name__ == "__main__":
    unittest.main()
