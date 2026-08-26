"""Indonesian number formatting for anything a reader will see.

A report about a BEI filing that prints ``214,000,000,000`` is reporting in the
wrong locale — Indonesian filings use a dot as the thousands separator and a
comma as the decimal separator. Rendering matters here beyond aesthetics: an
analyst cross-checking a figure against the source page needs it to look like
the figure on the source page.
"""

from __future__ import annotations

from typing import Optional


def idr(value: Optional[float], decimals: int = 0, unit: str = "") -> str:
    """Format a number in Indonesian convention: ``1.234.567,89``."""
    if value is None:
        return "—"
    formatted = f"{abs(value):,.{decimals}f}"
    # ``,`` → thousands and ``.`` → decimal, swapped via a placeholder.
    formatted = formatted.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    sign = "-" if value < 0 else ""
    suffix = f" {unit}" if unit else ""
    return f"{sign}{formatted}{suffix}"


def compact_idr(value: Optional[float]) -> str:
    """Human-scale Rupiah: ``Rp1,64 triliun``, ``Rp182,00 miliar``."""
    if value is None:
        return "—"
    magnitude = abs(value)
    sign = "-" if value < 0 else ""
    for threshold, label in ((1e12, "triliun"), (1e9, "miliar"), (1e6, "juta"), (1e3, "ribu")):
        if magnitude >= threshold:
            return f"{sign}Rp{idr(magnitude / threshold, 2)} {label}"
    return f"{sign}Rp{idr(magnitude, 0)}"


def pct(value: Optional[float], decimals: int = 1) -> str:
    """Format a fraction as an Indonesian percentage: ``0.293`` → ``29,3%``."""
    if value is None:
        return "—"
    return f"{idr(value * 100, decimals)}%"


def ratio(value: Optional[float], decimals: int = 2) -> str:
    """Format a multiple: ``2.168`` → ``2,17x``."""
    if value is None:
        return "—"
    return f"{idr(value, decimals)}x"
