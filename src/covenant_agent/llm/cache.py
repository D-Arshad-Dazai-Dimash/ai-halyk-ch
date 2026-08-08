"""Disk cache for LLM extraction results, keyed by document content hash.
Purely an engineering resilience measure (the LLM call is the slow, flaky
step) -- callers still run every result through the same Pydantic
validation as an uncached call would. Not a source of truth: deleting this
cache only costs recomputation time, never changes correctness.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from covenant_agent.common.config import LLM_CACHE_DIR


def cached_json(cache_key: str, compute: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    LLM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = LLM_CACHE_DIR / f"{cache_key}.json"
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass  # corrupt cache entry -- fall through and recompute

    result = compute()
    path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result
