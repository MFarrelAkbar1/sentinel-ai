"""Central runtime configuration for Sentinel-AI.

Every tunable lives here so that thresholds used by the verification layer and
the reporting layer are visible in one place — the risk table in the technical
report treats confidence calibration as a first-class design concern, not an
implementation detail buried in an agent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional; the app runs fine without a .env file
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is a convenience, not a dependency
    pass


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or value == "" else value


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _resolve(path_str: str) -> Path:
    path = Path(path_str)
    return path if path.is_absolute() else PROJECT_ROOT / path


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of runtime configuration."""

    # --- LLM ---------------------------------------------------------------
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    model_primary: str = field(default_factory=lambda: _env("SENTINEL_MODEL_PRIMARY", "claude-sonnet-4-6"))
    model_light: str = field(default_factory=lambda: _env("SENTINEL_MODEL_LIGHT", "claude-haiku-4-5"))

    # --- Search ------------------------------------------------------------
    tavily_api_key: str = field(default_factory=lambda: os.getenv("TAVILY_API_KEY", ""))

    # --- Runtime -----------------------------------------------------------
    offline: bool = field(default_factory=lambda: _env("SENTINEL_OFFLINE", "0") in ("1", "true", "True"))
    db_path: Path = field(default_factory=lambda: _resolve(_env("SENTINEL_DB_PATH", "data/sentinel.db")))
    doc_dir: Path = field(default_factory=lambda: _resolve(_env("SENTINEL_DOC_DIR", "data/documents")))
    index_dir: Path = field(default_factory=lambda: _resolve(_env("SENTINEL_INDEX_DIR", "data/index")))
    report_dir: Path = field(default_factory=lambda: _resolve(_env("SENTINEL_REPORT_DIR", "data/reports")))

    # --- Verification ------------------------------------------------------
    verify_rel_tolerance: float = field(default_factory=lambda: _env_float("SENTINEL_VERIFY_REL_TOLERANCE", 0.005))
    verify_abs_tolerance: float = field(default_factory=lambda: _env_float("SENTINEL_VERIFY_ABS_TOLERANCE", 2.0))
    max_extraction_attempts: int = field(default_factory=lambda: _env_int("SENTINEL_MAX_EXTRACTION_ATTEMPTS", 3))

    # --- Reporting ---------------------------------------------------------
    min_confidence: float = field(default_factory=lambda: _env_float("SENTINEL_MIN_CONFIDENCE", 0.55))

    # --- Scheduler ---------------------------------------------------------
    monitor_cron: str = field(default_factory=lambda: _env("SENTINEL_MONITOR_CRON", "0 3 * * *"))
    watchlist: tuple = field(
        default_factory=lambda: tuple(
            t.strip().upper() for t in _env("SENTINEL_WATCHLIST", "GOTO,BUMI,TLKM").split(",") if t.strip()
        )
    )

    @property
    def has_llm(self) -> bool:
        return bool(self.anthropic_api_key) and not self.offline

    @property
    def has_search(self) -> bool:
        return bool(self.tavily_api_key) and not self.offline

    def ensure_dirs(self) -> None:
        for path in (self.db_path.parent, self.doc_dir, self.index_dir, self.report_dir):
            path.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    """Return the process-wide settings snapshot."""
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings


# ---------------------------------------------------------------------------
# Source credibility tiers (Agent 2)
# ---------------------------------------------------------------------------
# Weight drives how much a management claim or news item can move a hypothesis.
# Official regulator / exchange filings outrank media, which outranks forums.

CREDIBILITY_TIERS: dict[str, tuple[int, float]] = {
    # domain fragment: (tier, weight)
    "idx.co.id": (1, 1.00),
    "ojk.go.id": (1, 1.00),
    "ksei.co.id": (1, 1.00),
    "bi.go.id": (1, 1.00),
    "idxchannel.com": (2, 0.70),
    "kontan.co.id": (2, 0.75),
    "bisnis.com": (2, 0.75),
    "cnbcindonesia.com": (2, 0.72),
    "katadata.co.id": (2, 0.75),
    "kompas.com": (2, 0.72),
    "tempo.co": (2, 0.72),
    "detik.com": (2, 0.68),
    "antaranews.com": (2, 0.72),
    "reuters.com": (2, 0.85),
    "bloomberg.com": (2, 0.85),
    "investor.id": (2, 0.70),
    "emitennews.com": (3, 0.45),
    "stockbit.com": (4, 0.20),
    "reddit.com": (4, 0.15),
    "twitter.com": (4, 0.15),
    "x.com": (4, 0.15),
    "facebook.com": (4, 0.12),
    "youtube.com": (4, 0.15),
}

TIER_LABELS = {
    1: "Sumber resmi (regulator / bursa / perusahaan)",
    2: "Media kredibel",
    3: "Media sekunder",
    4: "Forum / media sosial",
}

DEFAULT_TIER = 3
DEFAULT_WEIGHT = 0.40


def credibility_for(url: str) -> tuple[int, float, str]:
    """Classify a URL into (tier, weight, label)."""
    lowered = (url or "").lower()
    # Company investor-relations sites are treated as tier 1 primary sources.
    if ".co.id/investor" in lowered or "/investor-relations" in lowered:
        return 1, 0.95, TIER_LABELS[1]
    for fragment, (tier, weight) in CREDIBILITY_TIERS.items():
        if fragment in lowered:
            return tier, weight, TIER_LABELS[tier]
    return DEFAULT_TIER, DEFAULT_WEIGHT, TIER_LABELS[DEFAULT_TIER]
