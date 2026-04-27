"""Pass 1: arithmetic cross-check — recomputes line totals and compares to the extracted total."""

from __future__ import annotations

from decimal import Decimal

from ..models import ExtractedInvoice, InvoiceLine, ValidationWarning

TOTAL_ABS_THRESHOLD = Decimal("0.50")
TOTAL_REL_THRESHOLD = Decimal("0.01")


def compute_lines_total(lines: list[InvoiceLine]) -> Decimal:
    return sum((line.quantity * line.unit_price for line in lines), Decimal("0"))


def arithmetic_validate(extracted: ExtractedInvoice) -> ValidationWarning | None:
    """Return a ValidationWarning if the lines total diverges from the stated total, else None."""
    if not extracted.lines:
        return None

    if extracted.total is None:
        return ValidationWarning(
            code="total_missing",
            message="Invoice has line items but no total was extracted.",
        )

    computed = compute_lines_total(extracted.lines)
    diff = abs(computed - extracted.total)
    threshold = max(TOTAL_ABS_THRESHOLD, abs(computed) * TOTAL_REL_THRESHOLD)

    if diff > threshold:
        return ValidationWarning(
            code="total_mismatch",
            message=(
                f"Computed lines total {computed} differs from extracted total "
                f"{extracted.total} by {diff} (threshold {threshold:.2f})."
            ),
            computed=computed,
            extracted=extracted.total,
        )

    return None
