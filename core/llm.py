"""LLM abstraction layer.

The risk table in the technical report lists third-party API dependence as a
named risk, mitigated by "a layer of abstraction over model invocation so the
LLM provider can be swapped without changing agent logic". This module is that
layer.

Two properties matter beyond provider-swapping:

* **Structured output by construction.** Every call goes through a forced tool
  call with an explicit JSON schema, so agents receive validated dicts rather
  than prose they must parse. This is what keeps the LLM inside its lane:
  it chooses *which* caption on the page is `total_aset`, it never states what
  `total_aset` equals.
* **Graceful degradation.** When no API key is present (or ``SENTINEL_OFFLINE=1``),
  ``available`` is False and every agent falls back to its deterministic path.
  The system produces a smaller report, never a fabricated one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from core.config import get_settings

log = logging.getLogger("sentinel.llm")


@dataclass
class LLMResult:
    data: dict[str, Any]
    raw_text: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    ok: bool = True
    error: Optional[str] = None


@dataclass
class LLMClient:
    """Thin wrapper over the Anthropic Messages API.

    Swap target: replace ``_call_anthropic`` with another provider's client and
    every agent keeps working — they only ever see ``LLMResult``.
    """

    model_primary: str = ""
    model_light: str = ""
    api_key: str = ""
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "calls": 0})
    _client: Any = None

    def __post_init__(self) -> None:
        settings = get_settings()
        self.model_primary = self.model_primary or settings.model_primary
        self.model_light = self.model_light or settings.model_light
        self.api_key = self.api_key or settings.anthropic_api_key
        if settings.offline:
            self.api_key = ""

    @property
    def available(self) -> bool:
        if not self.api_key:
            return False
        return self._ensure_client() is not None

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.api_key)
        except Exception as exc:  # pragma: no cover - import/environment failure path
            log.warning("Anthropic client unavailable: %s", exc)
            self._client = None
        return self._client

    # -----------------------------------------------------------------------

    def structured(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        tool_name: str = "submit",
        tool_description: str = "Kirim hasil terstruktur.",
        light: bool = False,
        max_tokens: int = 8000,
        thinking: bool = False,
    ) -> LLMResult:
        """Force a single tool call whose input schema is ``schema``.

        Forced tool use (rather than free-form JSON) is what guarantees the
        agent receives keys it asked for. A refusal or malformed response
        returns ``ok=False`` — callers treat that as "extraction failed", never
        as "value unknown but assume something".
        """
        client = self._ensure_client()
        if client is None:
            return LLMResult(data={}, ok=False, error="LLM tidak tersedia (mode deterministik).")

        model = self.model_light if light else self.model_primary
        tools = [{"name": tool_name, "description": tool_description, "input_schema": schema}]
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": tools,
            "tool_choice": {"type": "tool", "name": tool_name},
        }
        if thinking:
            # Adaptive thinking on Claude 4.6+; forced tool_choice and thinking
            # are incompatible, so callers asking for thinking get free choice.
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["tool_choice"] = {"type": "any"}

        try:
            response = client.messages.create(**kwargs)
        except Exception as exc:
            log.warning("[%s] LLM call failed: %s", purpose, exc)
            return LLMResult(data={}, model=model, ok=False, error=str(exc))

        self.usage["calls"] += 1
        usage = getattr(response, "usage", None)
        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        self.usage["input_tokens"] += in_tok
        self.usage["output_tokens"] += out_tok

        if getattr(response, "stop_reason", None) == "refusal":
            return LLMResult(data={}, model=model, ok=False, error="refusal",
                             input_tokens=in_tok, output_tokens=out_tok)

        payload: dict[str, Any] = {}
        text_parts: list[str] = []
        for block in getattr(response, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "tool_use" and getattr(block, "name", "") == tool_name:
                raw = getattr(block, "input", {})
                # Tool inputs must be treated as JSON, never string-matched.
                payload = raw if isinstance(raw, dict) else json.loads(str(raw))
            elif btype == "text":
                text_parts.append(getattr(block, "text", ""))

        if not payload:
            return LLMResult(
                data={}, raw_text="\n".join(text_parts), model=model, ok=False,
                error="model tidak mengembalikan tool call", input_tokens=in_tok, output_tokens=out_tok,
            )

        return LLMResult(
            data=payload, raw_text="\n".join(text_parts), model=model,
            input_tokens=in_tok, output_tokens=out_tok, ok=True,
        )

    def classify(self, *, purpose: str, system: str, user: str, labels: list[str]) -> Optional[str]:
        """Single-label classification on the light model (cost mitigation)."""
        result = self.structured(
            purpose=purpose,
            system=system,
            user=user,
            light=True,
            max_tokens=256,
            tool_name="classify",
            tool_description="Pilih satu label.",
            schema={
                "type": "object",
                "properties": {"label": {"type": "string", "enum": labels}},
                "required": ["label"],
                "additionalProperties": False,
            },
        )
        return result.data.get("label") if result.ok else None


_shared: Optional[LLMClient] = None


def get_llm() -> LLMClient:
    global _shared
    if _shared is None:
        _shared = LLMClient()
    return _shared
