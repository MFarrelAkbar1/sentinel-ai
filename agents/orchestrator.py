"""Orchestrator — the LangGraph state machine that coordinates the four agents.

    START
      │
      ▼
    plan ──────► ingest ──┬──► financial_auditor ──┐
                          │                        ├──► join ──┐
                          └──► market_intelligence ┘           │
                                                               │ conditional
                        ┌──────────────────────────────────────┤
                        │  verifikasi gagal → audit_retry ─────┘ (loop)
                        │  verifikasi selesai
                        ▼
                   investigate ──► synthesize ──► finalize ──► END

Three properties of this shape are deliberate:

* **Agents 1 and 2 fan out from ``ingest`` in the same superstep**, so document
  extraction and market search genuinely run concurrently, and ``join`` waits
  for both before anything downstream sees the state.
* **The retry loop sits entirely downstream of the join.** A conditional edge
  routes back into ``audit_retry`` while identity checks are still failing.
  Keeping the loop on one branch means a slow re-read can never race the market
  branch into the investigator.
* **Every transition is logged.** The router functions write their decision to
  the audit trail before returning it, so the trail explains not just what each
  agent did but why the graph went where it went.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from core.audit_trail import AuditTrail
from core.config import get_settings
from core.llm import LLMClient, get_llm
from core.report_generator import write_report
from core.state import SentinelState, new_state
from agents.financial_auditor import FinancialAuditor
from agents.market_intelligence import MarketIntelligenceAnalyst
from agents.red_flag_investigator import RedFlagInvestigator
from agents.synthesizer import Synthesizer
from tools.document_parser import load_documents
from tools.retrieval import get_index

log = logging.getLogger("sentinel.orchestrator")

AGENT = "orchestrator"

#: Sector hints that change which detection patterns deserve priority. The
#: orchestrator "decides investigation strategy based on document type and
#: company sector" — this table is that decision, made explicit and auditable.
SECTOR_PRIORITIES: dict[str, list[str]] = {
    "keuangan": ["leverage_solvabilitas", "pihak_berelasi", "opini_auditor"],
    "properti": ["piutang_vs_pendapatan", "kualitas_laba_akrual", "arus_kas_vs_laba"],
    "pertambangan": ["arus_kas_vs_laba", "leverage_solvabilitas", "pihak_berelasi"],
    "teknologi": ["arus_kas_vs_laba", "kualitas_laba_akrual", "klaim_vs_angka"],
    "konsumer": ["piutang_vs_pendapatan", "kualitas_laba_akrual"],
    "infrastruktur": ["leverage_solvabilitas", "arus_kas_vs_laba"],
    "kesehatan": ["piutang_vs_pendapatan", "pihak_berelasi"],
}

_DEFAULT_PRIORITIES = ["arus_kas_vs_laba", "piutang_vs_pendapatan", "opini_auditor"]


class Orchestrator:
    """Builds and runs the four-agent graph for one entity."""

    def __init__(
        self,
        trail: Optional[AuditTrail] = None,
        llm: Optional[LLMClient] = None,
        run_id: Optional[str] = None,
    ):
        self.settings = get_settings()
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.trail = trail or AuditTrail(self.run_id)
        self.llm = llm or get_llm()
        self.auditor = FinancialAuditor(llm=self.llm, trail=self.trail)
        self.market = MarketIntelligenceAnalyst(llm=self.llm, trail=self.trail)
        self.investigator = RedFlagInvestigator(llm=self.llm, trail=self.trail)
        self.synthesizer = Synthesizer(llm=self.llm, trail=self.trail)
        self.graph = self._build()

    # -- graph construction --------------------------------------------------

    def _build(self) -> Any:
        graph = StateGraph(SentinelState)

        graph.add_node("plan", self.plan_node)
        graph.add_node("ingest", self.ingest_node)
        graph.add_node("financial_auditor", self.auditor.run)
        graph.add_node("market_intelligence", self.market.run)
        graph.add_node("join", self.join_node)
        graph.add_node("audit_retry", self.auditor.run)
        graph.add_node("investigate", self.investigator.run)
        graph.add_node("synthesize", self.synthesizer.run)
        graph.add_node("finalize", self.finalize_node)

        graph.add_edge(START, "plan")
        graph.add_edge("plan", "ingest")

        # Fan out: Agent 1 and Agent 2 run concurrently.
        graph.add_edge("ingest", "financial_auditor")
        graph.add_edge("ingest", "market_intelligence")

        # Fan in: both must land before the graph moves on.
        graph.add_edge("financial_auditor", "join")
        graph.add_edge("market_intelligence", "join")

        # Retry loop, entirely on the audit branch.
        graph.add_conditional_edges(
            "join", self.route_after_join,
            {"retry": "audit_retry", "investigate": "investigate"},
        )
        graph.add_conditional_edges(
            "audit_retry", self.route_after_retry,
            {"retry": "audit_retry", "investigate": "investigate"},
        )

        graph.add_edge("investigate", "synthesize")
        graph.add_edge("synthesize", "finalize")
        graph.add_edge("finalize", END)

        return graph.compile()

    # -- nodes ---------------------------------------------------------------

    def plan_node(self, state: dict[str, Any]) -> dict[str, Any]:
        """Decide the investigation strategy before any document is opened."""
        events: list[dict[str, Any]] = []
        ticker = state.get("ticker", "")
        sector = (state.get("sector") or "").strip().lower()

        paths = [Path(p) for p in state.get("document_paths", [])]
        doc_kinds = sorted({_document_kind(p.name) for p in paths}) or ["tidak diketahui"]

        if not sector and self.llm.available and paths:
            sector = self._classify_sector(state, events) or ""

        priorities = _DEFAULT_PRIORITIES
        for key, patterns in SECTOR_PRIORITIES.items():
            if sector and key in sector:
                priorities = patterns
                break

        plan = {
            "document_kinds": doc_kinds,
            "sector": sector or "tidak ditentukan",
            "priority_patterns": priorities,
            "parallel_branches": ["financial_auditor", "market_intelligence"],
            "max_extraction_attempts": self.settings.max_extraction_attempts,
            "confidence_threshold": self.settings.min_confidence,
            "llm_available": self.llm.available,
            "search_available": self.market.search.usable_for(ticker),
        }

        events.append(self.trail.log(
            AGENT, "susun_rencana_investigasi",
            decision=f"prioritas: {', '.join(priorities)}",
            detail=(f"{ticker} · jenis dokumen: {', '.join(doc_kinds)} · sektor: {plan['sector']} · "
                    f"LLM {'aktif' if plan['llm_available'] else 'nonaktif'} · "
                    f"pencarian {'aktif' if plan['search_available'] else 'nonaktif'}"),
            payload=plan,
        ))
        return {"plan": plan, "sector": sector or state.get("sector", ""), "audit_trail": events}

    def _classify_sector(self, state: dict[str, Any], events: list[dict[str, Any]]) -> Optional[str]:
        """Light-model classification — the tiered-model cost mitigation."""
        try:
            documents = load_documents(state.get("document_paths", [])[:1])
        except Exception as exc:
            events.append(self.trail.log(AGENT, "klasifikasi_sektor", tool="document_parser",
                                         decision="gagal", detail=str(exc)))
            return None
        if not documents:
            return None

        sample = "\n".join(page.text[:900] for page in documents[0].pages[:3])
        label = self.llm.classify(
            purpose="orchestrator_sector",
            system=("Klasifikasikan sektor emiten Bursa Efek Indonesia berdasarkan kutipan "
                    "halaman awal laporan tahunan. Jawab satu label saja."),
            user=f"Emiten: {state.get('company_name')} ({state.get('ticker')})\n\n{sample}",
            labels=list(SECTOR_PRIORITIES.keys()) + ["lainnya"],
        )
        events.append(self.trail.log(
            AGENT, "klasifikasi_sektor", tool="llm",
            decision=label or "tidak terklasifikasi",
            detail="model ringan dipakai untuk klasifikasi, bukan model penalaran utama",
        ))
        return label

    def ingest_node(self, state: dict[str, Any]) -> dict[str, Any]:
        """Parse the filings once and build the retrieval index for Agent 3."""
        events: list[dict[str, Any]] = []
        paths = state.get("document_paths", [])

        try:
            documents = load_documents(paths)
        except Exception as exc:
            events.append(self.trail.log(AGENT, "parsing_dokumen", tool="document_parser",
                                         decision="gagal", detail=str(exc)))
            return {"audit_trail": events, "errors": [f"Orchestrator: parsing gagal — {exc}"]}

        summaries = [d.summary() for d in documents]
        events.append(self.trail.log(
            AGENT, "parsing_dokumen", tool="document_parser",
            decision=f"{len(documents)} dokumen, {sum(d.page_count for d in documents)} halaman",
            detail="; ".join(
                f"{d.doc_id}: {d.parser}, satuan {d.default_scale_label}" for d in documents
            ),
            payload={"documents": summaries},
        ))

        try:
            index = get_index(paths, collection_name=f"sentinel_{state.get('ticker', 'run')}", rebuild=True)
            self.investigator.index = index
            events.append(self.trail.log(
                AGENT, "bangun_indeks_retrieval", tool="retrieval",
                decision=f"backend {index.backend}",
                detail=("pencarian semantik aktif" if index.backend == "chroma"
                        else "pencarian leksikal BM25 (indeks vektor tidak tersedia)"),
                payload=index.stats(),
            ))
        except Exception as exc:
            events.append(self.trail.log(AGENT, "bangun_indeks_retrieval", tool="retrieval",
                                         decision="gagal", detail=str(exc)))

        return {"documents": summaries, "audit_trail": events}

    def join_node(self, state: dict[str, Any]) -> dict[str, Any]:
        """Barrier for the two parallel branches."""
        auditor = state.get("auditor_status", "pending")
        market = state.get("market_status", "pending")
        metrics = len(state.get("metrics", []))
        news = len(state.get("news", []))
        return {"audit_trail": [self.trail.log(
            AGENT, "gabungkan_cabang_paralel",
            decision=f"auditor={auditor}, pasar={market}",
            detail=f"{metrics} angka terekstraksi, {news} sumber pasar terkumpul",
        )]}

    def finalize_node(self, state: dict[str, Any]) -> dict[str, Any]:
        """Persist artefacts and close the run."""
        events: list[dict[str, Any]] = []
        report = state.get("report", {}) or {}

        paths: dict[str, str] = {}
        if report:
            # The audit trail is only complete at this point, so the annex is
            # refreshed from the live trail before the file is written.
            report.setdefault("annex", {})["audit_trail"] = self.trail.events
            from core.report_generator import render_markdown

            report["markdown"] = render_markdown(report)
            try:
                paths = write_report(report)
            except Exception as exc:
                events.append(self.trail.log(AGENT, "tulis_laporan", decision="gagal", detail=str(exc)))

        score = state.get("risk_score", {}) or {}
        self.trail.persist_findings(state.get("findings", []))

        events.append(self.trail.log(
            AGENT, "selesaikan_siklus",
            decision=f"skor {score.get('score', '—')}/100 ({score.get('band', '—')})",
            detail=f"laporan: {paths.get('markdown', 'tidak ditulis')}",
            payload={"paths": paths, "stats": report.get("stats", {})},
        ))

        self.trail.close_run(
            status="completed",
            risk_score=score.get("score"),
            risk_band=score.get("band"),
            flags_retained=int(score.get("flags_retained", 0) or 0),
            hypotheses_dropped=int(score.get("hypotheses_dropped", 0) or 0),
            patterns_run=int(score.get("patterns_run", 0) or 0),
            report_path=paths.get("markdown"),
        )
        return {"report": report, "report_paths": paths, "audit_trail": events}

    # -- routers -------------------------------------------------------------

    def route_after_join(self, state: dict[str, Any]) -> str:
        return self._route(state, origin="join")

    def route_after_retry(self, state: dict[str, Any]) -> str:
        return self._route(state, origin="audit_retry")

    def _route(self, state: dict[str, Any], origin: str) -> str:
        status = state.get("auditor_status", "done")
        attempt = int(state.get("extraction_attempt", 0))
        limit = self.settings.max_extraction_attempts

        if status == "retry" and attempt < limit:
            self.trail.log(
                AGENT, "keputusan_alur",
                decision="ulangi ekstraksi",
                detail=(f"dari {origin}: verifikasi aritmetik belum konsisten setelah percobaan "
                        f"{attempt}/{limit}; membaca ulang dengan strategi berikutnya"),
            )
            return "retry"

        if status == "retry":
            self.trail.log(
                AGENT, "keputusan_alur",
                decision="lanjut dengan gap yang diakui",
                detail=(f"batas {limit} percobaan tercapai; metrik yang tetap tidak konsisten "
                        "dilaporkan sebagai tidak dapat diekstraksi dengan andal"),
            )
        else:
            self.trail.log(
                AGENT, "keputusan_alur", decision="lanjut ke investigasi",
                detail=f"dari {origin}: status auditor = {status}",
            )
        return "investigate"

    # -- execution -----------------------------------------------------------

    def run(
        self,
        ticker: str,
        document_paths: list[str],
        company_name: str = "",
        sector: str = "",
        trigger: str = "manual",
    ) -> dict[str, Any]:
        state = new_state(
            run_id=self.run_id, ticker=ticker, document_paths=document_paths,
            company_name=company_name, sector=sector, trigger=trigger,
        )
        self.trail.open_run(ticker=ticker.upper(), company_name=company_name or ticker,
                            sector=sector, trigger=trigger)
        self.trail.log(AGENT, "mulai_siklus",
                       decision=f"{ticker.upper()} · {len(document_paths)} dokumen",
                       detail=f"pemicu: {trigger}",
                       payload={"documents": document_paths})

        started = datetime.now(timezone.utc)
        try:
            # The recursion limit bounds the retry loop as a hard stop even if a
            # router misbehaves; the loop's real bound is max_extraction_attempts.
            final = self.graph.invoke(state, config={"recursion_limit": 40})
        except Exception as exc:
            log.exception("Siklus gagal untuk %s", ticker)
            self.trail.log(AGENT, "siklus_gagal", decision="error", detail=str(exc))
            self.trail.close_run(status="failed")
            raise

        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        final["audit_trail"] = self.trail.events
        final["duration_seconds"] = round(elapsed, 2)
        final["token_usage"] = dict(self.llm.usage)
        return final


def _document_kind(filename: str) -> str:
    lowered = filename.lower()
    if "tahunan" in lowered or "annual" in lowered or "_ar" in lowered:
        return "laporan tahunan"
    if "keuangan" in lowered or "financial" in lowered or "fs" in lowered:
        return "laporan keuangan"
    if "prospektus" in lowered or "prospectus" in lowered:
        return "prospektus"
    if "keterbukaan" in lowered or "disclosure" in lowered:
        return "keterbukaan informasi"
    return "dokumen lain"


def run_analysis(
    ticker: str,
    document_paths: list[str],
    company_name: str = "",
    sector: str = "",
    trigger: str = "manual",
    run_id: Optional[str] = None,
    echo: bool = False,
) -> dict[str, Any]:
    """Convenience entrypoint used by the CLI, the dashboard, and the scheduler."""
    resolved_run_id = run_id or uuid.uuid4().hex[:12]
    trail = AuditTrail(resolved_run_id, echo=echo)
    orchestrator = Orchestrator(trail=trail, run_id=resolved_run_id)
    return orchestrator.run(
        ticker=ticker, document_paths=document_paths,
        company_name=company_name, sector=sector, trigger=trigger,
    )
