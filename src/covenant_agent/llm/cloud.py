"""Cloud model access, used only where the deterministic parser gives up.

Two providers are supported and selected from whichever API key is present:
Anthropic (Claude) and Groq. With no key configured this module reports no
provider and the pipeline runs exactly as it did before -- offline, and with
no outbound request possible.

The same two guarantees as the local client apply, and matter more here
because the text is going off the machine:

1. Document text is untrusted. It is wrapped in explicit delimiters under a
   system instruction that says so, and the model is told never to follow
   instructions found inside it (THREAT_MODEL.md #1).
2. The model performs no arithmetic. It proposes a clause's *structure*
   only; the caller validates that structure against a closed AST and the
   deterministic engine computes every number. This module returns raw text
   and never a figure a caller could use directly.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from covenant_agent.common.config import (
    ANTHROPIC_MODEL,
    GROQ_MODEL,
    MODEL_FALLBACK_PROVIDER,
    MODEL_FALLBACK_TIMEOUT_SECONDS,
)
from covenant_agent.llm.client import UNTRUSTED_CONTENT_SYSTEM_PREAMBLE

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

#: Sent on hand-rolled HTTP requests. Not cosmetic: providers front their
#: APIs with bot protection that blocks a default client agent string.
USER_AGENT = "covenant-agent/0.1"

# Bounds on a provider response. A clause AST is a few hundred tokens, but
# the two providers need very different headroom -- and for different
# reasons:
#
# * Claude Opus 5 thinks by default, and `max_tokens` caps thinking *plus*
#   the answer, so a tight budget truncates mid-reasoning.
# * Groq bills the *requested* `max_tokens` against a per-minute token
#   quota (12k TPM on the free tier), so an 8k request consumes the whole
#   minute's budget and the next clause is rate-limited. Groq does not
#   think, so the JSON is all it needs room for.
ANTHROPIC_MAX_OUTPUT_TOKENS = 8192
GROQ_MAX_OUTPUT_TOKENS = 1536

#: Longest quota pause worth waiting out. Beyond this the cell is left
#: unresolved instead: a stalled run costs more than a missing clause.
GROQ_MAX_RETRY_WAIT_SECONDS = 20.0


@dataclass(frozen=True)
class Provider:
    """A configured, usable model backend."""

    name: str  # "anthropic" | "groq"
    model: str
    api_key: str

    @property
    def label(self) -> str:
        """How this model is named in the submission's ``model`` field."""
        return self.model


class ModelUnavailable(Exception):
    """No provider is configured, or the configured one could not answer.

    Always recoverable: the caller leaves the cell honestly unresolved
    rather than guessing.
    """


def detect_providers() -> list[Provider]:
    """Every configured provider, in the order to try them.

    Returned as a list rather than a single choice so a provider that is
    configured but unusable -- an exhausted balance, a quota, an outage --
    falls through to the next instead of costing the cell. Discovering that
    at submission time is exactly when a second provider is worth having.

    ``COVENANT_AGENT_MODEL_PROVIDER`` pins one; the default ``auto`` tries
    Anthropic first and falls back to Groq.
    """
    choice = (MODEL_FALLBACK_PROVIDER or "auto").strip().lower()
    if choice in {"off", "none", "disabled"}:
        return []

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()

    candidates: list[tuple[str, str, str]] = []
    for model in _model_list(ANTHROPIC_MODEL):
        candidates.append(("anthropic", model, anthropic_key))
    for model in _model_list(GROQ_MODEL):
        candidates.append(("groq", model, groq_key))

    return [
        Provider(name, model, key)
        for name, model, key in candidates
        if key and choice in {"auto", name}
    ]


def _model_list(setting: str) -> list[str]:
    """Models to try for one provider, in order.

    A provider setting may name several models ("a,b"): quotas are metered
    per model, so a second name is a live route when the first is spent.
    Order matters -- the first is asked first, and answers are cached per
    model, so a preferred model's existing answers are reused rather than
    re-earned.
    """
    return [name.strip() for name in (setting or "").split(",") if name.strip()]


def complete_json(
    provider: Provider,
    *,
    task_instructions: str,
    schema_description: str,
    untrusted_document_text: str,
) -> dict:
    """Ask the provider for one JSON object describing the given text.

    Returns the parsed dict as the model produced it. The caller MUST
    validate every field before it can influence a calculation -- nothing
    here performs business validation.
    """
    system = (
        f"{UNTRUSTED_CONTENT_SYSTEM_PREAMBLE}\n\n"
        f"Task:\n{task_instructions}\n\n"
        f"Required JSON schema:\n{schema_description}"
    )
    user = (
        "<untrusted_document_content>\n"
        f"{untrusted_document_text}\n"
        "</untrusted_document_content>"
    )

    if provider.name == "anthropic":
        raw = _call_anthropic(provider, system, user)
    elif provider.name == "groq":
        raw = _call_groq(provider, system, user)
    else:
        raise ModelUnavailable(f"unknown provider {provider.name!r}")

    return _parse_json_object(raw)


def _call_anthropic(provider: Provider, system: str, user: str) -> str:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise ModelUnavailable(
            "the anthropic package is not installed; `pip install anthropic`"
        ) from exc

    client = anthropic.Anthropic(
        api_key=provider.api_key, timeout=MODEL_FALLBACK_TIMEOUT_SECONDS
    )
    request = {
        "model": provider.model,
        "max_tokens": ANTHROPIC_MAX_OUTPUT_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }

    # Safety classifiers can decline a request outright. Server-side
    # fallbacks re-serve it on another model inside the same call, which
    # turns a hard stop into an answer; if the beta is unavailable the
    # plain call still works, so it is never a reason to fail.
    try:
        response = client.beta.messages.create(
            **request,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except Exception:
        try:
            response = client.messages.create(**request)
        except Exception as exc:
            raise ModelUnavailable(f"Anthropic request failed: {exc}") from exc

    if getattr(response, "stop_reason", None) == "refusal":
        raise ModelUnavailable("Anthropic declined the request")

    text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )
    if not text.strip():
        raise ModelUnavailable("Anthropic returned no text content")
    return text


def _call_groq(provider: Provider, system: str, user: str) -> str:
    """One Groq completion, retried once if the minute's quota is exhausted.

    Groq's free tier meters tokens per minute, so a burst of clauses can
    exhaust it even with a modest per-call budget. One short wait usually
    clears it; a long one is refused outright rather than spent, because a
    stalled fallback is worse than an unresolved cell.
    """
    payload = json.dumps(
        {
            "model": provider.model,
            "temperature": 0,
            "max_tokens": GROQ_MAX_OUTPUT_TOKENS,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        GROQ_ENDPOINT,
        data=payload,
        headers={
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
            # Groq sits behind Cloudflare, which rejects urllib's default
            # agent string outright (403, Cloudflare error 1010) before the
            # request ever reaches the API. Identify ourselves properly.
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(
                request, timeout=MODEL_FALLBACK_TIMEOUT_SECONDS
            ) as handle:
                body = json.loads(handle.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            # Carry the server's own explanation: under a submission
            # deadline "HTTP 403" alone costs minutes that the actual
            # reason does not. The response body never contains the key.
            try:
                detail = _redact(exc.read().decode("utf-8", "replace").strip())[:300]
            except Exception:
                detail = ""
            wait = _retry_after(exc)
            if exc.code == 429 and attempt == 1 and wait <= GROQ_MAX_RETRY_WAIT_SECONDS:
                time.sleep(wait)
                continue
            raise ModelUnavailable(
                f"Groq request failed: HTTP {exc.code}{f' -- {detail}' if detail else ''}"
            ) from exc
        except Exception as exc:
            raise ModelUnavailable(f"Groq request failed: {exc}") from exc

    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelUnavailable("Groq returned an unexpected response shape") from exc


#: Account identifiers a provider echoes back in error bodies. They are not
#: credentials, but diagnostics are committed as run evidence and an account
#: id does not belong in a published artifact.
_ACCOUNT_ID = re.compile(r"\borg_[A-Za-z0-9]+")


def _redact(detail: str) -> str:
    return _ACCOUNT_ID.sub("org_<redacted>", detail)


def _retry_after(exc: urllib.error.HTTPError) -> float:
    """How long the server asked us to wait, defaulting to a short pause."""
    header = exc.headers.get("retry-after") if exc.headers else None
    try:
        return max(0.0, float(header))
    except (TypeError, ValueError):
        return 5.0


def _parse_json_object(raw: str) -> dict:
    """Parse the model's reply, tolerating a markdown fence around it."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[: -len("```")]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ModelUnavailable(f"model reply contained no JSON object: {raw[:200]!r}")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ModelUnavailable(f"model returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ModelUnavailable("model returned a JSON value that is not an object")
    return parsed
