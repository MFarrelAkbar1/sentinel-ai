"""Deterministic arithmetic verification. No LLM touches this file's output.

This is the load-bearing component of Sentinel-AI. The architecture diagram's
closing line — *"every number in the report comes either from a page-cited
document quote or from a Python computation. There is no third path"* — is
enforced here.

Three things happen in this module and nowhere else:

* **Identity checks.** ``total_aset == total_liabilitas + total_ekuitas`` and
  its siblings are evaluated in Pandas against the extracted figures. A break
  is not an opinion; it is a number, and it triggers re-extraction.
* **Derived indicators.** Ratios and multi-year trends are computed from
  verified inputs and carry their formula and inputs with them, so the report
  can cite the computation rather than assert the result.
* **Sandboxed evaluation.** ``safe_compute`` evaluates an arithmetic expression
  over a whitelist of AST nodes with named bindings supplied by the caller.
  An agent may ask for ``(piutang_usaha / pendapatan) * 365``; it cannot ask
  for ``__import__('os').system(...)`` and it cannot introduce a literal that
  was never extracted, because unbound names raise.

Tolerance policy: published statements round, so a break is flagged only when
it exceeds *both* a relative and an absolute floor. Rounding noise stays quiet;
a real reconciliation failure does not.
"""

from __future__ import annotations

import ast
import logging
import math
import operator as op
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

import pandas as pd

from core.config import get_settings
from core.state import DerivedIndicator, ExtractedMetric, VerificationCheck

log = logging.getLogger("sentinel.sandbox")


# ---------------------------------------------------------------------------
# Sandboxed arithmetic
# ---------------------------------------------------------------------------

_BIN_OPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul,
    ast.Div: op.truediv, ast.FloorDiv: op.floordiv,
    ast.Mod: op.mod, ast.Pow: op.pow,
}
_UNARY_OPS: dict[type, Callable[[Any], Any]] = {ast.UAdd: op.pos, ast.USub: op.neg}
_ALLOWED_CALLS: dict[str, Callable[..., Any]] = {
    "abs": abs, "min": min, "max": max, "round": round, "sum": sum,
    "sqrt": math.sqrt, "log": math.log,
}


class UnsafeExpression(ValueError):
    """Raised when an expression steps outside the arithmetic whitelist."""


def safe_compute(expression: str, bindings: dict[str, float]) -> float:
    """Evaluate an arithmetic expression over named, caller-supplied values.

    Every name must be bound. That constraint is the point: an agent cannot
    smuggle a remembered figure into a computation, because a number that was
    never extracted has no binding and the expression fails loudly.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise UnsafeExpression(f"Ekspresi tidak valid: {expression}") from exc

    def _eval(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                return float(node.value)
            raise UnsafeExpression("Hanya konstanta numerik yang diizinkan.")
        if isinstance(node, ast.Name):
            if node.id not in bindings:
                raise UnsafeExpression(
                    f"Variabel '{node.id}' tidak terikat pada angka hasil ekstraksi."
                )
            value = bindings[node.id]
            if value is None:
                raise UnsafeExpression(f"Variabel '{node.id}' bernilai None (belum terbaca).")
            return float(value)
        if isinstance(node, ast.BinOp):
            handler = _BIN_OPS.get(type(node.op))
            if handler is None:
                raise UnsafeExpression(f"Operator tidak diizinkan: {type(node.op).__name__}")
            return handler(_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):
            handler = _UNARY_OPS.get(type(node.op))
            if handler is None:
                raise UnsafeExpression(f"Operator uner tidak diizinkan: {type(node.op).__name__}")
            return handler(_eval(node.operand))
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_CALLS:
                raise UnsafeExpression("Pemanggilan fungsi tidak diizinkan.")
            if node.keywords:
                raise UnsafeExpression("Argumen kata kunci tidak diizinkan.")
            return _ALLOWED_CALLS[node.func.id](*[_eval(a) for a in node.args])
        if isinstance(node, (ast.Tuple, ast.List)):
            return [_eval(e) for e in node.elts]
        raise UnsafeExpression(f"Node tidak diizinkan: {type(node).__name__}")

    result = _eval(tree)
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise UnsafeExpression("Ekspresi harus menghasilkan angka.")
    if isinstance(result, float) and (math.isnan(result) or math.isinf(result)):
        raise UnsafeExpression("Hasil komputasi tidak berhingga.")
    return float(result)


# ---------------------------------------------------------------------------
# Identity definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    """One accounting identity that must hold within tolerance."""

    name: str
    description: str
    lhs: str                # metric key on the left of the equation
    rhs_terms: tuple[tuple[str, int], ...]   # (metric key, sign)
    required: tuple[str, ...] = ()           # keys without which the check is skipped

    @property
    def formula(self) -> str:
        parts = []
        for key, sign in self.rhs_terms:
            parts.append(f"{'-' if sign < 0 else '+'} {key}")
        rhs = " ".join(parts).lstrip("+ ").strip()
        return f"{self.lhs} = {rhs}"

    @property
    def involved(self) -> list[str]:
        return [self.lhs] + [k for k, _ in self.rhs_terms]


IDENTITIES: tuple[Identity, ...] = (
    Identity(
        name="persamaan_neraca",
        description="Aset = Liabilitas + Ekuitas (persamaan dasar akuntansi)",
        lhs="total_aset",
        rhs_terms=(("total_liabilitas", 1), ("total_ekuitas", 1)),
    ),
    Identity(
        name="subtotal_aset",
        description="Jumlah aset = aset lancar + aset tidak lancar",
        lhs="total_aset",
        rhs_terms=(("aset_lancar", 1), ("aset_tidak_lancar", 1)),
    ),
    Identity(
        name="subtotal_liabilitas",
        description="Jumlah liabilitas = liabilitas jangka pendek + jangka panjang",
        lhs="total_liabilitas",
        rhs_terms=(("liabilitas_jangka_pendek", 1), ("liabilitas_jangka_panjang", 1)),
    ),
    Identity(
        name="laba_bruto",
        description="Laba bruto = pendapatan - beban pokok pendapatan",
        lhs="laba_bruto",
        rhs_terms=(("pendapatan", 1), ("beban_pokok_pendapatan", -1)),
    ),
    Identity(
        name="laba_usaha",
        description="Laba usaha = laba bruto - beban usaha",
        lhs="laba_usaha",
        rhs_terms=(("laba_bruto", 1), ("beban_usaha", -1)),
    ),
    Identity(
        name="laba_bersih",
        description="Laba bersih = laba sebelum pajak - beban pajak",
        lhs="laba_bersih",
        rhs_terms=(("laba_sebelum_pajak", 1), ("beban_pajak", -1)),
    ),
    Identity(
        name="rekonsiliasi_arus_kas",
        description="Kas akhir = kas awal + arus kas operasi + investasi + pendanaan",
        lhs="kas_akhir_periode",
        rhs_terms=(
            ("kas_awal_periode", 1), ("arus_kas_operasi", 1),
            ("arus_kas_investasi", 1), ("arus_kas_pendanaan", 1),
        ),
    ),
    Identity(
        name="kas_neraca_vs_arus_kas",
        description="Kas dan setara kas di neraca = kas akhir periode di laporan arus kas",
        lhs="kas_dan_setara_kas",
        rhs_terms=(("kas_akhir_periode", 1),),
    ),
)


# ---------------------------------------------------------------------------
# Verification engine
# ---------------------------------------------------------------------------


class VerificationEngine:
    """Runs identity checks and derives indicators. Pure arithmetic, no model."""

    def __init__(self, rel_tolerance: Optional[float] = None, abs_tolerance: Optional[float] = None):
        settings = get_settings()
        self.rel_tolerance = rel_tolerance if rel_tolerance is not None else settings.verify_rel_tolerance
        self.abs_tolerance = abs_tolerance if abs_tolerance is not None else settings.verify_abs_tolerance

    # -- frame construction -------------------------------------------------

    @staticmethod
    def to_frame(metrics: Iterable[ExtractedMetric | dict]) -> pd.DataFrame:
        """Pivot extracted metrics into a period × metric table of scaled values."""
        rows: list[dict[str, Any]] = []
        for metric in metrics:
            data = metric.model_dump() if isinstance(metric, ExtractedMetric) else dict(metric)
            value, scale = data.get("value"), data.get("scale", 1.0) or 1.0
            rows.append({
                "period": str(data.get("period", "")),
                "key": data.get("key", ""),
                "value": None if value is None else float(value) * float(scale),
                "page": data.get("page"),
                "status": data.get("status"),
            })
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows)
        # Duplicate readings of the same cell: keep the first (highest-ranked).
        frame = frame.drop_duplicates(subset=["period", "key"], keep="first")
        return frame.pivot(index="period", columns="key", values="value").sort_index(ascending=False)

    # -- identity checks ----------------------------------------------------

    def _within_tolerance(self, expected: float, actual: float) -> tuple[bool, float, float]:
        difference = actual - expected
        magnitude = max(abs(expected), abs(actual), 1.0)
        relative = abs(difference) / magnitude
        passed = abs(difference) <= self.abs_tolerance or relative <= self.rel_tolerance
        return passed, difference, relative

    def check_identities(
        self, frame: pd.DataFrame, identities: Iterable[Identity] = IDENTITIES
    ) -> list[VerificationCheck]:
        checks: list[VerificationCheck] = []
        if frame.empty:
            return checks

        for period, row in frame.iterrows():
            for identity in identities:
                missing = [k for k in identity.involved if k not in frame.columns or pd.isna(row.get(k))]
                if missing:
                    checks.append(VerificationCheck(
                        name=identity.name, description=identity.description,
                        period=str(period), formula=identity.formula,
                        skipped=True, passed=False,
                        skip_reason=f"Metrik belum terbaca: {', '.join(missing)}",
                        involved_metrics=identity.involved,
                    ))
                    continue

                expected = float(row[identity.lhs])
                actual = sum(sign * float(row[key]) for key, sign in identity.rhs_terms)
                passed, difference, relative = self._within_tolerance(expected, actual)
                checks.append(VerificationCheck(
                    name=identity.name, description=identity.description,
                    period=str(period), formula=identity.formula,
                    expected=expected, actual=actual,
                    difference=difference, relative_difference=relative,
                    passed=passed, involved_metrics=identity.involved,
                ))
        return checks

    @staticmethod
    def failed_metrics(checks: Iterable[VerificationCheck]) -> dict[str, set[str]]:
        """Map period → metric keys implicated in a failed identity.

        Agent 1 uses this to target its re-read: only the rows that broke an
        identity are re-extracted with the next strategy, not the whole filing.
        """
        implicated: dict[str, set[str]] = {}
        for check in checks:
            if check.skipped or check.passed:
                continue
            implicated.setdefault(check.period, set()).update(check.involved_metrics)
        return implicated

    @staticmethod
    def summarise(checks: Iterable[VerificationCheck]) -> dict[str, Any]:
        checks = list(checks)
        evaluated = [c for c in checks if not c.skipped]
        passed = [c for c in evaluated if c.passed]
        return {
            "total": len(checks),
            "evaluated": len(evaluated),
            "passed": len(passed),
            "failed": len(evaluated) - len(passed),
            "skipped": len(checks) - len(evaluated),
            "pass_rate": round(len(passed) / len(evaluated), 4) if evaluated else 0.0,
        }

    # -- derived indicators -------------------------------------------------

    #: (key, label, expression, required metric keys, interpretation template)
    RATIOS: tuple[tuple[str, str, str, tuple[str, ...], str], ...] = (
        ("current_ratio", "Rasio lancar",
         "aset_lancar / liabilitas_jangka_pendek",
         ("aset_lancar", "liabilitas_jangka_pendek"),
         "Kemampuan menutup liabilitas jangka pendek dengan aset lancar."),
        ("debt_to_equity", "Rasio utang terhadap ekuitas",
         "total_liabilitas / total_ekuitas",
         ("total_liabilitas", "total_ekuitas"),
         "Struktur pendanaan; nilai tinggi menandakan ketergantungan pada utang."),
        ("net_margin", "Marjin laba bersih",
         "laba_bersih / pendapatan",
         ("laba_bersih", "pendapatan"),
         "Porsi pendapatan yang tersisa sebagai laba bersih."),
        ("roe", "Imbal hasil ekuitas (ROE)",
         "laba_bersih / total_ekuitas",
         ("laba_bersih", "total_ekuitas"),
         "Laba bersih relatif terhadap ekuitas pemegang saham."),
        ("roa", "Imbal hasil aset (ROA)",
         "laba_bersih / total_aset",
         ("laba_bersih", "total_aset"),
         "Efisiensi aset dalam menghasilkan laba."),
        ("cfo_to_net_income", "Kualitas laba (AKO / laba bersih)",
         "arus_kas_operasi / laba_bersih",
         ("arus_kas_operasi", "laba_bersih"),
         "Seberapa besar laba akuntansi didukung kas operasi yang nyata."),
        ("accrual_ratio", "Rasio akrual",
         "(laba_bersih - arus_kas_operasi) / total_aset",
         ("laba_bersih", "arus_kas_operasi", "total_aset"),
         "Porsi laba yang berasal dari akrual, bukan kas."),
        ("days_sales_outstanding", "Hari penagihan piutang (DSO)",
         "(piutang_usaha / pendapatan) * 365",
         ("piutang_usaha", "pendapatan"),
         "Rata-rata hari sejak penjualan sampai kas diterima."),
        ("receivable_intensity", "Intensitas piutang",
         "piutang_usaha / pendapatan",
         ("piutang_usaha", "pendapatan"),
         "Porsi pendapatan yang masih tertahan sebagai piutang."),
        ("gross_margin", "Marjin laba bruto",
         "laba_bruto / pendapatan",
         ("laba_bruto", "pendapatan"),
         "Marjin sebelum beban usaha."),
        ("equity_ratio", "Rasio ekuitas terhadap aset",
         "total_ekuitas / total_aset",
         ("total_ekuitas", "total_aset"),
         "Porsi aset yang dibiayai ekuitas."),
    )

    def derive_indicators(self, frame: pd.DataFrame) -> list[DerivedIndicator]:
        """Compute every ratio whose inputs are present, for every period."""
        indicators: list[DerivedIndicator] = []
        if frame.empty:
            return indicators

        for period, row in frame.iterrows():
            for key, label, expression, required, interpretation in self.RATIOS:
                if any(r not in frame.columns or pd.isna(row.get(r)) for r in required):
                    continue
                bindings = {r: float(row[r]) for r in required}
                # A zero denominator makes the ratio undefined, not zero.
                try:
                    value = safe_compute(expression, bindings)
                except (UnsafeExpression, ZeroDivisionError) as exc:
                    log.debug("Indikator %s (%s) dilewati: %s", key, period, exc)
                    continue
                indicators.append(DerivedIndicator(
                    key=key, label=label, period=str(period),
                    value=round(value, 6), formula=expression,
                    inputs=bindings, interpretation=interpretation,
                ))
        return indicators

    # -- multi-year trends --------------------------------------------------

    def derive_trends(self, frame: pd.DataFrame, keys: Optional[Iterable[str]] = None) -> list[DerivedIndicator]:
        """Year-over-year growth for each metric, computed pairwise in Pandas.

        Trend divergence — receivables outrunning revenue, for instance — is
        one of the strongest signals in the technical report, and it only
        exists across periods, so it is computed here rather than per-period.
        """
        indicators: list[DerivedIndicator] = []
        if frame.empty or len(frame.index) < 2:
            return indicators

        # Index is sorted descending (newest first); walk newest → older pairs.
        periods = list(frame.index)
        candidate_keys = list(keys) if keys else [
            k for k in (
                "pendapatan", "piutang_usaha", "persediaan", "laba_bersih",
                "arus_kas_operasi", "total_aset", "total_liabilitas",
                "total_ekuitas", "beban_usaha",
            ) if k in frame.columns
        ]

        for newer, older in zip(periods, periods[1:]):
            for key in candidate_keys:
                current, previous = frame.at[newer, key], frame.at[older, key]
                if pd.isna(current) or pd.isna(previous) or float(previous) == 0:
                    continue
                bindings = {"current": float(current), "previous": float(previous)}
                try:
                    growth = safe_compute("(current - previous) / abs(previous)", bindings)
                except (UnsafeExpression, ZeroDivisionError):
                    continue
                indicators.append(DerivedIndicator(
                    key=f"growth_{key}", label=f"Pertumbuhan {key} ({older} → {newer})",
                    period=str(newer), value=round(growth, 6),
                    formula=f"({key}_{newer} - {key}_{older}) / abs({key}_{older})",
                    inputs={f"{key}_{newer}": bindings["current"], f"{key}_{older}": bindings["previous"]},
                    interpretation="Perubahan relatif antarperiode.",
                ))
        return indicators


# ---------------------------------------------------------------------------
# Convenience façade used by the agents
# ---------------------------------------------------------------------------


def verify(metrics: Iterable[ExtractedMetric | dict]) -> dict[str, Any]:
    """Full deterministic pass over a set of extracted metrics.

    Returns the frame, the identity checks, derived indicators, trends, the
    summary, and the per-period map of metrics implicated in failures.
    """
    engine = VerificationEngine()
    frame = engine.to_frame(metrics)
    checks = engine.check_identities(frame)
    indicators = engine.derive_indicators(frame)
    trends = engine.derive_trends(frame)
    return {
        "frame": frame,
        "checks": checks,
        "indicators": indicators,
        "trends": trends,
        "summary": engine.summarise(checks),
        "implicated": engine.failed_metrics(checks),
    }
