"""Unit tests for Pass 1 arithmetic validation."""

from __future__ import annotations

from decimal import Decimal

from fakturoid_naklady.extraction.validation import arithmetic_validate, compute_lines_total
from fakturoid_naklady.models import ExtractedInvoice, InvoiceLine, VendorInfo


def _invoice(lines: list[dict], total: float | None = None) -> ExtractedInvoice:
    return ExtractedInvoice(
        vendor=VendorInfo(name="ACME s.r.o."),
        invoice_number="INV-1",
        issued_on="2024-01-01",
        lines=[InvoiceLine(**ln) for ln in lines],
        total=Decimal(str(total)) if total is not None else None,
    )


_LINE = {"name": "Work", "quantity": "2", "unit_price": "500", "vat_rate": 0}


class TestComputeLinesTotal:
    def test_single_line(self) -> None:
        lines = [
            InvoiceLine(name="x", quantity=Decimal("3"), unit_price=Decimal("100"), vat_rate=0)
        ]
        assert compute_lines_total(lines) == Decimal("300")

    def test_multi_line(self) -> None:
        lines = [
            InvoiceLine(name="a", quantity=Decimal("2"), unit_price=Decimal("50"), vat_rate=0),
            InvoiceLine(name="b", quantity=Decimal("1"), unit_price=Decimal("200"), vat_rate=0),
        ]
        assert compute_lines_total(lines) == Decimal("300")

    def test_empty(self) -> None:
        assert compute_lines_total([]) == Decimal("0")


class TestArithmeticValidate:
    def test_no_lines_returns_none(self) -> None:
        inv = _invoice(lines=[], total=100.0)
        assert arithmetic_validate(inv) is None

    def test_total_missing_with_lines(self) -> None:
        inv = _invoice(lines=[_LINE], total=None)
        w = arithmetic_validate(inv)
        assert w is not None
        assert w.code == "total_missing"

    def test_matching_total_returns_none(self) -> None:
        # 2 x 500 = 1000 -- exact match
        inv = _invoice(lines=[_LINE], total=1000.0)
        assert arithmetic_validate(inv) is None

    def test_within_abs_threshold_returns_none(self) -> None:
        # Small invoice: computed=1, threshold=max(0.50, 0.01)=0.50; diff=0.30 < 0.50
        line = {"name": "x", "quantity": "1", "unit_price": "1", "vat_rate": 0}
        inv = _invoice(lines=[line], total=1.30)
        assert arithmetic_validate(inv) is None

    def test_exceeds_abs_threshold_returns_warning(self) -> None:
        # Small invoice: computed=1, threshold=max(0.50, 0.01)=0.50; diff=0.60 > 0.50
        line = {"name": "x", "quantity": "1", "unit_price": "1", "vat_rate": 0}
        inv = _invoice(lines=[line], total=1.60)
        w = arithmetic_validate(inv)
        assert w is not None
        assert w.code == "total_mismatch"
        assert w.computed == Decimal("1")
        assert w.extracted == Decimal("1.60")

    def test_exceeds_rel_threshold_returns_warning(self) -> None:
        # Large invoice: computed=1000, threshold=max(0.50, 10)=10; diff=15 > 10
        inv = _invoice(lines=[_LINE], total=1015.0)
        w = arithmetic_validate(inv)
        assert w is not None
        assert w.code == "total_mismatch"

    def test_within_rel_threshold_returns_none(self) -> None:
        # Large invoice: computed=1000, threshold=max(0.50, 10)=10; diff=5 < 10
        inv = _invoice(lines=[_LINE], total=1005.0)
        assert arithmetic_validate(inv) is None

    def test_very_large_invoice_warning(self) -> None:
        # computed=100_000; threshold=max(0.50, 1000)=1000; diff=1500 > 1000
        line = {"name": "x", "quantity": "1", "unit_price": "100000", "vat_rate": 0}
        inv = _invoice(lines=[line], total=101_500.0)
        w = arithmetic_validate(inv)
        assert w is not None
        assert w.code == "total_mismatch"

    def test_very_large_invoice_within_threshold(self) -> None:
        # computed=100_000; threshold=max(0.50, 1000)=1000; diff=500 < 1000
        line = {"name": "x", "quantity": "1", "unit_price": "100000", "vat_rate": 0}
        inv = _invoice(lines=[line], total=100_500.0)
        assert arithmetic_validate(inv) is None
