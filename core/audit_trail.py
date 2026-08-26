"""Full decision logging — the audit trail that makes the pipeline traceable.

Every agent decision, tool call, verification outcome, and dropped hypothesis
is written here with a timestamp. The trail is the evidence that the system did
what it claims to have done; the technical report treats it as a deliverable of
Stage 1, not as debugging output.

Storage is SQLite for the MVP. The schema is deliberately plain so the upgrade
path (PostgreSQL, one connection-string change plus ``%s`` placeholders) stays
mechanical.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from core.config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    ticker        TEXT NOT NULL,
    company_name  TEXT,
    sector        TEXT,
    trigger       TEXT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'running',
    risk_score    REAL,
    risk_band     TEXT,
    flags_retained    INTEGER DEFAULT 0,
    hypotheses_dropped INTEGER DEFAULT 0,
    patterns_run  INTEGER DEFAULT 0,
    report_path   TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id    TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    timestamp   TEXT NOT NULL,
    agent       TEXT NOT NULL,
    action      TEXT NOT NULL,
    tool        TEXT,
    decision    TEXT,
    detail      TEXT,
    payload     TEXT,
    duration_ms INTEGER,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_events_run ON audit_events(run_id, seq);

CREATE TABLE IF NOT EXISTS run_metrics (
    run_id  TEXT NOT NULL,
    period  TEXT NOT NULL,
    key     TEXT NOT NULL,
    value   REAL,
    page    INTEGER,
    status  TEXT,
    source_line TEXT,
    PRIMARY KEY (run_id, period, key)
);

CREATE TABLE IF NOT EXISTS run_findings (
    finding_id TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL,
    title      TEXT,
    pattern    TEXT,
    severity   TEXT,
    confidence REAL,
    suppressed INTEGER DEFAULT 0,
    payload    TEXT
);
"""


class AuditTrail:
    """Append-only decision log scoped to one analysis run."""

    _lock = threading.Lock()

    def __init__(self, run_id: str, db_path: Optional[Path] = None, echo: bool = False):
        self.run_id = run_id
        self.db_path = Path(db_path or get_settings().db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.echo = echo
        self._seq = 0
        self._buffer: list[dict[str, Any]] = []
        self._init_schema()

    # -- plumbing -----------------------------------------------------------

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(_SCHEMA)

    # -- run lifecycle ------------------------------------------------------

    def open_run(self, ticker: str, company_name: str = "", sector: str = "", trigger: str = "manual") -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO runs
                   (run_id, ticker, company_name, sector, trigger, started_at, status)
                   VALUES (?,?,?,?,?,?,'running')""",
                (self.run_id, ticker, company_name, sector, trigger, _now()),
            )

    def close_run(
        self,
        status: str = "completed",
        risk_score: Optional[float] = None,
        risk_band: Optional[str] = None,
        flags_retained: int = 0,
        hypotheses_dropped: int = 0,
        patterns_run: int = 0,
        report_path: Optional[str] = None,
    ) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """UPDATE runs SET finished_at=?, status=?, risk_score=?, risk_band=?,
                       flags_retained=?, hypotheses_dropped=?, patterns_run=?, report_path=?
                   WHERE run_id=?""",
                (
                    _now(), status, risk_score, risk_band, flags_retained,
                    hypotheses_dropped, patterns_run, report_path, self.run_id,
                ),
            )

    # -- event logging ------------------------------------------------------

    def log(
        self,
        agent: str,
        action: str,
        *,
        tool: Optional[str] = None,
        decision: Optional[str] = None,
        detail: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
        duration_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        """Record one decision. Returns the event dict for inclusion in state."""
        self._seq += 1
        event = {
            "event_id": uuid.uuid4().hex,
            "run_id": self.run_id,
            "seq": self._seq,
            "timestamp": _now(),
            "agent": agent,
            "action": action,
            "tool": tool,
            "decision": decision,
            "detail": detail,
            "payload": payload or {},
            "duration_ms": duration_ms,
        }
        self._buffer.append(event)
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO audit_events
                   (event_id, run_id, seq, timestamp, agent, action, tool, decision, detail, payload, duration_ms)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event["event_id"], event["run_id"], event["seq"], event["timestamp"],
                    agent, action, tool, decision, detail,
                    json.dumps(event["payload"], ensure_ascii=False, default=str),
                    duration_ms,
                ),
            )
        if self.echo:
            print(f"[{event['seq']:03d}] {agent:<22} {action:<28} {decision or ''} {detail or ''}")
        return event

    @contextmanager
    def timed(self, agent: str, action: str, *, tool: Optional[str] = None, **kwargs) -> Iterator[dict]:
        """Log an action with its wall-clock duration.

        The yielded dict may be mutated in the block; ``decision``, ``detail``,
        and ``payload`` are picked up when the block exits.
        """
        start = datetime.now(timezone.utc)
        sink: dict[str, Any] = {"decision": None, "detail": None, "payload": {}}
        try:
            yield sink
        finally:
            elapsed = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
            self.log(
                agent, action, tool=tool,
                decision=sink.get("decision"), detail=sink.get("detail"),
                payload=sink.get("payload"), duration_ms=elapsed, **kwargs,
            )

    # -- derived persistence ------------------------------------------------

    def persist_metrics(self, metrics: list[dict[str, Any]]) -> None:
        rows = [
            (
                self.run_id, m.get("period", ""), m.get("key", ""),
                m.get("value"), m.get("page"), m.get("status"), m.get("source_line"),
            )
            for m in metrics
        ]
        if not rows:
            return
        with self._lock, self._conn() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO run_metrics
                   (run_id, period, key, value, page, status, source_line)
                   VALUES (?,?,?,?,?,?,?)""",
                rows,
            )

    def persist_findings(self, findings: list[dict[str, Any]]) -> None:
        rows = [
            (
                f.get("id") or uuid.uuid4().hex, self.run_id, f.get("title"),
                f.get("pattern"), f.get("severity"), f.get("confidence"),
                1 if f.get("suppressed") else 0,
                json.dumps(f, ensure_ascii=False, default=str),
            )
            for f in findings
        ]
        if not rows:
            return
        with self._lock, self._conn() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO run_findings
                   (finding_id, run_id, title, pattern, severity, confidence, suppressed, payload)
                   VALUES (?,?,?,?,?,?,?,?)""",
                rows,
            )

    # -- reads --------------------------------------------------------------

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._buffer)

    def to_markdown(self) -> str:
        lines = ["| # | Waktu | Agen | Aksi | Tool | Keputusan | Detail |",
                 "|---|-------|------|------|------|-----------|--------|"]
        for e in self._buffer:
            ts = e["timestamp"][11:19]
            lines.append(
                f"| {e['seq']} | {ts} | {e['agent']} | {e['action']} | {e['tool'] or '—'} | "
                f"{e['decision'] or '—'} | {(e['detail'] or '—')[:120]} |"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cross-run queries (used by the dashboard and the scheduler)
# ---------------------------------------------------------------------------


def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(db_path or get_settings().db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def list_runs(ticker: Optional[str] = None, limit: int = 50, db_path: Optional[Path] = None) -> list[dict]:
    conn = _connect(db_path)
    try:
        if ticker:
            cur = conn.execute(
                "SELECT * FROM runs WHERE ticker=? ORDER BY started_at DESC LIMIT ?", (ticker.upper(), limit)
            )
        else:
            cur = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_events(run_id: str, db_path: Optional[Path] = None) -> list[dict]:
    conn = _connect(db_path)
    try:
        cur = conn.execute("SELECT * FROM audit_events WHERE run_id=? ORDER BY seq", (run_id,))
        rows = []
        for r in cur.fetchall():
            row = dict(r)
            try:
                row["payload"] = json.loads(row.get("payload") or "{}")
            except json.JSONDecodeError:
                row["payload"] = {}
            rows.append(row)
        return rows
    finally:
        conn.close()


def score_trajectory(ticker: str, limit: int = 12, db_path: Optional[Path] = None) -> list[dict]:
    """Historical composite scores — feeds the dashboard's trajectory chart."""
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            """SELECT started_at, risk_score, risk_band FROM runs
               WHERE ticker=? AND risk_score IS NOT NULL
               ORDER BY started_at DESC LIMIT ?""",
            (ticker.upper(), limit),
        )
        return list(reversed([dict(r) for r in cur.fetchall()]))
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
