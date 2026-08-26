"""PDF extraction for Indonesian filings (PyMuPDF primary, pdfplumber fallback).

Responsibilities, in order of importance:

1. **Read numbers the way BEI filings write them.** ``1.234.567`` is not a
   float in most locales and ``(1.234)`` is negative, not parenthesised prose.
   Getting this wrong silently poisons every downstream check, so
   ``parse_id_number`` is the single chokepoint and is unit-tested.
2. **Know the reporting scale.** A statement headed *"dalam jutaan Rupiah"*
   means each figure is ×1e6. Comparing a scaled figure against an unscaled one
   is the most common way an automated reader "finds" a balance-sheet break
   that does not exist.
3. **Locate statements without reading 400 pages.** Page classification is
   keyword-driven and deterministic, so the expensive model is only ever shown
   the handful of pages that matter.
4. **Never guess.** Every candidate line returned carries its page number and
   its verbatim text, so the caller can cite it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from core.state import METRIC_CAPTION_EXCLUSIONS, METRIC_VOCABULARY, StatementType

log = logging.getLogger("sentinel.parser")

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    fitz = None  # type: ignore


# ---------------------------------------------------------------------------
# Number parsing
# ---------------------------------------------------------------------------

_NUM_TOKEN = re.compile(
    r"""
    (?P<open>\()?                      # parenthesis = negative
    \s*(?P<sign>[-−–])?\s*             # or an ASCII/unicode minus
    (?P<body>
        \d{1,3}(?:[.\s]\d{3})+(?:,\d+)?   # 1.234.567,89  (Indonesian)
      | \d{1,3}(?:,\d{3})+(?:\.\d+)?      # 1,234,567.89  (English)
      | \d+(?:[.,]\d+)?                   # 1234  /  12,5
    )
    \s*(?P<close>\))?
    """,
    re.VERBOSE,
)

_SCALE_PATTERNS: list[tuple[re.Pattern[str], float, str]] = [
    (re.compile(r"dalam\s+(?:jutaan|juta)\s+(?:rupiah|rp)?", re.I), 1e6, "jutaan Rupiah"),
    (re.compile(r"dalam\s+(?:ribuan|ribu)\s+(?:rupiah|rp)?", re.I), 1e3, "ribuan Rupiah"),
    (re.compile(r"dalam\s+miliar(?:an)?\s+(?:rupiah|rp)?", re.I), 1e9, "miliaran Rupiah"),
    (re.compile(r"in\s+millions?\s+of\s+(?:rupiah|idr)", re.I), 1e6, "millions of IDR"),
    (re.compile(r"in\s+thousands?\s+of\s+(?:rupiah|idr)", re.I), 1e3, "thousands of IDR"),
    (re.compile(r"\(dalam\s+rupiah\s+penuh\)", re.I), 1.0, "Rupiah penuh"),
]


def parse_id_number(token: str) -> Optional[float]:
    """Parse one Indonesian-formatted numeric token.

    Handles ``1.234.567``, ``1.234.567,89``, ``(1.234)`` → ``-1234``,
    ``1,234,567.89``, ``12,5`` → ``12.5``. Returns ``None`` when the token is
    not a number — the caller must treat ``None`` as *unread*, never as zero.
    """
    if token is None:
        return None
    match = _NUM_TOKEN.fullmatch(str(token).strip())
    if not match:
        match = _NUM_TOKEN.search(str(token).strip())
        if not match:
            return None

    body = match.group("body")
    negative = bool(match.group("open") and match.group("close")) or bool(match.group("sign"))

    has_dot, has_comma = "." in body, "," in body
    body = body.replace(" ", "")

    if has_dot and has_comma:
        # Whichever separator appears last is the decimal separator.
        if body.rfind(",") > body.rfind("."):
            normalised = body.replace(".", "").replace(",", ".")   # Indonesian
        else:
            normalised = body.replace(",", "")                      # English
    elif has_comma:
        parts = body.split(",")
        # 1,234 with a 3-digit tail is a thousands group; 12,5 is a decimal.
        if len(parts) > 2 or (len(parts[-1]) == 3 and len(parts[0]) <= 3 and len(parts) == 2 and len(body) > 4):
            normalised = body.replace(",", "")
        else:
            normalised = body.replace(",", ".")
    elif has_dot:
        parts = body.split(".")
        if len(parts) > 2 or (len(parts[-1]) == 3 and len(parts) == 2):
            normalised = body.replace(".", "")                      # thousands
        else:
            normalised = body                                       # decimal
    else:
        normalised = body

    try:
        value = float(normalised)
    except ValueError:
        return None
    return -value if negative else value


def extract_numbers(line: str) -> list[float]:
    """All numeric tokens on a line, left to right, in reading order."""
    values: list[float] = []
    for match in _NUM_TOKEN.finditer(line or ""):
        parsed = parse_id_number(match.group(0))
        if parsed is not None:
            values.append(parsed)
    return values


def detect_scale(text: str) -> tuple[float, str]:
    """Detect the reporting unit declared on a page (default: full Rupiah)."""
    for pattern, multiplier, label in _SCALE_PATTERNS:
        if pattern.search(text or ""):
            return multiplier, label
    return 1.0, "Rupiah penuh"


_YEAR = re.compile(r"\b(19|20)\d{2}\b")


def detect_periods(text: str) -> list[str]:
    """Reporting periods referenced on a page, most recent first."""
    years = sorted({m.group(0) for m in _YEAR.finditer(text or "")}, reverse=True)
    return [y for y in years if 1990 <= int(y) <= 2100]


# ---------------------------------------------------------------------------
# Page classification
# ---------------------------------------------------------------------------

#: (type, anchors, supporting keywords, requires numeric density)
#:
#: Anchors are the captions that only appear on a page *of* that kind; supports
#: are corroborating vocabulary. Scoring anchors far above supports is what
#: keeps an auditor's report — whose scope paragraph names every other statement
#: by title — from being filed as a balance sheet.
_PAGE_SIGNATURES: list[tuple[StatementType, list[str], list[str], bool]] = [
    (StatementType.AUDITOR_OPINION,
     ["laporan auditor independen", "independent auditor", "opini tanpa modifikasian",
      "tidak menyatakan pendapat", "penekanan suatu hal", "opini wajar"],
     ["kelangsungan usaha", "ketidakpastian material", "kantor akuntan publik",
      "dengan pengecualian", "menurut opini kami", "hal audit utama"],
     False),
    (StatementType.MANAGEMENT_DISCUSSION,
     ["analisis dan pembahasan manajemen", "laporan direksi", "management discussion",
      "sambutan direktur utama", "laporan dewan komisaris"],
     ["tinjauan operasional", "prospek usaha", "tinjauan kinerja keuangan",
      "strategi perseroan"],
     False),
    (StatementType.NOTES,
     ["catatan atas laporan keuangan", "notes to the consolidated"],
     ["pihak berelasi", "penyisihan penurunan nilai", "umur piutang",
      "sifat hubungan", "ikhtisar kebijakan akuntansi"],
     False),
    (StatementType.BALANCE_SHEET,
     ["laporan posisi keuangan", "neraca konsolidasian", "statements of financial position"],
     ["jumlah liabilitas dan ekuitas", "jumlah aset", "aset lancar",
      "liabilitas jangka pendek", "jumlah ekuitas"],
     True),
    (StatementType.INCOME_STATEMENT,
     ["laporan laba rugi", "laba rugi dan penghasilan komprehensif",
      "statements of profit or loss"],
     ["laba tahun berjalan", "beban pokok", "laba bruto", "laba usaha",
      "laba per saham"],
     True),
    (StatementType.CASH_FLOW,
     ["laporan arus kas", "statements of cash flows"],
     ["aktivitas operasi", "aktivitas investasi", "aktivitas pendanaan",
      "kas dan setara kas akhir"],
     True),
    (StatementType.EQUITY_CHANGES,
     ["laporan perubahan ekuitas", "statements of changes in equity"],
     ["saldo laba", "tambahan modal disetor", "kepentingan nonpengendali"],
     True),
]


def classify_page(text: str) -> tuple[StatementType, float]:
    """Deterministic page classification. Returns (type, score).

    Ordering matters on ties: narrative sections are evaluated before statement
    sections, so a page that reads like prose but quotes statement captions
    stays prose.
    """
    lowered = (text or "").lower()
    if len(lowered.strip()) < 40:
        return StatementType.OTHER, 0.0

    digits = sum(c.isdigit() for c in lowered)
    density = digits / max(len(lowered), 1)

    best_type, best_score = StatementType.OTHER, 0.0
    for stype, anchors, supports, needs_numbers in _PAGE_SIGNATURES:
        anchor_hits = sum(1 for kw in anchors if kw in lowered)
        if anchor_hits == 0:
            continue
        support_hits = sum(1 for kw in supports if kw in lowered)
        score = 1.2 * anchor_hits + 0.35 * support_hits
        if needs_numbers:
            # A statement page is a grid of figures; a page that names the
            # statement in a sentence is not.
            if density < 0.02:
                continue
            score += min(density * 4, 0.8)
        if score > best_score:
            best_type, best_score = stype, score
    return best_type, round(best_score, 3)


# ---------------------------------------------------------------------------
# Document model
# ---------------------------------------------------------------------------


@dataclass
class ParsedPage:
    number: int                        # 1-indexed, matches what a human cites
    text: str
    layout_text: str                   # whitespace-preserving, column-aligned
    statement_type: StatementType
    classification_score: float
    scale: float
    scale_label: str
    periods: list[str] = field(default_factory=list)
    tables: list[list[list[str]]] = field(default_factory=list)

    def lines(self, layout: bool = True) -> list[str]:
        source = self.layout_text if layout and self.layout_text else self.text
        return [ln for ln in source.splitlines() if ln.strip()]


@dataclass
class ParsedDocument:
    doc_id: str
    path: str
    page_count: int
    pages: list[ParsedPage]
    title: str = ""
    parser: str = "pymupdf"
    default_scale: float = 1.0
    default_scale_label: str = "Rupiah penuh"

    # -- page selection -----------------------------------------------------

    def pages_of(self, statement_type: StatementType, limit: int = 6) -> list[ParsedPage]:
        matched = [p for p in self.pages if p.statement_type == statement_type]
        matched.sort(key=lambda p: p.classification_score, reverse=True)
        return matched[:limit]

    def page(self, number: int) -> Optional[ParsedPage]:
        for p in self.pages:
            if p.number == number:
                return p
        return None

    def window(self, number: int, radius: int = 1) -> list[ParsedPage]:
        """Pages around ``number`` — statements often spill onto the next page."""
        return [p for p in self.pages if abs(p.number - number) <= radius]

    def search(self, needles: Iterable[str], limit: int = 12) -> list[tuple[ParsedPage, str]]:
        """Every (page, line) whose text contains any needle. Case-insensitive."""
        lowered = [n.lower() for n in needles]
        hits: list[tuple[ParsedPage, str]] = []
        for page in self.pages:
            for line in page.lines():
                low = line.lower()
                if any(n in low for n in lowered):
                    hits.append((page, line.strip()))
                    if len(hits) >= limit:
                        return hits
        return hits

    def summary(self) -> dict[str, Any]:
        by_type: dict[str, list[int]] = {}
        for p in self.pages:
            if p.statement_type != StatementType.OTHER:
                by_type.setdefault(p.statement_type.value, []).append(p.number)
        return {
            "doc_id": self.doc_id,
            "path": self.path,
            "title": self.title,
            "page_count": self.page_count,
            "parser": self.parser,
            "scale": self.default_scale,
            "scale_label": self.default_scale_label,
            "statement_pages": by_type,
            "periods": sorted({y for p in self.pages for y in p.periods}, reverse=True)[:6],
        }


# ---------------------------------------------------------------------------
# Parsing entrypoints
# ---------------------------------------------------------------------------


def parse_pdf(path: str | Path, doc_id: Optional[str] = None, extract_tables: bool = True) -> ParsedDocument:
    """Parse a filing with PyMuPDF, falling back to pdfplumber on failure.

    Two parsing paths is the mitigation named in the risk table: a table-layout
    failure in one engine should not become a silent extraction failure for the
    whole run.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Dokumen tidak ditemukan: {path}")

    doc_id = doc_id or path.stem
    try:
        if fitz is None:
            raise RuntimeError("PyMuPDF tidak terpasang")
        return _parse_with_pymupdf(path, doc_id, extract_tables)
    except Exception as exc:
        log.warning("PyMuPDF gagal pada %s (%s); mencoba pdfplumber.", path.name, exc)
        return _parse_with_pdfplumber(path, doc_id)


def _parse_with_pymupdf(path: Path, doc_id: str, extract_tables: bool) -> ParsedDocument:
    doc = fitz.open(str(path))
    pages: list[ParsedPage] = []
    try:
        for index in range(doc.page_count):
            page = doc.load_page(index)
            text = page.get_text("text") or ""
            layout = page.get_text("text", sort=True) or text
            stype, score = classify_page(text)
            scale, scale_label = detect_scale(text)

            tables: list[list[list[str]]] = []
            # Table extraction is expensive; only run it where numbers live.
            if extract_tables and stype in (
                StatementType.BALANCE_SHEET, StatementType.INCOME_STATEMENT,
                StatementType.CASH_FLOW, StatementType.NOTES,
            ):
                tables = _pymupdf_tables(page)

            pages.append(ParsedPage(
                number=index + 1, text=text, layout_text=layout,
                statement_type=stype, classification_score=score,
                scale=scale, scale_label=scale_label,
                periods=detect_periods(text), tables=tables,
            ))
        title = (doc.metadata or {}).get("title") or path.stem
        page_count = doc.page_count
    finally:
        doc.close()

    parsed = ParsedDocument(
        doc_id=doc_id, path=str(path), page_count=page_count, pages=pages,
        title=title, parser="pymupdf",
    )
    _apply_default_scale(parsed)
    return parsed


def _pymupdf_tables(page: Any) -> list[list[list[str]]]:
    try:
        finder = page.find_tables()
    except Exception:  # pragma: no cover - older PyMuPDF builds
        return []
    tables: list[list[list[str]]] = []
    for table in getattr(finder, "tables", []) or []:
        try:
            rows = table.extract()
        except Exception:
            continue
        cleaned = [[(cell or "").strip() for cell in row] for row in rows if row]
        if cleaned:
            tables.append(cleaned)
    return tables


def _parse_with_pdfplumber(path: Path, doc_id: str) -> ParsedDocument:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Kedua parser gagal: PyMuPDF error dan pdfplumber tidak terpasang."
        ) from exc

    pages: list[ParsedPage] = []
    with pdfplumber.open(str(path)) as pdf:
        for index, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            stype, score = classify_page(text)
            scale, scale_label = detect_scale(text)
            tables: list[list[list[str]]] = []
            if stype in (StatementType.BALANCE_SHEET, StatementType.INCOME_STATEMENT, StatementType.CASH_FLOW):
                for table in page.extract_tables() or []:
                    cleaned = [[(cell or "").strip() for cell in row] for row in table if row]
                    if cleaned:
                        tables.append(cleaned)
            pages.append(ParsedPage(
                number=index + 1, text=text, layout_text=text,
                statement_type=stype, classification_score=score,
                scale=scale, scale_label=scale_label,
                periods=detect_periods(text), tables=tables,
            ))

    parsed = ParsedDocument(
        doc_id=doc_id, path=str(path), page_count=len(pages), pages=pages,
        title=path.stem, parser="pdfplumber",
    )
    _apply_default_scale(parsed)
    return parsed


def _apply_default_scale(doc: ParsedDocument) -> None:
    """Adopt the scale declared on statement pages as the document default.

    A page with no explicit unit inside a filing that says "dalam jutaan" on
    every statement is far more likely to inherit that unit than to switch to
    full Rupiah, so unlabelled statement pages inherit rather than default.
    """
    votes: dict[tuple[float, str], int] = {}
    for page in doc.pages:
        if page.statement_type in (
            StatementType.BALANCE_SHEET, StatementType.INCOME_STATEMENT, StatementType.CASH_FLOW
        ) and page.scale != 1.0:
            votes[(page.scale, page.scale_label)] = votes.get((page.scale, page.scale_label), 0) + 1
    if votes:
        (scale, label), _ = max(votes.items(), key=lambda kv: kv[1])
        doc.default_scale, doc.default_scale_label = scale, label
        for page in doc.pages:
            if page.scale == 1.0 and page.statement_type in (
                StatementType.BALANCE_SHEET, StatementType.INCOME_STATEMENT, StatementType.CASH_FLOW
            ):
                page.scale, page.scale_label = scale, label


# ---------------------------------------------------------------------------
# Process-level cache
# ---------------------------------------------------------------------------

_CACHE: dict[tuple[str, str], ParsedDocument] = {}


def load_documents(paths: Iterable[str | Path], force_parser: Optional[str] = None) -> list[ParsedDocument]:
    """Parse (or return cached) documents for a list of paths.

    Parsing a 400-page filing twice in one run is pure waste, and Agent 1's
    retry loop re-reads the same file up to three times, so results are cached
    per (path, parser) pair.
    """
    documents: list[ParsedDocument] = []
    for raw_path in paths:
        path = Path(raw_path).resolve()
        key = (str(path), force_parser or "auto")
        if key not in _CACHE:
            if force_parser == "pdfplumber":
                _CACHE[key] = _parse_with_pdfplumber(path, path.stem)
            else:
                _CACHE[key] = parse_pdf(path)
        documents.append(_CACHE[key])
    return documents


def clear_document_cache() -> None:
    _CACHE.clear()


# ---------------------------------------------------------------------------
# Caption-anchored candidate lines
# ---------------------------------------------------------------------------


@dataclass
class CandidateLine:
    """A statement line that may hold a metric, with everything needed to cite it."""

    metric_key: str
    page: int
    text: str
    numbers: list[float]
    scale: float
    match_score: float
    source: str = "text"               # "text" | "table"


#: A caption must be mostly consumed by the matched alias. Below this, the
#: alias is incidental vocabulary in a sentence rather than a statement row.
_MIN_CAPTION_COVERAGE = 0.5


def caption_of(line: str) -> str:
    """The label portion of a statement row — everything before the figures.

    Statement rows read ``caption [note ref] value_current value_prior``, so
    truncating at the first numeric token isolates the caption cleanly and
    incidentally strips the note-reference column.
    """
    match = _NUM_TOKEN.search(line or "")
    caption = (line or "")[: match.start()] if match else (line or "")
    return re.sub(r"\s+", " ", caption).strip(" .:;-–—|").lower()


def match_metric(caption: str) -> Optional[tuple[str, float]]:
    """Resolve a caption to (metric_key, coverage), longest specific alias wins.

    Matching against the *whole* vocabulary rather than only the requested keys
    is what stops ``jumlah aset`` from claiming the row ``Jumlah aset lancar``:
    the longer alias belongs to a different metric and outranks it.
    """
    if not caption:
        return None
    best_key, best_coverage, best_length = None, 0.0, 0
    for key, aliases in METRIC_VOCABULARY.items():
        if any(bad in caption for bad in METRIC_CAPTION_EXCLUSIONS.get(key, ())):
            continue
        for alias in aliases:
            if alias not in caption:
                continue
            coverage = len(alias) / max(len(caption), 1)
            if (coverage, len(alias)) > (best_coverage, best_length):
                best_key, best_coverage, best_length = key, coverage, len(alias)
    if best_key is None or best_coverage < _MIN_CAPTION_COVERAGE:
        return None
    return best_key, round(min(best_coverage, 1.0), 4)


def find_candidate_lines(
    doc: ParsedDocument,
    metric_keys: Iterable[str],
    pages: Optional[Iterable[ParsedPage]] = None,
    layout: bool = True,
) -> list[CandidateLine]:
    """Every line on the target pages whose caption resolves to a wanted metric."""
    target_pages = list(pages) if pages is not None else doc.pages
    keys = set(metric_keys)
    results: list[CandidateLine] = []

    for page in target_pages:
        for line in page.lines(layout=layout):
            numbers = extract_numbers(line)
            if not numbers:
                continue
            matched = match_metric(caption_of(line))
            if matched is None or matched[0] not in keys:
                continue
            results.append(CandidateLine(
                metric_key=matched[0], page=page.number, text=line.strip(),
                numbers=numbers, scale=page.scale, match_score=matched[1],
            ))

    # Table cells give a second, independent reading of the same rows.
    for page in target_pages:
        for table in page.tables:
            for row in table:
                if not row:
                    continue
                numbers: list[float] = []
                for cell in row[1:]:
                    numbers.extend(extract_numbers(cell))
                if not numbers:
                    continue
                matched = match_metric(caption_of(" ".join(c for c in row[:2] if c)))
                if matched is None or matched[0] not in keys:
                    continue
                results.append(CandidateLine(
                    metric_key=matched[0], page=page.number,
                    text=" | ".join(c for c in row if c).strip(),
                    numbers=numbers, scale=page.scale,
                    match_score=matched[1], source="table",
                ))

    results.sort(key=lambda c: c.match_score, reverse=True)
    return results
