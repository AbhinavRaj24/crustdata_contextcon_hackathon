"""Gemini, used only for language: parsing messy requests, classifying news, wording letters.

Never used for facts or decisions. Chain: flash-lite -> 2.5 flash -> None (caller falls back to rules).
Results are cached by input hash, so the same text always gets the same answer.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

import httpx

from . import config, store

MODELS = ["gemini-3.5-flash-lite", "gemini-2.5-flash"]
URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TIMEOUT = 15.0


def available() -> bool:
    return bool(config.GEMINI_API_KEY)


def _call(model: str, prompt: str, schema: Optional[dict]) -> Optional[str]:
    gen: dict[str, Any] = {"temperature": 0}
    if schema:
        gen["responseMimeType"] = "application/json"
        gen["responseSchema"] = schema
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gen}
    try:
        r = httpx.post(URL.format(model=model), json=body, timeout=TIMEOUT,
                       headers={"x-goog-api-key": config.GEMINI_API_KEY or "", "content-type": "application/json"})
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        cand = r.json()["candidates"][0]
        parts = cand["content"]["parts"]
    except (KeyError, IndexError, ValueError):
        return None
    if cand.get("finishReason") not in (None, "STOP"):
        return None  # cut off (MAX_TOKENS, SAFETY...): never use a partial answer
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    return text or None


def generate(task: str, prompt: str, schema: Optional[dict] = None) -> tuple[Any, Optional[str]]:
    """Returns (result, model) or (None, None). JSON results are parsed when a schema is given."""
    if not available():
        return None, None
    key = hashlib.sha256(json.dumps({"t": task, "p": prompt, "s": schema}, sort_keys=True).encode()).hexdigest()
    cached = store.llm_cache_get(key)
    if cached:
        return cached
    for model in MODELS:
        text = _call(model, prompt, schema)
        if text is None:
            continue
        if schema:
            try:
                result: Any = json.loads(text)
            except json.JSONDecodeError:
                continue
        else:
            result = text.strip()
        store.llm_cache_put(key, model, result)
        return result, model
    return None, None
