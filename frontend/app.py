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

import html
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

# Design tokens. Mirrors the palette in sentinel-ai-dashboard.html so the
# Streamlit surface and the static mockup read as one product.
NAVY = "#0F1E4A"
GOLD = "#C8A85A"
IVORY = "#F7F8FA"
GREEN = "#10b981"
RED = "#ef4444"
TEXT = "#0F1E4A"
MUTED = "rgba(15,30,74,.45)"
BORDER = "rgba(15,30,74,.09)"
RADIUS = "12px"
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, monospace"

SEVERITY_COLORS = {
    "kritis": "#dc2626",
    "tinggi": RED,
    "sedang": GOLD,
    "rendah": "#0891b2",
}
BAND_COLORS = {"Low Risk": GREEN, "Medium Risk": GOLD, "High Risk": RED}

# Agent → timeline column (1-4) and accent colour. Agents outside this map are
# rendered full-width, like the orchestrator, so an added agent still shows up.
AGENT_COLUMNS = {
    "financial_auditor": 1,
    "market_intelligence": 2,
    "red_flag_investigator": 3,
    "synthesizer": 4,
}
AGENT_COLORS = {
    "financial_auditor": "#1d4ed8",
    "market_intelligence": "#0891b2",
    "red_flag_investigator": RED,
    "synthesizer": GOLD,
    "orchestrator": NAVY,
}

st.markdown(
    f"""
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');

      /* Streamlit chrome. Targeted by data-testid — class names change between versions.
         The header itself stays: it hosts the "expand sidebar" button, which is the only
         way back once the sidebar is collapsed. Hiding the whole header stranded the user
         with no control to reopen it, so only the toolbar's right-hand actions go. */
      [data-testid="stToolbarActions"], [data-testid="stMainMenu"],
      [data-testid="stStatusWidget"], [data-testid="stAppDeployButton"],
      #MainMenu, footer {{ display: none; }}

      /* Belt and braces across Streamlit versions: the control is called
         stExpandSidebarButton on 1.5x+ and collapsedControl on older builds. */
      [data-testid="stExpandSidebarButton"],
      [data-testid="collapsedControl"] {{ display: flex !important; visibility: visible !important; }}

      /* Give the expand control the same card language as the rest of the surface so it
         reads as a control rather than a stray glyph floating over the brand line. */
      [data-testid="stExpandSidebarButton"] {{
        background: #FFFFFF; border: 1px solid var(--border); border-radius: 8px;
        box-shadow: 0 1px 2px rgba(15,30,74,.06); transition: background .15s, border-color .15s;
      }}
      [data-testid="stExpandSidebarButton"]:hover {{
        background: var(--ivory); border-color: var(--gold);
      }}

      /* Keep the sidebar's own collapse arrow permanently visible instead of hover-only,
         so the toggle is discoverable in both directions. */
      [data-testid="stSidebarCollapseButton"] {{ display: flex !important; opacity: 1 !important;
                                                visibility: visible !important; }}

      :root {{
        --navy: {NAVY}; --gold: {GOLD}; --ivory: {IVORY};
        --green: {GREEN}; --red: {RED};
        --text: {TEXT}; --muted: {MUTED}; --border: {BORDER};
        --radius: {RADIUS};
      }}

      html, body, [data-testid="stAppViewContainer"], [data-testid="stSidebar"],
      .stMarkdown, .stButton button, [data-testid="stMetricValue"],
      [data-testid="stMetricLabel"] {{
        font-family: 'Inter', ui-sans-serif, system-ui, -apple-system, sans-serif;
        -webkit-font-smoothing: antialiased;
      }}

      .block-container {{ padding-top: 2.2rem; max-width: 1400px; }}
      /* The header is position:absolute, so it paints over the top of the page. It only
         gains content (the expand button) while the sidebar is collapsed, so only that
         state pays for the extra headroom. */
      .stApp:has([data-testid="stExpandSidebarButton"]) .block-container {{ padding-top: 4.2rem; }}
      .sentinel-brand {{ font-size: 1.45rem; font-weight: 600; color: var(--navy); letter-spacing:-0.02em; }}
      .sentinel-brand span {{ color: var(--gold); }}
      .sentinel-sub {{ font-size: 0.66rem; letter-spacing: 0.14em; color: var(--muted);
                       text-transform: uppercase; font-weight: 500; }}
      .crumb {{ font-size: 0.8rem; color: var(--muted); }}
      .entity {{ font-size: 2.4rem; font-weight: 600; color: var(--text); letter-spacing: -0.03em; margin: 0.2rem 0 0 0; }}
      .entity small {{ font-size: 1.15rem; color: var(--muted); font-weight: 400; }}
      .metaline {{ font-size: 0.83rem; color: var(--muted); margin-bottom: 1.2rem; }}
      .metaline b {{ color: var(--text); font-weight: 500; }}

      .scorecard {{ background: var(--navy); border-radius: var(--radius);
                    padding: 1.9rem 2rem; color: rgba(255,255,255,.55); }}
      .scorecard .label {{ font-size: 0.69rem; letter-spacing: 0.16em; color: var(--gold);
                           text-transform: uppercase; font-weight: 600; }}
      .scorecard .caption {{ font-size: 0.82rem; color: rgba(255,255,255,.45); margin-top: 0.3rem; }}
      .scorecard .stat {{ font-size: 1.35rem; font-weight: 600; color: #FFFFFF; line-height: 1.1;
                          font-variant-numeric: tabular-nums; }}
      .scorecard .statlabel {{ font-size: 0.72rem; color: rgba(255,255,255,.40); margin-top: 0.15rem; }}
      .scorecard .rule {{ border: 0; border-top: 1px solid rgba(255,255,255,.10); margin: 1.4rem 0 1.1rem 0; }}

      .pill {{ display:inline-block; padding: 0.3rem 0.8rem; border-radius: 999px;
               font-size: 0.76rem; font-weight: 600; }}

      .findingcard {{ border: 1px solid var(--border); border-left: 3px solid var(--sev);
                      border-radius: var(--radius); padding: 1.35rem 1.5rem; margin-bottom: 1rem;
                      background: #FFFFFF; }}
      .findingcard h4 {{ margin: 0 0 0.45rem 0; color: var(--text); font-size: 0.95rem; font-weight: 600;
                         letter-spacing: -0.01em; }}
      .findingmeta {{ font-size: 0.76rem; color: var(--muted); margin-bottom: 0.7rem; }}
      .question {{ background: var(--ivory); border-left: 3px solid var(--gold);
                   padding: 0.7rem 0.9rem; font-size: 0.84rem; color: var(--text); border-radius: 6px; }}
      .droppedcard {{ border: 1px solid var(--border); border-radius: var(--radius);
                      padding: 1.15rem 1.35rem; margin-bottom: 0.8rem; background: var(--ivory);
                      color: var(--muted); font-size: 0.85rem; line-height: 1.55; }}
      .droppedcard b {{ color: var(--text); font-weight: 600; }}
      .disclaimer {{ font-size: 0.78rem; color: var(--muted); border-top: 1px solid var(--border);
                     padding-top: 1.1rem; margin-top: 2rem; line-height: 1.6; }}
      .cite {{ font-family: {MONO}; font-size: 0.74rem; color: var(--muted); }}

      /* ── Agent run timeline (annex tab 5) ──────────────────────────────
         Four equal columns, one per parallel agent. Cards keep their seq
         order top-to-bottom, so agents that ran at the same time sit at the
         same vertical position and idle stretches read as blank column. */
      .tl-meta {{ font-size: 0.78rem; color: var(--muted); margin-bottom: 0.85rem; }}
      .tl-meta b {{ color: var(--text); font-weight: 500; }}
      .tl-head {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.75rem;
                  margin-bottom: 0.75rem; }}
      .tl-head div {{ border-top: 2px solid var(--agent); padding-top: 0.5rem; }}
      .tl-head b {{ display: block; font-size: 0.7rem; font-weight: 600; letter-spacing: 0.1em;
                    text-transform: uppercase; color: var(--agent); }}
      .tl-head span {{ display: block; font-size: 0.72rem; color: var(--muted); margin-top: 0.15rem; }}
      .tl-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.55rem 0.75rem; }}
      .tl-rail {{ grid-row: 1 / -1; background-image: linear-gradient(var(--border), var(--border));
                  background-size: 1px 100%; background-position: center top;
                  background-repeat: no-repeat; }}
      .tl-card {{ position: relative; z-index: 1; background: #FFFFFF;
                  border: 1px solid var(--border); border-left: 3px solid var(--agent);
                  border-radius: var(--radius); padding: 0.75rem 0.9rem 0.8rem 0.9rem; }}
      .tl-seq {{ position: absolute; top: 0.7rem; right: 0.85rem; font-family: {MONO};
                 font-size: 11px; color: var(--muted); }}
      .tl-action {{ font-size: 13px; font-weight: 600; color: var(--text); padding-right: 3rem;
                    letter-spacing: -0.005em; }}
      .tl-tool {{ display: inline-block; margin-top: 0.4rem; font-family: {MONO}; font-size: 10.5px;
                  padding: 0.12rem 0.42rem; border-radius: 5px; background: rgba(15,30,74,.05);
                  color: var(--agent); }}
      .tl-decision {{ font-size: 13px; color: var(--text); margin-top: 0.4rem; line-height: 1.4; }}
      .tl-detail {{ font-size: 12px; color: var(--muted); margin-top: 0.3rem; line-height: 1.45;
                    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
                    overflow: hidden; }}
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
                 + (f' · <b style="color:{RED}">{unreliable} tidak dapat diekstraksi</b>' if unreliable else ""))
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
            stroke="rgba(255,255,255,.12)" stroke-width="11" stroke-linecap="round"/>
      <path d="M 25 100 A {radius} {radius} 0 0 1 165 100" fill="none"
            stroke="{colour}" stroke-width="11" stroke-linecap="round"
            stroke-dasharray="{filled:.1f} {circumference:.1f}"/>
      <text x="95" y="94" text-anchor="middle" fill="#FFFFFF" letter-spacing="-1.5"
            font-size="42" font-weight="600" font-family="Inter, system-ui">{score:.0f}</text>
      <text x="95" y="110" text-anchor="middle" fill="rgba(255,255,255,.35)"
            font-size="12" font-family="Inter, system-ui">/100</text>
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
                  <span class="pill" style="background:{colour}26; color:{colour};
                        box-shadow: inset 0 0 0 1px {colour}4D;">● {band}</span>
                  <div class="caption" style="margin-top:0.7rem;">
                    Skor terbalik — makin tinggi makin aman.<br/>
                    High &lt; 60 &nbsp;·&nbsp; Medium 60–84 &nbsp;·&nbsp; Low ≥ 85
                  </div>
                </div>
              </div>
              <hr class="rule"/>
              <div style="display:flex; gap:2.6rem;">
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
                <span class="pill" style="background:{colour}1F; color:{colour};">
                  {finding['severity'].upper()}</span>
                &nbsp; keyakinan <b>{finding['confidence']:.2f}</b>
                &nbsp;·&nbsp; pola <code>{finding['pattern']}</code>
              </div>
              <div style="font-size:0.86rem; color:rgba(15,30,74,.72); line-height:1.6; margin-bottom:0.85rem;">{finding['narrative']}</div>
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


def _timeline_layout(events: list[dict[str, Any]]) -> tuple[list[tuple[dict, int, int, int]], int]:
    """Place audit events on a four-column grid, preserving `seq` order.

    This is the CSS grid sparse auto-placement algorithm run in Python rather
    than left to the browser, so the row of every card is explicit and the
    layout is identical everywhere. The consequence that matters: two agents
    whose events are adjacent in `seq` land on the same row — parallel work
    reads as parallel — while an agent that stops emitting events leaves its
    column blank instead of having its later cards float upwards.

    Returns ``([(event, column_start, span, row)], row_count)``.
    """
    placed: list[tuple[dict, int, int, int]] = []
    occupied: set[tuple[int, int]] = set()
    row, next_col = 1, 1

    for event in sorted(events, key=lambda e: e.get("seq") or 0):
        column = AGENT_COLUMNS.get(event.get("agent"))
        start, span = (column, 1) if column else (1, 4)
        if start < next_col:
            row += 1
        while any((row, start + offset) in occupied for offset in range(span)):
            row += 1
        for offset in range(span):
            occupied.add((row, start + offset))
        placed.append((event, start, span, row))
        next_col = start + span

    return placed, (max(r for _, _, _, r in placed) if placed else 1)


def _timeline_card(event: dict[str, Any], start: int, span: int, row: int) -> str:
    colour = AGENT_COLORS.get(event.get("agent"), MUTED)
    style = (f"--agent:{colour}; grid-column:{start} / span {span}; grid-row:{row};"
             if span == 1 else f"--agent:{colour}; grid-column:1 / -1; grid-row:{row};")
    escape = html.escape

    parts = [f'<div class="tl-card" style="{style}">',
             f'<span class="tl-seq">[{int(event.get("seq") or 0):03d}]</span>',
             f'<div class="tl-action">{escape(str(event.get("action") or ""))}</div>']
    if event.get("tool"):
        parts.append(f'<span class="tl-tool">{escape(str(event["tool"]))}</span>')
    if event.get("decision"):
        parts.append(f'<div class="tl-decision">{escape(str(event["decision"]))}</div>')
    if event.get("detail"):
        parts.append(f'<div class="tl-detail">{escape(str(event["detail"]))}</div>')
    parts.append("</div>")
    return "".join(parts)


def agent_timeline(result: dict[str, Any]) -> None:
    """The audit trail as a run timeline: one column per parallel agent."""
    events = result.get("audit_trail") or []
    if not events:
        st.info("Jejak audit tidak tersedia untuk siklus ini.", icon="ℹ️")
        return

    trigger = (result.get("trigger")
               or result.get("report", {}).get("meta", {}).get("trigger", "manual"))
    run_id = (result.get("run_id")
              or result.get("report", {}).get("meta", {}).get("run_id", ""))

    counts: dict[str, int] = {}
    for event in events:
        agent = event.get("agent") or "—"
        counts[agent] = counts.get(agent, 0) + 1

    spanning = [a for a in counts if a not in AGENT_COLUMNS]
    st.markdown(
        f'<div class="tl-meta">Siklus <b>{html.escape(str(run_id))}</b> &nbsp;·&nbsp; '
        f'pemicu <b>{html.escape(str(trigger))}</b> &nbsp;·&nbsp; '
        f'<b>{len(events)}</b> keputusan tercatat &nbsp;·&nbsp; '
        + " &nbsp;·&nbsp; ".join(
            f'<b style="color:{AGENT_COLORS.get(a, MUTED)}">{html.escape(a.replace("_", " "))}</b> '
            f'{counts[a]} langkah (melintasi kolom)'
            for a in spanning
        )
        + "</div>",
        unsafe_allow_html=True,
    )

    columns = {column: agent for agent, column in AGENT_COLUMNS.items()}
    heads = "".join(
        f'<div style="--agent:{AGENT_COLORS.get(columns.get(index), MUTED)}">'
        f'<b>{html.escape((columns.get(index) or "—").replace("_", " "))}</b>'
        f'<span>{counts.get(columns.get(index), 0)} langkah</span></div>'
        for index in range(1, 5)
    )

    placed, rows = _timeline_layout(events)
    rails = "".join(
        f'<div class="tl-rail" style="grid-column:{index}; grid-row:1 / -1;"></div>'
        for index in range(1, 5)
    )
    cards = "".join(_timeline_card(*item) for item in placed)

    st.markdown(
        f'<div class="tl-head">{heads}</div>'
        f'<div class="tl-grid" style="grid-template-rows: repeat({rows}, auto);">'
        f"{rails}{cards}</div>",
        unsafe_allow_html=True,
    )


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
        st.caption(
            "Tujuan → rencana → tool → hasil → keputusan, satu kolom per agen. "
            "Kartu yang sejajar berjalan pada saat yang sama; kolom yang kosong "
            "berarti agen tersebut sedang menganggur."
        )
        agent_timeline(result)
        with st.expander("Tabel jejak audit (mentah)"):
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
