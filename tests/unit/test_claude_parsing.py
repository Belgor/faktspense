from __future__ import annotations

import json

import pytest

from fakturoid_naklady.extraction.claude import ClaudeExtractor
from fakturoid_naklady.extraction.renderer import RenderedPdf
from tests.conftest import StubAnthropic

_VALID_JSON = json.dumps(
    {
        "vendor": {"name": "ACME", "ico": "12345678", "dic": "CZ12345678"},
        "invoice_number": "2024-0042",
        "issued_on": "2024-03-15",
        "due_date": "2024-03-29",
        "currency": "CZK",
        "lines": [
            {
                "name": "Konzultační práce",
                "quantity": "10",
                "unit_name": "h",
                "unit_price": "1000",
                "vat_rate": 21,
            }
        ],
        "total": 12100,
        "total_vat": 2100,
    }
)


def test_extract_happy_path() -> None:
    stub = StubAnthropic([_VALID_JSON])
    ex = ClaudeExtractor(client=stub, model="claude-haiku-4-5")
    result = ex.extract(RenderedPdf(pages_png=[b"\x89PNG"], text=""))
    assert result.vendor.ico == "12345678"
    assert result.invoice_number == "2024-0042"
    assert len(stub.messages.calls) == 1


def test_extract_strips_code_fences() -> None:
    fenced = f"```json\n{_VALID_JSON}\n```"
    stub = StubAnthropic([fenced])
    ex = ClaudeExtractor(client=stub)
    result = ex.extract(RenderedPdf(pages_png=[b"\x89PNG"], text=""))
    assert result.invoice_number == "2024-0042"


def test_extract_retries_once_on_bad_json() -> None:
    stub = StubAnthropic(["not json at all", _VALID_JSON])
    ex = ClaudeExtractor(client=stub)
    result = ex.extract(RenderedPdf(pages_png=[b"\x89PNG"], text=""))
    assert result.invoice_number == "2024-0042"
    assert len(stub.messages.calls) == 2


def test_extract_gives_up_after_two_attempts() -> None:
    stub = StubAnthropic(["not json", "still not json"])
    ex = ClaudeExtractor(client=stub)
    with pytest.raises(json.JSONDecodeError):
        ex.extract(RenderedPdf(pages_png=[b"\x89PNG"], text=""))


def test_text_layer_included_when_substantial() -> None:
    stub = StubAnthropic([_VALID_JSON])
    ex = ClaudeExtractor(client=stub)
    big_text = "Dodavatel: ACME\n" * 20
    ex.extract(RenderedPdf(pages_png=[b"\x89PNG"], text=big_text))
    msg = stub.messages.calls[0]["messages"][0]["content"]
    text_items = [c for c in msg if c["type"] == "text"]
    assert any("Text layer" in t["text"] for t in text_items)


def test_text_layer_skipped_when_short() -> None:
    stub = StubAnthropic([_VALID_JSON])
    ex = ClaudeExtractor(client=stub)
    ex.extract(RenderedPdf(pages_png=[b"\x89PNG"], text="tiny"))
    msg = stub.messages.calls[0]["messages"][0]["content"]
    text_items = [c for c in msg if c["type"] == "text"]
    assert all("Text layer" not in t["text"] for t in text_items)
