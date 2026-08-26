"""Integration tests for the agent pipeline and the orchestrator graph.

These exercise the paths that unit tests over the verification layer cannot
reach: the model-assisted extraction contract, the verification-driven retry
loop and its conditional edge, and the falsification step's three verdicts.

The LLM is stubbed rather than called. That is not only about avoiding network
in CI — it is how the central architectural claim gets tested. The stub returns
*pointers* (candidate id + column index) and never a value, and the assertions
confirm the resulting figures came from the document. A stub that tried to
return a number would have nowhere to put it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pytest

from core.state import ExtractionStatus, new_state
from tools.demo_corpus import DEMO_COMPANY, DEMO_SECTOR, DEMO_TICKER, ensure_demo_corpus
from tools.document_parser import clear_document_cache
from tools.retrieval import clear_index_cache


@pytest.fixture(scope="module")
def demo_pdf() -> str:
    created = ensure_demo_corpus()
    return created["pdf"]


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Each test gets its own SQLite file and report directory."""
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setenv("SENTINEL_REPORT_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("SENTINEL_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("SENTINEL_OFFLINE", "1")
    from core import config

    config.get_settings(refresh=True)
    clear_document_cache()
    clear_index_cache()
    yield
    config.get_settings(refresh=True)


# ---------------------------------------------------------------------------
# LLM stub
# ---------------------------------------------------------------------------


@dataclass
class StubResult:
    data: dict[str, Any]
    ok: bool = True
    error: Optional[str] = None
    model: str = "stub"
    raw_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class StubLLM:
    """Stands in for ``LLMClient``. Responds by ``purpose``."""

    responses: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    available: bool = True
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "calls": 0})

    def structured(self, *, purpose: str, system: str, user: str, schema: dict, **kwargs) -> StubResult:
        self.calls.append((purpose, user))
        self.usage["calls"] += 1
        handler = self.responses.get(purpose)
        if handler is None:
            return StubResult(data={}, ok=False, error="no stub configured")
        payload = handler(user) if callable(handler) else handler
        return StubResult(data=payload, ok=bool(payload))

    def classify(self, *, purpose: str, system: str, user: str, labels: list[str]) -> Optional[str]:
        self.calls.append((purpose, user))
        return self.responses.get(purpose)


def _pointer_extraction(user: str) -> dict[str, Any]:
    """Parse the candidate listing back out and point at the trailing columns.

    Mirrors what a well-behaved model returns: a candidate id and, for each
    period, which numeric column on that line holds it — no values.
    """
    import re

    periods = re.search(r"Periode pelaporan[^:]*:\s*(.+)", user)
    period_list = [p.strip() for p in periods.group(1).split(",")] if periods else []

    assignments = []
    for line in user.splitlines():
        match = re.match(r"\[(\d+)\] hal\.\d+ \((?:text|table)\) \| (.+?) \| angka terbaca: \[(.*)\]", line)
        if not match:
            continue
        index = int(match.group(1))
        count = len([n for n in match.group(3).split(",") if n.strip()])
        columns = []
        offset = max(count - len(period_list), 0)
        for position, period in enumerate(period_list):
            if offset + position < count:
                columns.append({"period": period, "number_index": offset + position})
        # The metric key is recovered from the vocabulary block above the listing.
        assignments.append({"candidate_id": index, "columns": columns})

    # Resolve each candidate's metric via the deterministic caption matcher —
    # the stub is standing in for the model's judgement, not for the parser.
    from tools.document_parser import caption_of, match_metric

    resolved = []
    for assignment in assignments:
        for line in user.splitlines():
            if line.startswith(f"[{assignment['candidate_id']}] "):
                body = line.split(" | ")[1]
                matched = match_metric(caption_of(body))
                if matched:
                    resolved.append({**assignment, "metric_key": matched[0], "confidence": 0.9})
                break
    return {"assignments": resolved, "not_found": []}


# ---------------------------------------------------------------------------
# Agent 1 — model-assisted extraction
# ---------------------------------------------------------------------------


def test_model_assisted_extraction_reads_values_from_the_document(demo_pdf):
    from agents.financial_auditor import FinancialAuditor

    llm = StubLLM(responses={"agent1_extraction": _pointer_extraction})
    state = new_state("stub1", DEMO_TICKER, [demo_pdf], DEMO_COMPANY)
    result = FinancialAuditor(llm=llm).run(dict(state))

    assert any(purpose == "agent1_extraction" for purpose, _ in llm.calls)
    assert result["auditor_status"] == "done"

    by_key = {(m["key"], m["period"]): m for m in result["metrics"]}
    assert by_key[("total_aset", "2025")]["value"] == 6_400_000
    assert by_key[("arus_kas_operasi", "2025")]["value"] == -145_000
    # Every figure carries the page and the verbatim line it was read from.
    for metric in result["metrics"]:
        assert metric["page"] is not None
        assert metric["source_line"]
        assert metric["document_id"]


def test_extraction_falls_back_to_heuristics_when_the_model_fails(demo_pdf):
    from agents.financial_auditor import FinancialAuditor

    llm = StubLLM(responses={})           # every call returns ok=False
    state = new_state("stub2", DEMO_TICKER, [demo_pdf], DEMO_COMPANY)
    result = FinancialAuditor(llm=llm).run(dict(state))

    actions = [event["action"] for event in result["audit_trail"]]
    assert "fallback_deterministik" in actions
    assert result["auditor_status"] == "done"
    checks = [c for c in result["verifications"] if not c["skipped"]]
    assert checks and all(c["passed"] for c in checks)


def test_a_model_pointing_at_a_nonexistent_column_yields_no_metric(demo_pdf):
    """Out-of-range pointers are dropped, never coerced into a value."""
    from agents.financial_auditor import FinancialAuditor

    llm = StubLLM(responses={
        "agent1_extraction": {
            "assignments": [
                {"metric_key": "total_aset", "candidate_id": 0,
                 "columns": [{"period": "2025", "number_index": 99}]},
                {"metric_key": "tidak_ada_metrik_ini", "candidate_id": 0,
                 "columns": [{"period": "2025", "number_index": 0}]},
                {"metric_key": "total_aset", "candidate_id": 9999,
                 "columns": [{"period": "2025", "number_index": 0}]},
            ],
            "not_found": [],
        },
    })
    state = new_state("stub3", DEMO_TICKER, [demo_pdf], DEMO_COMPANY)
    result = FinancialAuditor(llm=llm).run(dict(state))
    assert result["metrics"] == []


# ---------------------------------------------------------------------------
# Retry loop
# ---------------------------------------------------------------------------


def test_verification_failure_escalates_the_extraction_strategy(demo_pdf):
    """A broken read must change strategy rather than repeat the same one."""
    from agents.financial_auditor import FinancialAuditor
    from core.state import ExtractionStrategy

    auditor = FinancialAuditor(llm=StubLLM(available=False))
    state = dict(new_state("retry1", DEMO_TICKER, [demo_pdf], DEMO_COMPANY))

    first = auditor.run(state)
    assert first["extraction_strategy"] == ExtractionStrategy.TABLE_FIRST.value

    # Force a second round by presenting a failed identity from round one.
    state.update(first)
    state["verifications"] = [{
        "name": "persamaan_neraca", "period": "2025", "passed": False, "skipped": False,
        "involved_metrics": ["total_aset", "total_liabilitas", "total_ekuitas"],
    }]
    second = auditor.run(state)
    assert second["extraction_attempt"] == 2
    assert second["extraction_strategy"] == ExtractionStrategy.TEXT_LAYOUT.value
    actions = [e["action"] for e in second["audit_trail"]]
    assert "penargetan_ulang" in actions


def test_an_inconsistent_filing_drives_the_loop_and_ends_in_declared_uncertainty(tmp_path, monkeypatch):
    """A figure that never reconciles is reported as unextractable, not guessed.

    The whole point of the retry loop is what it does when it *fails*: after the
    strategies are exhausted, the rows that still break an identity are labelled
    ``tidak dapat diekstraksi dengan andal`` and no value is invented for them.
    """
    from agents.orchestrator import run_analysis
    from core import config
    from tools.demo_corpus import build_demo_pdf

    broken = build_demo_pdf(tmp_path / "ABCI_TIDAK_KONSISTEN.pdf", overwrite=True, corrupt_balance=True)
    monkeypatch.setenv("SENTINEL_MAX_EXTRACTION_ATTEMPTS", "2")
    config.get_settings(refresh=True)
    clear_document_cache()

    result = run_analysis(
        ticker="ABCI", document_paths=[str(broken)],
        company_name=DEMO_COMPANY, trigger="test",
    )

    # The loop actually ran a second time with a different strategy.
    attempts = [e for e in result["audit_trail"] if e["action"] == "mulai_ekstraksi"]
    assert len(attempts) >= 2
    strategies = {e["payload"]["strategy"] for e in attempts}
    assert len(strategies) >= 2, "pembacaan ulang harus memakai strategi berbeda"
    assert any(e["action"] == "putuskan_baca_ulang" for e in result["audit_trail"])

    # The break was isolated to the equity row, and it is declared, not guessed.
    unreliable = set(result["unreliable_metrics"])
    assert "total_ekuitas" in unreliable
    assert "pendapatan" not in unreliable, "baris yang sehat tidak boleh ikut ditandai"

    by_key = {(m["key"], m["period"]): m for m in result["metrics"]}
    equity = by_key[("total_ekuitas", "2025")]
    assert equity["status"] == ExtractionStatus.UNRELIABLE.value
    assert "tidak dapat diekstraksi dengan andal" in (equity["note"] or "")

    # The uncertainty reaches the report rather than stopping at the state.
    markdown = Path(result["report_paths"]["markdown"]).read_text(encoding="utf-8")
    assert "Tidak dapat diekstraksi dengan andal" in markdown
    assert "total_ekuitas" in markdown


def test_a_corrupt_document_fails_loudly_rather_than_reporting_nothing_found(tmp_path):
    from agents.financial_auditor import FinancialAuditor

    corrupt = tmp_path / "BROKEN_Laporan.pdf"
    corrupt.write_bytes(b"%PDF-1.4 not really a pdf")

    auditor = FinancialAuditor(llm=StubLLM(available=False))
    state = dict(new_state("retry3", "BRKN", [str(corrupt)], "PT Rusak"))
    with pytest.raises(Exception):
        # Both parsers fail; that must surface as an error, never as an
        # empty-but-confident "no risks found" report.
        auditor.run(state)


def test_orchestrator_routes_back_into_the_audit_branch_while_verification_fails(demo_pdf):
    from agents.orchestrator import Orchestrator

    orchestrator = Orchestrator(run_id="route1")
    retry = orchestrator._route({"auditor_status": "retry", "extraction_attempt": 1}, origin="join")
    assert retry == "retry"

    limit = orchestrator.settings.max_extraction_attempts
    exhausted = orchestrator._route(
        {"auditor_status": "retry", "extraction_attempt": limit}, origin="audit_retry"
    )
    assert exhausted == "investigate"

    done = orchestrator._route({"auditor_status": "done", "extraction_attempt": 1}, origin="join")
    assert done == "investigate"


# ---------------------------------------------------------------------------
# Agent 3 — falsification
# ---------------------------------------------------------------------------


def _dossier_state(demo_pdf: str) -> dict[str, Any]:
    from agents.financial_auditor import FinancialAuditor

    state = dict(new_state("agent3", DEMO_TICKER, [demo_pdf], DEMO_COMPANY, DEMO_SECTOR))
    state.update(FinancialAuditor(llm=StubLLM(available=False)).run(state))
    return state


def test_counter_evidence_search_excludes_the_pages_that_raised_the_hypothesis(demo_pdf):
    from agents.red_flag_investigator import RedFlagInvestigator

    state = _dossier_state(demo_pdf)
    result = RedFlagInvestigator(llm=StubLLM(available=False)).run(state)

    searches = [e for e in result["audit_trail"] if e["action"] == "cari_bukti_penyangkal"]
    assert searches, "falsifikasi harus dijalankan untuk setiap hipotesis"
    for event in searches:
        excluded = set(event["payload"]["excluded_pages"])
        found = set(event["payload"]["pages_found"])
        assert not (excluded & found), "bukti penyangkal tidak boleh berasal dari halaman asal hipotesis"


def test_a_refuting_verdict_drops_the_hypothesis(demo_pdf):
    from agents.red_flag_investigator import RedFlagInvestigator

    state = _dossier_state(demo_pdf)
    llm = StubLLM(responses={
        "agent3_falsification": {
            "verdict": "digugurkan",
            "reason": "Catatan 5 menjelaskan kenaikan piutang berasal dari kontrak kuartal keempat.",
            "confidence_adjustment": 0.0,
        },
    })
    result = RedFlagInvestigator(llm=llm).run(state)

    assert result["findings"] == []
    assert result["dropped_hypotheses"], "hipotesis yang terbantah harus tercatat sebagai digugurkan"
    for hypothesis in result["dropped_hypotheses"]:
        assert hypothesis["status"] == "digugurkan"
        assert hypothesis["confidence"] == 0.0
        assert hypothesis["verdict_reason"]


def test_a_surviving_verdict_promotes_the_hypothesis_to_a_finding(demo_pdf):
    from agents.red_flag_investigator import RedFlagInvestigator

    state = _dossier_state(demo_pdf)
    llm = StubLLM(responses={
        "agent3_falsification": {
            "verdict": "bertahan",
            "reason": "Tidak ada bagian dokumen yang menjelaskan anomali ini.",
            "confidence_adjustment": 1.0,
        },
    })
    result = RedFlagInvestigator(llm=llm).run(state)

    assert result["findings"], "hipotesis yang bertahan harus menjadi temuan"
    assert not result["dropped_hypotheses"]
    for finding in result["findings"]:
        assert finding["evidence"], "setiap temuan wajib membawa bukti bersitasi"
        for evidence in finding["evidence"]:
            assert evidence["citation"]["kind"] in {"document", "computation", "web"}


def test_patterns_do_not_fire_on_metrics_flagged_unreliable(demo_pdf):
    from agents.red_flag_investigator import RedFlagInvestigator

    state = _dossier_state(demo_pdf)
    state["unreliable_metrics"] = ["arus_kas_operasi", "laba_bersih"]
    result = RedFlagInvestigator(llm=StubLLM(available=False)).run(state)

    fired = {h["pattern"] for h in result["hypotheses"]}
    assert "arus_kas_vs_laba" not in fired
    actions = [e["action"] for e in result["audit_trail"]]
    assert "batasi_cakupan" in actions


# ---------------------------------------------------------------------------
# Agent 4 and the full graph
# ---------------------------------------------------------------------------


def test_low_confidence_findings_are_suppressed_with_a_stated_reason(demo_pdf):
    from agents.synthesizer import Synthesizer

    state = {
        "run_id": "syn1", "ticker": "TEST", "company_name": "PT Uji", "periods": ["2025"],
        "metrics": [], "verifications": [], "indicators": [], "documents": [],
        "patterns_run": ["arus_kas_vs_laba"], "dropped_hypotheses": [], "audit_trail": [],
        "findings": [
            {"id": "H1", "title": "Sinyal kuat", "pattern": "arus_kas_vs_laba",
             "severity": "tinggi", "confidence": 0.80, "narrative": "n", "question_for_management": "q"},
            {"id": "H2", "title": "Sinyal lemah", "pattern": "pihak_berelasi",
             "severity": "rendah", "confidence": 0.20, "narrative": "n", "question_for_management": "q"},
        ],
    }
    result = Synthesizer(llm=StubLLM(available=False)).run(state)

    by_id = {f["id"]: f for f in result["findings"]}
    assert by_id["H1"]["suppressed"] is False
    assert by_id["H2"]["suppressed"] is True
    assert "di bawah ambang" in by_id["H2"]["suppression_reason"]
    # Suppressed findings stay in the annex rather than disappearing.
    assert any(f["id"] == "H2" for f in result["report"]["suppressed_findings"])
    assert result["risk_score"]["flags_retained"] == 1


def test_low_verification_coverage_is_declared_not_scored_as_safe():
    from agents.synthesizer import Synthesizer

    state = {
        "run_id": "syn2", "ticker": "TEST", "company_name": "PT Uji", "periods": ["2025"],
        "metrics": [], "indicators": [], "documents": [], "patterns_run": [],
        "dropped_hypotheses": [], "findings": [], "audit_trail": [],
        "verifications": [{"name": "x", "period": "2025", "skipped": True, "passed": False,
                           "formula": "a = b", "skip_reason": "metrik belum terbaca"}] * 8,
        "unreliable_metrics": ["total_aset"],
    }
    result = Synthesizer(llm=StubLLM(available=False)).run(state)
    score = result["risk_score"]

    assert score["score"] == 100.0            # nothing fired…
    assert score["assessable"] is False       # …but nothing could be checked either
    assert "tidak dapat diperlakukan sebagai penilaian menyeluruh" in score["coverage_note"]
    assert "Cakupan data tidak memadai" in result["report"]["markdown"]


def test_full_graph_produces_a_cited_report_and_a_complete_audit_trail(demo_pdf):
    from agents.orchestrator import run_analysis

    result = run_analysis(
        ticker=DEMO_TICKER, document_paths=[demo_pdf],
        company_name=DEMO_COMPANY, sector=DEMO_SECTOR, trigger="test",
    )

    # Every agent ran, and the graph's own decisions are in the trail.
    agents_seen = {event["agent"] for event in result["audit_trail"]}
    assert agents_seen >= {
        "orchestrator", "financial_auditor", "market_intelligence",
        "red_flag_investigator", "synthesizer",
    }
    actions = {event["action"] for event in result["audit_trail"]}
    assert {"susun_rencana_investigasi", "gabungkan_cabang_paralel", "keputusan_alur"} <= actions

    # The report exists on disk and carries page-level citations.
    markdown_path = Path(result["report_paths"]["markdown"])
    assert markdown_path.exists()
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Lampiran Teknis" in markdown
    assert "hal." in markdown
    assert "bukan penasihat investasi" in markdown

    # No surfaced claim rests on an unsourced number.
    for finding in result["report"]["findings"]:
        assert finding["evidence"]
        for evidence in finding["evidence"]:
            citation = evidence["citation"]
            if citation["kind"] == "document":
                assert citation["page"] and citation["quote"]
            elif citation["kind"] == "computation":
                assert citation["formula"]


def test_run_is_persisted_and_queryable(demo_pdf):
    from agents.orchestrator import run_analysis
    from core.audit_trail import get_events, list_runs

    result = run_analysis(
        ticker=DEMO_TICKER, document_paths=[demo_pdf],
        company_name=DEMO_COMPANY, trigger="test",
    )
    runs = list_runs(ticker=DEMO_TICKER, limit=5)
    assert runs and runs[0]["status"] == "completed"
    assert runs[0]["risk_score"] is not None
    assert get_events(result["run_id"])
