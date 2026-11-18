"""The web replay's JS engine must agree with the Python desk.

Runs web/engine.js under Node (skipped when Node isn't installed) and compares
its pricer, fee, Kelly and regression math with grokdesk.quant, then drives a
tiny synthetic replay through the JS desk and checks the ledger arithmetic.
Also checks that web/build.py produces one self-contained page.
"""

import json
import math
import os
import shutil
import subprocess
import tempfile
import unittest

from grokdesk.quant import FeeModel, PricerInputs, kelly_fraction, linear_regression, prob_up

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "web", "engine.js")
NODE = shutil.which("node")

CASES = [
    (60030, 60000, 300, 1e-4, 0, 60, None, 2),
    (59990, 60000, 10, 1e-4, 1e-6, 60, 60040, 2),
    (60010, 60000, 30, 1e-4, 0, 60, 60005, 5),
    (64000, 63900, 700, 6e-5, 2e-6, 60, None, 2),
    (63900, 64000, 61, 8e-5, -1e-6, 60, None, 0),
]


def synthetic_replay():
    """Two windows: BTC drifts up in the first (resolves UP), down in the second."""
    def window(start, up):
        cents, px = 6_000_000, []
        for i in range(1200):
            cents += (3 if up else -3) if i >= 300 else (1 if i % 2 else -1)
            px.append(cents)
        spot = [px[0]] + [px[i] - px[i - 1] for i in range(1, len(px))]
        # market lags far behind the move, so the model sees a big edge
        mkt = [[o, 0.5] for o in range(330, 900, 60)]
        return {"start": start, "up": up, "q": f"w{start}", "fee": [0.07, 1], "spot": spot, "mkt": mkt}

    return {"generated": 0, "lookback": 300, "step": 900,
            "windows": [window(1_800_000_000, True), window(1_800_000_900, False)]}


def node(script: str) -> object:
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60, cwd=ROOT)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


@unittest.skipUnless(NODE, "node not installed")
class EngineParityTest(unittest.TestCase):
    def test_math_matches_python(self):
        got = node(f"""
            const GD = require({json.dumps(ENGINE)}), T = GD._t;
            const cases = {json.dumps(CASES)};
            console.log(JSON.stringify({{
              p: cases.map(c => T.probUp(...c)),
              fee: [0.1, 0.5, 0.83].map(p => T.feePerShare(0.07, 1, p)),
              kelly: [[0.6, 0.5], [0.4, 0.5], [0.9, 0.7]].map(a => T.kelly(...a)),
              reg: T.regression([0, 1, 2, 3, 4], [1, 2.9, 5.2, 7.1, 8.8]),
            }}));""")
        for js, c in zip(got["p"], CASES):
            self.assertAlmostEqual(js, prob_up(PricerInputs(*c)), places=6)
        fm = FeeModel(0.07, 1.0)
        for js, p in zip(got["fee"], [0.1, 0.5, 0.83]):
            self.assertAlmostEqual(js, fm.per_share(p), places=12)
        for js, (p, c) in zip(got["kelly"], [(0.6, 0.5), (0.4, 0.5), (0.9, 0.7)]):
            self.assertAlmostEqual(js, kelly_fraction(p, c), places=12)
        r = linear_regression([0, 1, 2, 3, 4], [1, 2.9, 5.2, 7.1, 8.8])
        self.assertAlmostEqual(got["reg"]["slope"], r.slope, places=12)
        self.assertAlmostEqual(got["reg"]["r2"], r.r2, places=12)

    def test_replay_ledger_arithmetic(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(synthetic_replay(), f)
        try:
            got = node(f"""
                const GD = require({json.dumps(ENGINE)}), fs = require("fs");
                const d = new GD.Desk(GD.decode(JSON.parse(fs.readFileSync({json.dumps(f.name)}))), {{supervisor: "off"}});
                const s = d.runToEnd();
                console.log(JSON.stringify({{s, pos: d.positions, eq: d.equity}}));""")
        finally:
            os.unlink(f.name)
        pos, s = got["pos"], got["s"]
        self.assertEqual([p["side"] for p in pos], ["UP", "DOWN"])  # one per window, right direction
        self.assertTrue(all(p["status"] == "won" for p in pos))
        cash = 500.0
        for p in pos:
            fee = p["shares"] * 0.07 * p["price"] * (1 - p["price"])
            self.assertAlmostEqual(p["fee"], fee, places=9)
            self.assertAlmostEqual(p["pnl"], p["shares"] - p["shares"] * p["price"] - fee, places=9)
            self.assertLessEqual(p["cost"], cash * 0.02 + 1e-9)  # 2% of bankroll at trade time
            cash += p["pnl"]
        self.assertAlmostEqual(s["bankroll"], 500 + sum(p["pnl"] for p in pos), places=9)
        self.assertEqual(len(got["eq"]), 3)
        self.assertEqual(s["trades"], 2)


class BuildTest(unittest.TestCase):
    def test_build_inlines_everything(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("webbuild", os.path.join(ROOT, "web", "build.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as d:
            data = os.path.join(d, "replay.json")
            with open(data, "w") as f:
                json.dump(synthetic_replay(), f)
            out = os.path.join(d, "page.html")
            mod.build(data, out)
            with open(out, encoding="utf-8") as f:
                html = f.read()
        self.assertTrue(html.startswith("<title>Grok Desk</title>"))
        self.assertIn("window.__REPLAY__=", html)
        self.assertIn("var GD = (function", html)
        self.assertNotIn("/*__DATA__*/", html)
        self.assertNotIn("module.exports", html)
        # the only external origins are Google Fonts
        hosts = {h for h in __import__("re").findall(r'(?:src|href)="(https?://[^/"]+)', html)}
        self.assertEqual(hosts, {"https://fonts.googleapis.com"})


if __name__ == "__main__":
    unittest.main()
