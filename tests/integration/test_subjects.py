from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from pytest_httpx import HTTPXMock

from fakturoid_naklady.fakturoid.client import FakturoidClient
from fakturoid_naklady.fakturoid.subjects import SubjectStore
from fakturoid_naklady.models import VendorInfo

SubjectsCacheFactory = Callable[[list[dict]], Path]


def test_find_by_ico_uses_cache_when_present(
    fakturoid_client: FakturoidClient,
    httpx_mock: HTTPXMock,
    subjects_cache: SubjectsCacheFactory,
) -> None:
    cache = subjects_cache([{"id": 7, "name": "ACME", "registration_no": "12345678"}])
    store = SubjectStore(client=fakturoid_client, cache_path=cache)
    hit = store.find_by_ico("12345678")
    assert hit is not None
    assert hit["id"] == 7
    assert httpx_mock.get_requests() == []


def test_find_by_ico_cache_miss_triggers_refetch(
    fakturoid_client: FakturoidClient,
    httpx_mock: HTTPXMock,
    subjects_cache: SubjectsCacheFactory,
) -> None:
    cache = subjects_cache([])
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/subjects.json?page=1",
        json=[{"id": 11, "name": "New", "registration_no": "99999999"}],
    )
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/subjects.json?page=2",
        json=[],
    )

    store = SubjectStore(client=fakturoid_client, cache_path=cache)
    hit = store.find_by_ico("99999999")
    assert hit is not None
    assert hit["id"] == 11
    reloaded = json.loads(cache.read_text(encoding="utf-8"))
    assert reloaded["subjects"][0]["registration_no"] == "99999999"


def test_refresh_paginates(
    tmp_path: Path, fakturoid_client: FakturoidClient, httpx_mock: HTTPXMock
) -> None:
    full_page = [{"id": i, "name": f"s{i}", "registration_no": f"{i:08d}"} for i in range(1, 41)]
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/subjects.json?page=1",
        json=full_page,
    )
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/subjects.json?page=2",
        json=[{"id": 41, "name": "s41", "registration_no": "00000041"}],
    )
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/subjects.json?page=3",
        json=[],
    )
    store = SubjectStore(client=fakturoid_client, cache_path=tmp_path / "s.json")
    store.refresh()
    hit = store.find_by_ico("00000041")
    assert hit is not None


def test_create_posts_payload_and_updates_cache(
    fakturoid_client: FakturoidClient,
    httpx_mock: HTTPXMock,
    subjects_cache: SubjectsCacheFactory,
) -> None:
    cache = subjects_cache([])
    created = {"id": 42, "name": "New Co", "registration_no": "12345678"}
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/subjects.json",
        method="POST",
        json=created,
    )

    store = SubjectStore(client=fakturoid_client, cache_path=cache)
    out = store.create(VendorInfo(name="New Co", ico="12345678", dic="CZ12345678"))
    assert out["id"] == 42

    req = httpx_mock.get_request(method="POST")
    assert req is not None
    body = json.loads(req.content)
    assert body["name"] == "New Co"
    assert body["registration_no"] == "12345678"
    assert body["vat_no"] == "CZ12345678"

    data = json.loads(cache.read_text(encoding="utf-8"))
    assert data["subjects"][0]["id"] == 42


def test_fuzzy_name_candidates(
    fakturoid_client: FakturoidClient,
    subjects_cache: SubjectsCacheFactory,
) -> None:
    cache = subjects_cache(
        [
            {"id": 1, "name": "ACME s.r.o.", "registration_no": "11111111"},
            {"id": 2, "name": "Acme Czech s.r.o.", "registration_no": "22222222"},
            {"id": 3, "name": "Unrelated", "registration_no": "33333333"},
        ]
    )
    store = SubjectStore(client=fakturoid_client, cache_path=cache)
    candidates = store.fuzzy_name_candidates("ACME s.r.o")
    names = {c["name"] for c in candidates}
    assert "ACME s.r.o." in names
