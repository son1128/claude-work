"""재시도·호출 간격 제한이 있는 간단한 JSON HTTP 클라이언트 (표준 라이브러리만 사용)."""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class HttpError(RuntimeError):
    def __init__(self, status, body, url):
        super().__init__(f"HTTP {status} {url}: {body[:300]}")
        self.status = status
        self.body = body
        self.url = url


class Http:
    """min_interval초 간격으로 호출하고, 네트워크 오류·429·5xx는 지수 백오프로 재시도합니다."""

    def __init__(self, base, min_interval=0.2, retries=5, timeout=30, sleep=time.sleep):
        self.base = base.rstrip("/")
        self.min_interval = min_interval
        self.retries = retries
        self.timeout = timeout
        self._sleep = sleep
        self._last = 0.0
        self._lock = threading.Lock()

    def _throttle(self):
        with self._lock:
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()

    def request(self, method, path, params=None, body=None, headers=None):
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self.base}{path}" + (f"?{query}" if query else "")
        data = None if body is None else (body if isinstance(body, (bytes, str)) else json.dumps(body))
        if isinstance(data, str):
            data = data.encode()
        hdrs = {"Accept": "application/json", "Content-Type": "application/json", **(headers or {})}
        for attempt in range(self.retries):
            self._throttle()
            req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read().decode()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries - 1:
                    self._sleep(min(30, 1.5 * 2 ** attempt))
                    continue
                raise HttpError(e.code, text, url) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == self.retries - 1:
                    raise
                self._sleep(min(30, 1.5 * 2 ** attempt))

    def get(self, path, params=None, headers=None):
        return self.request("GET", path, params=params, headers=headers)
