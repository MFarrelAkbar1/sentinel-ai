"""Executive Due Diligence Report assembly.

The report is tiered because its two readers need different things. A decision
maker needs the headline, the score, and the questions to ask. An analyst
verifying the work needs every figure traced to a page and every ratio traced
to a formula. Both tiers are generated from the same objects, so the annex can
never drift from the summary.

The document carries three things that most automated reports omit and that the
technical report treats as load-bearing:

* **The verification table** — which accounting identities were checked, which
  passed, and which were skipped because a figure was never read.
* **Dropped hypotheses** — what the system suspected and then discarded, with
  the counter-evidence that discarded it.
* **Suppressed findings** — what fell below the confidence threshold, so the
  analyst can see the calibration decision rather than infer it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from core.config import get_settings
from core.formatting import compact_idr, idr, pct
from core.state import Citation, Finding, RiskScore, Severity

DISCLAIMER = (
    "Sentinel-AI adalah alat riset dan penyaringan risiko, bukan penasihat investasi. "
    "Sistem tidak memprediksi harga saham dan tidak memberikan rekomendasi beli, jual, "
    "atau tahan. Setiap temuan disajikan sebagai indikator yang perlu diklarifikasi, "
    "disertai tautan ke halaman dokumen sumbernya. Keputusan tetap berada pada pengguna."
)

PROVENANCE_NOTE = (
    "Setiap angka dalam laporan ini berasal dari kutipan dokumen bersitasi halaman atau "
    "dari perhitungan Python atas angka bersitasi. Tidak ada jalur ketiga."
)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_report(
    state: dict[str, Any],
    findings: list[Finding],
    score: RiskScore,
    narrative: dict[str, Any],
) -> dict[str, Any]:
    """Compile the full report object (tier 1 + tier 2) as plain data."""
    surfaced = [f for f in findings if not f.suppressed]
    suppressed = [f for f in findings if f.suppressed]

    citations = _collect_citations(findings)
    report: dict[str, Any] = {
        "meta": {
            "run_id": state.get("run_id"),
            "ticker": state.get("ticker"),
            "company_name": state.get("company_name"),
            "sector": state.get("sector"),
            "trigger": state.get("trigger"),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "periods": state.get("periods", []),
            "documents": state.get("documents", []),
            "narrative_source": narrative.get("source", "template"),
        },
        "disclaimer": DISCLAIMER,
        "provenance_note": PROVENANCE_NOTE,
        "executive": {
            "headline": narrative.get("headline", ""),
            "summary": narrative.get("executive_summary", ""),
            "key_observations": narrative.get("key_observations", []),
            "questions": narrative.get("areas_requiring_clarification", []),
            "data_limitations": narrative.get("data_limitations", ""),
            "risk_score": score.model_dump(mode="json"),
        },
        "findings": [f.model_dump(mode="json") for f in surfaced],
        "suppressed_findings": [f.model_dump(mode="json") for f in suppressed],
        "dropped_hypotheses": state.get("dropped_hypotheses", []),
        "annex": {
            "metrics": state.get("metrics", []),
            "verifications": state.get("verifications", []),
            "indicators": state.get("indicators", []),
            "unreliable_metrics": state.get("unreliable_metrics", []),
            "news": state.get("news", []),
            "management_claims": state.get("management_claims", []),
            "audit_trail": state.get("audit_trail", []),
        },
        "sections": ["executive", "findings", "annex"],
        "stats": {
            "findings_surfaced": len(surfaced),
            "findings_suppressed": len(suppressed),
            "hypotheses_dropped": len(state.get("dropped_hypotheses", [])),
            "citations": len(citations),
            "metrics_extracted": len(state.get("metrics", [])),
            "checks_run": len(state.get("verifications", [])),
            "audit_events": len(state.get("audit_trail", [])),
        },
    }
    report["markdown"] = render_markdown(report)
    return report


def _collect_citations(findings: Iterable[Finding]) -> list[Citation]:
    citations: list[Citation] = []
    for finding in findings:
        for evidence in list(finding.evidence) + list(finding.counter_evidence_considered):
            citations.append(evidence.citation)
    return citations


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_markdown(report: dict[str, Any]) -> str:
    meta = report["meta"]
    executive = report["executive"]
    score = executive["risk_score"]

    out: list[str] = []
    add = out.append

    # --- Tier 1: executive --------------------------------------------------
    add(f"# Executive Due Diligence Report — {meta['company_name']} ({meta['ticker']})")
    add("")
    add(f"*Dihasilkan {meta['generated_at']} · Run `{meta['run_id']}` · Pemicu: {meta['trigger']}*")
    add("")
    add(f"> {report['disclaimer']}")
    add("")
    add("## 1. Ringkasan Eksekutif")
    add("")
    add(f"**{executive['headline']}**")
    add("")
    add(executive["summary"])
    add("")

    add("### Skor Risiko Komposit")
    add("")
    add("| | |")
    add("|---|---|")
    add(f"| **Skor** | **{score['score']:.0f} / 100 — {score['band']}** (skor terbalik: makin tinggi makin aman) |")
    add(f"| Pola dijalankan | {score['patterns_run']} |")
    add(f"| Temuan dipertahankan | {score['flags_retained']} |")
    add(f"| Hipotesis digugurkan | {score['hypotheses_dropped']} |")
    add(f"| Cakupan verifikasi | {pct(score['data_coverage'], 0)} identitas akuntansi dapat dievaluasi |")
    add("")
    if score.get("coverage_note"):
        add(f"> ⚠️ {score['coverage_note']}")
        add("")
    if not score.get("assessable", True):
        add("> ⚠️ Cakupan data tidak memadai untuk penilaian menyeluruh. Skor di atas "
            "hanya merefleksikan pola yang dapat dijalankan.")
        add("")

    if executive.get("key_observations"):
        add("### Observasi Utama")
        add("")
        for observation in executive["key_observations"]:
            add(f"- {observation}")
        add("")

    if executive.get("questions"):
        add("### Hal yang Perlu Diklarifikasi kepada Manajemen")
        add("")
        for index, question in enumerate(executive["questions"], 1):
            add(f"{index}. {question}")
        add("")

    if executive.get("data_limitations"):
        add("### Keterbatasan Data")
        add("")
        add(executive["data_limitations"])
        add("")

    # --- Tier 1.5: findings -------------------------------------------------
    add("## 2. Temuan")
    add("")
    if not report["findings"]:
        add("Tidak ada temuan yang melampaui ambang keyakinan pada siklus ini.")
        add("")
    for finding in report["findings"]:
        add(f"### [{finding['id']}] {finding['title']}")
        add("")
        add(f"**Severitas:** {finding['severity']} · **Keyakinan:** {finding['confidence']:.2f} · "
            f"**Pola:** `{finding['pattern']}`")
        add("")
        add(finding["narrative"])
        add("")
        add(f"**Pertanyaan untuk manajemen:** {finding['question_for_management']}")
        add("")
        if finding.get("evidence"):
            add("**Bukti pendukung:**")
            add("")
            for evidence in finding["evidence"]:
                add(f"- {evidence['summary']}  \n  {_render_citation(evidence['citation'])}")
            add("")
        if finding.get("counter_evidence_considered"):
            add("**Bukti penyangkal yang ditelaah:**")
            add("")
            for evidence in finding["counter_evidence_considered"]:
                add(f"- {evidence['summary']}  \n  {_render_citation(evidence['citation'])}")
            add("")

    # --- Tier 2: technical annex --------------------------------------------
    annex = report["annex"]
    add("---")
    add("")
    add("## 3. Lampiran Teknis")
    add("")
    add(f"*{report['provenance_note']}*")
    add("")

    add("### 3.1 Dokumen Sumber")
    add("")
    add("| Dokumen | Halaman | Parser | Satuan | Bagian terklasifikasi |")
    add("|---|---|---|---|---|")
    for document in meta.get("documents", []):
        sections = ", ".join(
            f"{name} (hal. {', '.join(str(p) for p in pages[:4])})"
            for name, pages in (document.get("statement_pages") or {}).items()
        ) or "—"
        add(f"| `{document.get('doc_id')}` | {document.get('page_count')} | "
            f"{document.get('parser')} | {document.get('scale_label')} | {sections} |")
    add("")

    add("### 3.2 Angka Terekstraksi")
    add("")
    add("| Metrik | Periode | Nilai | Status | Halaman | Baris sumber |")
    add("|---|---|---|---|---|---|")
    for metric in sorted(annex.get("metrics", []), key=lambda m: (m.get("period", ""), m.get("key", "")), reverse=True):
        value = metric.get("value")
        scale = metric.get("scale", 1.0) or 1.0
        rendered = compact_idr(value * scale) if value is not None else "—"
        source = (metric.get("source_line") or "").replace("|", "/")[:90]
        add(f"| `{metric.get('key')}` | {metric.get('period')} | {rendered} | "
            f"{metric.get('status')} | {metric.get('page')} | {source} |")
    add("")
    if annex.get("unreliable_metrics"):
        add("**Tidak dapat diekstraksi dengan andal:** "
            + ", ".join(f"`{k}`" for k in annex["unreliable_metrics"]))
        add("")

    add("### 3.3 Verifikasi Aritmetik")
    add("")
    add("| Identitas | Periode | Formula | Diharapkan | Terhitung | Selisih | Hasil |")
    add("|---|---|---|---|---|---|---|")
    for check in annex.get("verifications", []):
        if check.get("skipped"):
            add(f"| {check.get('name')} | {check.get('period')} | `{check.get('formula')}` | — | — | — | "
                f"dilewati: {check.get('skip_reason', '')[:70]} |")
            continue
        expected, actual = check.get("expected"), check.get("actual")
        difference = check.get("difference")
        add(f"| {check.get('name')} | {check.get('period')} | `{check.get('formula')}` | "
            f"{compact_idr(expected)} | {compact_idr(actual)} | {idr(difference)} | "
            f"{'✅ cocok' if check.get('passed') else '❌ tidak cocok'} |")
    add("")

    indicators = [i for i in annex.get("indicators", []) if i.get("value") is not None]
    if indicators:
        add("### 3.4 Indikator Turunan")
        add("")
        add("| Indikator | Periode | Nilai | Formula | Input |")
        add("|---|---|---|---|---|")
        for indicator in indicators:
            inputs = ", ".join(f"{k}={idr(v)}" for k, v in (indicator.get("inputs") or {}).items())
            add(f"| {indicator.get('label')} | {indicator.get('period')} | "
                f"{idr(indicator.get('value'), 4)} | `{indicator.get('formula')}` | {inputs} |")
        add("")

    if report.get("dropped_hypotheses"):
        add("### 3.5 Hipotesis yang Digugurkan")
        add("")
        add("Hipotesis berikut dibentuk oleh pola deteksi, lalu digugurkan setelah bukti "
            "penyangkal ditemukan pada bagian lain dokumen.")
        add("")
        for hypothesis in report["dropped_hypotheses"]:
            add(f"- **[{hypothesis.get('id')}] {hypothesis.get('pattern')}** — {hypothesis.get('statement', '')}")
            add(f"  - Alasan digugurkan: {hypothesis.get('verdict_reason', '')}")
            for evidence in (hypothesis.get("counter_evidence") or [])[:3]:
                add(f"  - {_render_citation(evidence['citation'])}")
        add("")

    if report.get("suppressed_findings"):
        add("### 3.6 Temuan yang Ditahan (di bawah ambang keyakinan)")
        add("")
        for finding in report["suppressed_findings"]:
            add(f"- **[{finding['id']}] {finding['title']}** (keyakinan {finding['confidence']:.2f}) — "
                f"{finding.get('suppression_reason', '')}")
        add("")

    if annex.get("management_claims"):
        add("### 3.7 Klaim Manajemen yang Ditelaah")
        add("")
        add("| Klaim | Jenis | Metrik penguji | Sumber (tier) |")
        add("|---|---|---|---|")
        for claim in annex["management_claims"]:
            metrics = ", ".join(f"`{m}`" for m in claim.get("checkable_metrics", [])) or "—"
            add(f"| {claim.get('claim', '')[:140].replace('|', '/')} | {claim.get('claim_type')} | "
                f"{metrics} | [{claim.get('url', '')[:60]}]({claim.get('url', '')}) (tier {claim.get('source_tier')}) |")
        add("")

    if annex.get("news"):
        add("### 3.8 Sumber Pasar")
        add("")
        add("| Sumber | Tier | Bobot | Tanggal |")
        add("|---|---|---|---|")
        for item in annex["news"][:25]:
            add(f"| [{item.get('title', '')[:80].replace('|', '/')}]({item.get('url')}) | "
                f"{item.get('source_tier')} | {item.get('source_weight')} | {item.get('published') or '—'} |")
        add("")

    add("### 3.9 Jejak Audit")
    add("")
    add("Setiap keputusan agen, tool yang dipanggil, dan hasilnya.")
    add("")
    add("| # | Waktu | Agen | Aksi | Tool | Keputusan | Detail |")
    add("|---|---|---|---|---|---|---|")
    for event in annex.get("audit_trail", []):
        timestamp = str(event.get("timestamp", ""))[11:19]
        detail = str(event.get("detail") or "—").replace("|", "/")[:110]
        add(f"| {event.get('seq', '')} | {timestamp} | {event.get('agent')} | {event.get('action')} | "
            f"{event.get('tool') or '—'} | {str(event.get('decision') or '—')[:60]} | {detail} |")
    add("")

    return "\n".join(out)


def _render_citation(citation: dict[str, Any] | Citation) -> str:
    data = citation.model_dump() if isinstance(citation, Citation) else dict(citation)
    kind = data.get("kind")
    if kind == "document":
        quote = (data.get("quote") or "").replace("\n", " ")[:200]
        return f"`[{data.get('document_id')} hal. {data.get('page')}]` “{quote}”"
    if kind == "computation":
        inputs = ", ".join(f"{k}={idr(v, 2)}" for k, v in (data.get("inputs") or {}).items())
        result = data.get("result")
        rendered = f" = {idr(result, 4)}" if isinstance(result, (int, float)) else ""
        return f"`[hitung]` `{data.get('formula')}`{rendered} ({inputs})"
    return f"`[web tier {data.get('source_tier')}]` [{data.get('title') or data.get('url')}]({data.get('url')})"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def write_report(report: dict[str, Any], output_dir: Optional[Path] = None) -> dict[str, str]:
    """Write the Markdown and JSON artefacts; return their paths."""
    settings = get_settings()
    directory = Path(output_dir or settings.report_dir)
    directory.mkdir(parents=True, exist_ok=True)

    meta = report.get("meta", {})
    stem = f"{meta.get('ticker', 'UNKNOWN')}_{meta.get('run_id', 'run')}"

    markdown_path = directory / f"{stem}.md"
    markdown_path.write_text(report.get("markdown") or render_markdown(report), encoding="utf-8")

    json_path = directory / f"{stem}.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    return {"markdown": str(markdown_path), "json": str(json_path)}


def severity_rank(severity: str | Severity) -> int:
    order = {
        Severity.CRITICAL.value: 3, Severity.HIGH.value: 2,
        Severity.MEDIUM.value: 1, Severity.LOW.value: 0,
    }
    key = severity.value if isinstance(severity, Severity) else str(severity)
    return order.get(key, 0)
