"""Agent 1 — Financial Auditor.

Extracts the primary financial statements from a filing and refuses to hand
downstream agents a number it could not verify.

The architectural rule this agent exists to enforce: **the model chooses which
line on which page holds a metric; Python reads the digits off that line.** The
extraction tool schema makes it impossible to express it any other way — the
model returns a *pointer* (candidate id + which numeric column), never a value.
A model that never types a number cannot hallucinate one.

Verification drives re-extraction:

    extract(strategy) → verify → identity break?
        yes → escalate strategy, re-extract only the implicated rows → verify
        no  → mark metrics verified

After ``max_extraction_attempts`` the remaining broken figures are labelled
``tidak_dapat_diekstraksi_dengan_andal`` and passed on as explicit gaps. That
label travels all the way into the report; it is never quietly dropped.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from core.audit_trail import AuditTrail
from core.config import get_settings
from core.formatting import compact_idr, pct
from core.llm import LLMClient, get_llm
from core.state import (
    BALANCE_SHEET_METRICS,
    CASH_FLOW_METRICS,
    DEDUCTION_METRICS,
    INCOME_STATEMENT_METRICS,
    METRIC_VOCABULARY,
    ExtractedMetric,
    ExtractionStatus,
    ExtractionStrategy,
    StatementType,
)
from tools.document_parser import (
    CandidateLine,
    ParsedDocument,
    ParsedPage,
    extract_numbers,
    find_candidate_lines,
    load_documents,
)
from tools.python_sandbox import VerificationEngine

log = logging.getLogger("sentinel.agent1")

AGENT = "financial_auditor"

#: Which metric groups live in which statement, and how many pages to consider.
_STATEMENT_PLAN: tuple[tuple[StatementType, list[str], int], ...] = (
    (StatementType.BALANCE_SHEET, BALANCE_SHEET_METRICS, 4),
    (StatementType.INCOME_STATEMENT, INCOME_STATEMENT_METRICS, 4),
    (StatementType.CASH_FLOW, CASH_FLOW_METRICS, 4),
)

#: Escalation order. Each failed verification round moves one step down.
_STRATEGY_ORDER: tuple[ExtractionStrategy, ...] = (
    ExtractionStrategy.TABLE_FIRST,
    ExtractionStrategy.TEXT_LAYOUT,
    ExtractionStrategy.LINE_ANCHORED,
    ExtractionStrategy.PDFPLUMBER_FALLBACK,
)

_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "description": "Satu entri per metrik yang berhasil dipetakan ke satu baris kandidat.",
            "items": {
                "type": "object",
                "properties": {
                    "metric_key": {"type": "string"},
                    "candidate_id": {
                        "type": "integer",
                        "description": "Nomor baris kandidat yang memuat metrik ini.",
                    },
                    "columns": {
                        "type": "array",
                        "description": (
                            "Pemetaan kolom angka pada baris tersebut ke periode. "
                            "number_index dihitung dari 0 pada angka paling kiri."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "period": {"type": "string"},
                                "number_index": {"type": "integer"},
                            },
                            "required": ["period", "number_index"],
                        },
                    },
                    "confidence": {"type": "number"},
                },
                "required": ["metric_key", "candidate_id", "columns"],
            },
        },
        "not_found": {
            "type": "array",
            "description": "Metrik yang tidak ditemukan pada halaman yang diberikan.",
            "items": {"type": "string"},
        },
        "notes": {"type": "string"},
    },
    "required": ["assignments", "not_found"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = """Anda adalah auditor laporan keuangan untuk emiten Bursa Efek Indonesia.

Tugas Anda HANYA memilih baris dan kolom, bukan menyebutkan angka.

Aturan mutlak:
1. Anda TIDAK BOLEH menuliskan nilai angka apa pun. Anda hanya menunjuk
   candidate_id (baris) dan number_index (kolom angka ke berapa pada baris itu).
   Sistem yang membaca digitnya, bukan Anda.
2. Jika sebuah metrik tidak ada pada halaman yang diberikan, masukkan ke
   not_found. Jangan menebak dan jangan memakai baris yang mirip.
3. Baris total ditandai kata "Jumlah"/"Total". Jangan memilih baris rincian
   ketika yang diminta adalah total.
4. Angka pertama pada baris laporan keuangan sering merupakan referensi
   Catatan (mis. "2c,4"), bukan nilai. Perhatikan urutan kolom periode pada
   judul halaman.
5. Nilai dalam kurung berarti negatif. Anda tidak perlu mengoreksinya;
   sistem menanganinya.
"""


class FinancialAuditor:
    """Agent 1. Deterministic where it can be, model-assisted where it must be."""

    def __init__(self, llm: Optional[LLMClient] = None, trail: Optional[AuditTrail] = None):
        self.llm = llm or get_llm()
        self.trail = trail
        self.engine = VerificationEngine()
        self.settings = get_settings()

    # -- LangGraph node ------------------------------------------------------

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        attempt = int(state.get("extraction_attempt", 0)) + 1
        strategy = _STRATEGY_ORDER[min(attempt - 1, len(_STRATEGY_ORDER) - 1)]
        events: list[dict[str, Any]] = []

        self._log(events, "mulai_ekstraksi",
                  decision=f"percobaan {attempt}/{self.settings.max_extraction_attempts}",
                  detail=f"strategi={strategy.value}",
                  payload={"attempt": attempt, "strategy": strategy.value})

        documents = load_documents(
            state.get("document_paths", []),
            force_parser="pdfplumber" if strategy is ExtractionStrategy.PDFPLUMBER_FALLBACK else None,
        )
        if not documents:
            self._log(events, "ekstraksi_dibatalkan", decision="tidak ada dokumen",
                      detail="document_paths kosong")
            return {
                "extraction_attempt": attempt, "extraction_strategy": strategy.value,
                "auditor_status": "failed", "audit_trail": events,
                "errors": ["Agent 1: tidak ada dokumen untuk dianalisis."],
            }

        periods = self._resolve_periods(documents)
        self._log(events, "identifikasi_periode", tool="document_parser",
                  decision=", ".join(periods) or "tidak terdeteksi",
                  payload={"periods": periods})

        # Retry rounds re-read only the rows implicated by a failed identity.
        previous = [ExtractedMetric(**m) for m in state.get("metrics", [])]
        focus = self._focus_keys(state) if attempt > 1 else None
        if focus:
            self._log(events, "penargetan_ulang", decision=f"{len(focus)} metrik",
                      detail="membaca ulang hanya baris yang memicu ketidakcocokan",
                      payload={"focus": sorted(focus)})

        extracted = self._extract(documents, periods, strategy, events, focus=focus)
        metrics = _merge_metrics(previous, extracted) if attempt > 1 else extracted

        # --- deterministic verification ------------------------------------
        frame = self.engine.to_frame(metrics)
        checks = self.engine.check_identities(frame)
        summary = self.engine.summarise(checks)
        implicated = self.engine.failed_metrics(checks)

        self._log(events, "verifikasi_aritmetik", tool="python_sandbox",
                  decision=f"{summary['passed']}/{summary['evaluated']} identitas cocok",
                  detail="; ".join(
                      f"{c.name} {c.period}: selisih {compact_idr(c.difference)} "
                      f"({pct(c.relative_difference or 0, 2)})"
                      for c in checks if not c.skipped and not c.passed
                  ) or "tidak ada ketidakcocokan",
                  payload={"summary": summary, "checks": [c.model_dump() for c in checks]})

        exhausted = attempt >= self.settings.max_extraction_attempts
        needs_retry = bool(implicated) and not exhausted

        if needs_retry:
            self._log(events, "putuskan_baca_ulang", decision="ulangi dengan strategi berbeda",
                      detail=f"metrik bermasalah: {sorted({k for v in implicated.values() for k in v})}",
                      payload={"implicated": {k: sorted(v) for k, v in implicated.items()}})
            status = "retry"
        else:
            status = "done"

        metrics = self._apply_status(metrics, checks, implicated, exhausted=exhausted)
        unreliable = sorted({m.key for m in metrics if m.status is ExtractionStatus.UNRELIABLE})
        if unreliable and not needs_retry:
            self._log(events, "tandai_tidak_andal", decision=f"{len(unreliable)} metrik",
                      detail=("Dilaporkan sebagai tidak dapat diekstraksi dengan andal: "
                              + ", ".join(unreliable)),
                      payload={"unreliable": unreliable})

        indicators = self.engine.derive_indicators(frame) if status == "done" else []
        trends = self.engine.derive_trends(frame) if status == "done" else []
        if status == "done":
            self._log(events, "hitung_indikator", tool="python_sandbox",
                      decision=f"{len(indicators)} rasio, {len(trends)} tren",
                      detail="seluruhnya dihitung dari angka tersitasi",
                      payload={"indicator_keys": sorted({i.key for i in indicators})})

        if self.trail and status == "done":
            self.trail.persist_metrics([m.model_dump(mode="json") for m in metrics])

        return {
            "extraction_attempt": attempt,
            "extraction_strategy": strategy.value,
            "documents": [d.summary() for d in documents],
            "periods": periods,
            "metrics": [m.model_dump(mode="json") for m in metrics],
            "verifications": [c.model_dump(mode="json") for c in checks],
            "indicators": [i.model_dump(mode="json") for i in (indicators + trends)],
            "unreliable_metrics": unreliable,
            "auditor_status": status,
            "auditor_notes": [
                f"Strategi ekstraksi: {strategy.value}",
                f"Verifikasi: {summary['passed']}/{summary['evaluated']} identitas cocok "
                f"({summary['skipped']} dilewati karena metrik belum terbaca).",
            ],
            "audit_trail": events,
        }

    # -- extraction ----------------------------------------------------------

    def _extract(
        self,
        documents: Iterable[ParsedDocument],
        periods: list[str],
        strategy: ExtractionStrategy,
        events: list[dict[str, Any]],
        focus: Optional[set[str]] = None,
    ) -> list[ExtractedMetric]:
        results: list[ExtractedMetric] = []

        for document in documents:
            for statement_type, metric_keys, page_limit in _STATEMENT_PLAN:
                keys = [k for k in metric_keys if not focus or k in focus]
                if not keys:
                    continue

                pages = self._pages_for(document, statement_type, page_limit, strategy)
                if not pages:
                    self._log(events, "halaman_tidak_ditemukan", tool="document_parser",
                              decision=statement_type.value,
                              detail=f"{document.doc_id}: tidak ada halaman terklasifikasi")
                    continue

                candidates = find_candidate_lines(
                    document, keys, pages=pages,
                    layout=strategy is not ExtractionStrategy.TABLE_FIRST,
                )
                if strategy is ExtractionStrategy.TABLE_FIRST:
                    table_first = [c for c in candidates if c.source == "table"]
                    candidates = table_first + [c for c in candidates if c.source != "table"]

                self._log(events, "pindai_halaman", tool="document_parser",
                          decision=f"{statement_type.value}: hal. {[p.number for p in pages]}",
                          detail=f"{len(candidates)} baris kandidat untuk {len(keys)} metrik",
                          payload={"pages": [p.number for p in pages], "candidates": len(candidates)})

                if not candidates:
                    continue

                assigned = self._assign(
                    document, statement_type, keys, candidates, periods, strategy, events
                )
                results.extend(assigned)

        return results

    def _pages_for(
        self,
        document: ParsedDocument,
        statement_type: StatementType,
        limit: int,
        strategy: ExtractionStrategy,
    ) -> list[ParsedPage]:
        pages = document.pages_of(statement_type, limit=limit)
        if strategy in (ExtractionStrategy.LINE_ANCHORED, ExtractionStrategy.PDFPLUMBER_FALLBACK) and pages:
            # Statements spill across pages; a widened window is exactly the
            # "different extraction strategy" the report calls for on retry.
            widened: dict[int, ParsedPage] = {p.number: p for p in pages}
            for page in list(pages):
                for neighbour in document.window(page.number, radius=1):
                    widened.setdefault(neighbour.number, neighbour)
            pages = sorted(widened.values(), key=lambda p: p.number)
        return pages

    def _assign(
        self,
        document: ParsedDocument,
        statement_type: StatementType,
        keys: list[str],
        candidates: list[CandidateLine],
        periods: list[str],
        strategy: ExtractionStrategy,
        events: list[dict[str, Any]],
    ) -> list[ExtractedMetric]:
        """Map candidate lines to (metric, period) cells.

        The model picks pointers; ``_materialise`` reads the digits. When no
        model is available the deterministic ranking below does the picking —
        the arithmetic verification that follows is identical either way.
        """
        if self.llm.available:
            pointers = self._assign_with_model(statement_type, keys, candidates, periods, events)
            if pointers is not None:
                return self._materialise(document, candidates, pointers, periods, strategy)
            self._log(events, "fallback_deterministik", tool="llm",
                      decision="pemetaan heuristik",
                      detail="model tidak mengembalikan pemetaan valid; memakai peringkat kecocokan kaption")

        return self._materialise(
            document, candidates, self._assign_deterministically(keys, candidates, periods), periods, strategy
        )

    def _assign_with_model(
        self,
        statement_type: StatementType,
        keys: list[str],
        candidates: list[CandidateLine],
        periods: list[str],
        events: list[dict[str, Any]],
    ) -> Optional[list[dict[str, Any]]]:
        listing = "\n".join(
            f"[{i}] hal.{c.page} ({c.source}) | {c.text[:220]} | angka terbaca: {c.numbers}"
            for i, c in enumerate(candidates[:120])
        )
        vocabulary = "\n".join(
            f"- {key}: kaption umum = {', '.join(METRIC_VOCABULARY.get(key, [])[:3])}" for key in keys
        )
        user = (
            f"Bagian laporan: {statement_type.value}\n"
            f"Periode pelaporan (urutan kolom biasanya mengikuti urutan ini): {', '.join(periods)}\n\n"
            f"Metrik yang dicari:\n{vocabulary}\n\n"
            f"Baris kandidat:\n{listing}\n\n"
            "Untuk setiap metrik, tunjuk satu candidate_id dan petakan number_index "
            "ke periode. Jangan menuliskan nilai angka."
        )

        result = self.llm.structured(
            purpose="agent1_extraction",
            system=_SYSTEM_PROMPT,
            user=user,
            schema=_EXTRACTION_SCHEMA,
            tool_name="submit_extraction",
            tool_description="Kirim pemetaan baris-kandidat ke metrik dan periode.",
            max_tokens=6000,
        )
        if not result.ok:
            self._log(events, "panggil_model", tool="llm", decision="gagal",
                      detail=result.error or "tidak ada respons",
                      payload={"model": result.model})
            return None

        assignments = result.data.get("assignments") or []
        self._log(events, "panggil_model", tool="llm",
                  decision=f"{len(assignments)} pemetaan",
                  detail=f"tidak ditemukan: {result.data.get('not_found') or '—'}",
                  payload={
                      "model": result.model,
                      "not_found": result.data.get("not_found"),
                      "tokens": {"in": result.input_tokens, "out": result.output_tokens},
                  })
        return assignments

    @staticmethod
    def _assign_deterministically(
        keys: list[str], candidates: list[CandidateLine], periods: list[str]
    ) -> list[dict[str, Any]]:
        """Highest-scoring caption match per metric; rightmost columns as values.

        Statement rows read ``caption [note] value_current value_prior``, so the
        trailing N numbers are the period columns and anything before them is a
        note reference.
        """
        pointers: list[dict[str, Any]] = []
        for key in keys:
            best = next((c for c in candidates if c.metric_key == key), None)
            if best is None:
                continue
            index = candidates.index(best)
            count = min(len(periods), len(best.numbers)) or 1
            offset = max(len(best.numbers) - count, 0)
            columns = [
                {"period": periods[i], "number_index": offset + i}
                for i in range(count) if i < len(periods)
            ]
            pointers.append({"metric_key": key, "candidate_id": index, "columns": columns,
                             "confidence": best.match_score})
        return pointers

    def _materialise(
        self,
        document: ParsedDocument,
        candidates: list[CandidateLine],
        pointers: list[dict[str, Any]],
        periods: list[str],
        strategy: ExtractionStrategy,
    ) -> list[ExtractedMetric]:
        """Turn pointers into metrics by reading digits off the cited line.

        This is the only place a value is created, and it always comes from
        re-parsing the source line — never from the model's output.
        """
        metrics: list[ExtractedMetric] = []
        for pointer in pointers:
            key = pointer.get("metric_key")
            candidate_id = pointer.get("candidate_id")
            if key not in METRIC_VOCABULARY or not isinstance(candidate_id, int):
                continue
            if not (0 <= candidate_id < len(candidates)):
                continue
            candidate = candidates[candidate_id]
            # Re-parse rather than trusting the cached list: the source line is
            # what gets cited, so the digits must come from that exact string.
            numbers = extract_numbers(candidate.text) or candidate.numbers
            page = document.page(candidate.page)
            scale = page.scale if page else candidate.scale

            for column in pointer.get("columns") or []:
                period = str(column.get("period", "")).strip()
                index = column.get("number_index")
                if not period or not isinstance(index, int) or not (0 <= index < len(numbers)):
                    continue
                if periods and period not in periods:
                    continue

                raw = numbers[index]
                value, note = raw, None
                if key in DEDUCTION_METRICS and raw < 0:
                    # Parentheses on an expense line mark the subtraction, not a
                    # negative expense. Record the magnitude; the identity
                    # formulas do the subtracting.
                    value = abs(raw)
                    note = "Disajikan dalam kurung sebagai pengurang; dicatat sebagai magnitudo."

                metrics.append(ExtractedMetric(
                    key=key,
                    label=METRIC_VOCABULARY[key][0],
                    value=value,
                    period=period,
                    scale=scale,
                    document_id=document.doc_id,
                    page=candidate.page,
                    source_line=candidate.text[:400],
                    strategy=strategy,
                    status=ExtractionStatus.UNVERIFIED,
                    note=note,
                ))
        return metrics

    # -- periods -------------------------------------------------------------

    @staticmethod
    def _resolve_periods(documents: Iterable[ParsedDocument], limit: int = 3) -> list[str]:
        """Reporting years, newest first, taken from the statement pages only.

        Restricting to statement pages matters: an annual report's narrative
        sections mention many years, and a comparative balance sheet mentions
        exactly the ones that have columns.
        """
        votes: dict[str, int] = {}
        for document in documents:
            for page in document.pages:
                if page.statement_type not in (
                    StatementType.BALANCE_SHEET, StatementType.INCOME_STATEMENT, StatementType.CASH_FLOW
                ):
                    continue
                for year in page.periods:
                    votes[year] = votes.get(year, 0) + 1
        if not votes:
            for document in documents:
                for page in document.pages[:20]:
                    for year in page.periods:
                        votes[year] = votes.get(year, 0) + 1
        ranked = sorted(votes.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
        return sorted([year for year, _ in ranked[:limit]], reverse=True)

    # -- status assignment ---------------------------------------------------

    @staticmethod
    def _focus_keys(state: dict[str, Any]) -> set[str]:
        focus: set[str] = set()
        for check in state.get("verifications", []):
            if check.get("skipped"):
                focus.update(check.get("involved_metrics") or [])
            elif not check.get("passed"):
                focus.update(check.get("involved_metrics") or [])
        return focus

    @staticmethod
    def _apply_status(
        metrics: list[ExtractedMetric],
        checks: list[Any],
        implicated: dict[str, set[str]],
        exhausted: bool,
    ) -> list[ExtractedMetric]:
        """Promote to VERIFIED only what an identity actually confirmed."""
        confirmed: dict[str, set[str]] = {}
        for check in checks:
            if check.skipped or not check.passed:
                continue
            confirmed.setdefault(check.period, set()).update(check.involved_metrics)

        for metric in metrics:
            broken = metric.key in implicated.get(metric.period, set())
            if broken:
                if exhausted:
                    metric.status = ExtractionStatus.UNRELIABLE
                    metric.note = (
                        "Angka ini memicu ketidakcocokan identitas akuntansi setelah "
                        "beberapa strategi pembacaan; tidak dapat diekstraksi dengan andal."
                    )
                else:
                    metric.status = ExtractionStatus.UNVERIFIED
            elif metric.key in confirmed.get(metric.period, set()):
                metric.status = ExtractionStatus.VERIFIED
            else:
                metric.status = ExtractionStatus.UNVERIFIED
                metric.note = metric.note or "Terbaca dari dokumen; belum tercakup identitas verifikasi."
        return metrics

    # -- logging -------------------------------------------------------------

    def _log(self, events: list[dict[str, Any]], action: str, **kwargs: Any) -> None:
        if self.trail:
            events.append(self.trail.log(AGENT, action, **kwargs))
        else:
            events.append({"agent": AGENT, "action": action, **{k: v for k, v in kwargs.items()}})


def _merge_metrics(previous: list[ExtractedMetric], fresh: list[ExtractedMetric]) -> list[ExtractedMetric]:
    """Newly re-read cells win; everything previously verified is retained."""
    merged: dict[tuple[str, str], ExtractedMetric] = {(m.key, m.period): m for m in previous}
    for metric in fresh:
        merged[(metric.key, metric.period)] = metric
    return list(merged.values())


def financial_auditor_node(state: dict[str, Any], *, llm=None, trail=None) -> dict[str, Any]:
    """LangGraph node entrypoint."""
    return FinancialAuditor(llm=llm, trail=trail).run(state)
