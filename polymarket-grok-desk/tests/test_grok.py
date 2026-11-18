import os
import unittest

from grokdesk.grok import GrokConfig, GrokSupervisor, parse_verdict
from grokdesk.http import HttpError
from tests.helpers import FakeClock, FakeTransport


def reply(text, pin=1000, pout=50):
    return {"choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": pin, "completion_tokens": pout}}


class ParseTest(unittest.TestCase):
    def test_parse(self):
        self.assertTrue(parse_verdict('{"decision":"APPROVE","reason":"ok"}').approve)
        v = parse_verdict('Sure!\n```json\n{"decision": "veto", "reason": "thin book"}\n```')
        self.assertFalse(v.approve)
        self.assertEqual(v.reason, "thin book")
        self.assertIsNone(parse_verdict("approve it"))
        self.assertIsNone(parse_verdict('{"decision":"MAYBE"}'))


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        os.environ["XAI_API_KEY"] = "test-key"
        self.t = FakeTransport()
        self.cfg = GrokConfig(enabled=True, daily_budget_usd=0.01,
                              input_price_per_m=3.0, output_price_per_m=15.0)

    def tearDown(self):
        os.environ.pop("XAI_API_KEY", None)

    def sup(self, **kw):
        return GrokSupervisor(GrokConfig(**{**self.cfg.__dict__, **kw}), self.t, clock=FakeClock(1e9))

    def test_disabled_approves_without_calling(self):
        v = self.sup(enabled=False).review({})
        self.assertTrue(v.approve)
        self.assertEqual(self.t.calls, [])

    def test_request_shape_and_cost(self):
        self.t.on("/chat/completions", reply('{"decision":"APPROVE","reason":"fine"}'))
        s = self.sup()
        v = s.review({"x": 1})
        self.assertTrue(v.approve)
        self.assertEqual(v.source, "grok")
        method, url, body, headers = self.t.calls[0]
        self.assertEqual(url, "https://api.x.ai/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertEqual(body["model"], "grok-4")
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertAlmostEqual(v.cost_usd, (1000 * 3 + 50 * 15) / 1e6)

    def test_budget_exhaustion_fails_closed(self):
        self.t.on("/chat/completions", reply('{"decision":"APPROVE","reason":"ok"}', pin=4000))
        s = self.sup()
        for _ in range(3):
            s.review({})
        v = s.review({})
        self.assertFalse(v.approve)
        self.assertEqual(v.source, "budget")
        self.assertEqual(len(self.t.calls), 1)  # 0.012 > 0.01 after the first call

    def test_failures_follow_fail_mode(self):
        self.t.on("/chat/completions", HttpError("u", 503))
        self.assertFalse(self.sup().review({}).approve)
        self.assertTrue(self.sup(fail_mode="approve").review({}).approve)
        self.t.on("/chat/completions", reply("I think this is fine"))
        self.assertEqual(self.sup().review({}).source, "fail_closed")

    def test_missing_key(self):
        os.environ.pop("XAI_API_KEY")
        v = self.sup().review({})
        self.assertFalse(v.approve)
        self.assertIn("XAI_API_KEY", v.reason)


if __name__ == "__main__":
    unittest.main()
