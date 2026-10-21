from __future__ import annotations

import json
import re
from typing import Protocol


class LLMError(RuntimeError):
    """The model could not produce a usable answer (truncation, bad JSON). Worth retrying."""


class LLMRefusal(LLMError):
    """The model declined the request. Retrying the same request won't help."""


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
    start, end = text.find("{"), text.rfind("}")
    candidate = text[start : end + 1] if 0 <= start < end else text
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        near = candidate[max(0, e.pos - 60) : e.pos + 20]
        raise LLMError(f"model returned invalid JSON ({e.msg} at char {e.pos}, near {near!r})") from None
