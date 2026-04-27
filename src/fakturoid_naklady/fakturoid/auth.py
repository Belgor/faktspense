"""OAuth2 Client Credentials token provider for Fakturoid v3."""

from __future__ import annotations

from typing import Protocol

import httpx

OAUTH_TOKEN_URL = "https://app.fakturoid.cz/api/v3/oauth/token"


class TokenProvider(Protocol):
    def get(self) -> str: ...
    def invalidate(self) -> None: ...


class StaticTokenProvider:
    """Test-only provider that returns a fixed token."""

    def __init__(self, token: str) -> None:
        self._token = token

    def get(self) -> str:
        return self._token

    def invalidate(self) -> None:  # pragma: no cover - no-op
        pass


class OAuth2TokenProvider:
    """Fetches a bearer token via Client Credentials and caches it in memory."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        http: httpx.Client,
        user_agent: str,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._user_agent = user_agent
        self._token: str | None = None

    def get(self) -> str:
        if self._token is None:
            self._token = self._fetch()
        return self._token

    def invalidate(self) -> None:
        self._token = None

    def _fetch(self) -> str:
        resp = self._http.post(
            OAUTH_TOKEN_URL,
            json={"grant_type": "client_credentials"},
            auth=(self._client_id, self._client_secret),
            headers={"User-Agent": self._user_agent, "Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"OAuth response missing access_token: {data!r}")
        return token
