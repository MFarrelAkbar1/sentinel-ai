"""Autonomous monitoring — the trigger that makes Sentinel-AI an agent.

The system's stated position is that it "does not wait for instructions": a
cycle starts because a schedule fired, because a new filing appeared, or because
news broke about a watched entity. This module implements the first two of those
and defines the seam for the third.

    monitor = SentinelMonitor()
    monitor.add_to_watchlist("ABCI", document_dir="data/documents")
    monitor.start()          # blocks; APScheduler owns the loop

Two triggers run:

* **Scheduled sweep** (``SENTINEL_MONITOR_CRON``) — re-analyses every watched
  entity whose documents have changed, or which has never been analysed.
* **New-filing watch** (every 15 minutes) — hashes the watch directory and fires
  a cycle the moment a new PDF lands, without waiting for the nightly sweep.

Re-analysing an unchanged filing burns tokens for a report that would be
identical, so the fingerprint check is what makes continuous monitoring
economically viable rather than merely possible.
"""

from __future__ import annotations

import hashlib
import logging
import signal
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.audit_trail import list_runs
from core.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("sentinel.monitor")


@dataclass
class WatchedEntity:
    ticker: str
    company_name: str = ""
    sector: str = ""
    document_dir: Optional[Path] = None
    document_paths: list[str] = field(default_factory=list)
    last_fingerprint: Optional[str] = None
    last_run_id: Optional[str] = None
    last_run_at: Optional[str] = None

    def resolve_documents(self) -> list[str]:
        """Explicit paths win; otherwise every PDF in the watch directory."""
        if self.document_paths:
            return [p for p in self.document_paths if Path(p).exists()]
        if self.document_dir and self.document_dir.exists():
            pattern = f"{self.ticker.upper()}*.pdf"
            matches = sorted(self.document_dir.glob(pattern))
            if not matches:
                matches = sorted(self.document_dir.glob("*.pdf"))
            return [str(p) for p in matches]
        return []

    def fingerprint(self, paths: list[str]) -> str:
        """Content-addressed identity of the document set.

        Size plus mtime would flag a touched-but-unchanged file; hashing the
        bytes means a cycle only fires when the filing actually differs.
        """
        digest = hashlib.sha256()
        for path in sorted(paths):
            file = Path(path)
            digest.update(file.name.encode("utf-8"))
            try:
                with file.open("rb") as handle:
                    for block in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(block)
            except OSError as exc:
                log.warning("Tidak dapat membaca %s: %s", file, exc)
        return digest.hexdigest()


class SentinelMonitor:
    """APScheduler-driven autonomous monitoring loop."""

    def __init__(self, blocking: bool = True, on_cycle_complete: Optional[Callable[[dict], None]] = None):
        self.settings = get_settings()
        self.entities: dict[str, WatchedEntity] = {}
        self.scheduler = BlockingScheduler() if blocking else BackgroundScheduler()
        self.on_cycle_complete = on_cycle_complete
        self._running = False

    # -- watchlist -----------------------------------------------------------

    def add_to_watchlist(
        self,
        ticker: str,
        company_name: str = "",
        sector: str = "",
        document_dir: Optional[str | Path] = None,
        document_paths: Optional[list[str]] = None,
    ) -> WatchedEntity:
        ticker = ticker.upper()
        entity = WatchedEntity(
            ticker=ticker,
            company_name=company_name or ticker,
            sector=sector,
            document_dir=Path(document_dir) if document_dir else self.settings.doc_dir,
            document_paths=list(document_paths or []),
        )
        # Seed from history so a restart does not re-analyse everything.
        previous = list_runs(ticker=ticker, limit=1)
        if previous:
            entity.last_run_id = previous[0].get("run_id")
            entity.last_run_at = previous[0].get("started_at")
        self.entities[ticker] = entity
        log.info("Ditambahkan ke watchlist: %s (%s)", ticker, entity.company_name)
        return entity

    def load_watchlist_from_settings(self) -> None:
        for ticker in self.settings.watchlist:
            self.add_to_watchlist(ticker, document_dir=self.settings.doc_dir)

    # -- cycles --------------------------------------------------------------

    def run_cycle(self, ticker: str, trigger: str = "scheduled", force: bool = False) -> Optional[dict[str, Any]]:
        """Analyse one entity if its documents changed (or ``force``)."""
        entity = self.entities.get(ticker.upper())
        if entity is None:
            log.warning("%s tidak ada pada watchlist.", ticker)
            return None

        paths = entity.resolve_documents()
        if not paths:
            log.info("%s: tidak ada dokumen pada direktori pantauan; siklus dilewati.", ticker)
            return None

        fingerprint = entity.fingerprint(paths)
        if not force and fingerprint == entity.last_fingerprint:
            log.info("%s: dokumen tidak berubah sejak siklus terakhir; siklus dilewati.", ticker)
            return None

        log.info("%s: memulai siklus (%s) atas %d dokumen.", ticker, trigger, len(paths))
        # Imported lazily so that adding entities to a watchlist does not pull
        # in LangGraph and the whole agent stack.
        from agents.orchestrator import run_analysis

        try:
            result = run_analysis(
                ticker=entity.ticker,
                document_paths=paths,
                company_name=entity.company_name,
                sector=entity.sector,
                trigger=trigger,
            )
        except Exception:
            log.exception("%s: siklus gagal.", ticker)
            return None

        entity.last_fingerprint = fingerprint
        entity.last_run_id = result.get("run_id")
        entity.last_run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        score = result.get("risk_score", {})
        log.info(
            "%s: siklus selesai — skor %s (%s), %s temuan, %s hipotesis digugurkan.",
            ticker, score.get("score"), score.get("band"),
            score.get("flags_retained"), score.get("hypotheses_dropped"),
        )
        self._notify(entity, result)
        if self.on_cycle_complete:
            self.on_cycle_complete(result)
        return result

    def sweep(self, trigger: str = "scheduled") -> list[dict[str, Any]]:
        """One pass over the whole watchlist."""
        log.info("Sapuan pemantauan dimulai untuk %d entitas.", len(self.entities))
        results = []
        for ticker in list(self.entities):
            result = self.run_cycle(ticker, trigger=trigger)
            if result:
                results.append(result)
        log.info("Sapuan selesai: %d siklus dijalankan.", len(results))
        return results

    def check_for_new_filings(self) -> list[dict[str, Any]]:
        """Fire immediately on a newly-arrived document, ahead of the sweep."""
        results = []
        for ticker, entity in self.entities.items():
            paths = entity.resolve_documents()
            if not paths:
                continue
            if entity.fingerprint(paths) != entity.last_fingerprint:
                log.info("%s: dokumen baru terdeteksi.", ticker)
                result = self.run_cycle(ticker, trigger="new_filing")
                if result:
                    results.append(result)
        return results

    # -- notification --------------------------------------------------------

    def _notify(self, entity: WatchedEntity, result: dict[str, Any]) -> None:
        """Escalation hook.

        Stage 2 of the roadmap routes this to email and WhatsApp. The
        calibration decision the roadmap flags as the hard part is already made
        upstream: only findings that survived falsification *and* cleared the
        confidence threshold reach ``findings`` unsuppressed, so this method
        never has to re-judge what is worth sending.
        """
        surfaced = [f for f in result.get("findings", []) if not f.get("suppressed")]
        critical = [f for f in surfaced if f.get("severity") in ("kritis", "tinggi")]
        if not critical:
            return
        score = result.get("risk_score", {})
        log.warning(
            "NOTIFIKASI %s — skor %s (%s). %d temuan severitas tinggi/kritis: %s. Laporan: %s",
            entity.ticker, score.get("score"), score.get("band"), len(critical),
            "; ".join(f.get("title", "") for f in critical),
            (result.get("report_paths") or {}).get("markdown", "—"),
        )

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if not self.entities:
            self.load_watchlist_from_settings()

        self.scheduler.add_job(
            self.sweep, CronTrigger.from_crontab(self.settings.monitor_cron),
            id="sweep", name="Sapuan due diligence terjadwal",
            replace_existing=True, misfire_grace_time=3600,
        )
        self.scheduler.add_job(
            self.check_for_new_filings, IntervalTrigger(minutes=15),
            id="new_filings", name="Deteksi dokumen baru",
            replace_existing=True, misfire_grace_time=600,
        )

        log.info(
            "Pemantauan otonom aktif — cron '%s', deteksi dokumen baru tiap 15 menit, %d entitas.",
            self.settings.monitor_cron, len(self.entities),
        )
        self._install_signal_handlers()
        self._running = True
        try:
            self.scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            self.stop()

    def stop(self) -> None:
        if self._running:
            log.info("Menghentikan pemantauan otonom.")
            self.scheduler.shutdown(wait=False)
            self._running = False

    def _install_signal_handlers(self) -> None:
        def handler(signum, frame):  # noqa: ARG001 - signal signature
            self.stop()
            sys.exit(0)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, AttributeError):  # pragma: no cover - non-main thread / Windows
                pass

    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "ticker": e.ticker,
                "company_name": e.company_name,
                "documents": len(e.resolve_documents()),
                "last_run_id": e.last_run_id,
                "last_run_at": e.last_run_at,
                "fingerprinted": bool(e.last_fingerprint),
            }
            for e in self.entities.values()
        ]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Pemantauan otonom Sentinel-AI.")
    parser.add_argument("--tickers", help="Daftar ticker dipisahkan koma (default: SENTINEL_WATCHLIST).")
    parser.add_argument("--doc-dir", help="Direktori dokumen yang dipantau.")
    parser.add_argument("--once", action="store_true", help="Jalankan satu sapuan lalu keluar.")
    parser.add_argument("--force", action="store_true", help="Analisis ulang walau dokumen tidak berubah.")
    args = parser.parse_args()

    monitor = SentinelMonitor()
    settings = get_settings()
    tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else list(settings.watchlist)
    for ticker in tickers:
        monitor.add_to_watchlist(ticker, document_dir=args.doc_dir or settings.doc_dir)

    if args.once:
        if args.force:
            for ticker in tickers:
                monitor.run_cycle(ticker, trigger="manual", force=True)
        else:
            monitor.sweep(trigger="manual")
        return

    monitor.start()


if __name__ == "__main__":
    main()
