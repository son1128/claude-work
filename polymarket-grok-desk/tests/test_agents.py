import math
import os
import tempfile
import unittest

from grokdesk.agents import EdgeAgent, EdgeConfig, KellyAgent, KellyConfig, PriorAgent, PriorConfig
from grokdesk.execution import PaperExecutor, assert_live_allowed, LIVE_CONFIRM_ENV, LIVE_CONFIRM_VALUE
from grokdesk.feeds.polymarket import Book, Level, UpDownMarket
from grokdesk.feeds.spot import Tick
from grokdesk.quant import FeeModel
from grokdesk.risk import RiskConfig, RiskManager, RiskState

T0 = 1_800_000_000.0


def market(**kw):
    base = dict(slug="btc-updown-15m-1800000000", question="q", condition_id="c",
                up_token="UP", down_token="DN", start_ts=T0, end_ts=T0 + 900, duration="15m",
                twap_seconds=60, min_order_size=5, tick_size=0.01, fee=FeeModel(0.07, 1.0),
                accepting_orders=True, closed=False)
    base.update(kw)
    return UpDownMarket(**base)


def book(asks, bids=()):
    return Book("x", [Level(p, s) for p, s in bids], [Level(p, s) for p, s in asks], T0)


def ticks(n=300, end=T0 + 600, start_px=60000.0, step=0.0):
    return [Tick(end - n + 1 + i, start_px * math.exp(step * i + 0.00005 * math.sin(i))) for i in range(n)]


class PriorAgentTest(unittest.TestCase):
    def test_none_when_too_few_or_stale_ticks(self):
        a = PriorAgent(PriorConfig(min_ticks=60))
        self.assertIsNone(a.price(market(), 60000, ticks(10), T0 + 600))
        self.assertIsNone(a.price(market(), 60000, ticks(300), T0 + 700))  # 100s stale

    def test_above_strike_favours_up(self):
        a = PriorAgent(PriorConfig())
        s = a.price(market(), 59900, ticks(), T0 + 600)
        self.assertGreater(s.p_up, 0.6)
        self.assertAlmostEqual(s.seconds_left, 300)

    def test_drift_gated_by_r2_and_off_by_default(self):
        trending = ticks(step=2e-6)
        self.assertEqual(PriorAgent(PriorConfig()).price(market(), 60000, trending, T0 + 600).drift_used, 0.0)
        on = PriorAgent(PriorConfig(drift_weight=0.5)).price(market(), 60000, trending, T0 + 600)
        self.assertGreater(on.r2, 0.35)
        self.assertGreater(on.drift_used, 0)
        self.assertGreater(on.p_up, on.p_up_no_drift)
        gated = PriorAgent(PriorConfig(drift_weight=0.5, r2_min=0.9999)).price(market(), 60000, trending, T0 + 600)
        self.assertEqual(gated.drift_used, 0.0)


class EdgeKellyTest(unittest.TestCase):
    def sig(self, p_up):
        from grokdesk.agents import Signal
        return Signal(p_up, p_up, 60000, 60000, 300, 1e-4, 0, 0, 0, 300, 0.5)

    def test_picks_side_with_edge_after_fees(self):
        e = EdgeAgent(EdgeConfig(min_edge=0.03))
        opp, notes = e.evaluate(market(), self.sig(0.70), book([(0.60, 100)]), book([(0.42, 100)]))
        self.assertEqual(opp.side, "UP")
        self.assertAlmostEqual(opp.limit_price, 0.61)
        self.assertAlmostEqual(opp.fee_per_share, 0.07 * 0.61 * 0.39)
        self.assertAlmostEqual(opp.edge_per_share, 0.70 - 0.61 - 0.07 * 0.61 * 0.39)
        self.assertEqual(len(notes), 2)

    def test_fees_can_kill_a_raw_edge(self):
        # 2¢ raw edge at the touch, but the limit tick + ~1.7¢ fee eat it.
        e = EdgeAgent(EdgeConfig(min_edge=0.0))
        opp, _ = e.evaluate(market(), self.sig(0.52), book([(0.50, 100)]), book([(0.52, 100)]))
        self.assertIsNone(opp)

    def test_refuses_huge_model_market_gap(self):
        e = EdgeAgent(EdgeConfig(max_model_market_gap=0.2))
        opp, notes = e.evaluate(market(), self.sig(0.95), book([(0.50, 100)]), book([(0.52, 100)]))
        self.assertIsNone(opp)
        self.assertEqual(notes[0]["skip"], "model/market gap too large")

    def test_kelly_caps(self):
        e = EdgeAgent(EdgeConfig(min_edge=0.03))
        opp, _ = e.evaluate(market(), self.sig(0.75), book([(0.60, 1000)]), book([(0.42, 0)]))
        k = KellyAgent(KellyConfig(kelly_multiplier=0.25, max_bankroll_fraction=0.02, max_stake_usd=25))
        s = k.size(opp, bankroll=500, min_shares=5)
        self.assertLessEqual(s.stake_usd, 500 * 0.02 + 1e-6)
        self.assertGreater(s.full_kelly, 0)
        tiny = k.size(opp, bankroll=50, min_shares=5)
        self.assertEqual(tiny.shares, 0)
        self.assertIn("min size", tiny.reason)
        thin = KellyAgent(KellyConfig(max_depth_fraction=0.25)).size(
            opp.__class__(**{**opp.__dict__, "depth_shares": 40}), 10_000, 5)
        self.assertLessEqual(thin.shares, 10)


class PaperExecutorTest(unittest.TestCase):
    def test_walks_book_and_charges_fee(self):
        e = EdgeAgent(EdgeConfig(min_edge=0.0))
        from grokdesk.agents import Sizing
        m = market()
        b = book([(0.60, 10), (0.61, 10), (0.65, 100)])
        opp, _ = e.evaluate(m, EdgeKellyTest.sig(None, 0.8), b, book([(0.3, 1)]))
        f = PaperExecutor().buy(m, opp, Sizing(0, 15, 0, "ok"), b)
        self.assertEqual(f.shares, 15)
        self.assertAlmostEqual(f.avg_price, (10 * 0.60 + 5 * 0.61) / 15)
        self.assertAlmostEqual(f.fee_usd, 10 * m.fee.per_share(0.60) + 5 * m.fee.per_share(0.61), places=6)

    def test_fill_or_kill(self):
        from grokdesk.agents import Sizing
        m = market()
        b = book([(0.60, 3), (0.70, 100)])
        opp, _ = EdgeAgent(EdgeConfig(min_edge=0.0)).evaluate(m, EdgeKellyTest.sig(None, 0.8), b, book([]))
        self.assertIsNone(PaperExecutor().buy(m, opp, Sizing(0, 10, 0, "ok"), b))


class LiveGateTest(unittest.TestCase):
    def setUp(self):
        self.env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)

    def test_all_gates(self):
        ok_geo = {"blocked": False, "country": "XX"}
        with self.assertRaises(PermissionError):
            assert_live_allowed(ok_geo, "paper")
        os.environ.pop(LIVE_CONFIRM_ENV, None)
        with self.assertRaises(PermissionError):
            assert_live_allowed(ok_geo, "live")
        os.environ[LIVE_CONFIRM_ENV] = LIVE_CONFIRM_VALUE
        with self.assertRaisesRegex(PermissionError, "geoblock"):
            assert_live_allowed({"blocked": True, "country": "US"}, "live")
        with self.assertRaisesRegex(PermissionError, "geoblock"):
            assert_live_allowed({}, "live")  # unknown -> treat as blocked
        os.environ.pop("POLY_PRIVATE_KEY", None)
        with self.assertRaisesRegex(PermissionError, "POLY_PRIVATE_KEY"):
            assert_live_allowed(ok_geo, "live")
        os.environ["POLY_PRIVATE_KEY"] = "k"
        os.environ["POLY_FUNDER"] = "f"
        assert_live_allowed(ok_geo, "live")


class RiskTest(unittest.TestCase):
    def st(self, **kw):
        base = dict(realized_pnl_today=0, open_positions=0, open_in_market=0, seconds_since_last_trade_in_market=None)
        base.update(kw)
        return RiskState(**base)

    def test_limits(self):
        r = RiskManager(RiskConfig(kill_switch_file=""))
        self.assertIsNone(r.check(300, self.st()))
        self.assertEqual(r.check(300, self.st(realized_pnl_today=-50)), "daily loss limit hit")
        self.assertEqual(r.check(300, self.st(open_positions=3)), "max open positions")
        self.assertEqual(r.check(300, self.st(open_in_market=1)), "already positioned in this market")
        self.assertEqual(r.check(5, self.st()), "too close to expiry")
        self.assertEqual(r.check(800, self.st()), "too early in window")
        self.assertEqual(r.check(300, self.st(seconds_since_last_trade_in_market=3)), "market cooldown")

    def test_kill_switch(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "STOP")
            r = RiskManager(RiskConfig(kill_switch_file=path))
            self.assertIsNone(r.check(300, self.st()))
            open(path, "w").close()
            self.assertIn("kill switch", r.check(300, self.st()))


if __name__ == "__main__":
    unittest.main()
