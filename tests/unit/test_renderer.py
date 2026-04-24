from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from fakturoid_naklady.extraction.renderer import render_pdf


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    """Generate a 2-page PDF with some Czech text so the text layer is non-empty."""
    doc = pymupdf.open()
    for i in range(2):
        page = doc.new_page()
        page.insert_text((50, 72), f"Page {i + 1} — Dodavatel: ACME s.r.o.")
    out = tmp_path / "sample.pdf"
    doc.save(out)
    doc.close()
    return out


def test_render_pdf_returns_png_per_page(sample_pdf: Path) -> None:
    rendered = render_pdf(sample_pdf)
    assert len(rendered.pages_png) == 2
    for png in rendered.pages_png:
        assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_render_pdf_extracts_text(sample_pdf: Path) -> None:
    rendered = render_pdf(sample_pdf)
    assert "Dodavatel" in rendered.text
    assert "ACME" in rendered.text


def test_render_pdf_caps_at_max_pages(tmp_path: Path) -> None:
    doc = pymupdf.open()
    for _ in range(8):
        page = doc.new_page()
        page.insert_text((50, 72), "x")
    pdf = tmp_path / "big.pdf"
    doc.save(pdf)
    doc.close()
    rendered = render_pdf(pdf, max_pages=3)
    assert len(rendered.pages_png) == 3
