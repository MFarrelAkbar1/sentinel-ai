"""Sentinel-AI dashboard.

    streamlit run frontend/app.py

The layout follows the product design: a composite risk score that is inverted
(higher is safer), a trajectory across monitoring cycles, the findings that
survived falsification, and — the part that distinguishes this from a scoring
widget — the agent-by-agent audit trail and the technical annex where every
figure is traced to a page.

Three things the UI is careful about:

* **Dropped hypotheses are displayed, not hidden.** They are evidence the system
  investigated rather than merely alarmed, and they sit next to the findings.
* **Suppressed findings are shown with their suppression reason**, so the
  confidence threshold is visible as a decision rather than felt as a silence.
* **The disclaimer travels with the score.** The product position is a research
  and screening tool, and the surface that shows a risk number is exactly the
  surface where that has to be legible.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.audit_trail import get_events, list_runs, score_trajectory  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.formatting import compact_idr, idr  # noqa: E402

st.set_page_config(
    page_title="Sentinel-AI · Due Diligence",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

NAVY = "#0F1B3C"
NAVY_SOFT = "#16264D"
GOLD = "#C9A227"
MUTED = "#64748B"
SEVERITY_COLORS = {
    "kritis": "#B91C1C",
    "tinggi": "#DC2626",
    "sedang": "#D97706",
    "rendah": "#0891B2",
}
BAND_COLORS = {"Low Risk": "#0E9F6E", "Medium Risk": "#C9A227", "High Risk": "#DC2626"}

st.markdown(
    f"""
    <style>
      .block-container {{ padding-top: 2.2rem; max-width: 1400px; }}
      .sentinel-brand {{ font-size: 1.45rem; font-weight: 700; color: {NAVY}; letter-spacing:-0.02em; }}
      .sentinel-brand span {{ color: {GOLD}; }}
      .sentinel-sub {{ font-size: 0.72rem; letter-spacing: 0.16em; color: {MUTED}; text-transform: uppercase; }}
      .crumb {{ font-size: 0.8rem; color: {MUTED}; }}
      .entity {{ font-size: 2.5rem; font-weight: 700; color: {NAVY}; letter-spacing: -0.03em; margin: 0.2rem 0 0 0; }}
      .entity small {{ font-size: 1.2rem; color: {MUTED}; font-weight: 400; }}
      .metaline {{ font-size: 0.86rem; color: {MUTED}; margin-bottom: 1.2rem; }}
      .metaline b {{ color: {NAVY}; font-weight: 600; }}
      .scorecard {{ background: linear-gradient(160deg, {NAVY} 0%, {NAVY_SOFT} 100%);
                    border-radius: 16px; padding: 1.6rem 1.8rem; color: #E7ECF5; }}
      .scorecard .label {{ font-size: 0.7rem; letter-spacing: 0.16em; color: {GOLD};
                           text-transform: uppercase; font-weight: 700; }}
      .scorecard .caption {{ font-size: 0.85rem; color: #A9B6CE; margin-top: 0.15rem; }}
      .scorecard .stat {{ font-size: 1.7rem; font-weight: 700; color: #FFFFFF; line-height: 1.1; }}
      .scorecard .statlabel {{ font-size: 0.75rem; color: #A9B6CE; }}
      .pill {{ display:inline-block; padding: 0.2rem 0.75rem; border-radius: 999px;
               font-size: 0.78rem; font-weight: 600; }}
      .findingcard {{ border: 1px solid #E2E8F0; border-left: 5px solid var(--sev);
                      border-radius: 12px; padding: 1rem 1.2rem; margin-bottom: 0.9rem;
                      background: #FFFFFF; }}
      .findingcard h4 {{ margin: 0 0 0.35rem 0; color: {NAVY}; font-size: 1.02rem; }}
      .findingmeta {{ font-size: 0.78rem; color: {MUTED}; margin-bottom: 0.55rem; }}
      .question {{ background: #F8FAFC; border-left: 3px solid {GOLD};
                   padding: 0.55rem 0.85rem; font-size: 0.88rem; color: {NAVY}; border-radius: 4px; }}
      .droppedcard {{ border: 1px dashed #CBD5E1; border-radius: 12px; padding: 0.9rem 1.1rem;
                      margin-bottom: 0.7rem; background: #F8FAFC; color: {MUTED}; font-size: 0.88rem; }}
      .disclaimer {{ font-size: 0.8rem; color: {MUTED}; border-top: 1px solid #E2E8F0;
                     padding-top: 0.9rem; margin-top: 2rem; }}
      .cite {{ font-family: ui-monospace, SFMono-Regular, monospace; font-size: 0.76rem; color: {MUTED}; }}
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Sidebar — inputs and system status
# ---------------------------------------------------------------------------


def sidebar() -> dict[str, Any]:
    settings = get_settings()
    with st.sidebar:
        st.markdown(
            '<div class="sentinel-brand">Sentinel<span>-AI</span></div>'
            '<div class="sentinel-sub">Due Diligence</div>',
            unsafe_allow_html=True,
        )
        st.divider()

        mode = st.radio(
            "Sumber dokumen",
            ["Dokumen demo (ABCI)", "Unggah laporan tahunan", "Berkas di data/documents"],
            help="Dokumen demo adalah entitas fiktif yang dibuat untuk menguji pipeline.",
        )

        ticker, company, sector, paths = "ABCI", "PT ABC Indonesia Tbk", "konsumer", []

        if mode == "Dokumen demo (ABCI)":
            from tools.demo_corpus import DEMO_COMPANY, DEMO_SECTOR, DEMO_TICKER, ensure_demo_corpus

            created = ensure_demo_corpus()
            ticker, company, sector = DEMO_TICKER, DEMO_COMPANY, DEMO_SECTOR
            paths = [created["pdf"]]
            st.caption(f"📄 {Path(created['pdf']).name}")

        elif mode == "Unggah laporan tahunan":
            ticker = st.text_input("Kode emiten", value="", placeholder="mis. GOTO").upper()
            company = st.text_input("Nama perusahaan", value="")
            sector = st.selectbox(
                "Sektor",
                ["", "keuangan", "properti", "pertambangan", "teknologi",
                 "konsumer", "infrastruktur", "kesehatan"],
                help="Menentukan pola deteksi mana yang diprioritaskan orkestrator.",
            )
            uploads = st.file_uploader("Laporan tahunan (PDF)", type=["pdf"], accept_multiple_files=True)
            if uploads and ticker:
                settings.doc_dir.mkdir(parents=True, exist_ok=True)
                for upload in uploads:
                    destination = settings.doc_dir / f"{ticker}_{upload.name}"
                    destination.write_bytes(upload.getbuffer())
                    paths.append(str(destination))
                st.success(f"{len(paths)} dokumen siap dianalisis.")

        else:
            available = sorted(settings.doc_dir.glob("*.pdf"))
            if not available:
                st.info("Belum ada PDF pada data/documents.")
            else:
                chosen = st.multiselect(
                    "Pilih dokumen", [p.name for p in available], default=[available[0].name]
                )
                paths = [str(settings.doc_dir / name) for name in chosen]
                ticker = st.text_input("Kode emiten", value=Path(chosen[0]).stem[:4].upper() if chosen else "")
                company = st.text_input("Nama perusahaan", value=ticker)
                sector = st.selectbox(
                    "Sektor",
                    ["", "keuangan", "properti", "pertambangan", "teknologi",
                     "konsumer", "infrastruktur", "kesehatan"],
                )

        st.divider()
        run = st.button("▶  Jalankan analisis", type="primary", width="stretch",
                        disabled=not (paths and ticker))

        st.divider()
        st.markdown("**Status sistem**")
        st.markdown(
            f"- Model penalaran: {'`' + settings.model_primary + '`' if settings.has_llm else '⚠️ nonaktif'}\n"
            f"- Model ringan: {'`' + settings.model_light + '`' if settings.has_llm else '—'}\n"
            f"- Pencarian web: {'✅ Tavily' if settings.has_search else '⚠️ fixture / nonaktif'}\n"
            f"- Ambang keyakinan: `{settings.min_confidence:.2f}`\n"
            f"- Toleransi verifikasi: `{settings.verify_rel_tolerance:.3%}`"
        )
        if not settings.has_llm:
            st.caption(
                "Tanpa ANTHROPIC_API_KEY sistem berjalan pada jalur deterministik: "
                "ekstraksi berbasis kaption, verifikasi aritmetik, dan pola deteksi tetap "
                "berjalan; adjudikasi bukti penyangkal dan ringkasan naratif dilewati."
            )

        st.divider()
        history = list_runs(limit=12)
        if history:
            st.markdown("**Siklus sebelumnya**")
            options = {
                f"{r['ticker']} · {str(r['started_at'])[:16]} · {r['risk_score'] or '—'}": r["run_id"]
                for r in history
            }
            selected = st.selectbox("Muat hasil", ["—"] + list(options), label_visibility="collapsed")
            if selected != "—":
                st.session_state["loaded_run_id"] = options[selected]

    return {"ticker": ticker, "company": company, "sector": sector, "paths": paths, "run": run}


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------


def header(result: dict[str, Any]) -> None:
    meta_periods = ", ".join(result.get("periods", [])) or "—"
    documents = result.get("documents", []) or []
    sector = result.get("sector") or "sektor tidak ditentukan"
    generated = (result.get("report", {}).get("meta", {}) or {}).get("generated_at", "")
    pages = sum(d.get("page_count", 0) for d in documents)

    left, right = st.columns([3, 1.15])
    with left:
        st.markdown(
            f'<div class="crumb">Portfolio › {sector.title()} › {result.get("ticker","")}</div>'
            f'<div class="entity">{result.get("company_name","")} '
            f'<small>{result.get("ticker","")}</small></div>',
            unsafe_allow_html=True,
        )
        verified = sum(1 for m in result.get("metrics", []) if m.get("status") == "terverifikasi")
        unreliable = len(result.get("unreliable_metrics", []) or [])
        badge = (f'<b>{verified} angka terverifikasi</b>'
                 + (f' · <b style="color:#B91C1C">{unreliable} tidak dapat diekstraksi</b>' if unreliable else ""))
        st.markdown(
            f'<div class="metaline">Financial Due Diligence Report &nbsp;·&nbsp; IDX &nbsp;·&nbsp; '
            f'{meta_periods} &nbsp;·&nbsp; {len(documents)} dokumen, {pages} halaman &nbsp;·&nbsp; '
            f'{badge} &nbsp;·&nbsp; siklus {generated[:16].replace("T", " ")}</div>',
            unsafe_allow_html=True,
        )
    with right:
        paths = result.get("report_paths", {}) or {}
        markdown_path = paths.get("markdown")
        if markdown_path and Path(markdown_path).exists():
            st.download_button(
                "⬇  Unduh laporan (Markdown)",
                data=Path(markdown_path).read_text(encoding="utf-8"),
                file_name=Path(markdown_path).name,
                mime="text/markdown",
                width="stretch",
            )
        json_path = paths.get("json")
        if json_path and Path(json_path).exists():
            st.download_button(
                "⬇  Unduh data (JSON)",
                data=Path(json_path).read_text(encoding="utf-8"),
                file_name=Path(json_path).name,
                mime="application/json",
                width="stretch",
            )


# ---------------------------------------------------------------------------
# Score card + trajectory
# ---------------------------------------------------------------------------


def _gauge_svg(score: float, band: str) -> str:
    """Semicircular gauge; the arc length encodes the inverted score."""
    colour = BAND_COLORS.get(band, GOLD)
    radius, circumference = 70.0, 3.14159 * 70.0
    filled = max(0.0, min(score, 100.0)) / 100.0 * circumference
    return f"""
    <svg width="190" height="112" viewBox="0 0 190 112">
      <path d="M 25 100 A {radius} {radius} 0 0 1 165 100" fill="none"
            stroke="#2C3F6B" stroke-width="13" stroke-linecap="round"/>
      <path d="M 25 100 A {radius} {radius} 0 0 1 165 100" fill="none"
            stroke="{colour}" stroke-width="13" stroke-linecap="round"
            stroke-dasharray="{filled:.1f} {circumference:.1f}"/>
      <text x="95" y="92" text-anchor="middle" fill="#FFFFFF"
            font-size="40" font-weight="700" font-family="system-ui">{score:.0f}</text>
      <text x="95" y="108" text-anchor="middle" fill="#A9B6CE"
            font-size="12" font-family="system-ui">/100</text>
    </svg>
    """


def score_panel(result: dict[str, Any]) -> None:
    score = result.get("risk_score", {}) or {}
    value = float(score.get("score", 0) or 0)
    band = score.get("band", "—")
    colour = BAND_COLORS.get(band, GOLD)

    left, right = st.columns([1, 1.55])

    with left:
        st.markdown(
            f"""
            <div class="scorecard">
              <div class="label">Composite Risk Score</div>
              <div class="caption">Berbobot atas {score.get('patterns_run', 0)} pola deteksi</div>
              <div style="display:flex; align-items:center; gap:1.1rem; margin-top:0.6rem;">
                <div>{_gauge_svg(value, band)}</div>
                <div>
                  <span class="pill" style="background:{colour}22; color:{colour};">● {band}</span>
                  <div class="caption" style="margin-top:0.6rem;">
                    Skor terbalik — makin tinggi makin aman.<br/>
                    High &lt; 60 &nbsp;·&nbsp; Medium 60–84 &nbsp;·&nbsp; Low ≥ 85
                  </div>
                </div>
              </div>
              <hr style="border-color:#2C3F6B; margin:1rem 0 0.8rem 0;"/>
              <div style="display:flex; gap:2.2rem;">
                <div><div class="stat">{score.get('patterns_run', 0)}</div>
                     <div class="statlabel">Pola dijalankan</div></div>
                <div><div class="stat">{score.get('flags_retained', 0)}</div>
                     <div class="statlabel">Temuan dipertahankan</div></div>
                <div><div class="stat">{score.get('hypotheses_dropped', 0)}</div>
                     <div class="statlabel">Hipotesis digugurkan</div></div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if score.get("coverage_note"):
            st.warning(score["coverage_note"], icon="⚠️")

    with right:
        st.markdown("##### Lintasan skor risiko")
        st.caption("Skor komposit pada siklus pemantauan terakhir untuk emiten ini.")
        _trajectory_chart(result.get("ticker", ""), value)
        if score.get("breakdown"):
            st.caption("Kontribusi penalti per pola:")
            st.dataframe(
                [
                    {
                        "Pola": row["pattern"],
                        "Severitas": row["severity"],
                        "Keyakinan": f"{row['confidence']:.2f}",
                        "Bobot maks": row["weight"],
                        "Penalti": f"−{row['penalty']:.1f}",
                    }
                    for row in score["breakdown"]
                ],
                hide_index=True,
                width="stretch",
            )


def _trajectory_chart(ticker: str, current: float) -> None:
    import pandas as pd

    history = score_trajectory(ticker, limit=12)
    if len(history) < 2:
        st.info(
            "Lintasan terbentuk setelah beberapa siklus pemantauan. "
            "Jalankan analisis berulang (atau aktifkan penjadwal) untuk mengisinya.",
            icon="📈",
        )
        return

    frame = pd.DataFrame([
        {"Siklus": str(row["started_at"])[:16].replace("T", " "),
         "Skor": row["risk_score"], "Band": row["risk_band"]}
        for row in history
    ])
    try:
        import altair as alt

        base = alt.Chart(frame).encode(
            x=alt.X("Siklus:O", title=None, axis=alt.Axis(labelAngle=-30)),
            y=alt.Y("Skor:Q", scale=alt.Scale(domain=[0, 100]), title=None),
        )
        band = alt.Chart(pd.DataFrame({"y": [60], "y2": [85]})).mark_rect(
            opacity=0.10, color=GOLD
        ).encode(y="y:Q", y2="y2:Q")
        line = base.mark_line(color=NAVY, strokeWidth=2.5)
        points = base.mark_point(color=NAVY, filled=True, size=70).encode(
            tooltip=["Siklus", "Skor", "Band"]
        )
        st.altair_chart(band + line + points, width="stretch")
    except Exception:
        st.line_chart(frame.set_index("Siklus")["Skor"])


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def _render_citation(citation: dict[str, Any]) -> str:
    kind = citation.get("kind")
    if kind == "document":
        quote = (citation.get("quote") or "").replace("\n", " ")[:200]
        return f'<span class="cite">[{citation.get("document_id")} hal. {citation.get("page")}]</span> “{quote}”'
    if kind == "computation":
        inputs = ", ".join(f"{k}={idr(v)}" for k, v in (citation.get("inputs") or {}).items())
        result = citation.get("result")
        rendered = f" = {idr(result, 4)}" if isinstance(result, (int, float)) else ""
        return f'<span class="cite">[hitung] {citation.get("formula")}{rendered}</span> <small>({inputs})</small>'
    return (f'<span class="cite">[web tier {citation.get("source_tier")}]</span> '
            f'<a href="{citation.get("url")}" target="_blank">{citation.get("title") or citation.get("url")}</a>')


def findings_panel(result: dict[str, Any]) -> None:
    findings = result.get("findings", []) or []
    surfaced = [f for f in findings if not f.get("suppressed")]
    suppressed = [f for f in findings if f.get("suppressed")]
    dropped = result.get("dropped_hypotheses", []) or []

    st.markdown("### Temuan")
    st.caption(
        "Hipotesis yang bertahan setelah pencarian bukti penyangkal pada bagian lain dokumen. "
        "Setiap temuan disajikan sebagai indikator yang perlu diklarifikasi, bukan sebagai tuduhan."
    )

    if not surfaced:
        st.success("Tidak ada temuan yang melampaui ambang keyakinan pada siklus ini.", icon="✅")

    for finding in surfaced:
        colour = SEVERITY_COLORS.get(finding.get("severity", ""), MUTED)
        st.markdown(
            f"""
            <div class="findingcard" style="--sev:{colour}">
              <h4>[{finding['id']}] {finding['title']}</h4>
              <div class="findingmeta">
                <span class="pill" style="background:{colour}18; color:{colour};">
                  {finding['severity'].upper()}</span>
                &nbsp; keyakinan <b>{finding['confidence']:.2f}</b>
                &nbsp;·&nbsp; pola <code>{finding['pattern']}</code>
              </div>
              <div style="font-size:0.92rem; color:#1E293B; margin-bottom:0.7rem;">{finding['narrative']}</div>
              <div class="question">❓ {finding['question_for_management']}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        with st.expander(f"Bukti dan sitasi — {finding['id']}"):
            st.markdown("**Bukti pendukung**")
            for evidence in finding.get("evidence", []):
                st.markdown(f"- {evidence['summary']}<br/>&nbsp;&nbsp;{_render_citation(evidence['citation'])}",
                            unsafe_allow_html=True)
            counter = finding.get("counter_evidence_considered", [])
            st.markdown("**Bukti penyangkal yang ditelaah**")
            if not counter:
                st.caption("Tidak ditemukan bagian dokumen lain yang menjelaskan anomali ini. "
                           "Ketiadaan bantahan bukan konfirmasi.")
            for evidence in counter:
                st.markdown(f"- {evidence['summary']}<br/>&nbsp;&nbsp;{_render_citation(evidence['citation'])}",
                            unsafe_allow_html=True)

    if dropped:
        st.markdown("### Hipotesis yang digugurkan")
        st.caption(
            "Pola deteksi memunculkan hipotesis berikut, lalu sistem menemukan penjelasan pada "
            "bagian lain dokumen dan menggugurkannya. Langkah falsifikasi inilah yang membedakan "
            "investigasi dari pencocokan pola."
        )
        for hypothesis in dropped:
            st.markdown(
                f"""
                <div class="droppedcard">
                  <b>[{hypothesis.get('id')}] {hypothesis.get('pattern')}</b> — {hypothesis.get('statement','')}
                  <br/><br/>↳ <i>{hypothesis.get('verdict_reason','')}</i>
                </div>
                """,
                unsafe_allow_html=True,
            )

    if suppressed:
        with st.expander(f"Temuan yang ditahan di bawah ambang keyakinan ({len(suppressed)})"):
            st.caption(
                "Ditahan agar tidak menimbulkan alarm palsu, tetapi tetap ditampilkan agar "
                "keputusan kalibrasi dapat ditelaah analis."
            )
            for finding in suppressed:
                st.markdown(
                    f"- **[{finding['id']}] {finding['title']}** "
                    f"(keyakinan {finding['confidence']:.2f}) — {finding.get('suppression_reason','')}"
                )


# ---------------------------------------------------------------------------
# Executive summary, annex, audit trail
# ---------------------------------------------------------------------------


def executive_panel(result: dict[str, Any]) -> None:
    executive = (result.get("report", {}) or {}).get("executive", {})
    if not executive:
        return
    st.markdown("### Ringkasan eksekutif")
    source = (result.get("report", {}).get("meta", {}) or {}).get("narrative_source", "template")
    if source == "template":
        st.caption("Disusun dari templat deterministik (model penalaran tidak tersedia pada siklus ini).")
    st.markdown(f"**{executive.get('headline','')}**")
    st.markdown(executive.get("summary", ""))
    questions = executive.get("questions") or []
    if questions:
        st.markdown("**Hal yang perlu diklarifikasi kepada manajemen**")
        for index, question in enumerate(questions, 1):
            st.markdown(f"{index}. {question}")


def annex_panel(result: dict[str, Any]) -> None:
    st.markdown("### Lampiran teknis")
    st.caption(
        "Setiap angka berasal dari kutipan dokumen bersitasi halaman atau dari perhitungan "
        "Python atas angka bersitasi. Tidak ada jalur ketiga."
    )
    tabs = st.tabs([
        "Angka terekstraksi", "Verifikasi aritmetik", "Indikator turunan",
        "Sumber pasar", "Jejak audit",
    ])

    with tabs[0]:
        rows = []
        for metric in result.get("metrics", []):
            value, scale = metric.get("value"), metric.get("scale", 1.0) or 1.0
            rows.append({
                "Metrik": metric.get("key"),
                "Periode": metric.get("period"),
                "Nilai": compact_idr(value * scale) if value is not None else "—",
                "Status": metric.get("status"),
                "Hal.": metric.get("page"),
                "Strategi": metric.get("strategy"),
                "Baris sumber": (metric.get("source_line") or "")[:110],
            })
        if rows:
            st.dataframe(rows, hide_index=True, width="stretch", height=420)
        unreliable = result.get("unreliable_metrics") or []
        if unreliable:
            st.error(
                "Tidak dapat diekstraksi dengan andal: " + ", ".join(f"`{k}`" for k in unreliable)
                + ". Pola deteksi yang bergantung pada metrik ini tidak dijalankan.",
                icon="⚠️",
            )

    with tabs[1]:
        checks = result.get("verifications", []) or []
        evaluated = [c for c in checks if not c.get("skipped")]
        passed = [c for c in evaluated if c.get("passed")]
        col1, col2, col3 = st.columns(3)
        col1.metric("Identitas dievaluasi", len(evaluated))
        col2.metric("Cocok", len(passed))
        col3.metric("Tidak cocok", len(evaluated) - len(passed))
        st.dataframe(
            [
                {
                    "Identitas": c.get("name"),
                    "Periode": c.get("period"),
                    "Formula": c.get("formula"),
                    "Diharapkan": compact_idr(c.get("expected")) if not c.get("skipped") else "—",
                    "Terhitung": compact_idr(c.get("actual")) if not c.get("skipped") else "—",
                    "Selisih": idr(c.get("difference")) if not c.get("skipped") else "—",
                    "Hasil": ("dilewati: " + (c.get("skip_reason") or "")[:60]) if c.get("skipped")
                             else ("✅ cocok" if c.get("passed") else "❌ tidak cocok"),
                }
                for c in checks
            ],
            hide_index=True, width="stretch", height=380,
        )

    with tabs[2]:
        indicators = [i for i in (result.get("indicators") or []) if i.get("value") is not None]
        st.dataframe(
            [
                {
                    "Indikator": i.get("label"),
                    "Periode": i.get("period"),
                    "Nilai": idr(i.get("value"), 4),
                    "Formula": i.get("formula"),
                    "Input": ", ".join(f"{k}={idr(v)}" for k, v in (i.get("inputs") or {}).items()),
                }
                for i in indicators
            ],
            hide_index=True, width="stretch", height=380,
        )

    with tabs[3]:
        news = result.get("news") or []
        claims = result.get("management_claims") or []
        if not news:
            st.info("Intelijen pasar tidak tersedia pada siklus ini.", icon="ℹ️")
        else:
            st.markdown("**Klaim manajemen yang dapat diuji**")
            if claims:
                st.dataframe(
                    [
                        {
                            "Klaim": c.get("claim", "")[:160],
                            "Jenis": c.get("claim_type"),
                            "Arah": c.get("direction") or "—",
                            "Metrik penguji": ", ".join(c.get("checkable_metrics") or []) or "—",
                            "Tier": c.get("source_tier"),
                            "Sumber": c.get("url"),
                        }
                        for c in claims
                    ],
                    hide_index=True, width="stretch",
                )
            else:
                st.caption("Tidak ada klaim manajemen yang dapat diuji terhadap laporan keuangan.")
            st.markdown("**Sumber dan bobot kredibilitas**")
            st.dataframe(
                [
                    {
                        "Judul": n.get("title", "")[:110],
                        "Tier": n.get("source_tier"),
                        "Bobot": n.get("source_weight"),
                        "Kategori": n.get("source_label"),
                        "Tanggal": n.get("published") or "—",
                        "URL": n.get("url"),
                    }
                    for n in news
                ],
                hide_index=True, width="stretch",
            )

    with tabs[4]:
        events = result.get("audit_trail") or []
        st.caption(f"{len(events)} keputusan tercatat: agen, aksi, tool yang dipanggil, dan hasilnya.")
        st.dataframe(
            [
                {
                    "#": e.get("seq"),
                    "Waktu": str(e.get("timestamp", ""))[11:19],
                    "Agen": e.get("agent"),
                    "Aksi": e.get("action"),
                    "Tool": e.get("tool") or "—",
                    "Keputusan": e.get("decision") or "—",
                    "Detail": (e.get("detail") or "")[:160],
                    "ms": e.get("duration_ms") or "",
                }
                for e in events
            ],
            hide_index=True, width="stretch", height=480,
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_pipeline(config: dict[str, Any]) -> Optional[dict[str, Any]]:
    from agents.orchestrator import run_analysis

    progress = st.progress(0, text="Menyusun rencana investigasi…")
    try:
        progress.progress(15, text="Agen 1 & 2 berjalan paralel: ekstraksi dokumen dan intelijen pasar…")
        result = run_analysis(
            ticker=config["ticker"],
            document_paths=config["paths"],
            company_name=config["company"],
            sector=config["sector"],
            trigger="dashboard",
        )
        progress.progress(100, text="Selesai.")
        return result
    except Exception as exc:
        progress.empty()
        st.error(f"Siklus gagal: {exc}")
        st.exception(exc)
        return None
    finally:
        progress.empty()


def load_from_history(run_id: str) -> Optional[dict[str, Any]]:
    """Rehydrate a previous cycle from its stored JSON report."""
    import json

    settings = get_settings()
    for path in settings.report_dir.glob(f"*_{run_id}.json"):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        meta, annex = report.get("meta", {}), report.get("annex", {})
        return {
            "run_id": run_id,
            "ticker": meta.get("ticker", ""),
            "company_name": meta.get("company_name", ""),
            "sector": meta.get("sector", ""),
            "periods": meta.get("periods", []),
            "documents": meta.get("documents", []),
            "metrics": annex.get("metrics", []),
            "verifications": annex.get("verifications", []),
            "indicators": annex.get("indicators", []),
            "unreliable_metrics": annex.get("unreliable_metrics", []),
            "news": annex.get("news", []),
            "management_claims": annex.get("management_claims", []),
            "audit_trail": annex.get("audit_trail", []) or get_events(run_id),
            "findings": report.get("findings", []) + report.get("suppressed_findings", []),
            "dropped_hypotheses": report.get("dropped_hypotheses", []),
            "risk_score": report.get("executive", {}).get("risk_score", {}),
            "report": report,
            "report_paths": {"markdown": str(path.with_suffix(".md")), "json": str(path)},
        }
    return None


def welcome() -> None:
    st.markdown(
        '<div class="sentinel-brand" style="font-size:2.1rem;">Sentinel<span>-AI</span></div>'
        '<div class="sentinel-sub">Autonomous Financial Due Diligence · Bursa Efek Indonesia</div>',
        unsafe_allow_html=True,
    )
    st.write("")
    st.markdown(
        "Sistem multi-agen yang membaca laporan tahunan emiten, **memverifikasi setiap angka "
        "secara deterministik**, mencari kontradiksi lintas dokumen, lalu **menguji hipotesisnya "
        "terhadap bukti penyangkal** sebelum menyusun Executive Due Diligence Report bersitasi halaman."
    )
    columns = st.columns(4)
    for column, (title, body) in zip(columns, [
        ("Agen 1 · Financial Auditor",
         "Mengekstraksi laporan keuangan dan memverifikasinya dengan Pandas. "
         "Identitas akuntansi yang tidak cocok memicu pembacaan ulang dengan strategi berbeda."),
        ("Agen 2 · Market Intelligence",
         "Mengumpulkan berita dan keterbukaan informasi, memberi bobot kredibilitas sumber, "
         "dan mengekstraksi klaim manajemen yang dapat diuji."),
        ("Agen 3 · Red Flag Investigator",
         "Menjalankan tujuh pola deteksi, lalu mencari bukti penyangkal pada bagian lain "
         "dokumen dan menggugurkan hipotesis yang terbantah."),
        ("Agen 4 · Synthesizer",
         "Menyusun laporan berjenjang, memberi tingkat keyakinan, dan menahan temuan "
         "berkeyakinan rendah agar tidak menimbulkan alarm palsu."),
    ]):
        with column:
            st.markdown(f"**{title}**")
            st.caption(body)
    st.write("")
    st.info("Pilih sumber dokumen di panel kiri, lalu tekan **Jalankan analisis**.", icon="👈")


def main() -> None:
    config = sidebar()

    if config["run"]:
        st.session_state["result"] = run_pipeline(config)
        st.session_state.pop("loaded_run_id", None)
    elif st.session_state.get("loaded_run_id"):
        loaded = load_from_history(st.session_state["loaded_run_id"])
        if loaded:
            st.session_state["result"] = loaded
        st.session_state.pop("loaded_run_id", None)

    result = st.session_state.get("result")
    if not result:
        welcome()
        return

    header(result)
    score_panel(result)
    st.write("")
    executive_panel(result)
    st.write("")
    findings_panel(result)
    st.write("")
    annex_panel(result)

    st.markdown(
        '<div class="disclaimer">Sentinel-AI adalah alat riset dan penyaringan risiko, '
        'bukan penasihat investasi. Sistem tidak memprediksi harga saham dan tidak memberikan '
        'rekomendasi beli, jual, atau tahan. Setiap temuan disajikan sebagai indikator yang perlu '
        'diklarifikasi, disertai tautan ke halaman dokumen sumbernya. Keputusan tetap berada '
        'pada pengguna.</div>',
        unsafe_allow_html=True,
    )


main()
