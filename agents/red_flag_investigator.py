"""Agent 3 — Red Flag Investigator. The primary differentiator.

Pattern matching is the cheap half of this agent. Seven deterministic detectors
run over the verified figures and produce *hypotheses* — not findings. What
makes the system agentic rather than a rule engine is what happens next:

    for each hypothesis:
        formulate what evidence would REFUTE it
        go back into the filing and search for exactly that
        if the refutation is found → drop the hypothesis and log why
        if partially found        → downgrade confidence
        if not found              → escalate to a finding

Dropping a hypothesis is a first-class, logged outcome. The dashboard reports
"hypotheses dropped" alongside "flags retained" because a system that never
discards anything is not investigating, it is just alarming.

Two guardrails run underneath every detector:

* A pattern whose inputs are ``tidak_dapat_diekstraksi_dengan_andal`` does not
  fire. An unread number cannot be evidence of anything.
* Evidence sourced from the web is scaled by source credibility, so a forum
  post can raise a question but cannot sustain a finding on its own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from core.audit_trail import AuditTrail
from core.formatting import compact_idr, idr, pct, ratio
from core.llm import LLMClient, get_llm
from core.state import (
    Citation,
    Evidence,
    ExtractedMetric,
    ExtractionStatus,
    Finding,
    Hypothesis,
    HypothesisStatus,
    Severity,
)
from tools.retrieval import DocumentIndex, get_index

log = logging.getLogger("sentinel.agent3")

AGENT = "red_flag_investigator"


# ---------------------------------------------------------------------------
# Working view over Agent 1 + Agent 2 output
# ---------------------------------------------------------------------------


@dataclass
class Dossier:
    """Everything Agent 3 is allowed to reason from, indexed for lookup."""

    metrics: dict[tuple[str, str], ExtractedMetric]      # (key, period) -> metric
    indicators: dict[tuple[str, str], dict[str, Any]]    # (key, period) -> indicator dump
    periods: list[str]
    news: list[dict[str, Any]]
    claims: list[dict[str, Any]]
    unreliable: set[str]

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "Dossier":
        metrics: dict[tuple[str, str], ExtractedMetric] = {}
        for dump in state.get("metrics", []):
            metric = ExtractedMetric(**dump)
            metrics[(metric.key, metric.period)] = metric
        indicators = {
            (i.get("key", ""), i.get("period", "")): i for i in state.get("indicators", [])
        }
        periods = sorted({p for _, p in metrics} | {p for _, p in indicators}, reverse=True)
        return cls(
            metrics=metrics,
            indicators=indicators,
            periods=[p for p in (state.get("periods") or periods) if p],
            news=state.get("news", []),
            claims=state.get("management_claims", []),
            unreliable=set(state.get("unreliable_metrics", [])),
        )

    # -- guarded accessors --------------------------------------------------

    def value(self, key: str, period: str) -> Optional[float]:
        """Scaled value, or None if unread or flagged unreliable."""
        if key in self.unreliable:
            return None
        metric = self.metrics.get((key, period))
        if metric is None or metric.status is ExtractionStatus.UNRELIABLE:
            return None
        return metric.scaled_value

    def indicator(self, key: str, period: str) -> Optional[float]:
        entry = self.indicators.get((key, period))
        return None if entry is None else entry.get("value")

    def metric_evidence(self, key: str, period: str, note: str = "") -> Optional[Evidence]:
        metric = self.metrics.get((key, period))
        if metric is None:
            return None
        citation = metric.to_citation()
        if citation is None:
            return None
        weight = 1.0 if metric.status is ExtractionStatus.VERIFIED else 0.6
        value = metric.scaled_value
        if note:
            summary = note
        elif value is None:
            summary = f"{metric.label} {period}: tidak terbaca"
        else:
            summary = f"{metric.label} {period}: {compact_idr(value)}"
        return Evidence(summary=summary, citation=citation, weight=weight)

    def indicator_evidence(self, key: str, period: str, note: str = "") -> Optional[Evidence]:
        entry = self.indicators.get((key, period))
        if entry is None:
            return None
        citation = Citation(
            kind="computation",
            formula=entry.get("formula", key),
            inputs=entry.get("inputs", {}) or {},
            result=entry.get("value"),
        )
        label = entry.get("label", key)
        value = entry.get("value")
        summary = note or (f"{label} {period} = {idr(value, 4)}" if value is not None else label)
        return Evidence(summary=summary, citation=citation, weight=1.0)


# ---------------------------------------------------------------------------
# Detection patterns
# ---------------------------------------------------------------------------


@dataclass
class PatternResult:
    fired: bool
    statement: str = ""
    rationale: str = ""
    magnitude: float = 0.0              # 0..1, drives base confidence and severity
    evidence: list[Evidence] = None      # type: ignore[assignment]
    counter_queries: list[str] = None    # type: ignore[assignment]
    skipped_reason: str = ""

    def __post_init__(self) -> None:
        self.evidence = self.evidence or []
        self.counter_queries = self.counter_queries or []


PATTERNS: dict[str, str] = {
    "arus_kas_vs_laba": "Arus kas operasi negatif meskipun laba bersih positif",
    "piutang_vs_pendapatan": "Pertumbuhan piutang melampaui pertumbuhan pendapatan",
    "kualitas_laba_akrual": "Porsi akrual tinggi terhadap laba yang dilaporkan",
    "leverage_solvabilitas": "Struktur utang dan likuiditas jangka pendek memburuk",
    "pihak_berelasi": "Intensitas transaksi pihak berelasi",
    "opini_auditor": "Kualitas opini auditor dan perubahan auditor",
    "klaim_vs_angka": "Klaim manajemen bertentangan dengan angka terverifikasi",
}


def _p_cash_flow_vs_profit(dossier: Dossier, index: Optional[DocumentIndex]) -> PatternResult:
    """The Garuda/Hanson signature: accounting profit without operating cash."""
    period = dossier.periods[0] if dossier.periods else ""
    cfo, net_income = dossier.value("arus_kas_operasi", period), dossier.value("laba_bersih", period)
    if cfo is None or net_income is None:
        return PatternResult(False, skipped_reason="arus_kas_operasi / laba_bersih belum terbaca andal")
    if not (net_income > 0 and cfo < 0):
        return PatternResult(False)

    gap = abs(cfo - net_income)
    magnitude = min(gap / max(abs(net_income), 1.0), 1.0)
    evidence = [e for e in (
        dossier.metric_evidence("laba_bersih", period),
        dossier.metric_evidence("arus_kas_operasi", period),
        dossier.indicator_evidence("cfo_to_net_income", period),
        dossier.indicator_evidence("accrual_ratio", period),
    ) if e]
    return PatternResult(
        True,
        statement=(
            f"Pada {period} perusahaan melaporkan laba bersih positif sebesar "
            f"{compact_idr(net_income)}, sementara arus kas dari aktivitas operasi "
            f"negatif sebesar {compact_idr(abs(cfo))}."
        ),
        rationale=(
            "Laba akuntansi yang tidak disertai kas operasi dapat mengindikasikan pengakuan "
            "pendapatan yang agresif, penumpukan piutang, atau kapitalisasi beban."
        ),
        magnitude=magnitude,
        evidence=evidence,
        counter_queries=[
            "perubahan modal kerja aktivitas operasi",
            "pembayaran pajak penghasilan badan restitusi",
            "akuisisi divestasi entitas anak arus kas",
            "pembayaran beban bunga restrukturisasi pinjaman",
            "musiman siklus penagihan pelanggan",
        ],
    )


def _p_receivables_vs_revenue(dossier: Dossier, index: Optional[DocumentIndex]) -> PatternResult:
    period = dossier.periods[0] if dossier.periods else ""
    receivable_growth = dossier.indicator("growth_piutang_usaha", period)
    revenue_growth = dossier.indicator("growth_pendapatan", period)
    if receivable_growth is None or revenue_growth is None:
        return PatternResult(False, skipped_reason="tren piutang/pendapatan memerlukan dua periode terbaca")

    divergence = receivable_growth - revenue_growth
    # A 15pp gap is the conventional screening threshold for revenue-quality work.
    if divergence < 0.15:
        return PatternResult(False)

    # A 40pp gap is treated as a full-strength signal; 15pp is the trigger.
    magnitude = min(divergence / 0.40, 1.0)
    evidence = [e for e in (
        dossier.indicator_evidence("growth_piutang_usaha", period),
        dossier.indicator_evidence("growth_pendapatan", period),
        dossier.indicator_evidence("days_sales_outstanding", period),
        dossier.metric_evidence("piutang_usaha", period),
    ) if e]
    return PatternResult(
        True,
        statement=(
            f"Pada {period} piutang usaha tumbuh {pct(receivable_growth)} sementara pendapatan "
            f"tumbuh {pct(revenue_growth)} — selisih {pct(divergence)}."
        ),
        rationale=(
            "Piutang yang tumbuh jauh lebih cepat daripada pendapatan menunjukkan penjualan "
            "yang belum terkonversi menjadi kas, atau pelonggaran syarat kredit menjelang tutup buku."
        ),
        magnitude=magnitude,
        evidence=evidence,
        counter_queries=[
            "penyisihan penurunan nilai piutang umur piutang",
            "perubahan kebijakan syarat pembayaran pelanggan",
            "kontrak baru pelanggan besar akhir tahun",
            "piutang pihak berelasi rincian",
            "penagihan setelah periode pelaporan peristiwa setelah tanggal neraca",
        ],
    )


def _p_accrual_quality(dossier: Dossier, index: Optional[DocumentIndex]) -> PatternResult:
    period = dossier.periods[0] if dossier.periods else ""
    accrual = dossier.indicator("accrual_ratio", period)
    cfo_ni = dossier.indicator("cfo_to_net_income", period)
    if accrual is None:
        return PatternResult(False, skipped_reason="rasio akrual memerlukan laba bersih, AKO, dan total aset")
    # Sloan's accrual anomaly screens at roughly 10% of total assets.
    if accrual <= 0.10:
        return PatternResult(False)

    magnitude = min((accrual - 0.10) / 0.25, 1.0)
    evidence = [e for e in (
        dossier.indicator_evidence("accrual_ratio", period),
        dossier.indicator_evidence("cfo_to_net_income", period),
        dossier.metric_evidence("total_aset", period),
    ) if e]
    detail = f" Rasio AKO terhadap laba bersih {idr(cfo_ni, 2)}." if cfo_ni is not None else ""
    return PatternResult(
        True,
        statement=(
            f"Rasio akrual {period} mencapai {pct(accrual)} dari total aset, di atas ambang "
            f"penyaringan 10%.{detail}"
        ),
        rationale=(
            "Porsi laba yang besar berasal dari akrual, bukan kas. Kualitas laba seperti ini "
            "secara historis kurang persisten pada periode berikutnya."
        ),
        magnitude=magnitude,
        evidence=evidence,
        counter_queries=[
            "penyusutan amortisasi beban non kas",
            "keuntungan revaluasi aset selisih kurs belum direalisasi",
            "pendapatan ditangguhkan kontrak jangka panjang",
            "beban imbalan kerja provisi",
        ],
    )


def _p_leverage(dossier: Dossier, index: Optional[DocumentIndex]) -> PatternResult:
    period = dossier.periods[0] if dossier.periods else ""
    der = dossier.indicator("debt_to_equity", period)
    current_ratio = dossier.indicator("current_ratio", period)
    if der is None and current_ratio is None:
        return PatternResult(False, skipped_reason="rasio leverage/likuiditas belum dapat dihitung")

    triggers, magnitudes = [], []
    if der is not None and der > 2.0:
        triggers.append(f"rasio utang terhadap ekuitas {ratio(der)}")
        magnitudes.append(min((der - 2.0) / 3.0, 1.0))
    if current_ratio is not None and current_ratio < 1.0:
        triggers.append(f"rasio lancar {ratio(current_ratio)} (di bawah 1,0)")
        magnitudes.append(min((1.0 - current_ratio) / 0.5, 1.0))

    prior = dossier.periods[1] if len(dossier.periods) > 1 else ""
    prior_der = dossier.indicator("debt_to_equity", prior) if prior else None
    if der is not None and prior_der is not None and prior_der > 0 and der / prior_der > 1.4:
        triggers.append(f"kenaikan leverage {ratio(prior_der)} → {ratio(der)} dalam satu periode")
        magnitudes.append(min((der / prior_der - 1.4) / 1.0, 1.0))

    if not triggers:
        return PatternResult(False)

    evidence = [e for e in (
        dossier.indicator_evidence("debt_to_equity", period),
        dossier.indicator_evidence("current_ratio", period),
        dossier.metric_evidence("liabilitas_jangka_pendek", period),
        dossier.metric_evidence("kas_dan_setara_kas", period),
    ) if e]
    return PatternResult(
        True,
        statement=f"Indikator solvabilitas {period} menunjukkan " + "; ".join(triggers) + ".",
        rationale=(
            "Struktur pendanaan yang berat pada utang jangka pendek meningkatkan risiko "
            "refinancing apabila arus kas operasi tidak mencukupi."
        ),
        # Crossing a solvency threshold at all is material; the scaled excess
        # then separates a marginal breach from a severe one.
        magnitude=min(0.45 + 0.55 * max(magnitudes), 1.0),
        evidence=evidence,
        counter_queries=[
            "fasilitas kredit belum ditarik komitmen pinjaman",
            "restrukturisasi utang perpanjangan jatuh tempo",
            "kas dan setara kas terbatas penggunaannya",
            "rencana penambahan modal rights issue",
        ],
    )


def _p_related_party(dossier: Dossier, index: Optional[DocumentIndex]) -> PatternResult:
    if index is None:
        return PatternResult(False, skipped_reason="indeks dokumen tidak tersedia")
    hits = index.search("transaksi pihak berelasi saldo piutang utang kepada pihak berelasi", k=6)
    if not hits:
        return PatternResult(False)

    # Prominence, not mere presence: every filing has a related-party note, and
    # BM25 will happily return a balance-sheet page that merely shares the words
    # "piutang" and "utang". Require the actual phrase.
    strong = [h for h in hits if "pihak berelasi" in h.text.lower()]
    if len(strong) < 3:
        return PatternResult(False)

    magnitude = min(len(strong) / 8.0, 0.8)
    evidence = [
        Evidence(
            summary=f"Catatan pihak berelasi, hal. {hit.page}: {hit.text.strip()[:200]}",
            citation=Citation(**hit.citation_payload()),
            weight=0.8,
        )
        for hit in strong[:3]
    ]
    pages = sorted({h.page for h in strong})
    return PatternResult(
        True,
        statement=(
            f"Transaksi dengan pihak berelasi dibahas pada {len(strong)} bagian catatan "
            f"(hal. {', '.join(str(p) for p in pages[:6])})."
        ),
        rationale=(
            "Konsentrasi transaksi pihak berelasi memerlukan penelaahan atas kewajaran harga "
            "dan atas kemungkinan penggunaannya untuk mengatur pengakuan pendapatan atau beban."
        ),
        magnitude=magnitude,
        evidence=evidence,
        counter_queries=[
            "transaksi pihak berelasi dilakukan dengan syarat dan kondisi yang sama pihak ketiga",
            "penilaian independen harga wajar transaksi afiliasi",
            "persetujuan pemegang saham independen transaksi material",
        ],
    )


_OPINION_WARNINGS = (
    ("tidak menyatakan pendapat", 1.0, "disclaimer of opinion"),
    ("opini tidak wajar", 1.0, "adverse opinion"),
    ("dengan pengecualian", 0.8, "qualified opinion"),
    ("penekanan suatu hal", 0.45, "emphasis of matter"),
    ("kelangsungan usaha", 0.80, "going concern"),
    ("ketidakpastian material", 0.75, "material uncertainty"),
)


def _p_auditor_opinion(dossier: Dossier, index: Optional[DocumentIndex]) -> PatternResult:
    if index is None:
        return PatternResult(False, skipped_reason="indeks dokumen tidak tersedia")
    hits = index.search(
        "laporan auditor independen opini kelangsungan usaha pengecualian penekanan suatu hal", k=8
    )
    if not hits:
        return PatternResult(False)

    triggered: list[tuple[str, float, Any]] = []
    for hit in hits:
        lowered = hit.text.lower()
        for phrase, weight, label in _OPINION_WARNINGS:
            if phrase in lowered:
                triggered.append((label, weight, hit))
                break
    if not triggered:
        return PatternResult(False)

    magnitude = max(weight for _, weight, _ in triggered)
    labels = sorted({label for label, _, _ in triggered})
    evidence = [
        Evidence(
            summary=f"Opini auditor ({label}), hal. {hit.page}: {hit.text.strip()[:220]}",
            citation=Citation(**hit.citation_payload()),
            weight=1.0,
        )
        for label, _, hit in triggered[:3]
    ]
    return PatternResult(
        True,
        statement="Laporan auditor memuat indikasi: " + ", ".join(labels) + ".",
        rationale=(
            "Modifikasi opini atau paragraf penekanan menandai area yang auditor sendiri "
            "anggap berisiko, dan biasanya merujuk pada pos yang sama dengan temuan kuantitatif."
        ),
        magnitude=magnitude,
        evidence=evidence,
        counter_queries=[
            "opini wajar tanpa modifikasian dalam semua hal yang material",
            "manajemen telah menyelesaikan rencana mitigasi kelangsungan usaha",
            "paragraf penekanan tidak memodifikasi opini",
        ],
    )


def _p_claim_vs_numbers(dossier: Dossier, index: Optional[DocumentIndex]) -> PatternResult:
    """Cross-examine management statements against the verified figures."""
    if not dossier.claims:
        return PatternResult(False, skipped_reason="tidak ada klaim manajemen yang dapat diuji")
    period = dossier.periods[0] if dossier.periods else ""

    contradictions: list[tuple[dict[str, Any], str, float]] = []
    for claim in dossier.claims:
        direction = claim.get("direction")
        if not direction:
            continue
        for key in claim.get("checkable_metrics") or []:
            growth = dossier.indicator(f"growth_{key}", period)
            level = dossier.value(key, period)
            if growth is None and level is None:
                continue
            contradiction = None
            if growth is not None:
                if direction in ("naik", "positif") and growth < -0.02:
                    contradiction = f"{key} justru turun {pct(abs(growth))}"
                elif direction == "turun" and growth > 0.02:
                    contradiction = f"{key} justru naik {pct(growth)}"
            if contradiction is None and level is not None and direction == "positif" and level < 0:
                contradiction = f"{key} bernilai negatif ({compact_idr(level)})"
            if contradiction:
                contradictions.append((claim, contradiction, claim.get("source_weight", 0.4)))
                break

    if not contradictions:
        return PatternResult(False)

    magnitude = min(0.4 + 0.2 * len(contradictions), 1.0)
    evidence: list[Evidence] = []
    lines: list[str] = []
    for claim, contradiction, weight in contradictions[:3]:
        lines.append(f"“{claim.get('claim', '')[:140]}” — {contradiction}")
        evidence.append(Evidence(
            summary=f"Klaim manajemen: {claim.get('claim', '')[:200]}",
            citation=Citation(
                kind="web", url=claim.get("url", ""), title=claim.get("speaker") or "Pernyataan manajemen",
                source_tier=claim.get("source_tier"), published=claim.get("published"),
            ),
            weight=float(weight),
        ))
        for key in (claim.get("checkable_metrics") or [])[:2]:
            item = dossier.indicator_evidence(f"growth_{key}", period) or dossier.metric_evidence(key, period)
            if item:
                evidence.append(item)

    return PatternResult(
        True,
        statement="Terdapat ketidaksesuaian antara pernyataan publik dan angka terverifikasi: " + "; ".join(lines),
        rationale=(
            "Selisih antara narasi manajemen dan angka yang diverifikasi adalah indikator yang "
            "perlu diklarifikasi, bukan tuduhan; perbedaan definisi atau cakupan sering menjelaskannya."
        ),
        magnitude=magnitude,
        evidence=evidence,
        counter_queries=[
            "definisi pendapatan segmen berkelanjutan operasi dilanjutkan",
            "kinerja proforma tidak termasuk pos luar biasa",
            "penyajian kembali laporan keuangan periode sebelumnya",
        ],
    )


_DETECTORS: tuple[tuple[str, Callable[[Dossier, Optional[DocumentIndex]], PatternResult]], ...] = (
    ("arus_kas_vs_laba", _p_cash_flow_vs_profit),
    ("piutang_vs_pendapatan", _p_receivables_vs_revenue),
    ("kualitas_laba_akrual", _p_accrual_quality),
    ("leverage_solvabilitas", _p_leverage),
    ("pihak_berelasi", _p_related_party),
    ("opini_auditor", _p_auditor_opinion),
    ("klaim_vs_angka", _p_claim_vs_numbers),
)


# ---------------------------------------------------------------------------
# Falsification
# ---------------------------------------------------------------------------

_FALSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["digugurkan", "melemah", "bertahan"],
            "description": (
                "digugurkan = bukti penyangkal menjelaskan anomali secara memadai; "
                "melemah = penjelasan sebagian; bertahan = tidak ada penjelasan memadai."
            ),
        },
        "reason": {"type": "string", "description": "Alasan singkat, maksimal 3 kalimat."},
        "decisive_passages": {
            "type": "array",
            "description": "Nomor kutipan yang menentukan verdict.",
            "items": {"type": "integer"},
        },
        "confidence_adjustment": {
            "type": "number",
            "description": "Faktor pengali 0.0-1.0 terhadap keyakinan awal.",
        },
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}

_FALSIFY_SYSTEM = """Anda adalah penguji hipotesis risiko. Tugas Anda BUKAN membenarkan hipotesis,
melainkan mencari alasan untuk menggugurkannya.

Anda diberi satu hipotesis risiko dan kutipan dari bagian LAIN dokumen yang sama
(bukan bagian yang memunculkan hipotesis). Nilai apakah kutipan tersebut
menjelaskan anomali secara memadai.

Aturan:
1. Jika kutipan memberi penjelasan wajar dan spesifik atas anomali, jawab "digugurkan".
2. Jika kutipan menjelaskan sebagian saja, jawab "melemah".
3. Jawab "bertahan" HANYA jika tidak ada kutipan yang menjelaskan anomali.
4. Ketiadaan kutipan yang relevan bukan bukti bahwa hipotesis benar; itu hanya
   berarti bantahan tidak ditemukan.
5. Jangan menambahkan pengetahuan luar tentang perusahaan ini.
"""


class RedFlagInvestigator:
    """Agent 3. Detect, then actively try to refute."""

    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        trail: Optional[AuditTrail] = None,
        index: Optional[DocumentIndex] = None,
    ):
        self.llm = llm or get_llm()
        self.trail = trail
        self.index = index

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        dossier = Dossier.from_state(state)

        index = self.index
        if index is None and state.get("document_paths"):
            try:
                index = get_index(state["document_paths"])
                self._log(events, "muat_indeks", tool="retrieval",
                          decision=index.backend, payload=index.stats())
            except Exception as exc:
                self._log(events, "muat_indeks", tool="retrieval", decision="gagal", detail=str(exc))
                index = None

        if dossier.unreliable:
            self._log(events, "batasi_cakupan",
                      decision=f"{len(dossier.unreliable)} metrik dikecualikan",
                      detail="pola yang bergantung pada metrik tidak andal tidak dijalankan",
                      payload={"unreliable": sorted(dossier.unreliable)})

        # --- 1. run detectors ------------------------------------------------
        hypotheses: list[Hypothesis] = []
        patterns_run: list[str] = []
        for pattern_id, detector in _DETECTORS:
            patterns_run.append(pattern_id)
            try:
                result = detector(dossier, index)
            except Exception as exc:  # a broken detector must not kill the run
                self._log(events, "pola_error", decision=pattern_id, detail=str(exc))
                continue

            if not result.fired:
                self._log(events, "jalankan_pola", tool="python_sandbox", decision=f"{pattern_id}: negatif",
                          detail=result.skipped_reason or "tidak ada sinyal di atas ambang")
                continue

            hypothesis = Hypothesis(
                id=f"H{len(hypotheses) + 1}",
                pattern=pattern_id,
                statement=result.statement,
                rationale=result.rationale,
                supporting=result.evidence,
                counter_search_queries=result.counter_queries,
                confidence=_base_confidence(result),
            )
            hypotheses.append(hypothesis)
            self._log(events, "jalankan_pola", tool="python_sandbox",
                      decision=f"{pattern_id}: hipotesis {hypothesis.id}",
                      detail=result.statement[:200],
                      payload={"magnitude": round(result.magnitude, 3),
                               "base_confidence": hypothesis.confidence})

        self._log(events, "ringkas_hipotesis",
                  decision=f"{len(hypotheses)} hipotesis dari {len(patterns_run)} pola",
                  payload={"patterns_run": patterns_run})

        # --- 2. falsify each one ---------------------------------------------
        retained: list[Hypothesis] = []
        dropped: list[Hypothesis] = []
        for hypothesis in hypotheses:
            self._falsify(hypothesis, index, dossier, events)
            if hypothesis.status is HypothesisStatus.DROPPED:
                dropped.append(hypothesis)
            else:
                retained.append(hypothesis)

        # --- 3. promote survivors to findings ---------------------------------
        findings = [_to_finding(h) for h in retained]
        findings.sort(key=lambda f: (_SEVERITY_ORDER[f.severity], f.confidence), reverse=True)

        self._log(events, "selesai_investigasi",
                  decision=f"{len(findings)} temuan dipertahankan, {len(dropped)} hipotesis digugurkan",
                  detail="; ".join(f.title for f in findings[:4]) or "tidak ada temuan",
                  payload={"retained": [h.id for h in retained], "dropped": [h.id for h in dropped]})

        return {
            "hypotheses": [h.model_dump(mode="json") for h in hypotheses],
            "dropped_hypotheses": [h.model_dump(mode="json") for h in dropped],
            "findings": [f.model_dump(mode="json") for f in findings],
            "patterns_run": patterns_run,
            "audit_trail": events,
        }

    # -- falsification -------------------------------------------------------

    def _falsify(
        self,
        hypothesis: Hypothesis,
        index: Optional[DocumentIndex],
        dossier: Dossier,
        events: list[dict[str, Any]],
    ) -> None:
        """Search for the evidence that would refute this hypothesis."""
        origin_pages = {
            e.citation.page for e in hypothesis.supporting
            if e.citation.kind == "document" and e.citation.page
        }

        passages = []
        if index is not None:
            for query in hypothesis.counter_search_queries:
                passages.extend(index.search(query, k=2, exclude_pages=origin_pages))

        # De-duplicate by chunk, keep the strongest.
        unique: dict[str, Any] = {}
        for passage in passages:
            existing = unique.get(passage.chunk_id)
            if existing is None or passage.score > existing.score:
                unique[passage.chunk_id] = passage
        candidates = sorted(unique.values(), key=lambda p: p.score, reverse=True)[:6]

        self._log(events, "cari_bukti_penyangkal", tool="retrieval",
                  decision=f"{hypothesis.id}: {len(candidates)} kutipan",
                  detail=f"kueri: {'; '.join(hypothesis.counter_search_queries[:3])}",
                  payload={"excluded_pages": sorted(origin_pages),
                           "pages_found": sorted({p.page for p in candidates})})

        hypothesis.counter_evidence = [
            Evidence(
                summary=f"hal. {p.page}: {p.text.strip()[:220]}",
                citation=Citation(**p.citation_payload()),
                weight=0.9,
            )
            for p in candidates
        ]

        if not candidates:
            hypothesis.status = HypothesisStatus.SURVIVED
            hypothesis.verdict_reason = (
                "Tidak ditemukan penjelasan penyangkal pada bagian lain dokumen. "
                "Ketiadaan bantahan bukan konfirmasi; temuan tetap disajikan sebagai "
                "pertanyaan yang perlu diklarifikasi."
            )
            # No refutation found is weaker than a refutation actively rejected,
            # so confidence is not raised — merely held.
            self._log(events, "putuskan_hipotesis", decision=f"{hypothesis.id}: bertahan",
                      detail="tanpa bukti penyangkal", payload={"confidence": hypothesis.confidence})
            return

        verdict, reason, adjustment = self._judge(hypothesis, candidates, events)
        hypothesis.verdict_reason = reason
        if verdict == HypothesisStatus.DROPPED:
            hypothesis.status = HypothesisStatus.DROPPED
            hypothesis.confidence = 0.0
        elif verdict == HypothesisStatus.WEAKENED:
            hypothesis.status = HypothesisStatus.WEAKENED
            hypothesis.confidence = round(hypothesis.confidence * adjustment, 3)
        else:
            hypothesis.status = HypothesisStatus.SURVIVED
            hypothesis.confidence = round(min(hypothesis.confidence * 1.05, 0.95), 3)

        self._log(events, "putuskan_hipotesis",
                  decision=f"{hypothesis.id}: {hypothesis.status.value}",
                  detail=reason[:220],
                  payload={"confidence": hypothesis.confidence,
                           "counter_pages": sorted({p.page for p in candidates})})

    def _judge(
        self, hypothesis: Hypothesis, candidates: list[Any], events: list[dict[str, Any]]
    ) -> tuple[HypothesisStatus, str, float]:
        if not self.llm.available:
            # Deterministic fallback: retrieval found related passages but no
            # model is available to weigh them. Downgrade rather than assert.
            return (
                HypothesisStatus.WEAKENED,
                ("Ditemukan bagian dokumen lain yang berpotensi menjelaskan anomali, namun "
                 "penilaian relevansinya memerlukan penalaran model yang tidak tersedia pada "
                 "eksekusi ini. Bukti penyangkal tetap dilampirkan dan keyakinan diturunkan "
                 "sebagai sikap konservatif."),
                0.85,
            )

        listing = "\n\n".join(
            f"[{i}] hal. {p.page} ({p.statement_type}): {p.text.strip()[:800]}"
            for i, p in enumerate(candidates)
        )
        result = self.llm.structured(
            purpose="agent3_falsification",
            system=_FALSIFY_SYSTEM,
            user=(
                f"HIPOTESIS ({hypothesis.pattern}):\n{hypothesis.statement}\n\n"
                f"Dasar penalaran:\n{hypothesis.rationale}\n\n"
                f"Kutipan dari bagian lain dokumen:\n{listing}\n\n"
                "Apakah kutipan di atas menggugurkan hipotesis ini?"
            ),
            schema=_FALSIFY_SCHEMA,
            tool_name="submit_verdict",
            tool_description="Kirim penilaian atas bukti penyangkal.",
            max_tokens=2000,
        )
        if not result.ok:
            self._log(events, "panggil_model", tool="llm", decision="gagal",
                      detail=result.error or "tidak ada respons")
            return HypothesisStatus.WEAKENED, "Penilaian bukti penyangkal gagal; keyakinan diturunkan.", 0.7

        mapping = {
            "digugurkan": HypothesisStatus.DROPPED,
            "melemah": HypothesisStatus.WEAKENED,
            "bertahan": HypothesisStatus.SURVIVED,
        }
        verdict = mapping.get(result.data.get("verdict", ""), HypothesisStatus.WEAKENED)
        reason = str(result.data.get("reason", ""))[:600] or "Tanpa alasan eksplisit."
        adjustment = result.data.get("confidence_adjustment")
        try:
            adjustment = min(max(float(adjustment), 0.0), 1.0)
        except (TypeError, ValueError):
            adjustment = 0.6
        return verdict, reason, adjustment

    def _log(self, events: list[dict[str, Any]], action: str, **kwargs: Any) -> None:
        if self.trail:
            events.append(self.trail.log(AGENT, action, **kwargs))
        else:
            events.append({"agent": AGENT, "action": action, **kwargs})


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

_SEVERITY_ORDER = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}


def _base_confidence(result: PatternResult) -> float:
    """Confidence before falsification: signal strength × evidence quality.

    Evidence quality is the mean weight of the citations backing the pattern, so
    a pattern resting on unverified figures or low-tier sources starts lower.
    """
    if not result.evidence:
        return round(min(0.35 + 0.25 * result.magnitude, 0.6), 3)
    quality = sum(e.weight for e in result.evidence) / len(result.evidence)
    raw = 0.40 + 0.55 * result.magnitude
    return round(min(raw * (0.6 + 0.4 * quality), 0.92), 3)


def _severity_for(confidence: float, pattern: str) -> Severity:
    # Patterns tied to the historical Indonesian manipulation cases carry more
    # weight at equal confidence than structural/context patterns.
    heavy = {"arus_kas_vs_laba", "opini_auditor", "klaim_vs_angka", "piutang_vs_pendapatan"}
    if confidence >= 0.8:
        return Severity.CRITICAL if pattern in heavy else Severity.HIGH
    if confidence >= 0.65:
        return Severity.HIGH if pattern in heavy else Severity.MEDIUM
    if confidence >= 0.5:
        return Severity.MEDIUM
    return Severity.LOW


_QUESTIONS: dict[str, str] = {
    "arus_kas_vs_laba": (
        "Faktor apa yang menyebabkan arus kas operasi negatif meskipun laba bersih positif, "
        "dan berapa bagian selisih tersebut yang bersifat non-berulang?"
    ),
    "piutang_vs_pendapatan": (
        "Apa yang menjelaskan perbedaan laju pertumbuhan piutang terhadap pendapatan, dan "
        "bagaimana profil umur piutang serta tingkat penagihan setelah tanggal pelaporan?"
    ),
    "kualitas_laba_akrual": (
        "Komponen akrual apa yang paling besar pada periode ini, dan apakah komponen tersebut "
        "diperkirakan berbalik pada periode berikutnya?"
    ),
    "leverage_solvabilitas": (
        "Bagaimana rencana pemenuhan liabilitas jangka pendek yang akan jatuh tempo, dan "
        "fasilitas pendanaan apa yang telah tersedia namun belum ditarik?"
    ),
    "pihak_berelasi": (
        "Bagaimana kewajaran harga pada transaksi pihak berelasi ditetapkan dan diverifikasi "
        "secara independen?"
    ),
    "opini_auditor": (
        "Hal spesifik apa yang mendasari modifikasi atau penekanan pada opini auditor, dan "
        "langkah apa yang telah diambil untuk menyelesaikannya?"
    ),
    "klaim_vs_angka": (
        "Atas dasar definisi atau cakupan apa pernyataan publik tersebut disampaikan, "
        "mengingat angka pada laporan keuangan menunjukkan arah yang berbeda?"
    ),
}


def _to_finding(hypothesis: Hypothesis) -> Finding:
    return Finding(
        id=hypothesis.id,
        title=PATTERNS.get(hypothesis.pattern, hypothesis.pattern),
        pattern=hypothesis.pattern,
        severity=_severity_for(hypothesis.confidence, hypothesis.pattern),
        confidence=hypothesis.confidence,
        narrative=f"{hypothesis.statement} {hypothesis.rationale}".strip(),
        question_for_management=_QUESTIONS.get(
            hypothesis.pattern, "Mohon klarifikasi atas indikator ini."
        ),
        evidence=hypothesis.supporting,
        counter_evidence_considered=hypothesis.counter_evidence,
    )


def red_flag_investigator_node(state: dict[str, Any], *, llm=None, trail=None, index=None) -> dict[str, Any]:
    """LangGraph node entrypoint."""
    return RedFlagInvestigator(llm=llm, trail=trail, index=index).run(state)
