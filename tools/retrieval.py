"""Retrieval over the filing corpus — LlamaIndex + Chroma, with a BM25 fallback.

Agent 3 depends on this. Forming a hypothesis is cheap; the expensive and
distinguishing step is going back into the *other* sections of the same filing
to look for the passage that explains the anomaly away. That search has to work
even when the vector stack is unavailable, so the module has two backends and
picks the best one it can actually construct:

* ``chroma``  — LlamaIndex ``VectorStoreIndex`` over a persisted Chroma
  collection. Semantic; catches paraphrase ("penjualan tempo" for receivables).
* ``bm25``    — pure-Python lexical ranking, no downloads, no network. Weaker
  on paraphrase, but Indonesian statement captions are highly standardised, so
  it degrades far less than it would on free prose.

Whichever backend is used is recorded in the audit trail, because a finding
retrieved by lexical search deserves slightly less confidence than one that
survived a semantic counter-evidence sweep.

Upgrade path: swap ``ChromaVectorStore`` for ``PGVectorStore`` and point it at
PostgreSQL; nothing above ``DocumentIndex`` changes.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from core.config import get_settings
from tools.document_parser import ParsedDocument, ParsedPage

log = logging.getLogger("sentinel.retrieval")

_TOKEN = re.compile(r"[a-zA-Z0-9]+")

# Indonesian stopwords that carry no retrieval signal in filings.
_STOPWORDS = {
    "yang", "dan", "atau", "dari", "pada", "untuk", "dengan", "dalam", "ini",
    "itu", "adalah", "akan", "telah", "tidak", "oleh", "sebagai", "juga",
    "atas", "ke", "di", "the", "of", "and", "to", "in", "a", "is", "for",
}


@dataclass
class Passage:
    """A retrievable chunk that never loses its page number."""

    text: str
    doc_id: str
    page: int
    statement_type: str
    score: float = 0.0
    chunk_id: str = ""

    def citation_payload(self) -> dict[str, Any]:
        return {
            "kind": "document",
            "document_id": self.doc_id,
            "page": self.page,
            "quote": self.text.strip()[:400],
        }


@dataclass
class DocumentIndex:
    """Chunked, searchable view of one or more parsed filings."""

    passages: list[Passage] = field(default_factory=list)
    backend: str = "bm25"
    collection_name: str = "sentinel"
    _df: Counter = field(default_factory=Counter, repr=False)
    _tokenised: list[list[str]] = field(default_factory=list, repr=False)
    _avg_len: float = 1.0
    _vector_index: Any = field(default=None, repr=False)

    # -- construction -------------------------------------------------------

    @classmethod
    def build(
        cls,
        documents: Iterable[ParsedDocument],
        *,
        chunk_chars: int = 1400,
        overlap_chars: int = 200,
        prefer_vector: bool = True,
        persist_dir: Optional[Path] = None,
        collection_name: str = "sentinel",
    ) -> "DocumentIndex":
        passages: list[Passage] = []
        for doc in documents:
            for page in doc.pages:
                passages.extend(_chunk_page(doc.doc_id, page, chunk_chars, overlap_chars))

        index = cls(passages=passages, collection_name=collection_name)
        index._prepare_bm25()

        if prefer_vector and passages:
            vector_index = _try_build_vector_index(passages, persist_dir, collection_name)
            if vector_index is not None:
                index._vector_index = vector_index
                index.backend = "chroma"
        log.info("Indeks retrieval siap: %d passage, backend=%s", len(passages), index.backend)
        return index

    # -- BM25 ---------------------------------------------------------------

    def _prepare_bm25(self) -> None:
        self._tokenised = [_tokenise(p.text) for p in self.passages]
        self._df = Counter()
        for tokens in self._tokenised:
            self._df.update(set(tokens))
        lengths = [len(t) for t in self._tokenised] or [1]
        self._avg_len = sum(lengths) / len(lengths)

    def _bm25(self, query: str, k: int, page_filter: Optional[set[int]] = None) -> list[Passage]:
        query_tokens = _tokenise(query)
        if not query_tokens or not self.passages:
            return []
        total = len(self.passages)
        k1, b = 1.5, 0.75
        scored: list[tuple[float, Passage]] = []

        for passage, tokens in zip(self.passages, self._tokenised):
            if page_filter is not None and passage.page not in page_filter:
                continue
            if not tokens:
                continue
            counts = Counter(tokens)
            length = len(tokens)
            score = 0.0
            for term in query_tokens:
                freq = counts.get(term, 0)
                if freq == 0:
                    continue
                idf = math.log(1 + (total - self._df[term] + 0.5) / (self._df[term] + 0.5))
                score += idf * (freq * (k1 + 1)) / (freq + k1 * (1 - b + b * length / self._avg_len))
            if score > 0:
                scored.append((score, passage))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        results = []
        for score, passage in scored[:k]:
            hit = Passage(**{**passage.__dict__, "score": round(score, 4)})
            results.append(hit)
        return results

    # -- public search ------------------------------------------------------

    def search(self, query: str, k: int = 5, exclude_pages: Optional[Iterable[int]] = None) -> list[Passage]:
        """Rank passages against a query.

        ``exclude_pages`` is what makes counter-evidence search meaningful: when
        Agent 3 tests a hypothesis raised by the cash-flow statement, it excludes
        that page so the search must find corroboration or contradiction
        *elsewhere* in the filing.
        """
        excluded = set(exclude_pages or [])
        hits: list[Passage] = []

        if self._vector_index is not None:
            try:
                hits = self._vector_search(query, k + len(excluded) + 2)
            except Exception as exc:  # pragma: no cover - backend runtime failure
                log.warning("Pencarian vektor gagal (%s); memakai BM25.", exc)
                hits = []
        if not hits:
            hits = self._bm25(query, k + len(excluded) + 2)

        filtered = [h for h in hits if h.page not in excluded]
        return filtered[:k]

    def _vector_search(self, query: str, k: int) -> list[Passage]:
        retriever = self._vector_index.as_retriever(similarity_top_k=k)
        nodes = retriever.retrieve(query)
        results: list[Passage] = []
        for node in nodes:
            meta = getattr(node, "metadata", None) or getattr(node.node, "metadata", {})
            results.append(Passage(
                text=node.get_content(),
                doc_id=str(meta.get("doc_id", "")),
                page=int(meta.get("page", 0) or 0),
                statement_type=str(meta.get("statement_type", "")),
                score=round(float(getattr(node, "score", 0.0) or 0.0), 4),
                chunk_id=str(meta.get("chunk_id", "")),
            ))
        return results

    def search_sections(self, query: str, statement_types: Iterable[str], k: int = 4) -> list[Passage]:
        """Search restricted to particular statement sections (e.g. the notes)."""
        wanted = {str(s) for s in statement_types}
        candidates = self._bm25(query, k * 6)
        if self._vector_index is not None:
            try:
                candidates = self._vector_search(query, k * 6) or candidates
            except Exception:
                pass
        return [c for c in candidates if c.statement_type in wanted][:k]

    def stats(self) -> dict[str, Any]:
        pages = {(p.doc_id, p.page) for p in self.passages}
        return {"passages": len(self.passages), "pages": len(pages), "backend": self.backend}


# ---------------------------------------------------------------------------
# Run-scoped cache
# ---------------------------------------------------------------------------

_INDEX_CACHE: dict[tuple[str, ...], DocumentIndex] = {}


def get_index(paths: Iterable[str], *, collection_name: str = "sentinel", rebuild: bool = False) -> DocumentIndex:
    """Return the index for a set of filings, building it at most once per run."""
    from tools.document_parser import load_documents

    key = tuple(sorted(str(Path(p).resolve()) for p in paths))
    if rebuild:
        _INDEX_CACHE.pop(key, None)
    if key not in _INDEX_CACHE:
        _INDEX_CACHE[key] = DocumentIndex.build(load_documents(key), collection_name=collection_name)
    return _INDEX_CACHE[key]


def clear_index_cache() -> None:
    _INDEX_CACHE.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tokenise(text: str) -> list[str]:
    return [t for t in (w.lower() for w in _TOKEN.findall(text or "")) if t not in _STOPWORDS and len(t) > 1]


def _chunk_page(doc_id: str, page: ParsedPage, chunk_chars: int, overlap: int) -> list[Passage]:
    text = (page.layout_text or page.text or "").strip()
    if not text:
        return []
    chunks: list[Passage] = []
    start, seq = 0, 0
    while start < len(text):
        end = min(start + chunk_chars, len(text))
        # Prefer a line boundary so a chunk never splits a statement row.
        if end < len(text):
            boundary = text.rfind("\n", start + int(chunk_chars * 0.6), end)
            if boundary > start:
                end = boundary
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(Passage(
                text=chunk, doc_id=doc_id, page=page.number,
                statement_type=page.statement_type.value,
                chunk_id=f"{doc_id}-p{page.number}-c{seq}",
            ))
            seq += 1
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def _try_build_vector_index(
    passages: list[Passage], persist_dir: Optional[Path], collection_name: str
) -> Any:
    """Build a LlamaIndex + Chroma index, or return None if unavailable.

    Returning None instead of raising is deliberate: a missing embedding model
    or a stale ONNX cache should downgrade retrieval quality, not fail the run.
    """
    settings = get_settings()
    if settings.offline:
        log.info("Mode offline: melewati indeks vektor, memakai BM25.")
        return None
    try:
        import chromadb
        from llama_index.core import Document, StorageContext, VectorStoreIndex
        from llama_index.core import Settings as LlamaSettings
        from llama_index.core.llms.mock import MockLLM
        from llama_index.vector_stores.chroma import ChromaVectorStore
    except ImportError as exc:
        log.info("LlamaIndex/Chroma tidak tersedia (%s); memakai BM25.", exc)
        return None

    try:
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding

        LlamaSettings.embed_model = HuggingFaceEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")
    except Exception as exc:
        log.info("Model embedding lokal tidak tersedia (%s); memakai BM25.", exc)
        return None

    try:
        # Retrieval only; no synthesis through LlamaIndex. Assigning MockLLM
        # rather than None is deliberate: LlamaIndex resolves a None here to the
        # same MockLLM, but does it via a bare print() to stdout — "LLM is
        # explicitly disabled. Using MockLLM." — which lands in the middle of the
        # live audit trail and reads as though Sentinel's own model were off.
        LlamaSettings.llm = MockLLM()
        directory = Path(persist_dir or settings.index_dir)
        directory.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(directory))
        collection = client.get_or_create_collection(collection_name)
        # Rebuild per run so a re-analysis never retrieves a previous filing's text.
        if collection.count() > 0:
            client.delete_collection(collection_name)
            collection = client.get_or_create_collection(collection_name)

        vector_store = ChromaVectorStore(chroma_collection=collection)
        storage = StorageContext.from_defaults(vector_store=vector_store)
        documents = [
            Document(
                text=p.text,
                metadata={
                    "doc_id": p.doc_id, "page": p.page,
                    "statement_type": p.statement_type, "chunk_id": p.chunk_id,
                },
            )
            for p in passages
        ]
        return VectorStoreIndex.from_documents(documents, storage_context=storage, show_progress=False)
    except Exception as exc:  # pragma: no cover - backend construction failure
        log.warning("Gagal membangun indeks Chroma (%s); memakai BM25.", exc)
        return None
