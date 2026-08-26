"""Unit tests for the deterministic layers.

These cover the parts of the system where a silent error would be invisible in
the final report but would corrupt everything downstream: Indonesian number
parsing, reporting-scale detection, caption resolution, accounting identity
checks, and the sandbox's refusal to evaluate anything but bound arithmetic.

Run with:  pytest -q
"""

from __future__ import annotations


import pytest

from core.state import ExtractedMetric, ExtractionStatus
from tools.document_parser import (
    caption_of,
    classify_page,
    detect_scale,
    extract_numbers,
    match_metric,
    parse_id_number,
)
from tools.python_sandbox import (
    IDENTITIES,
    UnsafeExpression,
    VerificationEngine,
    safe_compute,
    verify,
)


# ---------------------------------------------------------------------------
# Number parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token,expected",
    [
        ("1.234.567", 1_234_567.0),          # Indonesian thousands
        ("1.234.567,89", 1_234_567.89),      # Indonesian thousands + decimal
        ("1,234,567.89", 1_234_567.89),      # English convention
        ("(1.234)", -1234.0),                # parentheses mean negative
        ("(3.640.000)", -3_640_000.0),
        ("-145.000", -145_000.0),
        ("12,5", 12.5),                      # decimal comma
        ("1234", 1234.0),
        ("690.000", 690_000.0),
        ("0", 0.0),
    ],
)
def test_parse_id_number(token, expected):
    assert parse_id_number(token) == pytest.approx(expected)


@pytest.mark.parametrize("token", ["", "n/a", "-", "Catatan", None])
def test_parse_id_number_rejects_non_numbers(token):
    """Unreadable must be None, never 0 — a zero would silently pass identities."""
    assert parse_id_number(token) is None


def test_extract_numbers_preserves_reading_order():
    line = "Kas dan setara kas                 2c,4        182.000        162.000"
    # The note reference contributes 2 and 4 ahead of the two period columns;
    # the auditor's column mapping takes the trailing N values.
    assert extract_numbers(line) == [2.0, 4.0, 182_000.0, 162_000.0]


def test_extract_numbers_handles_parenthesised_columns():
    line = "Beban pokok pendapatan            17       (3.640.000)     (3.310.000)"
    assert extract_numbers(line)[-2:] == [-3_640_000.0, -3_310_000.0]


# ---------------------------------------------------------------------------
# Reporting scale
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,multiplier",
    [
        ("(Dinyatakan dalam jutaan Rupiah, kecuali dinyatakan lain)", 1e6),
        ("Disajikan dalam ribuan Rupiah", 1e3),
        ("dalam miliaran rupiah", 1e9),
        ("(Expressed in millions of Rupiah)", 1e6),
        ("Laporan posisi keuangan konsolidasian", 1.0),
    ],
)
def test_detect_scale(text, multiplier):
    assert detect_scale(text)[0] == multiplier


def test_scale_is_applied_before_comparison():
    """A figure stated in millions must be scaled before an identity is tested."""
    metrics = [
        ExtractedMetric(key="total_aset", label="jumlah aset", value=6_400_000, period="2025",
                        scale=1e6, page=3, source_line="JUMLAH ASET 6.400.000"),
        ExtractedMetric(key="total_liabilitas", label="jumlah liabilitas", value=4_380_000, period="2025",
                        scale=1e6, page=3, source_line="JUMLAH LIABILITAS 4.380.000"),
        ExtractedMetric(key="total_ekuitas", label="jumlah ekuitas", value=2_020_000, period="2025",
                        scale=1e6, page=3, source_line="JUMLAH EKUITAS 2.020.000"),
    ]
    frame = VerificationEngine.to_frame(metrics)
    assert frame.at["2025", "total_aset"] == pytest.approx(6.4e12)
    check = next(c for c in VerificationEngine().check_identities(frame) if c.name == "persamaan_neraca")
    assert check.passed


# ---------------------------------------------------------------------------
# Caption resolution
# ---------------------------------------------------------------------------


def test_caption_of_strips_figures_and_note_column():
    line = "Jumlah aset lancar                             2.900.000        2.090.000"
    assert caption_of(line) == "jumlah aset lancar"


@pytest.mark.parametrize(
    "caption,expected",
    [
        ("jumlah aset", "total_aset"),
        ("jumlah aset lancar", "aset_lancar"),               # longer alias wins
        ("jumlah aset tidak lancar", "aset_tidak_lancar"),
        ("jumlah liabilitas", "total_liabilitas"),
        ("jumlah liabilitas jangka pendek", "liabilitas_jangka_pendek"),
        ("laba sebelum pajak penghasilan", "laba_sebelum_pajak"),
        ("beban pokok pendapatan", "beban_pokok_pendapatan"),
        ("kas dan setara kas akhir tahun", "kas_akhir_periode"),
    ],
)
def test_match_metric_prefers_the_most_specific_caption(caption, expected):
    matched = match_metric(caption)
    assert matched is not None and matched[0] == expected


def test_match_metric_rejects_totals_row_that_carries_another_figure():
    """"Jumlah liabilitas dan ekuitas" holds total assets, not total liabilities."""
    assert match_metric("jumlah liabilitas dan ekuitas") is None


def test_match_metric_rejects_incidental_mentions_in_prose():
    caption = ("persentase penjualan kepada pihak berelasi terhadap jumlah pendapatan "
               "konsolidasian adalah")
    assert match_metric(caption) is None


def test_classify_page_does_not_mistake_an_audit_opinion_for_a_statement():
    """An auditor's scope paragraph names every statement by title."""
    text = (
        "LAPORAN AUDITOR INDEPENDEN\n"
        "Kami telah mengaudit laporan keuangan konsolidasian, yang terdiri dari laporan "
        "posisi keuangan konsolidasian, laporan laba rugi, laporan perubahan ekuitas dan "
        "laporan arus kas konsolidasian.\n"
        "Penekanan Suatu Hal\n"
        "Menurut opini kami, laporan keuangan menyajikan secara wajar."
    )
    statement_type, score = classify_page(text)
    assert statement_type.value == "opini_auditor"
    assert score > 0


# ---------------------------------------------------------------------------
# Accounting identities
# ---------------------------------------------------------------------------


def _metric(key: str, value: float, period: str = "2025") -> ExtractedMetric:
    return ExtractedMetric(
        key=key, label=key, value=value, period=period, scale=1.0,
        page=3, source_line=f"{key} {value}",
    )


BALANCED = {
    "total_aset": 6_400_000, "aset_lancar": 2_900_000, "aset_tidak_lancar": 3_500_000,
    "total_liabilitas": 4_380_000, "liabilitas_jangka_pendek": 3_010_000,
    "liabilitas_jangka_panjang": 1_370_000, "total_ekuitas": 2_020_000,
    "pendapatan": 4_850_000, "beban_pokok_pendapatan": 3_640_000, "laba_bruto": 1_210_000,
    "beban_usaha": 780_000, "laba_usaha": 430_000, "laba_sebelum_pajak": 290_000,
    "beban_pajak": 76_000, "laba_bersih": 214_000,
    "arus_kas_operasi": -145_000, "arus_kas_investasi": -260_000,
    "arus_kas_pendanaan": 425_000, "kas_awal_periode": 162_000,
    "kas_akhir_periode": 182_000, "kas_dan_setara_kas": 182_000,
}


def test_all_identities_pass_on_consistent_statements():
    result = verify([_metric(k, v) for k, v in BALANCED.items()])
    assert result["summary"]["failed"] == 0
    assert result["summary"]["evaluated"] == len(IDENTITIES)
    assert result["implicated"] == {}


def test_balance_sheet_break_is_detected_and_attributed():
    broken = dict(BALANCED, total_ekuitas=1_500_000)
    result = verify([_metric(k, v) for k, v in broken.items()])
    failed = [c for c in result["checks"] if not c.skipped and not c.passed]
    assert {c.name for c in failed} == {"persamaan_neraca"}
    # The re-read must target the rows the break implicates, nothing more.
    assert result["implicated"]["2025"] == {"total_aset", "total_liabilitas", "total_ekuitas"}


def test_cash_flow_reconciliation_break_is_detected():
    broken = dict(BALANCED, kas_akhir_periode=200_000)
    result = verify([_metric(k, v) for k, v in broken.items()])
    failed = {c.name for c in result["checks"] if not c.skipped and not c.passed}
    assert "rekonsiliasi_arus_kas" in failed


def test_rounding_noise_is_tolerated():
    """Published statements round; a 1-unit drift is not a reconciliation failure."""
    noisy = dict(BALANCED, total_aset=6_400_001)
    result = verify([_metric(k, v) for k, v in noisy.items()])
    check = next(c for c in result["checks"] if c.name == "persamaan_neraca")
    assert check.passed


def test_material_break_is_not_tolerated():
    material = dict(BALANCED, total_aset=6_500_000)   # 1.6% — well beyond rounding
    result = verify([_metric(k, v) for k, v in material.items()])
    check = next(c for c in result["checks"] if c.name == "persamaan_neraca")
    assert not check.passed


def test_missing_metric_skips_rather_than_assumes_zero():
    partial = {k: v for k, v in BALANCED.items() if k != "total_ekuitas"}
    result = verify([_metric(k, v) for k, v in partial.items()])
    check = next(c for c in result["checks"] if c.name == "persamaan_neraca")
    assert check.skipped and not check.passed
    assert "total_ekuitas" in (check.skip_reason or "")


def test_empty_input_produces_no_false_confidence():
    result = verify([])
    assert result["checks"] == []
    assert result["summary"]["pass_rate"] == 0.0


# ---------------------------------------------------------------------------
# Derived indicators and trends
# ---------------------------------------------------------------------------


def test_indicators_carry_their_formula_and_inputs():
    result = verify([_metric(k, v) for k, v in BALANCED.items()])
    current = next(i for i in result["indicators"] if i.key == "current_ratio")
    assert current.value == pytest.approx(2_900_000 / 3_010_000, rel=1e-6)
    assert current.formula == "aset_lancar / liabilitas_jangka_pendek"
    assert set(current.inputs) == {"aset_lancar", "liabilitas_jangka_pendek"}
    citation = current.to_citation()
    assert citation.kind == "computation" and citation.result == current.value


def test_year_over_year_trend_uses_both_periods():
    metrics = [
        _metric("pendapatan", 4_850_000, "2025"), _metric("pendapatan", 4_420_000, "2024"),
        _metric("piutang_usaha", 1_640_000, "2025"), _metric("piutang_usaha", 1_180_000, "2024"),
    ]
    trends = {t.key: t.value for t in verify(metrics)["trends"]}
    # Trend values are stored rounded to 6 decimal places for report stability.
    assert trends["growth_pendapatan"] == pytest.approx((4_850_000 - 4_420_000) / 4_420_000, abs=1e-6)
    assert trends["growth_piutang_usaha"] == pytest.approx((1_640_000 - 1_180_000) / 1_180_000, abs=1e-6)
    # The divergence Agent 3 screens on.
    assert trends["growth_piutang_usaha"] - trends["growth_pendapatan"] > 0.15


def test_zero_denominator_yields_no_indicator_rather_than_infinity():
    metrics = [_metric("aset_lancar", 100.0), _metric("liabilitas_jangka_pendek", 0.0)]
    indicators = verify(metrics)["indicators"]
    assert all(i.key != "current_ratio" for i in indicators)


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------


def test_safe_compute_evaluates_bound_arithmetic():
    assert safe_compute("(a - b) / c", {"a": 10.0, "b": 4.0, "c": 2.0}) == pytest.approx(3.0)
    assert safe_compute("abs(x) * 365", {"x": -2.0}) == pytest.approx(730.0)


def test_safe_compute_requires_every_name_to_be_bound():
    """An unbound name is a number that was never extracted — it must fail loudly."""
    with pytest.raises(UnsafeExpression):
        safe_compute("laba_bersih / pendapatan", {"laba_bersih": 100.0})


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('echo hi')",
        "open('secret.txt').read()",
        "(lambda: 1)()",
        "[x for x in range(10)]",
        "a.__class__",
        "eval('1+1')",
    ],
)
def test_safe_compute_rejects_anything_outside_arithmetic(expression):
    with pytest.raises(UnsafeExpression):
        safe_compute(expression, {"a": 1.0})


def test_safe_compute_rejects_non_finite_results():
    with pytest.raises((UnsafeExpression, ZeroDivisionError)):
        safe_compute("a / b", {"a": 1.0, "b": 0.0})


# ---------------------------------------------------------------------------
# Status assignment
# ---------------------------------------------------------------------------


def test_only_metrics_confirmed_by_an_identity_are_marked_verified():
    from agents.financial_auditor import FinancialAuditor

    metrics = [_metric(k, v) for k, v in BALANCED.items()]
    metrics.append(_metric("persediaan", 690_000))     # no identity covers it
    result = verify(metrics)
    graded = FinancialAuditor._apply_status(
        [ExtractedMetric(**m.model_dump()) for m in metrics],
        result["checks"], result["implicated"], exhausted=False,
    )
    by_key = {m.key: m for m in graded}
    assert by_key["total_aset"].status is ExtractionStatus.VERIFIED
    assert by_key["persediaan"].status is ExtractionStatus.UNVERIFIED


def test_persistent_break_after_final_attempt_is_reported_as_unreliable():
    from agents.financial_auditor import FinancialAuditor

    broken = dict(BALANCED, total_ekuitas=1_500_000)
    metrics = [_metric(k, v) for k, v in broken.items()]
    result = verify(metrics)
    graded = FinancialAuditor._apply_status(
        [ExtractedMetric(**m.model_dump()) for m in metrics],
        result["checks"], result["implicated"], exhausted=True,
    )
    by_key = {m.key: m for m in graded}
    assert by_key["total_ekuitas"].status is ExtractionStatus.UNRELIABLE
    assert "tidak dapat diekstraksi dengan andal" in (by_key["total_ekuitas"].note or "")
    # Unrelated rows keep their own status.
    assert by_key["pendapatan"].status is ExtractionStatus.VERIFIED


# ---------------------------------------------------------------------------
# Provenance invariants
# ---------------------------------------------------------------------------


def test_document_citation_requires_page_and_quote():
    from pydantic import ValidationError

    from core.state import Citation

    with pytest.raises(ValidationError):
        Citation(kind="document", document_id="x")          # no page, no quote
    with pytest.raises(ValidationError):
        Citation(kind="computation")                        # no formula
    with pytest.raises(ValidationError):
        Citation(kind="web")                                # no url


def test_metric_without_a_source_line_cannot_produce_a_citation():
    metric = ExtractedMetric(key="total_aset", label="jumlah aset", value=1.0, period="2025")
    assert metric.to_citation("doc") is None
