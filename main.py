"""Sentinel-AI command line entrypoint.

    python main.py demo                       # end-to-end run on the demo filing
    python main.py analyze ABCI --pdf a.pdf   # analyse a real filing
    python main.py trail <run_id>             # replay a run's decision log
    python main.py runs                       # list previous cycles
    python main.py monitor --once             # one autonomous sweep

The demo subcommand is the competition path: it generates the synthetic filing
if needed, runs all four agents, prints the agent-by-agent audit trail as it
happens, and writes the Executive Due Diligence Report.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from core.audit_trail import get_events, list_runs
from core.config import get_settings
from core.formatting import idr


def _banner(text: str) -> None:
    print()
    print("═" * 78)
    print(f"  {text}")
    print("═" * 78)


def _print_summary(result: dict) -> None:
    score = result.get("risk_score", {}) or {}
    _banner("HASIL SIKLUS")
    print(f"  Entitas          : {result.get('company_name')} ({result.get('ticker')})")
    print(f"  Periode          : {', '.join(result.get('periods', [])) or '—'}")
    print(f"  Skor komposit    : {score.get('score', '—')}/100 — {score.get('band', '—')}"
          f"   (skor terbalik: makin tinggi makin aman)")
    print(f"  Pola dijalankan  : {score.get('patterns_run', 0)}")
    print(f"  Temuan tampil    : {score.get('flags_retained', 0)}")
    print(f"  Hipotesis gugur  : {score.get('hypotheses_dropped', 0)}")
    print(f"  Cakupan verifikasi: {idr((score.get('data_coverage', 0) or 0) * 100, 0)}%")
    if score.get("coverage_note"):
        print(f"  ⚠️  {score['coverage_note']}")

    findings = result.get("findings", [])
    surfaced = [f for f in findings if not f.get("suppressed")]
    suppressed = [f for f in findings if f.get("suppressed")]

    if surfaced:
        _banner("TEMUAN")
        for finding in surfaced:
            print(f"  [{finding['id']}] {finding['title']}")
            print(f"       severitas {finding['severity']} · keyakinan {finding['confidence']:.2f} "
                  f"· pola {finding['pattern']}")
            print(f"       {finding['narrative'][:220]}")
            print(f"       ❓ {finding['question_for_management'][:200]}")
            print()
    else:
        _banner("TEMUAN")
        print("  Tidak ada temuan yang melampaui ambang keyakinan.")

    if suppressed:
        print(f"  ({len(suppressed)} temuan ditahan di bawah ambang keyakinan — lihat lampiran teknis)")

    dropped = result.get("dropped_hypotheses", [])
    if dropped:
        _banner("HIPOTESIS YANG DIGUGURKAN SETELAH BUKTI PENYANGKAL")
        for hypothesis in dropped:
            print(f"  [{hypothesis['id']}] {hypothesis['statement'][:160]}")
            print(f"       → {hypothesis['verdict_reason'][:200]}")
            print()

    paths = result.get("report_paths", {}) or {}
    if paths:
        _banner("LAPORAN")
        for label, path in paths.items():
            print(f"  {label:<9}: {path}")

    usage = result.get("token_usage") or {}
    if usage.get("calls"):
        print()
        print(f"  Pemakaian model: {usage['calls']} panggilan, "
              f"{usage.get('input_tokens', 0):,} token masuk, "
              f"{usage.get('output_tokens', 0):,} token keluar")
    print()


def cmd_demo(args: argparse.Namespace) -> int:
    from tools.demo_corpus import ensure_demo_corpus

    created = ensure_demo_corpus(overwrite=args.rebuild)
    _banner("SENTINEL-AI — MODE DEMO")
    print(f"  Dokumen : {created['pdf']}")
    print(f"  Entitas : {created['company_name']} ({created['ticker']}) — entitas fiktif")
    print()
    settings = get_settings()
    print(f"  LLM      : {'aktif — ' + settings.model_primary if settings.has_llm else 'nonaktif (jalur deterministik)'}")
    print(f"  Pencarian: {'Tavily aktif' if settings.has_search else 'fixture offline'}")

    _banner("JEJAK AUDIT (langsung)")
    from agents.orchestrator import run_analysis

    result = run_analysis(
        ticker=created["ticker"],
        document_paths=[created["pdf"]],
        company_name=created["company_name"],
        sector=created["sector"],
        trigger="demo",
        echo=True,
    )
    _print_summary(result)
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    paths = [str(Path(p).resolve()) for p in args.pdf]
    missing = [p for p in paths if not Path(p).exists()]
    if missing:
        print(f"Dokumen tidak ditemukan: {', '.join(missing)}", file=sys.stderr)
        return 1

    _banner(f"SENTINEL-AI — ANALISIS {args.ticker.upper()}")
    from agents.orchestrator import run_analysis

    result = run_analysis(
        ticker=args.ticker,
        document_paths=paths,
        company_name=args.name or args.ticker,
        sector=args.sector or "",
        trigger="manual",
        echo=not args.quiet,
    )
    _print_summary(result)
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    rows = list_runs(ticker=args.ticker, limit=args.limit)
    if not rows:
        print("Belum ada siklus yang tercatat.")
        return 0
    _banner("RIWAYAT SIKLUS")
    print(f"  {'run_id':<14} {'ticker':<8} {'mulai':<21} {'skor':>6} {'band':<13} {'temuan':>7} {'gugur':>6}")
    for row in rows:
        print(f"  {row['run_id']:<14} {row['ticker']:<8} {str(row['started_at'])[:19]:<21} "
              f"{row['risk_score'] if row['risk_score'] is not None else '—':>6} "
              f"{row['risk_band'] or '—':<13} {row['flags_retained']:>7} {row['hypotheses_dropped']:>6}")
    print()
    return 0


def cmd_trail(args: argparse.Namespace) -> int:
    events = get_events(args.run_id)
    if not events:
        print(f"Tidak ada jejak audit untuk run {args.run_id}.", file=sys.stderr)
        return 1
    _banner(f"JEJAK AUDIT — {args.run_id}")
    for event in events:
        timestamp = str(event["timestamp"])[11:19]
        duration = f"{event['duration_ms']}ms" if event.get("duration_ms") else ""
        print(f"  [{event['seq']:03d}] {timestamp} {event['agent']:<22} {event['action']:<28} "
              f"{event.get('tool') or '':<18} {event.get('decision') or ''} {duration}")
        if event.get("detail"):
            print(f"        └─ {event['detail'][:150]}")
    print()
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    from scheduler.monitor import SentinelMonitor

    settings = get_settings()
    monitor = SentinelMonitor()
    tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else list(settings.watchlist)
    for ticker in tickers:
        monitor.add_to_watchlist(ticker, document_dir=args.doc_dir or settings.doc_dir)

    if args.once:
        monitor.sweep(trigger="manual")
        return 0
    monitor.start()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sentinel",
        description="Sentinel-AI — due diligence otonom untuk emiten Bursa Efek Indonesia.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Log level DEBUG.")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="Jalankan pipeline lengkap atas dokumen demo.")
    demo.add_argument("--rebuild", action="store_true", help="Buat ulang dokumen demo.")
    demo.set_defaults(func=cmd_demo)

    analyze = sub.add_parser("analyze", help="Analisis dokumen emiten sungguhan.")
    analyze.add_argument("ticker", help="Kode emiten, mis. GOTO.")
    analyze.add_argument("--pdf", nargs="+", required=True, help="Satu atau lebih berkas PDF.")
    analyze.add_argument("--name", help="Nama perusahaan.")
    analyze.add_argument("--sector", help="Sektor (memengaruhi prioritas pola).")
    analyze.add_argument("--quiet", action="store_true", help="Sembunyikan jejak audit langsung.")
    analyze.set_defaults(func=cmd_analyze)

    runs = sub.add_parser("runs", help="Daftar siklus sebelumnya.")
    runs.add_argument("--ticker", help="Saring berdasarkan emiten.")
    runs.add_argument("--limit", type=int, default=20)
    runs.set_defaults(func=cmd_runs)

    trail = sub.add_parser("trail", help="Tampilkan jejak audit satu siklus.")
    trail.add_argument("run_id")
    trail.set_defaults(func=cmd_trail)

    monitor = sub.add_parser("monitor", help="Jalankan pemantauan otonom.")
    monitor.add_argument("--tickers", help="Daftar ticker dipisahkan koma.")
    monitor.add_argument("--doc-dir", help="Direktori dokumen yang dipantau.")
    monitor.add_argument("--once", action="store_true", help="Satu sapuan lalu keluar.")
    monitor.set_defaults(func=cmd_monitor)

    return parser


def _force_utf8_console() -> None:
    """Windows consoles default to cp1252, which cannot print Rupiah reports."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - redirected streams
            pass


def main() -> int:
    _force_utf8_console()
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s | %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
