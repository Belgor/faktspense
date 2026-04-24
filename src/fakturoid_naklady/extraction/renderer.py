"""PyMuPDF-based PDF rendering: pages → PNG bytes + text-layer extraction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pymupdf

DEFAULT_DPI = 200
MAX_PAGES = 5


@dataclass(frozen=True)
class RenderedPdf:
    pages_png: list[bytes]
    text: str  # concatenated text layer across rendered pages, empty if scan-only


def render_pdf(path: Path, *, dpi: int = DEFAULT_DPI, max_pages: int = MAX_PAGES) -> RenderedPdf:
    pages_png: list[bytes] = []
    text_parts: list[str] = []
    zoom = dpi / 72  # PyMuPDF default is 72 DPI
    matrix = pymupdf.Matrix(zoom, zoom)
    with pymupdf.open(path) as doc:
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            pages_png.append(pix.tobytes("png"))
            text_parts.append(page.get_text("text"))
    return RenderedPdf(pages_png=pages_png, text="\n".join(text_parts).strip())
