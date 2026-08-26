"""LangGraph state schema and the domain vocabulary it carries.

Two rules are encoded structurally here rather than left to prose:

1. **Every number has a provenance.** ``ExtractedMetric`` cannot be constructed
   without a page number and the verbatim source line. ``Citation`` has exactly
   two shapes — ``document`` (page + quote) or ``computation`` (formula +
   cited inputs). There is no third variant, so there is no way to express an
   unsourced number in the report.
2. **Uncertainty is representable.** ``ExtractionStatus.UNRELIABLE`` is a
   first-class outcome. An agent that fails to extract a number says so; it
   never has the option of emitting a value it did not read.
"""

from __future__ import annotations

import operator
from enum import Enum
from typing import Annotated, Any, Literal, Optional, TypedDict

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Canonical metric vocabulary
# ---------------------------------------------------------------------------
# Keys are the internal identifiers used everywhere in the pipeline; values are
# the Indonesian statement captions the parser and the LLM should match on.
# Aliases matter: BEI filings vary between PSAK-era and IFRS-converged wording.

METRIC_VOCABULARY: dict[str, list[str]] = {
    # --- Laporan posisi keuangan (neraca) ---------------------------------
    "total_aset": ["jumlah aset", "total aset", "jumlah aktiva", "total assets"],
    "aset_lancar": ["jumlah aset lancar", "total aset lancar", "aset lancar"],
    "aset_tidak_lancar": ["jumlah aset tidak lancar", "total aset tidak lancar"],
    "kas_dan_setara_kas": ["kas dan setara kas", "kas dan bank"],
    "piutang_usaha": ["piutang usaha", "piutang dagang", "trade receivables"],
    "persediaan": ["persediaan", "inventories"],
    "total_liabilitas": ["jumlah liabilitas", "total liabilitas", "jumlah kewajiban"],
    "liabilitas_jangka_pendek": ["jumlah liabilitas jangka pendek", "liabilitas jangka pendek"],
    "liabilitas_jangka_panjang": ["jumlah liabilitas jangka panjang", "liabilitas jangka panjang"],
    "total_ekuitas": ["jumlah ekuitas", "total ekuitas", "jumlah modal"],
    # --- Laporan laba rugi -------------------------------------------------
    "pendapatan": ["pendapatan", "penjualan neto", "jumlah pendapatan", "revenue"],
    "beban_pokok_pendapatan": ["beban pokok pendapatan", "beban pokok penjualan", "harga pokok penjualan"],
    "laba_bruto": ["laba bruto", "laba kotor", "gross profit"],
    "beban_usaha": ["jumlah beban usaha", "beban usaha", "beban operasi"],
    "laba_usaha": ["laba usaha", "laba operasi", "operating profit"],
    "laba_sebelum_pajak": ["laba sebelum pajak", "laba sebelum pajak penghasilan"],
    "beban_pajak": ["beban pajak penghasilan", "beban pajak"],
    "laba_bersih": ["laba bersih", "laba tahun berjalan", "laba periode berjalan", "net income"],
    # --- Laporan arus kas ---------------------------------------------------
    "arus_kas_operasi": [
        "kas neto diperoleh dari aktivitas operasi",
        "arus kas neto dari aktivitas operasi",
        "kas bersih yang diperoleh dari aktivitas operasi",
        "arus kas dari aktivitas operasi",
    ],
    "arus_kas_investasi": [
        "kas neto digunakan untuk aktivitas investasi",
        "arus kas neto dari aktivitas investasi",
        "arus kas dari aktivitas investasi",
    ],
    "arus_kas_pendanaan": [
        "kas neto diperoleh dari aktivitas pendanaan",
        "arus kas neto dari aktivitas pendanaan",
        "arus kas dari aktivitas pendanaan",
    ],
    "kas_awal_periode": ["kas dan setara kas awal", "saldo kas awal"],
    "kas_akhir_periode": ["kas dan setara kas akhir", "saldo kas akhir"],
    # --- Catatan atas laporan keuangan --------------------------------------
    "transaksi_pihak_berelasi": ["transaksi dengan pihak berelasi", "pihak berelasi", "related party"],
    "penyisihan_piutang": ["penyisihan penurunan nilai piutang", "cadangan kerugian piutang"],
}

#: Captions that disqualify a metric even though one of its aliases matched.
#: "Jumlah liabilitas dan ekuitas" carries the *total assets* figure, so a
#: substring match on "jumlah liabilitas" there would silently record the wrong
#: number and then break the balance-sheet identity in a confusing place.
METRIC_CAPTION_EXCLUSIONS: dict[str, list[str]] = {
    "total_liabilitas": ["dan ekuitas"],
    "total_aset": ["dan ekuitas"],
    "pendapatan": ["beban", "persentase", "per saham", "ditangguhkan"],
    "piutang_usaha": ["penyisihan", "umur", "dikurangi"],
    "laba_bersih": ["per saham", "komprehensif lain"],
    "kas_dan_setara_kas": ["dibatasi", "terbatas"],
}

#: Income-statement lines presented as deductions. Indonesian statements print
#: these in parentheses to mark the subtraction, not to assert a negative
#: expense, so they are recorded as magnitudes and the identity formulas
#: subtract them explicitly. The original presentation is kept in the note.
DEDUCTION_METRICS: frozenset[str] = frozenset({
    "beban_pokok_pendapatan", "beban_usaha", "beban_pajak",
})

BALANCE_SHEET_METRICS = [
    "total_aset", "aset_lancar", "aset_tidak_lancar", "kas_dan_setara_kas",
    "piutang_usaha", "persediaan", "total_liabilitas", "liabilitas_jangka_pendek",
    "liabilitas_jangka_panjang", "total_ekuitas",
]
INCOME_STATEMENT_METRICS = [
    "pendapatan", "beban_pokok_pendapatan", "laba_bruto", "beban_usaha",
    "laba_usaha", "laba_sebelum_pajak", "beban_pajak", "laba_bersih",
]
CASH_FLOW_METRICS = [
    "arus_kas_operasi", "arus_kas_investasi", "arus_kas_pendanaan",
    "kas_awal_periode", "kas_akhir_periode",
]


class StatementType(str, Enum):
    BALANCE_SHEET = "laporan_posisi_keuangan"
    INCOME_STATEMENT = "laporan_laba_rugi"
    CASH_FLOW = "laporan_arus_kas"
    EQUITY_CHANGES = "laporan_perubahan_ekuitas"
    NOTES = "catatan_atas_laporan_keuangan"
    AUDITOR_OPINION = "opini_auditor"
    MANAGEMENT_DISCUSSION = "analisis_pembahasan_manajemen"
    OTHER = "lainnya"


class ExtractionStatus(str, Enum):
    VERIFIED = "terverifikasi"
    UNVERIFIED = "belum_diverifikasi"
    UNRELIABLE = "tidak_dapat_diekstraksi_dengan_andal"


class ExtractionStrategy(str, Enum):
    """Successive re-read strategies. Each failed verification escalates."""

    TABLE_FIRST = "table_first"          # PyMuPDF find_tables() on statement pages
    TEXT_LAYOUT = "text_layout"          # layout-preserving text + label anchoring
    LINE_ANCHORED = "line_anchored"      # regex anchor on caption, widen page window
    PDFPLUMBER_FALLBACK = "pdfplumber"   # second parser entirely


class Severity(str, Enum):
    CRITICAL = "kritis"
    HIGH = "tinggi"
    MEDIUM = "sedang"
    LOW = "rendah"


class HypothesisStatus(str, Enum):
    PROPOSED = "diajukan"
    SURVIVED = "bertahan"          # counter-evidence searched, none decisive
    WEAKENED = "melemah"           # partial counter-evidence; downgraded
    DROPPED = "digugurkan"         # counter-evidence explains it away


# ---------------------------------------------------------------------------
# Provenance primitives
# ---------------------------------------------------------------------------


class Citation(BaseModel):
    """The only two ways a number or claim may enter a Sentinel-AI report."""

    kind: Literal["document", "computation", "web"]

    # kind == "document"
    document_id: Optional[str] = None
    page: Optional[int] = None
    quote: Optional[str] = Field(default=None, description="Verbatim line from the source page")

    # kind == "computation"
    formula: Optional[str] = None
    inputs: dict[str, float] = Field(default_factory=dict)
    result: Optional[float] = None

    # kind == "web"
    url: Optional[str] = None
    title: Optional[str] = None
    source_tier: Optional[int] = None
    published: Optional[str] = None

    @model_validator(mode="after")
    def _enforce_provenance(self) -> "Citation":
        if self.kind == "document":
            if self.page is None or not self.quote:
                raise ValueError("Sitasi dokumen wajib memiliki nomor halaman dan kutipan verbatim.")
        elif self.kind == "computation":
            if not self.formula:
                raise ValueError("Sitasi komputasi wajib memiliki formula.")
        elif self.kind == "web":
            if not self.url:
                raise ValueError("Sitasi web wajib memiliki URL.")
        return self

    def render(self) -> str:
        if self.kind == "document":
            return f"[{self.document_id or 'dok'} hal. {self.page}] “{(self.quote or '')[:180]}”"
        if self.kind == "computation":
            from core.formatting import idr

            args = ", ".join(f"{k}={idr(v, 2)}" for k, v in self.inputs.items())
            if self.result is None:
                return f"[hitung] {self.formula}"
            return f"[hitung] {self.formula} ({args}) = {idr(self.result, 2)}"
        return f"[web tier {self.source_tier}] {self.title or ''} — {self.url}"


class ExtractedMetric(BaseModel):
    """A single financial figure, inseparable from where it was read."""

    key: str
    label: str
    value: Optional[float] = None
    period: str
    unit: str = "IDR"
    scale: float = 1.0                    # multiplier implied by "dalam jutaan" etc.
    document_id: str = ""
    page: Optional[int] = None
    source_line: Optional[str] = None
    strategy: ExtractionStrategy = ExtractionStrategy.TABLE_FIRST
    status: ExtractionStatus = ExtractionStatus.UNVERIFIED
    note: Optional[str] = None

    @property
    def scaled_value(self) -> Optional[float]:
        return None if self.value is None else self.value * self.scale

    def to_citation(self, document_id: str = "") -> Optional[Citation]:
        if self.page is None or not self.source_line:
            return None
        return Citation(
            kind="document",
            document_id=document_id or self.document_id or "dokumen",
            page=self.page,
            quote=self.source_line,
        )


class VerificationCheck(BaseModel):
    """Result of one deterministic arithmetic identity. Produced only by Pandas."""

    name: str
    description: str
    period: str
    formula: str
    expected: Optional[float] = None
    actual: Optional[float] = None
    difference: Optional[float] = None
    relative_difference: Optional[float] = None
    passed: bool = False
    skipped: bool = False
    skip_reason: Optional[str] = None
    involved_metrics: list[str] = Field(default_factory=list)


class DerivedIndicator(BaseModel):
    """A ratio or trend computed in Python from verified inputs."""

    key: str
    label: str
    period: str
    value: Optional[float] = None
    formula: str
    inputs: dict[str, float] = Field(default_factory=dict)
    interpretation: Optional[str] = None

    def to_citation(self) -> Citation:
        return Citation(kind="computation", formula=self.formula, inputs=self.inputs, result=self.value)


# ---------------------------------------------------------------------------
# Agent 2 outputs
# ---------------------------------------------------------------------------


class NewsItem(BaseModel):
    title: str
    url: str
    snippet: str = ""
    published: Optional[str] = None
    source_tier: int = 3
    source_weight: float = 0.4
    source_label: str = ""
    query: str = ""


class ManagementClaim(BaseModel):
    """A checkable assertion by management, kept with the metric it touches."""

    claim: str
    speaker: Optional[str] = None
    url: str
    published: Optional[str] = None
    source_tier: int = 3
    source_weight: float = 0.4
    claim_type: Literal["kinerja", "prospek", "likuiditas", "ekspansi", "tata_kelola", "lainnya"] = "lainnya"
    checkable_metrics: list[str] = Field(default_factory=list)
    direction: Optional[Literal["naik", "turun", "stabil", "positif", "negatif"]] = None


# ---------------------------------------------------------------------------
# Agent 3 outputs
# ---------------------------------------------------------------------------


class Evidence(BaseModel):
    summary: str
    citation: Citation
    weight: float = 1.0


class Hypothesis(BaseModel):
    """A risk hypothesis and — crucially — the record of its falsification test."""

    id: str
    pattern: str                       # which detection pattern raised it
    statement: str
    rationale: str = ""
    supporting: list[Evidence] = Field(default_factory=list)
    counter_evidence: list[Evidence] = Field(default_factory=list)
    counter_search_queries: list[str] = Field(default_factory=list)
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    confidence: float = 0.0
    verdict_reason: str = ""


class Finding(BaseModel):
    """A hypothesis that survived falsification, phrased as a question."""

    id: str
    title: str
    pattern: str
    severity: Severity
    confidence: float
    narrative: str
    question_for_management: str
    evidence: list[Evidence] = Field(default_factory=list)
    counter_evidence_considered: list[Evidence] = Field(default_factory=list)
    suppressed: bool = False
    suppression_reason: Optional[str] = None


class RiskScore(BaseModel):
    """Composite score. Inverted: higher is safer, matching the dashboard."""

    score: float = 100.0
    band: Literal["Low Risk", "Medium Risk", "High Risk"] = "Low Risk"
    patterns_run: int = 0
    flags_retained: int = 0
    hypotheses_dropped: int = 0
    breakdown: list[dict[str, Any]] = Field(default_factory=list)
    model_version: str = "v1.0"

    #: Share of identity checks that could actually be evaluated. A score
    #: computed over a filing whose numbers mostly failed to extract is not a
    #: clean bill of health, and the report says so rather than implying one.
    data_coverage: float = 0.0
    assessable: bool = True
    coverage_note: str = ""

    @staticmethod
    def band_for(score: float) -> str:
        if score >= 85:
            return "Low Risk"
        if score >= 60:
            return "Medium Risk"
        return "High Risk"


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------


def _merge_dicts(left: dict, right: dict) -> dict:
    """Reducer for dict-valued keys written by parallel branches."""
    merged = dict(left or {})
    merged.update(right or {})
    return merged


class SentinelState(TypedDict, total=False):
    """The single object that flows through the orchestrator graph.

    Keys written by the two parallel branches (Agent 1 / Agent 2) are disjoint;
    the two shared keys (``audit_trail`` and ``errors``) carry ``operator.add``
    reducers so concurrent writes merge instead of colliding.
    """

    # --- Run identity -------------------------------------------------------
    run_id: str
    ticker: str
    company_name: str
    sector: str
    trigger: str                       # "manual" | "scheduled" | "new_filing"
    started_at: str

    # --- Orchestrator planning ---------------------------------------------
    plan: dict[str, Any]

    # --- Ingestion ----------------------------------------------------------
    document_paths: list[str]
    documents: list[dict[str, Any]]    # DocumentIndex.summary() per document

    # --- Agent 1: Financial Auditor ----------------------------------------
    extraction_attempt: int
    extraction_strategy: str
    metrics: list[dict[str, Any]]      # ExtractedMetric dumps
    verifications: list[dict[str, Any]]  # VerificationCheck dumps
    indicators: list[dict[str, Any]]   # DerivedIndicator dumps
    unreliable_metrics: list[str]
    periods: list[str]
    auditor_status: str
    auditor_notes: list[str]

    # --- Agent 2: Market Intelligence --------------------------------------
    news: list[dict[str, Any]]
    management_claims: list[dict[str, Any]]
    market_status: str

    # --- Agent 3: Red Flag Investigator ------------------------------------
    hypotheses: list[dict[str, Any]]
    dropped_hypotheses: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    patterns_run: list[str]

    # --- Agent 4: Synthesizer ----------------------------------------------
    risk_score: dict[str, Any]
    report: dict[str, Any]
    report_paths: dict[str, str]

    # --- Cross-cutting ------------------------------------------------------
    audit_trail: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[str], operator.add]
    token_usage: Annotated[dict[str, Any], _merge_dicts]


def new_state(
    run_id: str,
    ticker: str,
    document_paths: list[str],
    company_name: str = "",
    sector: str = "",
    trigger: str = "manual",
) -> SentinelState:
    from datetime import datetime, timezone

    return SentinelState(
        run_id=run_id,
        ticker=ticker.upper(),
        company_name=company_name or ticker.upper(),
        sector=sector,
        trigger=trigger,
        started_at=datetime.now(timezone.utc).isoformat(),
        document_paths=list(document_paths),
        documents=[],
        extraction_attempt=0,
        extraction_strategy=ExtractionStrategy.TABLE_FIRST.value,
        metrics=[],
        verifications=[],
        indicators=[],
        unreliable_metrics=[],
        periods=[],
        auditor_status="pending",
        auditor_notes=[],
        news=[],
        management_claims=[],
        market_status="pending",
        hypotheses=[],
        dropped_hypotheses=[],
        findings=[],
        patterns_run=[],
        risk_score={},
        report={},
        report_paths={},
        audit_trail=[],
        errors=[],
        token_usage={},
    )
