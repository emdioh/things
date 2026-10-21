from __future__ import annotations

import json
import re
from typing import Protocol


class LLMError(RuntimeError):
    """The model could not produce a usable answer (refusal, truncation, bad JSON)."""


class LLMClient(Protocol):
    name: str

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        """Return a JSON object conforming (as closely as the provider allows) to ``schema``."""
        ...


def parse_json_loose(text: str) -> dict:
    """Parse JSON from a model reply, tolerating code fences and surrounding prose."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
    raise LLMError(f"model did not return valid JSON: {text[:200]!r}")
