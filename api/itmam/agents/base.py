"""What every agent returns, and the optional language model behind two of them."""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Proposal:
    """One agent's suggestion. `apply` names an engine command a person can approve."""

    agent: str
    case_id: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    apply: dict[str, Any] | None = None
    source: str = "rules"  # "rules" or "gemini"
    checks: list[str] = field(default_factory=list)  # guardrails that ran

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def gemini(prompt: str, timeout: float = 8.0) -> str | None:
    """Ask Gemini for text. Returns None when no key is set or the call fails, so every agent
    falls back to its rules. The model is configurable: GEMINI_MODEL."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = json.dumps(
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 400},
        }
    ).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json", "x-goog-api-key": key}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
        return str(data["candidates"][0]["content"]["parts"][0]["text"]).strip()
    except Exception:  # noqa: BLE001 - any failure means "use the rules"
        return None
