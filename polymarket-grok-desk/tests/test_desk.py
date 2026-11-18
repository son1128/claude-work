"""End-to-end: fake Binance + Gamma + CLOB + Grok, fake clock, real pipeline."""

import copy
import json
import os
import tempfile
import unittest

from grokdesk.config import Config, from_dict
from grokdesk.desk import Desk
from grokdesk.execution import PaperExecutor
from grokdesk.feeds.polymarket import PolymarketClient
from grokdesk.feeds.spot import SpotFeed, Tick
from grokdesk.grok import GrokSupervisor
from grokdesk.ledger import Ledger
from tests.helpers import FakeClock, FakeTransport, fixture

EV = fixture("gamma_event_15m.json")
START = int(EV[0]["slug"].rsplit("-", 1)[1])
END = START + 900
UP_TOK, DN_TOK = json.loads(EV[0]["markets"][0]["clobTokenIds"])


def book(token, ask, size=500):
    return {"asset_id": token, "timestamp": "0",
            "bids": [{"price": f"{ask - 0.02:.2f}", "size": "100"}],
            "asks": [{"price": f"{ask + 0.01:.2f}", "size": str(size)}, {"price": f"{ask:.2f}", "size": str(size)}]}


class DeskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock(START + 600)  # 300s left
        self.spot_px = 60_060.0  # well above the 60,000 open
        t = self.t = FakeTransport()
        t.on("/api/v3/ticker/price", lambda u, b: {"price": str(self.spot_px)})
        t.on("/api/v3/klines", lambda u, b: [[START * 1000, "60000", "0", "0", "60000"]])
        t.on("events?slug=", [])
        t.on(f"events?slug=btc-updown-15m-{START}", lambda u, b: self.event())
        t.on(f"book?token_id={UP_TOK}", lambda u, b: book(UP_TOK, self.up_ask))
        t.on(f"book?token_id={DN_TOK}", lambda u, b: book(DN_TOK, 1 - self.up_ask))
        t.on("/chat/completions", {"choices": [{"message": {"content": '{"decision":"APPROVE","reason":"clean"}'}}],
                                   "usage": {"prompt_tokens": 500, "completion_tokens": 20}})
        self.up_ask = 0.75  # market is too cheap on UP (model says ~0.97)
        self.resolved = None

        cfg = from_dict({"desk": {"data_dir": self.tmp.name, "starting_bankroll_usd": 1000,
                                  "settle_grace_s": 60, "proxy_settle_after_s": 600},
                         "risk": {"kill_switch_file": ""},
                         "grok": {"enabled": True}})
        os.environ["XAI_API_KEY"] = "k"
        spot = SpotFeed(t, clock=self.clock)
        for i in range(300):  # 5 min of flat-ish history
            spot.add(Tick(START + 300 + i, 60_060.0 + (1 if i % 2 else -1)))
        self.ledger = Ledger(self.tmp.name, 1000, clock=self.clock)
        self.logs = []
        self.desk = Desk(cfg, spot, PolymarketClient(t), PaperExecutor(),
                         GrokSupervisor(cfg.grok, t, clock=self.clock), self.ledger,
                         clock=self.clock, log=self.logs.append)

    def tearDown(self):
        os.environ.pop("XAI_API_KEY", None)
        self.tmp.cleanup()

    def event(self):
        ev = copy.deepcopy(EV)
        m = ev[0]["markets"][0]
        if self.clock.t >= END:
            m["acceptingOrders"] = False
        if self.resolved is not None:
            m["closed"] = True
            m["outcomePrices"] = '["1", "0"]' if self.resolved else '["0", "1"]'
        return ev

    def test_buys_mispriced_side_then_settles_from_gamma(self):
        self.desk.step()
        open_ = self.ledger.open_positions()
        self.assertEqual(len(open_), 1)
        p = open_[0]
        self.assertEqual(p.side, "UP")
        self.assertAlmostEqual(p.avg_price, 0.75)
        self.assertGreater(p.p_win, 0.9)
        self.assertLessEqual(p.cost_usd, 1000 * 0.02 + 1e-6)  # max_bankroll_fraction
        self.assertTrue(any("BUY UP" in s for s in self.logs))

        # Second tick in the same market: risk blocks a duplicate.
        self.clock.t += 1
        self.desk.step()
        self.assertEqual(len(self.ledger.positions), 1)

        # After expiry + grace, Gamma shows UP won.
        self.clock.t = END + 61
        self.resolved = True
        self.desk.step()
        p = self.ledger.positions[0]
        self.assertEqual(p.status, "won")
        self.assertEqual(p.settled_by, "gamma")
        self.assertAlmostEqual(p.pnl_usd, p.shares - p.cost_usd, places=5)
        self.assertAlmostEqual(self.ledger.bankroll(), 1000 + p.pnl_usd, places=5)

        # The trace recorded the Grok decision and the fill.
        with open(os.path.join(self.tmp.name, "trace.jsonl"), encoding="utf-8") as f:
            kinds = [json.loads(line)["kind"] for line in f]
        self.assertIn("grok", kinds)
        self.assertIn("fill", kinds)
        self.assertIn("settle", kinds)

    def test_fair_market_means_no_trade(self):
        self.up_ask = 0.97  # market already prices UP correctly (and above max_price)
        self.desk.step()
        self.assertEqual(self.ledger.positions, [])

    def test_grok_veto_blocks_trade(self):
        self.t.on("/chat/completions", {"choices": [{"message": {"content": '{"decision":"VETO","reason":"stale"}'}}]})
        self.desk.step()
        self.assertEqual(self.ledger.positions, [])

    def test_proxy_settlement_when_gamma_never_resolves(self):
        self.desk.step()
        self.assertEqual(len(self.ledger.open_positions()), 1)
        self.clock.t = END + 61
        self.desk.step()  # gamma unresolved, before proxy timeout
        self.assertEqual(len(self.ledger.open_positions()), 1)
        self.clock.t = END + 601
        self.t.on("/api/v3/klines", lambda u, b: [[END * 1000, "0", "0", "0", "59000"]])
        self.desk.step()
        p = self.ledger.positions[0]
        self.assertEqual((p.status, p.settled_by), ("lost", "spot_proxy"))

    def test_ledger_persists(self):
        self.desk.step()
        again = Ledger(self.tmp.name, 1000)
        self.assertEqual(len(again.positions), 1)


class ConfigTest(unittest.TestCase):
    def test_defaults_are_paper_and_grok_off(self):
        c = Config()
        self.assertEqual(c.desk.mode, "paper")
        self.assertFalse(c.grok.enabled)
        self.assertEqual(c.grok.fail_mode, "veto")

    def test_example_config_loads(self):
        from grokdesk.config import load
        here = os.path.dirname(os.path.dirname(__file__))
        c = load(os.path.join(here, "config.example.toml"))
        self.assertEqual(c.desk.mode, "paper")

    def test_rejects_typos(self):
        with self.assertRaises(ValueError):
            from_dict({"risk": {"max_daily_loss": 1}})
        with self.assertRaises(ValueError):
            from_dict({"desk": {"mode": "yolo"}})


if __name__ == "__main__":
    unittest.main()
