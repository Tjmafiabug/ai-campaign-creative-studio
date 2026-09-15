"""All outbound calls to external services live here.

Keeping every provider call in one module means timeouts, retries, and usage
accounting are implemented once rather than repeated at each call site — and
fixture mode can be applied in a single place.

Two providers:
  - OpenRouter: text (research agent) and images (generation + editing)
  - Tavily:     web search
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import httpx

from . import config, fixtures


class ProviderError(RuntimeError):
    """A provider call failed in a way the user should see.

    Carries `retryable` so the orchestrator can distinguish a transient blip
    (worth retrying) from a permanent problem such as a malformed request.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def _post_with_retry(
    url: str,
    *,
    headers: dict[str, str] | None,
    payload: dict[str, Any],
    timeout: int,
    label: str,
) -> dict[str, Any]:
    """POST JSON with bounded retries on transient failures.

    Retries on timeouts, connection errors, 429 and 5xx — the failures that are
    usually worth trying again. Does NOT retry 4xx like 400/401/403, because a
    malformed request or a bad key will fail identically every time; retrying
    those just wastes the user's time and money.
    """
    last_error: str = "unknown error"

    for attempt in range(config.MAX_PROVIDER_RETRIES + 1):
        try:
            response = httpx.post(
                url, headers=headers, json=payload, timeout=timeout
            )
        except httpx.TimeoutException:
            last_error = f"{label} timed out after {timeout}s"
        except httpx.HTTPError as exc:
            last_error = f"{label} connection error: {type(exc).__name__}"
        else:
            if response.status_code == 200:
                try:
                    return response.json()
                except json.JSONDecodeError:
                    raise ProviderError(
                        f"{label} returned a non-JSON response", retryable=False
                    ) from None

            body = response.text[:300]

            # 402 is ambiguous and must be read, not assumed. OpenRouter returns
            # it both for "you are out of money" (permanent) and for
            # `in_flight_budget_exhausted` (transient — concurrent requests have
            # reserved more than the balance covers, and it clears by itself).
            # A live run hit the transient form and the stage failed immediately
            # because 402 was bucketed with the permanent 4xx errors.
            transient_402 = response.status_code == 402 and (
                "in_flight" in body or "in-flight" in body
            )

            if response.status_code == 429 or response.status_code >= 500 or transient_402:
                last_error = f"{label} returned {response.status_code}: {body}"
            else:
                # Permanent: bad key, malformed request, content refusal, or a
                # genuinely empty account.
                raise ProviderError(
                    f"{label} returned {response.status_code}: {body}",
                    retryable=False,
                )

        # Backoff before the next attempt. Budget and rate-limit pressure needs
        # longer to clear than a network blip: 1s/2s is enough for a dropped
        # connection but not for concurrent requests to settle and release their
        # reservations, so those back off 5s/10s instead.
        if attempt < config.MAX_PROVIDER_RETRIES:
            slow = "in_flight" in last_error or "429" in last_error
            time.sleep((5 * 2**attempt) if slow else (2**attempt))

    raise ProviderError(
        f"{last_error} (after {config.MAX_PROVIDER_RETRIES + 1} attempts)",
        retryable=True,
    )


# ---------------------------------------------------------------------------
# Tavily — web search
# ---------------------------------------------------------------------------


def web_search(query: str, max_results: int = 4) -> list[dict[str, Any]]:
    """Search the web. Returns [{title, url, content}].

    `content` is Tavily's extracted page text — this is what the agent reads as
    evidence. It is untrusted input and is wrapped as such by the agent before
    ever reaching the model (see agent.py).
    """
    if config.FIXTURE_MODE:
        return fixtures.fixture_search(query, max_results)

    data = _post_with_retry(
        config.TAVILY_URL,
        headers=None,
        payload={
            "api_key": config.TAVILY_API_KEY,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        },
        timeout=config.SEARCH_TIMEOUT_SECONDS,
        label="Tavily search",
    )

    return [
        {
            "title": r.get("title", "Untitled"),
            "url": r.get("url", ""),
            "content": (r.get("content") or "")[:2000],
        }
        for r in data.get("results", [])
        if r.get("url")
    ]


def fetch_page(url: str) -> dict[str, Any]:
    """Read one specific page in more depth.

    Implemented via Tavily's extraction rather than a raw HTTP GET plus an HTML
    parser. Reason: Tavily returns cleaned main-article text, which avoids
    pulling in navigation, ads, and scripts — and avoids adding an HTML parsing
    dependency for a single call site.

    ponytail: search-with-the-url-as-query is a deliberate shortcut over a
    dedicated extract endpoint. Upgrade to Tavily /extract if page fidelity
    ever matters more than it does here.
    """
    if config.FIXTURE_MODE:
        return fixtures.fixture_fetch(url)

    data = _post_with_retry(
        config.TAVILY_URL,
        headers=None,
        payload={
            "api_key": config.TAVILY_API_KEY,
            "query": url,
            "max_results": 3,
            "search_depth": "advanced",
            "include_raw_content": True,
        },
        timeout=config.SEARCH_TIMEOUT_SECONDS,
        label="Tavily fetch",
    )

    results = data.get("results", [])
    # Prefer the result whose URL matches what was asked for.
    match = next((r for r in results if r.get("url") == url), None)
    if match is None and results:
        match = results[0]
    if match is None:
        raise ProviderError(f"No content retrieved for {url}", retryable=False)

    text = match.get("raw_content") or match.get("content") or ""
    return {
        "title": match.get("title", "Untitled"),
        "url": match.get("url", url),
        "content": text[:6000],
    }


# ---------------------------------------------------------------------------
# OpenRouter — text
# ---------------------------------------------------------------------------


def chat(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    max_tokens: int = 4000,
    temperature: float = 0.7,
) -> tuple[str, dict[str, Any]]:
    """Call a text model. Returns (content, usage)."""
    if config.FIXTURE_MODE:
        return fixtures.fixture_chat(messages)

    data = _post_with_retry(
        config.OPENROUTER_URL,
        headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"},
        payload={
            "model": model or config.AGENT_MODEL,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            # Ask OpenRouter to return what it actually billed. Computing cost
            # from published list prices was measured 7x too low, because image
            # tokens bill at a different rate than the listed text rate.
            "usage": {"include": True},
        },
        timeout=config.LLM_TIMEOUT_SECONDS,
        label="OpenRouter chat",
    )

    choices = data.get("choices") or []
    if not choices:
        raise ProviderError("OpenRouter returned no choices", retryable=True)

    content = choices[0].get("message", {}).get("content") or ""
    usage = dict(data.get("usage") or {})
    usage["model"] = model or config.AGENT_MODEL
    return content, usage


# ---------------------------------------------------------------------------
# OpenRouter — images
# ---------------------------------------------------------------------------


def generate_image(
    prompt: str,
    *,
    source_image_b64: str | None = None,
    usage_sink: list[dict[str, Any]] | None = None,
) -> bytes:
    """Generate an image, optionally editing an existing one.

    When `source_image_b64` is supplied the model receives that image alongside
    the prompt and edits it. That is the mechanism behind the whole consistency
    strategy: the 1:1 and 9:16 exports are edits of one master image, not
    independent generations.

    Returns raw PNG bytes.
    """
    if config.FIXTURE_MODE:
        return fixtures.fixture_image(prompt, source_image_b64)

    if source_image_b64:
        content: Any = [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{source_image_b64}"},
            },
        ]
    else:
        content = prompt

    data = _post_with_retry(
        config.OPENROUTER_URL,
        headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"},
        payload={
            "model": config.IMAGE_MODEL,
            "messages": [{"role": "user", "content": content}],
            "modalities": ["image", "text"],
            "usage": {"include": True},
        },
        timeout=config.IMAGE_TIMEOUT_SECONDS,
        label="OpenRouter image",
    )

    choices = data.get("choices") or []
    if not choices:
        raise ProviderError("Image model returned no choices", retryable=True)

    images = choices[0].get("message", {}).get("images") or []
    if not images:
        # This happens when the model refuses or replies with text instead.
        text = (choices[0].get("message", {}).get("content") or "")[:200]
        raise ProviderError(
            f"Image model returned no image. Model said: {text!r}", retryable=True
        )

    data_url = images[0].get("image_url", {}).get("url", "")
    if "," not in data_url:
        raise ProviderError("Image model returned a malformed data URL", retryable=True)

    # Image usage was previously discarded, which is why the `usage` table sat
    # empty despite the assignment requiring cost reporting. Collected via a
    # caller-supplied list so the cost travels back without changing the
    # function's return type for every existing call site.
    if usage_sink is not None:
        usage = dict(data.get("usage") or {})
        usage["model"] = config.IMAGE_MODEL
        usage["images_generated"] = 1
        usage_sink.append(usage)

    return base64.b64decode(data_url.split(",", 1)[1])
