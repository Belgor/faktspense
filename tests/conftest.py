"""Shared test fixtures."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pymupdf
import pytest

from fakturoid_naklady.fakturoid.auth import StaticTokenProvider
from fakturoid_naklady.fakturoid.client import FakturoidClient

# ---------- HTTP / Fakturoid ----------


@pytest.fixture
def http_client() -> httpx.Client:
    with httpx.Client() as c:
        yield c


@pytest.fixture
def fakturoid_client(http_client: httpx.Client) -> FakturoidClient:
    return FakturoidClient(slug="acme", http=http_client, token_provider=StaticTokenProvider("tkn"))


# ---------- PDF fixtures ----------


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    """One-page PDF with a Czech text layer — used by renderer + CLI tests."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 72), "Dodavatel: ACME s.r.o.")
    out = tmp_path / "acme.pdf"
    doc.save(out)
    doc.close()
    return out


# ---------- Anthropic stub ----------


@dataclass
class _TextBlock:
    text: str
    type: str = "text"


class _StubMessage:
    def __init__(self, text: str) -> None:
        self.content = [_TextBlock(text=text)]


class _StubMessages:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _StubMessage:
        self.calls.append(kwargs)
        return _StubMessage(self._responses.pop(0))


class StubAnthropic:
    """Test double for `anthropic.Anthropic` — feeds pre-canned responses in order."""

    def __init__(self, responses: list[str] | str) -> None:
        if isinstance(responses, str):
            responses = [responses]
        self.messages = _StubMessages(responses)


# ---------- Subjects cache ----------


@pytest.fixture
def subjects_cache(tmp_path: Path):
    """Factory that writes a subjects cache file and returns its path."""

    def _write(subjects: list[dict[str, Any]]) -> Path:
        cache = tmp_path / "subjects.json"
        cache.write_text(json.dumps({"subjects": subjects}), encoding="utf-8")
        return cache

    return _write


@pytest.fixture
def patched_default_cache_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect SubjectStore's default cache to tmp_path so tests never touch ~/.cache."""
    import fakturoid_naklady.fakturoid.subjects as subj_mod

    cache = tmp_path / "subjects.json"
    monkeypatch.setattr(subj_mod, "default_cache_path", lambda slug: cache)
    return cache
