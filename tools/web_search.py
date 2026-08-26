"""Web search for Agent 2 — Tavily, with source credibility weighting.

Agent 2's output is only useful if downstream agents know how much to trust it.
A press release on idx.co.id and a post on a retail forum are both "sources",
and a system that treats them alike will eventually escalate a rumour into a
finding. So every result leaves this module carrying a tier and a weight from
``core.config.CREDIBILITY_TIERS``, and Agent 3 multiplies evidence weight by it.

When no API key is configured the module returns an empty result set with
``ok=False`` rather than inventing coverage — a run with no market intelligence
is reported as such.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from core.config import PROJECT_ROOT, credibility_for, get_settings
from core.state import NewsItem

log = logging.getLogger("sentinel.search")

FIXTURE_DIR = PROJECT_ROOT / "data" / "fixtures"


@dataclass
class SearchResponse:
    query: str
    items: list[NewsItem] = field(default_factory=list)
    ok: bool = True
    provider: str = "tavily"
    error: Optional[str] = None


class WebSearchTool:
    """Thin, weighted wrapper over the Tavily Search API."""

    def __init__(self, api_key: Optional[str] = None, offline: Optional[bool] = None):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.tavily_api_key
        self.offline = settings.offline if offline is None else offline
        self._client: Any = None

    @property
    def available(self) -> bool:
        return bool(self.api_key) and not self.offline

    def has_fixtures(self, ticker: str) -> bool:
        """Offline demo coverage for this ticker, if any was bundled."""
        return bool(ticker) and (FIXTURE_DIR / f"news_{ticker.upper()}.json").exists()

    def usable_for(self, ticker: str) -> bool:
        """Whether Agent 2 can produce anything at all for this entity."""
        return self.available or self.has_fixtures(ticker)

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from tavily import TavilyClient

            self._client = TavilyClient(api_key=self.api_key)
        except Exception as exc:  # pragma: no cover
            log.warning("Klien Tavily tidak tersedia: %s", exc)
            self._client = None
        return self._client

    # -----------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        max_results: int = 6,
        days: Optional[int] = 400,
        include_domains: Optional[list[str]] = None,
        topic: str = "general",
    ) -> SearchResponse:
        if self.offline:
            fixtures = _load_fixture(query)
            if fixtures is not None:
                return SearchResponse(query=query, items=fixtures, provider="fixture")
            return SearchResponse(query=query, items=[], ok=False, provider="offline",
                                  error="Mode offline: pencarian web dinonaktifkan.")
        if not self.api_key:
            return SearchResponse(query=query, items=[], ok=False, provider="none",
                                  error="TAVILY_API_KEY belum dikonfigurasi.")

        client = self._ensure_client()
        if client is None:
            return SearchResponse(query=query, items=[], ok=False, provider="tavily",
                                  error="Klien Tavily gagal diinisialisasi.")

        kwargs: dict[str, Any] = {
            "query": query,
            "max_results": max_results,
            "search_depth": "advanced",
            "topic": topic,
        }
        if days and topic == "news":
            kwargs["days"] = days
        if include_domains:
            kwargs["include_domains"] = include_domains

        try:
            raw = client.search(**kwargs)
        except Exception as exc:
            log.warning("Pencarian Tavily gagal untuk '%s': %s", query, exc)
            return SearchResponse(query=query, items=[], ok=False, error=str(exc))

        items = [_to_news_item(result, query) for result in (raw.get("results") or [])]
        # Highest-credibility sources first, so downstream truncation drops
        # forum chatter before it drops an exchange filing.
        items.sort(key=lambda item: (item.source_weight, item.published or ""), reverse=True)
        return SearchResponse(query=query, items=items)

    def multi_search(self, queries: list[str], max_results: int = 5) -> tuple[list[NewsItem], list[dict[str, Any]]]:
        """Run several queries, de-duplicate by URL, and return a call log."""
        seen: set[str] = set()
        merged: list[NewsItem] = []
        call_log: list[dict[str, Any]] = []

        for query in queries:
            topic = "news" if any(w in query.lower() for w in ("berita", "news", "terbaru")) else "general"
            response = self.search(query, max_results=max_results, topic=topic)
            call_log.append({
                "query": query,
                "ok": response.ok,
                "provider": response.provider,
                "results": len(response.items),
                "error": response.error,
            })
            for item in response.items:
                if item.url in seen:
                    continue
                seen.add(item.url)
                merged.append(item)

        merged.sort(key=lambda item: item.source_weight, reverse=True)
        return merged, call_log


def build_queries(ticker: str, company_name: str, sector: str = "") -> list[str]:
    """The standing question set Agent 2 asks about any monitored entity.

    Queries are chosen to surface things that can later be *cross-checked
    against the financials* — management guidance, disclosure filings,
    auditor changes — rather than general sentiment.
    """
    name = company_name or ticker
    queries = [
        f"{name} {ticker} keterbukaan informasi BEI terbaru",
        f"{name} kinerja keuangan laporan tahunan terbaru",
        f"{name} pernyataan direksi manajemen target kinerja",
        f"{ticker} saham berita terbaru",
        f"{name} auditor opini laporan keuangan",
        f"{name} utang likuiditas restrukturisasi",
    ]
    if sector:
        queries.append(f"{name} {sector} prospek industri Indonesia")
    return queries


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_news_item(result: dict[str, Any], query: str) -> NewsItem:
    url = result.get("url", "")
    tier, weight, label = credibility_for(url)
    return NewsItem(
        title=result.get("title") or url,
        url=url,
        snippet=(result.get("content") or "")[:1200],
        published=result.get("published_date"),
        source_tier=tier,
        source_weight=weight,
        source_label=label,
        query=query,
    )


def _load_fixture(query: str) -> Optional[list[NewsItem]]:
    """Offline demo support: bundled search results keyed by ticker."""
    if not FIXTURE_DIR.exists():
        return None
    for path in sorted(FIXTURE_DIR.glob("news_*.json")):
        ticker = path.stem.replace("news_", "").upper()
        if ticker and ticker.lower() in query.lower():
            try:
                payload = json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            items = []
            for entry in payload:
                tier, weight, label = credibility_for(entry.get("url", ""))
                items.append(NewsItem(
                    title=entry.get("title", ""), url=entry.get("url", ""),
                    snippet=entry.get("snippet", ""), published=entry.get("published"),
                    source_tier=tier, source_weight=weight, source_label=label, query=query,
                ))
            return items
    return None
