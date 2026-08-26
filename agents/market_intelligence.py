"""Agent 2 — Market Intelligence Analyst.

Runs in parallel with Agent 1 and answers a different question: what is being
*said* about this entity, by whom, and how much should that source count?

Two design choices carry the weight:

* **Credibility is attached at the source, not argued about later.** Every item
  arrives with a tier and a weight from ``core.config``. Agent 3 multiplies
  evidence weight by it, so a claim sourced from a forum post can raise a
  hypothesis but can never, on its own, carry one to a finding.
* **Claims are extracted in a checkable shape.** The point of collecting
  management statements is to cross-examine them against verified numbers, so
  each claim records which metrics would confirm or contradict it and in which
  direction. A statement that cannot be checked against the financials is still
  recorded, but it is typed as such and does not feed the contradiction test.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from core.audit_trail import AuditTrail
from core.llm import LLMClient, get_llm
from core.state import METRIC_VOCABULARY, ManagementClaim, NewsItem
from tools.web_search import WebSearchTool, build_queries

log = logging.getLogger("sentinel.agent2")

AGENT = "market_intelligence"

_CLAIM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {
                        "type": "string",
                        "description": "Parafrase ringkas klaim manajemen, maksimal 2 kalimat.",
                    },
                    "speaker": {"type": "string", "description": "Nama/jabatan pihak yang menyatakan."},
                    "source_index": {
                        "type": "integer",
                        "description": "Nomor sumber pada daftar yang diberikan.",
                    },
                    "claim_type": {
                        "type": "string",
                        "enum": ["kinerja", "prospek", "likuiditas", "ekspansi", "tata_kelola", "lainnya"],
                    },
                    "checkable_metrics": {
                        "type": "array",
                        "description": "Metrik keuangan yang dapat menguji klaim ini.",
                        "items": {"type": "string"},
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["naik", "turun", "stabil", "positif", "negatif"],
                        "description": "Arah yang diklaim untuk metrik tersebut.",
                    },
                },
                "required": ["claim", "source_index", "claim_type"],
            },
        },
        "themes": {
            "type": "array",
            "description": "Tema utama pemberitaan periode ini.",
            "items": {"type": "string"},
        },
    },
    "required": ["claims"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = """Anda adalah analis intelijen pasar untuk emiten Bursa Efek Indonesia.

Dari kutipan berita dan keterbukaan informasi yang diberikan, ekstraksi
pernyataan manajemen atau perusahaan yang DAPAT DIUJI terhadap laporan keuangan.

Aturan:
1. Ambil hanya pernyataan yang benar-benar ada pada kutipan. Jangan menyimpulkan
   atau melengkapi dari pengetahuan Anda tentang perusahaan tersebut.
2. Setiap klaim wajib menunjuk source_index dari daftar sumber.
3. checkable_metrics harus dipilih dari daftar metrik yang diberikan. Jika tidak
   ada metrik yang relevan, kosongkan.
4. Jangan menyertakan opini analis, target harga, atau spekulasi pasar sebagai
   klaim manajemen.
5. Abaikan pernyataan yang tidak spesifik ("berkomitmen pada pertumbuhan").
"""


class MarketIntelligenceAnalyst:
    """Agent 2. Search, weight, and extract checkable claims."""

    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        trail: Optional[AuditTrail] = None,
        search: Optional[WebSearchTool] = None,
    ):
        self.llm = llm or get_llm()
        self.trail = trail
        self.search = search or WebSearchTool()

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        ticker = state.get("ticker", "")
        company = state.get("company_name") or ticker
        sector = state.get("sector", "")

        queries = build_queries(ticker, company, sector)
        self._log(events, "susun_kueri", decision=f"{len(queries)} kueri",
                  detail="; ".join(queries[:3]) + (" ..." if len(queries) > 3 else ""),
                  payload={"queries": queries})

        if not self.search.usable_for(ticker):
            self._log(events, "pencarian_dilewati", tool="tavily",
                      decision="tidak tersedia",
                      detail="TAVILY_API_KEY belum diatur atau mode offline aktif; "
                             "laporan akan menyatakan cakupan pasar tidak tersedia")
            return {
                "news": [], "management_claims": [], "market_status": "unavailable",
                "audit_trail": events,
                "errors": ["Agent 2: pencarian web tidak tersedia; intelijen pasar dilewati."],
            }

        items, call_log = self.search.multi_search(queries, max_results=5)
        by_tier: dict[int, int] = {}
        for item in items:
            by_tier[item.source_tier] = by_tier.get(item.source_tier, 0) + 1

        self._log(events, "pencarian_web", tool="tavily",
                  decision=f"{len(items)} sumber unik",
                  detail="distribusi kredibilitas: " + ", ".join(
                      f"tier {t}: {n}" for t, n in sorted(by_tier.items())) or "—",
                  payload={"calls": call_log, "tier_distribution": by_tier})

        if not items:
            return {
                "news": [], "management_claims": [], "market_status": "empty",
                "audit_trail": events,
            }

        claims = self._extract_claims(items, events)
        self._log(events, "ekstraksi_klaim",
                  decision=f"{len(claims)} klaim dapat diuji",
                  detail="; ".join(c.claim[:80] for c in claims[:3]) or "tidak ada",
                  payload={"claims": [c.model_dump(mode="json") for c in claims]})

        return {
            "news": [i.model_dump(mode="json") for i in items],
            "management_claims": [c.model_dump(mode="json") for c in claims],
            "market_status": "done",
            "audit_trail": events,
        }

    # -- claim extraction ----------------------------------------------------

    def _extract_claims(self, items: list[NewsItem], events: list[dict[str, Any]]) -> list[ManagementClaim]:
        if not self.llm.available:
            self._log(events, "ekstraksi_klaim_dilewati", tool="llm",
                      decision="mode deterministik",
                      detail="tanpa LLM, klaim manajemen tidak diekstraksi; berita tetap dicatat sebagai konteks")
            return []

        # Only credible sources are shown to the model. Forum chatter is kept in
        # the news list as context but is not allowed to become a "claim".
        sourced = [i for i in items if i.source_tier <= 2][:14]
        if not sourced:
            return []

        listing = "\n\n".join(
            f"[{index}] ({item.source_label}, tier {item.source_tier}) {item.title}\n"
            f"URL: {item.url}\nTanggal: {item.published or 'tidak tercantum'}\n"
            f"Kutipan: {item.snippet[:900]}"
            for index, item in enumerate(sourced)
        )
        metric_list = ", ".join(sorted(METRIC_VOCABULARY.keys()))

        result = self.llm.structured(
            purpose="agent2_claims",
            system=_SYSTEM_PROMPT,
            user=(
                f"Metrik yang tersedia untuk pengujian:\n{metric_list}\n\n"
                f"Sumber:\n{listing}\n\n"
                "Ekstraksi klaim manajemen yang dapat diuji terhadap laporan keuangan."
            ),
            schema=_CLAIM_SCHEMA,
            tool_name="submit_claims",
            tool_description="Kirim daftar klaim manajemen yang dapat diuji.",
            max_tokens=6000,
        )
        if not result.ok:
            self._log(events, "panggil_model", tool="llm", decision="gagal",
                      detail=result.error or "tidak ada respons")
            return []

        claims: list[ManagementClaim] = []
        for entry in result.data.get("claims") or []:
            index = entry.get("source_index")
            if not isinstance(index, int) or not (0 <= index < len(sourced)):
                continue  # a claim without a resolvable source is not usable
            source = sourced[index]
            metrics = [m for m in (entry.get("checkable_metrics") or []) if m in METRIC_VOCABULARY]
            claims.append(ManagementClaim(
                claim=str(entry.get("claim", ""))[:600],
                speaker=entry.get("speaker"),
                url=source.url,
                published=source.published,
                source_tier=source.source_tier,
                source_weight=source.source_weight,
                claim_type=entry.get("claim_type", "lainnya"),
                checkable_metrics=metrics,
                direction=entry.get("direction"),
            ))
        return claims

    def _log(self, events: list[dict[str, Any]], action: str, **kwargs: Any) -> None:
        if self.trail:
            events.append(self.trail.log(AGENT, action, **kwargs))
        else:
            events.append({"agent": AGENT, "action": action, **kwargs})


def market_intelligence_node(state: dict[str, Any], *, llm=None, trail=None, search=None) -> dict[str, Any]:
    """LangGraph node entrypoint."""
    return MarketIntelligenceAnalyst(llm=llm, trail=trail, search=search).run(state)
