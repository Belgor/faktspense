from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from fakturoid_naklady.fakturoid.auth import OAuth2TokenProvider, StaticTokenProvider
from fakturoid_naklady.fakturoid.client import USER_AGENT, FakturoidClient, FakturoidError


def test_user_agent_and_auth_header_present(
    httpx_mock: HTTPXMock, fakturoid_client: FakturoidClient
) -> None:
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/subjects.json?page=1",
        json=[],
    )
    fakturoid_client.request(
        "GET", fakturoid_client.account_url("/subjects.json"), params={"page": 1}
    )
    req = httpx_mock.get_request()
    assert req is not None
    assert req.headers["User-Agent"] == USER_AGENT
    assert req.headers["Authorization"] == "Bearer tkn"


def test_retries_on_401_with_token_refetch(
    httpx_mock: HTTPXMock, http_client: httpx.Client
) -> None:
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/oauth/token",
        json={"access_token": "fresh", "token_type": "Bearer", "expires_in": 7200},
    )
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/subjects.json",
        status_code=401,
    )
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/subjects.json",
        json=[{"id": 1, "name": "ACME", "registration_no": "12345678"}],
    )

    tp = OAuth2TokenProvider(
        client_id="cid", client_secret="sec", http=http_client, user_agent=USER_AGENT
    )
    # Pre-seed a stale token so the first request exercises the 401 → refetch path.
    tp._token = "stale"  # type: ignore[attr-defined]

    client = FakturoidClient(slug="acme", http=http_client, token_provider=tp)
    resp = client.request("GET", client.account_url("/subjects.json"))
    assert resp.status_code == 200
    assert resp.json()[0]["registration_no"] == "12345678"


def test_retries_on_429_with_backoff(httpx_mock: HTTPXMock, http_client: httpx.Client) -> None:
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/subjects.json",
        status_code=429,
        headers={"Retry-After": "0"},
    )
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/subjects.json",
        json=[],
    )

    slept: list[float] = []
    client = FakturoidClient(
        slug="acme",
        http=http_client,
        token_provider=StaticTokenProvider("tkn"),
        sleep=slept.append,
    )
    resp = client.request("GET", client.account_url("/subjects.json"))
    assert resp.status_code == 200
    assert slept == [0.0]


def test_raises_on_persistent_4xx(httpx_mock: HTTPXMock, fakturoid_client: FakturoidClient) -> None:
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/expenses.json",
        status_code=422,
        json={"errors": {"custom_id": ["is required"]}},
    )
    with pytest.raises(FakturoidError) as ei:
        fakturoid_client.request("POST", fakturoid_client.account_url("/expenses.json"), json={})
    assert ei.value.status_code == 422
    assert "custom_id" in (ei.value.body or "")


def test_oauth_token_fetch_includes_user_agent(
    httpx_mock: HTTPXMock, http_client: httpx.Client
) -> None:
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/oauth/token",
        json={"access_token": "t", "token_type": "Bearer", "expires_in": 7200},
    )
    tp = OAuth2TokenProvider(
        client_id="cid", client_secret="sec", http=http_client, user_agent=USER_AGENT
    )
    assert tp.get() == "t"
    req = httpx_mock.get_request()
    assert req is not None
    assert req.headers["User-Agent"] == USER_AGENT
