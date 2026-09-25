"""Minimal HTTP client built on urllib (no third-party dependencies)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__

log = logging.getLogger(__name__)

USER_AGENT = f"silverWRX03/mc-server-management/{__version__} (+https://github.com/silverWRX03/mc-server-management)"


class HttpError(Exception):
    def __init__(self, url: str, status: int | None, message: str):
        super().__init__(f"{message} ({url})")
        self.url = url
        self.status = status


class HashMismatch(Exception):
    pass


def with_query(url: str, params: dict[str, Any] | None) -> str:
    if not params:
        return url
    return f"{url}?{urllib.parse.urlencode(params)}"


RATE_LIMIT_DELAYS = (5, 10, 20, 30)
RATE_LIMIT_RETRIES = len(RATE_LIMIT_DELAYS)
MAX_RATE_LIMIT_DELAY = 60


def _retry_after(e: urllib.error.HTTPError) -> float | None:
    try:
        value = float((e.headers or {}).get("Retry-After", ""))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


class HttpClient:
    """GET/POST JSON and verified downloads, with retries and a short-lived GET cache.

    The cache lets one update check ask about many Minecraft versions without
    re-fetching the same data; it expires so a long-running daemon sees new releases.
    """

    def __init__(self, retries: int = 3, timeout: float = 30.0, cache_ttl: float = 300.0,
                 rate_limit_retries: int = RATE_LIMIT_RETRIES):
        self.retries = retries
        self.rate_limit_retries = rate_limit_retries
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, Any]] = {}

    def clear_cache(self) -> None:
        self._cache.clear()

    def _open(self, req: urllib.request.Request):
        last: Exception | None = None
        attempt = limited = 0
        while attempt < self.retries:
            try:
                return urllib.request.urlopen(req, timeout=self.timeout)
            except urllib.error.HTTPError as e:
                # 4xx (other than rate limiting) will not succeed on retry.
                if e.code != 429 and e.code < 500:
                    raise HttpError(req.full_url, e.code, f"HTTP {e.code}") from e
                last = e
                if e.code == 429 and limited < self.rate_limit_retries:
                    # Rate limited (Mojang's lookup API does this readily): wait as asked, or long
                    # enough for the limit to reset, without using up the normal retries.
                    delay = min(_retry_after(e) or RATE_LIMIT_DELAYS[limited], MAX_RATE_LIMIT_DELAY)
                    limited += 1
                    log.info("%s is busy (rate limited); trying again in %ss", req.host, delay)
                    time.sleep(delay)
                    continue
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = e
            delay = 2**attempt
            attempt += 1
            if attempt < self.retries:
                log.debug("request to %s failed (%s); retrying in %ss", req.full_url, last, delay)
                time.sleep(delay)
        status = last.code if isinstance(last, urllib.error.HTTPError) else None
        raise HttpError(req.full_url, status, f"request failed: {last}")

    def get_json(self, url: str, params: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None) -> Any:
        full = with_query(url, params)
        hit = self._cache.get(full)
        if hit and time.monotonic() - hit[0] < self.cache_ttl:
            return hit[1]
        req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                                    **(headers or {})})
        with self._open(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self._cache[full] = (time.monotonic(), data)
        return data

    def post_json(self, url: str, body: Any, headers: dict[str, str] | None = None) -> Any:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json",
                     "Accept": "application/json", **(headers or {})})
        with self._open(req) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def download(self, url: str, dest: Path, sha1: str | None = None, sha512: str | None = None,
                 headers: dict[str, str] | None = None, sha256: str | None = None) -> Path:
        """Download to ``dest`` atomically, verifying hashes when given."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
        h1, h256, h512 = hashlib.sha1(), hashlib.sha256(), hashlib.sha512()
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".part-")
        try:
            with self._open(req) as resp, os.fdopen(fd, "wb") as out:
                while chunk := resp.read(1 << 16):
                    h1.update(chunk)
                    h256.update(chunk)
                    h512.update(chunk)
                    out.write(chunk)
            if sha1 and h1.hexdigest() != sha1.lower():
                raise HashMismatch(f"sha1 mismatch for {url}: expected {sha1}, got {h1.hexdigest()}")
            if sha256 and h256.hexdigest() != sha256.lower():
                raise HashMismatch(f"sha256 mismatch for {url}")
            if sha512 and h512.hexdigest() != sha512.lower():
                raise HashMismatch(f"sha512 mismatch for {url}")
            shutil.move(tmp, dest)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return dest


def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 16):
            h.update(chunk)
    return h.hexdigest()
