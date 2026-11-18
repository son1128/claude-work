import json
import os
from typing import Any, Callable

from grokdesk.http import HttpError

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name: str) -> Any:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


class FakeClock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeTransport:
    """Routes URLs to handlers by substring; records every call."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, Callable[[str, Any], Any]]] = []
        self.calls: list[tuple[str, str, Any, dict]] = []

    def on(self, needle: str, handler: Callable[[str, Any], Any] | Any) -> "FakeTransport":
        fn = handler if callable(handler) else (lambda url, body, _v=handler: _v)
        self.routes.insert(0, (needle, fn))
        return self

    def _route(self, method: str, url: str, body: Any, headers: dict | None) -> Any:
        self.calls.append((method, url, body, dict(headers or {})))
        for needle, fn in self.routes:
            if needle in url:
                out = fn(url, body)
                if isinstance(out, Exception):
                    raise out
                return out
        raise HttpError(url, 404, "no fake route")

    def get_json(self, url, headers=None):
        return self._route("GET", url, None, headers)

    def post_json(self, url, payload, headers=None):
        return self._route("POST", url, payload, headers)
