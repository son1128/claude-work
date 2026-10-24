"""TradingView 알림 수신기.

ppp-oscillator-v2.pine의 alert() JSON을 그대로 받습니다:
  {"symbol":"BTCUSDT.P","tf":"15","side":"long","score":70,"price":80123.4,"stop":79500,"l1":1}
TradingView 알림은 헤더를 못 붙이므로, 토큰은 URL(?token=...)이나 본문의 "token"으로 확인합니다.
기본은 127.0.0.1에서만 받습니다. 외부에서 받으려면 앞단에 HTTPS 프록시를 두세요.
"""
import hmac
import json
import logging
import re
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .models import Signal

log = logging.getLogger("ppp2.webhook")
TF_MAP = {"1": "1m", "5": "5m", "15": "15m", "30": "30m", "60": "1h", "240": "4h", "D": "1d", "1D": "1d"}


def normalize_symbol(s):
    """BTCUSDT.P, BINANCE:BTCUSDT, BTC_USDT, BTC-USDT, BTCUSDT.PERP → BTC."""
    s = s.upper().split(":")[-1]
    s = re.sub(r"\.(P|PERP)$", "", s)
    s = re.sub(r"[-_]?PERP$", "", s)
    s = re.sub(r"[-_]?USDT$", "", s)
    return s.strip("-_")


def parse_alert(payload, now_ms):
    side = {"long": 1, "buy": 1, "short": -1, "sell": -1}.get(str(payload.get("side", "")).lower())
    if side is None:
        raise ValueError("side는 long/short 이어야 합니다")
    price = float(payload["price"])
    stop = payload.get("stop")
    l1 = payload.get("l1")
    return Signal(symbol=normalize_symbol(str(payload["symbol"])), side=side, price=price, ts_ms=int(payload.get("ts_ms") or now_ms),
                  stop=float(stop) if stop not in (None, "") else None, tf=TF_MAP.get(str(payload.get("tf", "")), str(payload.get("tf", ""))),
                  score=float(payload["score"]) if payload.get("score") not in (None, "") else None,
                  l1=int(l1) if l1 not in (None, "") else None, source="webhook")


def make_handler(engine, token):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def do_GET(self):
            if urllib.parse.urlparse(self.path).path == "/health":
                h, why = engine.halted()
                return self._reply(200, {"ok": True, "halted": h, "reason": why, "open": len(engine.broker.open_positions())})
            self._reply(404, {"error": "not found"})

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            if u.path != "/signal":
                return self._reply(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > 10_000:
                return self._reply(413, {"error": "too large"})
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._reply(400, {"error": "JSON이 아닙니다"})
            given = urllib.parse.parse_qs(u.query).get("token", [""])[0] or str(payload.pop("token", ""))
            if not token or not hmac.compare_digest(given, token):
                return self._reply(401, {"error": "토큰이 맞지 않습니다"})
            try:
                sig = parse_alert(payload, int(time.time() * 1000))
            except (KeyError, ValueError, TypeError) as e:
                return self._reply(400, {"error": f"신호 형식 오류: {e}"})
            try:
                d = engine.submit(sig)
            except Exception as e:
                return self._reply(500, {"error": f"주문 처리 오류: {e}"})
            self._reply(200, {"approved": d.approved, "summary": d.summary(),
                              "failed": [c.code for c in d.failed]})
    return Handler


def serve(engine, host, port, token):
    if not token:
        raise RuntimeError("웹훅 토큰이 없습니다 (환경 변수 PPP2_WEBHOOK_TOKEN)")
    srv = ThreadingHTTPServer((host, port), make_handler(engine, token))
    log.info("웹훅 수신 대기 http://%s:%d/signal", host, port)
    return srv
