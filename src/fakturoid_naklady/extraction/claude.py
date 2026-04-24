"""Claude-API-based extraction: rendered PDF → ExtractedInvoice."""

from __future__ import annotations

import base64
import json
import os
import re
from typing import Any

from pydantic import ValidationError

from ..models import ExtractedInvoice
from .renderer import RenderedPdf

DEFAULT_MODEL = "claude-haiku-4-5"
MAX_TOKENS = 4096

SYSTEM_PROMPT = """You extract structured invoice data from Czech supplier invoices (faktury/účty).

Return ONLY a single JSON object matching this schema:

{
  "vendor": {
    "name": string,
    "ico": string (8 digits, optional — the Czech IČO / company registration number),
    "dic": string (optional — Czech DIČ / VAT ID, usually starts with CZ),
    "address": string (optional)
  },
  "invoice_number": string,
  "issued_on": "YYYY-MM-DD",
  "due_date": "YYYY-MM-DD" (optional),
  "taxable_fulfillment_due": "YYYY-MM-DD" (optional — DUZP / Datum uskutečnění zdanitelného plnění),
  "currency": string (ISO, default "CZK"),
  "lines": [
    {
      "name": string,
      "quantity": string (numeric),
      "unit_name": string (optional, e.g. "ks", "h", "kg"),
      "unit_price": string (numeric, tax-exclusive if stated),
      "vat_rate": integer (0, 10, 12, 15, or 21 — Czech VAT rates)
    }
  ],
  "total": number (optional, total including VAT),
  "total_vat": number (optional, VAT amount)
}

Rules:
- IČO is always 8 digits. Preserve leading zeros.
- Use vendor (supplier, "dodavatel"), not customer ("odběratel").
- Amounts: use "." as decimal separator; strip thousands separators.
- Emit ONLY the JSON object, no prose, no markdown fences.
"""


class ClaudeExtractor:
    """Wraps the Anthropic SDK; call `extract(rendered, source_pdf)` to get an ExtractedInvoice."""

    def __init__(
        self,
        *,
        client: Any,
        model: str | None = None,
    ) -> None:
        self._client = client
        self._model = model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    def extract(self, rendered: RenderedPdf) -> ExtractedInvoice:
        """Send pages + text layer to Claude; return ExtractedInvoice. One retry on bad JSON."""
        content = _build_content(rendered)
        raw = self._call(content, extra_instruction=None)
        try:
            return _parse(raw)
        except (json.JSONDecodeError, ValidationError) as err:
            retry_raw = self._call(
                content,
                extra_instruction=(
                    f"Previous response failed validation: {err}. "
                    "Return ONLY the corrected JSON object."
                ),
            )
            return _parse(retry_raw)

    def _call(self, content: list[dict[str, Any]], *, extra_instruction: str | None) -> str:
        system = SYSTEM_PROMPT
        if extra_instruction:
            system = f"{SYSTEM_PROMPT}\n\n{extra_instruction}"
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": content}],
        )
        # SDK returns a Message with .content = [TextBlock, ...]
        blocks = getattr(resp, "content", [])
        texts = [getattr(b, "text", "") for b in blocks if getattr(b, "type", None) == "text"]
        return "".join(texts).strip()


def _build_content(rendered: RenderedPdf) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for png in rendered.pages_png:
        items.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(png).decode("ascii"),
                },
            }
        )
    prompt = "Extract the invoice as JSON."
    if len(rendered.text) > 100:
        prompt += f"\n\nText layer (may help disambiguate OCR):\n---\n{rendered.text}\n---"
    items.append({"type": "text", "text": prompt})
    return items


def _parse(raw: str) -> ExtractedInvoice:
    """Tolerate ```json fences just in case, then validate."""
    cleaned = _strip_fences(raw)
    data = json.loads(cleaned)
    return ExtractedInvoice.model_validate(data)


def _strip_fences(raw: str) -> str:
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, flags=re.DOTALL)
    return (m.group(1) if m else raw).strip()
