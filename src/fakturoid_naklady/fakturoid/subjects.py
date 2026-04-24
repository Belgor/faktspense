"""Fakturoid subject (vendor) lookup and creation with on-disk cache."""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

from ..models import VendorInfo
from .client import FakturoidClient


def default_cache_path(slug: str) -> Path:
    return Path.home() / ".cache" / "faktspense" / f"subjects_{slug}.json"


class SubjectStore:
    """Wraps paginated subject fetch, disk cache, IČO/fuzzy match, and create.

    Cache file layout:
        {"subjects": [ {raw subject JSON}, ... ]}

    IČO matching is an exact string compare on the ``registration_no`` field
    (Fakturoid's name for IČO). Fuzzy name matching uses ``difflib`` against
    the ``name`` field.
    """

    def __init__(
        self,
        *,
        client: FakturoidClient,
        cache_path: Path | None = None,
    ) -> None:
        self._client = client
        self._cache_path = cache_path or default_cache_path(client.slug)
        self._subjects: list[dict[str, Any]] | None = None
        self._loaded_from_cache = False

    # ------- cache -------

    def _load_cache(self) -> list[dict[str, Any]] | None:
        if not self._cache_path.exists():
            return None
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        subjects = data.get("subjects")
        return subjects if isinstance(subjects, list) else None

    def _write_cache(self, subjects: list[dict[str, Any]]) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"subjects": subjects}, ensure_ascii=False, indent=2)
        self._cache_path.write_text(payload, encoding="utf-8")

    # ------- fetch -------

    def _fetch_all(self) -> list[dict[str, Any]]:
        all_subjects: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = self._client.request(
                "GET",
                self._client.account_url("/subjects.json"),
                params={"page": page},
            )
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            all_subjects.extend(batch)
            page += 1
        return all_subjects

    def refresh(self) -> None:
        """Force re-fetch of all subjects from the API and update cache."""
        self._subjects = self._fetch_all()
        self._loaded_from_cache = False
        self._write_cache(self._subjects)

    def _ensure_loaded(self) -> list[dict[str, Any]]:
        if self._subjects is not None:
            return self._subjects
        cached = self._load_cache()
        if cached is not None:
            self._subjects = cached
            self._loaded_from_cache = True
            return cached
        self.refresh()
        assert self._subjects is not None
        return self._subjects

    # ------- lookup -------

    def find_by_ico(self, ico: str) -> dict[str, Any] | None:
        subjects = self._ensure_loaded()
        match = _match_ico(subjects, ico)
        if match is not None:
            return match
        # Only re-fetch if the initial load came from disk cache (might be stale).
        # If we just fetched fresh, there's nothing more to try.
        if not self._loaded_from_cache:
            return None
        self.refresh()
        return _match_ico(self._subjects or [], ico)

    def fuzzy_name_candidates(self, name: str, *, limit: int = 3) -> list[dict[str, Any]]:
        subjects = self._ensure_loaded()
        names = [s.get("name", "") for s in subjects]
        close = difflib.get_close_matches(name, names, n=limit, cutoff=0.6)
        return [s for s in subjects if s.get("name") in close]

    # ------- create -------

    def create(self, vendor: VendorInfo) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": vendor.name}
        if vendor.ico:
            payload["registration_no"] = vendor.ico
        if vendor.dic:
            payload["vat_no"] = vendor.dic
        if vendor.address:
            payload["street"] = vendor.address
        resp = self._client.request(
            "POST",
            self._client.account_url("/subjects.json"),
            json=payload,
        )
        created = resp.json()
        # append to in-memory + flush cache
        subjects = self._ensure_loaded()
        subjects.append(created)
        self._write_cache(subjects)
        return created


def _match_ico(subjects: list[dict[str, Any]], ico: str) -> dict[str, Any] | None:
    for s in subjects:
        if s.get("registration_no") == ico:
            return s
    return None
