"""Agent 4 — Synthesizer & Reporter.

Turns surviving findings into a tiered Executive Due Diligence Report and
assigns the composite risk score.

The suppression rule is the point of this agent. The risk table names
"mislabelling a healthy entity" as a first-order harm, mitigated by three
filters — counter-evidence search (Agent 3), a confidence threshold before a
finding is escalated (here), and language that asks rather than accuses (here).
A low-confidence finding is not deleted: it is retained in the technical annex
with its suppression reason, so the analyst can see what the system considered
and chose not to raise. Suppression is a disclosed decision, not a silent one.

The narrative is model-written but tightly fenced: the model receives only the
verified figures and the finding objects, and is instructed that it may not
introduce a number that is not in front of it. If the model is unavailable the
report is assembled from templates — smaller, but with identical citations.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from core.audit_trail import AuditTrail
from core.config import get_settings
from core.formatting import compact_idr, idr
from core.llm import LLMClient, get_llm
from core.state import Finding, RiskScore, Severity
from core.report_generator import build_report

log = logging.getLogger("sentinel.agent4")

AGENT = "synthesizer"

#: Maximum penalty each detection pattern may contribute to the composite score.
#: Weights follow the Indonesian case record cited in the technical report:
#: cash-flow/earnings divergence and audit-opinion modifications preceded the
#: Garuda and Hanson cases, so they dominate.
PATTERN_WEIGHTS: dict[str, float] = {
    "arus_kas_vs_laba": 22.0,
    "opini_auditor": 18.0,
    "piutang_vs_pendapatan": 16.0,
    "klaim_vs_angka": 16.0,
    "kualitas_laba_akrual": 14.0,
    "leverage_solvabilitas": 14.0,
    "pihak_berelasi": 10.0,
}

_SEVERITY_MULTIPLIER: dict[str, float] = {
    Severity.CRITICAL.value: 1.00,
    Severity.HIGH.value: 0.80,
    Severity.MEDIUM.value: 0.55,
    Severity.LOW.value: 0.30,
}

_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "Satu kalimat, netral, tanpa rekomendasi."},
        "executive_summary": {
            "type": "string",
            "description": "3-5 paragraf untuk pengambil keputusan non-teknis.",
        },
        "key_observations": {
            "type": "array",
            "description": "Poin utama, masing-masing satu kalimat.",
            "items": {"type": "string"},
        },
        "areas_requiring_clarification": {
            "type": "array",
            "description": "Pertanyaan yang perlu diajukan kepada manajemen.",
            "items": {"type": "string"},
        },
        "data_limitations": {
            "type": "string",
            "description": "Keterbatasan data dan cakupan analisis pada siklus ini.",
        },
    },
    "required": ["headline", "executive_summary", "key_observations"],
    "additionalProperties": False,
}

_SUMMARY_SYSTEM = """Anda menyusun Executive Due Diligence Report untuk pembaca non-teknis.

Sentinel-AI adalah alat riset dan penyaringan risiko, BUKAN penasihat investasi.

Aturan mutlak:
1. Anda TIDAK BOLEH memperkenalkan angka baru. Gunakan hanya angka yang tertera
   pada data yang diberikan, persis sebagaimana adanya.
2. Anda TIDAK BOLEH memberikan rekomendasi beli, jual, atau tahan, dan tidak
   boleh memprediksi harga saham.
3. Setiap temuan disajikan sebagai indikator yang perlu diklarifikasi, bukan
   sebagai tuduhan. Gunakan kalimat bertanya atau kalimat yang menyatakan
   "perlu diklarifikasi".
4. Sebutkan secara eksplisit apabila terdapat metrik yang tidak dapat
   diekstraksi dengan andal.
5. Bahasa Indonesia formal, ringkas, tanpa jargon yang tidak dijelaskan.
"""


class Synthesizer:
    """Agent 4. Score, suppress, narrate, and emit the report."""

    def __init__(self, llm: Optional[LLMClient] = None, trail: Optional[AuditTrail] = None):
        self.llm = llm or get_llm()
        self.trail = trail
        self.settings = get_settings()

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        findings = [Finding(**f) for f in state.get("findings", [])]

        # --- 1. suppression ---------------------------------------------------
        threshold = self.settings.min_confidence
        for finding in findings:
            if finding.confidence < threshold:
                finding.suppressed = True
                finding.suppression_reason = (
                    f"Keyakinan {finding.confidence:.2f} di bawah ambang {threshold:.2f}; "
                    "disimpan pada lampiran teknis untuk penelaahan analis, tidak dinaikkan "
                    "menjadi temuan eksekutif."
                )
        surfaced = [f for f in findings if not f.suppressed]
        suppressed = [f for f in findings if f.suppressed]

        self._log(events, "terapkan_ambang_keyakinan",
                  decision=f"{len(surfaced)} ditampilkan, {len(suppressed)} disembunyikan",
                  detail=f"ambang keyakinan = {threshold:.2f}",
                  payload={"suppressed": [{"id": f.id, "confidence": f.confidence} for f in suppressed]})

        # --- 2. composite score ----------------------------------------------
        score = self._score(state, surfaced)
        self._log(events, "hitung_skor_risiko", tool="python_sandbox",
                  decision=f"{score.score:.0f}/100 — {score.band}",
                  detail=score.coverage_note or "cakupan verifikasi memadai",
                  payload=score.model_dump(mode="json"))

        # --- 3. narrative ------------------------------------------------------
        narrative = self._narrative(state, surfaced, score, events)

        # --- 4. assemble -------------------------------------------------------
        report = build_report(state, findings=findings, score=score, narrative=narrative)
        self._log(events, "susun_laporan", tool="report_generator",
                  decision=f"{len(report.get('sections', []))} bagian",
                  detail=f"{report['stats']['citations']} sitasi terlampir",
                  payload={"stats": report.get("stats", {})})

        return {
            "findings": [f.model_dump(mode="json") for f in findings],
            "risk_score": score.model_dump(mode="json"),
            "report": report,
            "audit_trail": events,
        }

    # -- scoring -------------------------------------------------------------

    def _score(self, state: dict[str, Any], surfaced: list[Finding]) -> RiskScore:
        """Composite score, inverted so that higher is safer.

        Each surfaced finding subtracts ``weight × confidence × severity``. The
        weights are bounded per pattern, so a single pattern firing repeatedly
        cannot dominate the score.
        """
        breakdown: list[dict[str, Any]] = []
        penalty = 0.0
        for finding in surfaced:
            weight = PATTERN_WEIGHTS.get(finding.pattern, 10.0)
            multiplier = _SEVERITY_MULTIPLIER.get(
                finding.severity.value if isinstance(finding.severity, Severity) else str(finding.severity),
                0.5,
            )
            contribution = weight * finding.confidence * multiplier
            penalty += contribution
            breakdown.append({
                "pattern": finding.pattern,
                "title": finding.title,
                "severity": finding.severity.value if isinstance(finding.severity, Severity) else finding.severity,
                "confidence": finding.confidence,
                "weight": weight,
                "penalty": round(contribution, 2),
            })

        raw = max(0.0, 100.0 - penalty)

        checks = state.get("verifications", [])
        evaluated = [c for c in checks if not c.get("skipped")]
        coverage = round(len(evaluated) / len(checks), 3) if checks else 0.0
        unreliable = state.get("unreliable_metrics", []) or []

        assessable = coverage >= 0.35 and not (coverage == 0.0 and not surfaced)
        note = ""
        if not assessable:
            note = (
                f"Hanya {len(evaluated)} dari {len(checks)} identitas akuntansi dapat dievaluasi "
                f"({coverage:.0%}). Skor komposit tidak dapat diperlakukan sebagai penilaian menyeluruh."
            )
        elif unreliable:
            note = (
                f"{len(unreliable)} metrik ditandai tidak dapat diekstraksi dengan andal "
                f"({', '.join(sorted(unreliable)[:6])}); pola yang bergantung padanya tidak dijalankan."
            )

        return RiskScore(
            score=round(raw, 1),
            band=RiskScore.band_for(raw),
            patterns_run=len(state.get("patterns_run", [])),
            flags_retained=len(surfaced),
            hypotheses_dropped=len(state.get("dropped_hypotheses", [])),
            breakdown=breakdown,
            data_coverage=coverage,
            assessable=assessable,
            coverage_note=note,
        )

    # -- narrative -----------------------------------------------------------

    def _narrative(
        self,
        state: dict[str, Any],
        surfaced: list[Finding],
        score: RiskScore,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not self.llm.available:
            self._log(events, "susun_ringkasan", tool="llm", decision="mode deterministik",
                      detail="ringkasan eksekutif disusun dari templat; sitasi tidak berubah")
            return _template_narrative(state, surfaced, score)

        facts = _fact_sheet(state, surfaced, score)
        result = self.llm.structured(
            purpose="agent4_summary",
            system=_SUMMARY_SYSTEM,
            user=(
                "Susun ringkasan eksekutif berdasarkan data berikut. "
                "Jangan menambahkan angka yang tidak tercantum di bawah ini.\n\n" + facts
            ),
            schema=_SUMMARY_SCHEMA,
            tool_name="submit_summary",
            tool_description="Kirim ringkasan eksekutif.",
            max_tokens=6000,
        )
        if not result.ok:
            self._log(events, "susun_ringkasan", tool="llm", decision="gagal",
                      detail=(result.error or "tidak ada respons") + "; memakai templat")
            return _template_narrative(state, surfaced, score)

        self._log(events, "susun_ringkasan", tool="llm", decision="berhasil",
                  detail=str(result.data.get("headline", ""))[:160],
                  payload={"model": result.model,
                           "tokens": {"in": result.input_tokens, "out": result.output_tokens}})
        data = dict(result.data)
        data.setdefault("areas_requiring_clarification",
                        [f.question_for_management for f in surfaced])
        data.setdefault("data_limitations", score.coverage_note)
        data["source"] = "llm"
        return data

    def _log(self, events: list[dict[str, Any]], action: str, **kwargs: Any) -> None:
        if self.trail:
            events.append(self.trail.log(AGENT, action, **kwargs))
        else:
            events.append({"agent": AGENT, "action": action, **kwargs})


# ---------------------------------------------------------------------------
# Grounding helpers
# ---------------------------------------------------------------------------


def _fact_sheet(state: dict[str, Any], surfaced: list[Finding], score: RiskScore) -> str:
    """Everything the model is permitted to write about — and nothing else."""
    lines: list[str] = [
        f"Entitas: {state.get('company_name')} ({state.get('ticker')})",
        f"Sektor: {state.get('sector') or 'tidak ditentukan'}",
        f"Periode yang dianalisis: {', '.join(state.get('periods', [])) or 'tidak terdeteksi'}",
        f"Skor komposit: {score.score:.0f}/100 ({score.band}); skor terbalik, makin tinggi makin aman.",
        f"Pola dijalankan: {score.patterns_run}; temuan dipertahankan: {score.flags_retained}; "
        f"hipotesis digugurkan: {score.hypotheses_dropped}.",
        "",
        "ANGKA TERVERIFIKASI (satu-satunya angka yang boleh Anda gunakan):",
    ]

    for metric in state.get("metrics", []):
        value, scale = metric.get("value"), metric.get("scale", 1.0) or 1.0
        if value is None:
            continue
        lines.append(
            f"- {metric.get('key')} {metric.get('period')} = {compact_idr(value * scale)} "
            f"[{metric.get('status')}, hal. {metric.get('page')}]"
        )

    indicators = [i for i in state.get("indicators", []) if i.get("value") is not None]
    if indicators:
        lines.append("")
        lines.append("INDIKATOR TURUNAN (hasil komputasi Python):")
        for indicator in indicators[:40]:
            lines.append(f"- {indicator.get('label')} ({indicator.get('period')}) = {idr(indicator.get('value'), 4)}")

    unreliable = state.get("unreliable_metrics", []) or []
    if unreliable:
        lines.append("")
        lines.append("METRIK YANG TIDAK DAPAT DIEKSTRAKSI DENGAN ANDAL: " + ", ".join(sorted(unreliable)))

    lines.append("")
    lines.append("TEMUAN YANG LOLOS PENGUJIAN BUKTI PENYANGKAL:")
    if not surfaced:
        lines.append("- Tidak ada temuan yang melampaui ambang keyakinan.")
    for finding in surfaced:
        severity = finding.severity.value if isinstance(finding.severity, Severity) else finding.severity
        lines.append(
            f"- [{finding.id}] {finding.title} (severitas {severity}, keyakinan {finding.confidence:.2f})\n"
            f"  {finding.narrative}\n"
            f"  Pertanyaan: {finding.question_for_management}"
        )

    dropped = state.get("dropped_hypotheses", []) or []
    if dropped:
        lines.append("")
        lines.append("HIPOTESIS YANG DIGUGURKAN SETELAH DITEMUKAN BUKTI PENYANGKAL:")
        for hypothesis in dropped:
            lines.append(f"- {hypothesis.get('statement', '')[:200]} → {hypothesis.get('verdict_reason', '')[:200]}")

    if state.get("market_status") != "done":
        lines.append("")
        lines.append("CATATAN: intelijen pasar tidak tersedia pada siklus ini.")

    return "\n".join(lines)


def _template_narrative(state: dict[str, Any], surfaced: list[Finding], score: RiskScore) -> dict[str, Any]:
    """Deterministic fallback narrative. Same citations, plainer prose."""
    company = state.get("company_name") or state.get("ticker")
    periods = ", ".join(state.get("periods", [])) or "periode yang tersedia"

    if surfaced:
        headline = (
            f"{company}: {len(surfaced)} indikator risiko memerlukan klarifikasi "
            f"pada periode {periods}."
        )
    else:
        headline = (
            f"{company}: tidak ada indikator risiko yang melampaui ambang keyakinan "
            f"pada periode {periods}."
        )

    paragraphs = [
        f"Siklus analisis ini menelaah {len(state.get('documents', []))} dokumen untuk "
        f"{company} ({state.get('ticker')}) pada periode {periods}. "
        f"Sebanyak {score.patterns_run} pola deteksi dijalankan atas angka yang telah "
        f"diverifikasi secara aritmetik.",
        f"Dari pola tersebut, {score.flags_retained} temuan dipertahankan setelah pencarian "
        f"bukti penyangkal, dan {score.hypotheses_dropped} hipotesis digugurkan karena "
        f"ditemukan penjelasan pada bagian lain dokumen.",
    ]
    if score.coverage_note:
        paragraphs.append(score.coverage_note)
    paragraphs.append(
        "Seluruh temuan disajikan sebagai indikator yang perlu diklarifikasi. Sentinel-AI "
        "adalah alat riset dan penyaringan risiko; sistem tidak memberikan rekomendasi beli, "
        "jual, atau tahan, dan keputusan tetap berada pada pengguna."
    )

    return {
        "headline": headline,
        "executive_summary": "\n\n".join(paragraphs),
        "key_observations": [f"{f.title} — keyakinan {f.confidence:.2f}." for f in surfaced]
        or ["Tidak ada observasi yang melampaui ambang keyakinan."],
        "areas_requiring_clarification": [f.question_for_management for f in surfaced],
        "data_limitations": score.coverage_note or "Tidak ada keterbatasan data yang menonjol.",
        "source": "template",
    }


def synthesizer_node(state: dict[str, Any], *, llm=None, trail=None) -> dict[str, Any]:
    """LangGraph node entrypoint."""
    return Synthesizer(llm=llm, trail=trail).run(state)
