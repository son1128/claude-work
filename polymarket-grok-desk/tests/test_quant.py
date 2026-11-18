import math
import random
import unittest

from grokdesk.quant import (
    FeeModel,
    PricerInputs,
    kelly_fraction,
    linear_regression,
    norm_cdf,
    prob_up,
    realized_vol_per_sec,
)


class RegressionTest(unittest.TestCase):
    def test_perfect_line(self):
        r = linear_regression([0, 1, 2, 3], [1, 3, 5, 7])
        self.assertAlmostEqual(r.slope, 2.0)
        self.assertAlmostEqual(r.intercept, 1.0)
        self.assertAlmostEqual(r.r2, 1.0)

    def test_noise_has_low_r2(self):
        rng = random.Random(1)
        xs = list(range(200))
        ys = [rng.gauss(0, 1) for _ in xs]
        self.assertLess(linear_regression(xs, ys).r2, 0.05)

    def test_degenerate_inputs(self):
        self.assertEqual(linear_regression([1, 2], [1, 2]).r2, 0.0)
        self.assertEqual(linear_regression([1, 1, 1], [1, 2, 3]).slope, 0.0)
        self.assertEqual(linear_regression([1, 2, 3], [5, 5, 5]).r2, 0.0)
        with self.assertRaises(ValueError):
            linear_regression([1, 2, 3], [1, 2])


class VolTest(unittest.TestCase):
    def test_recovers_known_sigma(self):
        rng = random.Random(7)
        sigma = 0.0002
        p, ts, ps = 60000.0, [], []
        for i in range(20000):
            ts.append(float(i))
            ps.append(p)
            p *= math.exp(rng.gauss(0, sigma))
        self.assertAlmostEqual(realized_vol_per_sec(ts, ps), sigma, delta=sigma * 0.03)

    def test_uneven_sampling_scales(self):
        # 2-second steps with sigma*sqrt(2) moves -> same per-second vol
        ts = [0, 2, 4, 6, 8]
        r = 0.001 * math.sqrt(2)
        ps = [100.0]
        for i in range(4):
            ps.append(ps[-1] * math.exp(r if i % 2 else -r))
        self.assertAlmostEqual(realized_vol_per_sec(ts, ps), 0.001, places=6)


class ProbUpTest(unittest.TestCase):
    base = dict(spot=60000.0, open_price=60000.0, seconds_left=300, vol_per_sec=0.0001, basis_bps=0.0)

    def test_at_the_money_is_half(self):
        self.assertAlmostEqual(prob_up(PricerInputs(**self.base)), 0.5, places=6)

    def test_monotone_in_spot(self):
        lo = prob_up(PricerInputs(**{**self.base, "spot": 59950}))
        hi = prob_up(PricerInputs(**{**self.base, "spot": 60050}))
        self.assertLess(lo, 0.5)
        self.assertGreater(hi, 0.5)

    def test_matches_closed_form_outside_twap_window(self):
        inp = PricerInputs(**{**self.base, "spot": 60030})
        tau, L, sig = 300, 60, 0.0001
        z = math.log(60030 / 60000) / (sig * math.sqrt(tau - L + L / 3))
        self.assertAlmostEqual(prob_up(inp), norm_cdf(z), places=9)

    def test_less_time_means_more_confidence(self):
        far = prob_up(PricerInputs(**{**self.base, "spot": 60030, "seconds_left": 600}))
        near = prob_up(PricerInputs(**{**self.base, "spot": 60030, "seconds_left": 90}))
        self.assertGreater(near, far)

    def test_inside_twap_window_uses_realized_average(self):
        # Spot just dipped below the strike but the TWAP so far is well above it.
        inp = PricerInputs(**{**self.base, "spot": 59990, "seconds_left": 10, "realized_twap": 60040})
        self.assertGreater(prob_up(inp), 0.9)

    def test_basis_pulls_toward_half(self):
        tight = prob_up(PricerInputs(**{**self.base, "spot": 60010, "seconds_left": 30}))
        loose = prob_up(PricerInputs(**{**self.base, "spot": 60010, "seconds_left": 30, "basis_bps": 5}))
        self.assertLess(abs(loose - 0.5), abs(tight - 0.5))

    def test_positive_drift_raises_p(self):
        self.assertGreater(prob_up(PricerInputs(**{**self.base, "drift_per_sec": 1e-6})), 0.5)

    def test_clipped(self):
        p = prob_up(PricerInputs(**{**self.base, "spot": 70000}))
        self.assertEqual(p, 0.99)

    def test_rejects_bad_prices(self):
        with self.assertRaises(ValueError):
            prob_up(PricerInputs(**{**self.base, "open_price": 0}))


class FeeKellyTest(unittest.TestCase):
    def test_fee_peaks_at_half(self):
        f = FeeModel(0.07, 1.0)
        self.assertAlmostEqual(f.per_share(0.5), 0.0175)
        self.assertLess(f.per_share(0.9), f.per_share(0.5))
        self.assertEqual(FeeModel(enabled=False).per_share(0.5), 0.0)

    def test_kelly(self):
        self.assertAlmostEqual(kelly_fraction(0.6, 0.5), 0.2)
        self.assertEqual(kelly_fraction(0.4, 0.5), 0.0)
        self.assertEqual(kelly_fraction(0.9, 1.0), 0.0)


if __name__ == "__main__":
    unittest.main()
