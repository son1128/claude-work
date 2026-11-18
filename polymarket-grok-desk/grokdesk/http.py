"""Tiny JSON-over-HTTP helper (stdlib only).

Everything that talks to the network goes through a ``Transport`` so tests can
swap in a fake without patching urllib.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping, Protocol

USER_AGENT = "grok-desk/0.1 (+paper-trading research bot)"


class HttpError(RuntimeError):
    def __init__(self, url: str, status: int | None, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.url = url
        self.status = status
        self.body = body


class Transport(Protocol):
    def get_json(self, url: str, headers: Mapping[str, str] | None = None) -> Any: ...

    def post_json(
        self, url: str, payload: Any, headers: Mapping[str, str] | None = None
    ) -> Any: ...


class UrllibTransport:
    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    def _send(self, req: urllib.request.Request) -> Any:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace") if e.fp else ""
            raise HttpError(req.full_url, e.code, body) from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise HttpError(req.full_url, None, str(e)) from e

    def get_json(self, url: str, headers: Mapping[str, str] | None = None) -> Any:
        h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        h.update(headers or {})
        return self._send(urllib.request.Request(url, headers=h))

    def post_json(
        self, url: str, payload: Any, headers: Mapping[str, str] | None = None
    ) -> Any:
        h = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        h.update(headers or {})
        data = json.dumps(payload).encode("utf-8")
        return self._send(urllib.request.Request(url, data=data, headers=h, method="POST"))
