"""Polite HTTP fetching with an on-disk cache.

Bulk-processing a shelf of books means thousands of requests against free
public catalogues. Two rules keep that sustainable: never ask twice for the
same thing (cache), and never hammer a host (per-host rate limit).
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

LOG = logging.getLogger("blmeta.http")

USER_AGENT = (
    "blmeta/1.0 (Black Library metadata resolver; "
    "personal collection cataloguing; +https://github.com/gavinmcfall/bl-library-lookup)"
)

DEFAULT_TTL_SECONDS = 30 * 24 * 3600


class FetchError(RuntimeError):
    """A source could not be reached or returned an unusable response.

    Distinct from "the source answered and had nothing" -- that is a legitimate
    negative result, this is a failure to obtain evidence.
    """


@dataclass
class Response:
    url: str
    status: int
    body: str
    from_cache: bool = False


class Cache:
    """SQLite-backed response cache preserving raw evidence."""

    def __init__(self, path: Path | None, ttl: int = DEFAULT_TTL_SECONDS):
        self.path = path
        self.ttl = ttl
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS responses (
                       key TEXT PRIMARY KEY,
                       url TEXT NOT NULL,
                       status INTEGER NOT NULL,
                       body TEXT NOT NULL,
                       fetched_at REAL NOT NULL
                   )"""
            )
            self._conn.commit()

    @staticmethod
    def _key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def get(self, url: str, key: str | None = None) -> Response | None:
        if not self._conn:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT url, status, body, fetched_at FROM responses WHERE key = ?",
                (self._key(key or url),),
            ).fetchone()
        if not row:
            return None
        if self.ttl and (time.time() - row[3]) > self.ttl:
            return None
        return Response(url=row[0], status=row[1], body=row[2], from_cache=True)

    def put(self, response: Response, key: str | None = None) -> None:
        if not self._conn:
            return
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO responses (key, url, status, body, fetched_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    self._key(key or response.url),
                    response.url,
                    response.status,
                    response.body,
                    time.time(),
                ),
            )
            self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


class Fetcher:
    def __init__(
        self,
        cache: Cache | None = None,
        delay: float = 1.0,
        timeout: float = 45.0,
        retries: int = 3,
        refresh: bool = False,
    ):
        self.cache = cache
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.refresh = refresh
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()

    def _wait(self, host: str) -> None:
        with self._lock:
            last = self._last_hit.get(host, 0.0)
            gap = time.time() - last
            if gap < self.delay:
                time.sleep(self.delay - gap)
            self._last_hit[host] = time.time()

    def get(self, url: str, accept: str = "*/*") -> Response:
        return self._request(url, accept=accept)

    def post(
        self,
        url: str,
        body: str,
        headers: dict[str, str] | None = None,
        accept: str = "application/json",
    ) -> Response:
        """POST with the same cache and rate limiting as GET.

        The cache key covers url + body so distinct queries to one endpoint
        cache separately. Headers are deliberately excluded from the key and
        from storage: they are where credentials travel, and nothing secret
        may ever reach the on-disk cache.
        """
        cache_key = f"POST {url}\n{body}"
        return self._request(
            url, accept=accept, data=body, extra_headers=headers, cache_key=cache_key
        )

    def _request(
        self,
        url: str,
        accept: str,
        data: str | None = None,
        extra_headers: dict[str, str] | None = None,
        cache_key: str | None = None,
    ) -> Response:
        if self.cache and not self.refresh:
            cached = self.cache.get(url, key=cache_key)
            if cached:
                LOG.debug("cache hit %s", url)
                return cached

        host = urllib.parse.urlparse(url).netloc
        last_error: Exception | None = None

        for attempt in range(self.retries):
            self._wait(host)
            request_headers = {"User-Agent": USER_AGENT, "Accept": accept}
            if data is not None:
                request_headers["Content-Type"] = "application/json"
            if extra_headers:
                request_headers.update(extra_headers)
            request = urllib.request.Request(
                url,
                headers=request_headers,
                data=data.encode("utf-8") if data is not None else None,
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as handle:
                    charset = handle.headers.get_content_charset() or "utf-8"
                    body = handle.read().decode(charset, errors="replace")
                    response = Response(url=url, status=handle.status, body=body)
            except urllib.error.HTTPError as exc:
                # 404 is a real answer -- the resource does not exist. Anything
                # else in the 4xx/5xx range may be transient or a block.
                if exc.code == 404:
                    response = Response(url=url, status=404, body="")
                    if self.cache:
                        self.cache.put(response, key=cache_key)
                    return response
                last_error = exc
                LOG.debug("HTTP %s for %s (attempt %s)", exc.code, url, attempt + 1)
                if exc.code in (429, 503) and attempt < self.retries - 1:
                    time.sleep(2 ** (attempt + 1))
                    continue
                if attempt < self.retries - 1:
                    time.sleep(2**attempt)
                    continue
                raise FetchError(f"HTTP {exc.code} for {url}") from exc
            except Exception as exc:  # network, TLS, timeout
                last_error = exc
                LOG.debug("error for %s: %s (attempt %s)", url, exc, attempt + 1)
                if attempt < self.retries - 1:
                    time.sleep(2**attempt)
                    continue
                raise FetchError(f"{type(exc).__name__} for {url}: {exc}") from exc

            if self.cache:
                self.cache.put(response, key=cache_key)
            return response

        raise FetchError(f"exhausted retries for {url}: {last_error}")
