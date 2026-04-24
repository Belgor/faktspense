"""Thin httpx wrapper for Fakturoid v3 — UA, auth, 401 refetch, 429 backoff."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .auth import TokenProvider

USER_AGENT = "faktspense/0.1"
API_BASE = "https://app.fakturoid.cz/api/v3"

log = logging.getLogger(__name__)


class FakturoidError(Exception):
    """Raised when a Fakturoid request fails after retries."""

    def __init__(self, message: str, *, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class FakturoidClient:
    """Authenticated HTTP client.

    Automatically handles:
    - User-Agent header on every request
    - Bearer token injection via ``TokenProvider``
    - One 401 retry with token invalidation
    - One 429 retry respecting ``Retry-After``
    """

    def __init__(
        self,
        *,
        slug: str,
        http: httpx.Client,
        token_provider: TokenProvider,
        user_agent: str = USER_AGENT,
        sleep: Any = time.sleep,
    ) -> None:
        self._slug = slug
        self._http = http
        self._tokens = token_provider
        self._ua = user_agent
        self._sleep = sleep

    @property
    def slug(self) -> str:
        return self._slug

    def account_url(self, path: str) -> str:
        return f"{API_BASE}/accounts/{self._slug}{path}"

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        resp = self._send(method, url, json=json, params=params)

        if resp.status_code == 401:
            self._tokens.invalidate()
            resp = self._send(method, url, json=json, params=params)

        if resp.status_code == 429:
            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
            log.warning("Fakturoid 429 — sleeping %ss before retry", retry_after)
            self._sleep(retry_after)
            resp = self._send(method, url, json=json, params=params)

        _log_rate_limit(resp)

        if resp.status_code >= 400:
            raise FakturoidError(
                f"{method} {url} failed with {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text,
            )
        return resp

    def _send(
        self,
        method: str,
        url: str,
        *,
        json: Any,
        params: dict[str, Any] | None,
    ) -> httpx.Response:
        headers = {
            "User-Agent": self._ua,
            "Accept": "application/json",
            "Authorization": f"Bearer {self._tokens.get()}",
        }
        return self._http.request(method, url, json=json, params=params, headers=headers)


def _parse_retry_after(raw: str | None) -> float:
    if not raw:
        return 1.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 1.0


def _log_rate_limit(resp: httpx.Response) -> None:
    remaining = resp.headers.get("X-RateLimit-Remaining")
    if remaining is not None:
        log.debug("Fakturoid rate-limit remaining=%s", remaining)
