"""Configuration and execution bounds, loaded once from .env.

The assignment requires configurable limits on research tool calls, retries, and
execution time. Putting them all here means the bounds are auditable in one
place rather than scattered as magic numbers through the agent loop.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# .env lives at the repo root, one level above backend/
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")


def _int(name: str, default: int) -> int:
    """Read an int from the environment, falling back if unset or malformed."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# --- Provider credentials (never hardcoded, never committed) ---------------
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
TAVILY_URL = "https://api.tavily.com/search"

# --- Models ---------------------------------------------------------------
# Gemini Flash Lite, not Claude Sonnet. Measured on the real agent task:
# 3/3 clean JSON actions, 2.2s per call (faster than Sonnet's 3.6s), at
# $0.25/$1.50 per M tokens against Sonnet's $3/$15 — roughly 12x cheaper.
# Research was 95% of this project's spend, so this is where the money was.
AGENT_MODEL = os.getenv("AGENT_MODEL", "google/gemini-3.1-flash-lite")
# Flash Lite Image, chosen by side-by-side test on an identical prompt:
# it produced a clean single-frame ad, while the pricier flash-image added a
# blank white bar across the top (a literal reading of "keep the upper third
# clean") that would have collided with the text overlay. Half the cost and the
# better result. Note it returns JPEG rather than PNG — Pillow handles both.
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "google/gemini-3.1-flash-lite-image")

# --- Bounds ---------------------------------------------------------------
# These cap the research agent so a bad model decision cannot loop forever or
# run up a bill. Every one is enforced in agent.py and surfaced in the trace.
MAX_SEARCH_CALLS = _int("MAX_SEARCH_CALLS", 5)
MAX_FETCH_CALLS = _int("MAX_FETCH_CALLS", 5)
MAX_AGENT_STEPS = _int("MAX_AGENT_STEPS", 6)
STAGE_TIMEOUT_SECONDS = _int("STAGE_TIMEOUT_SECONDS", 180)

# Per-request HTTP timeouts. Distinct from STAGE_TIMEOUT_SECONDS, which bounds
# a whole stage; these bound one provider call.
#
# These are deliberately tight. A measured image generation takes ~11-25s, so a
# request still open at 75s is hung rather than slow — waiting longer only
# delays the retry that will actually succeed. An earlier 180s image timeout
# combined with 2 retries and 3 images per stage gave a 27-minute worst case.
LLM_TIMEOUT_SECONDS = _int("LLM_TIMEOUT_SECONDS", 90)
IMAGE_TIMEOUT_SECONDS = _int("IMAGE_TIMEOUT_SECONDS", 75)
SEARCH_TIMEOUT_SECONDS = _int("SEARCH_TIMEOUT_SECONDS", 30)

# Retries for transient provider failures (timeouts, 5xx, rate limits).
# Deliberately small: a stage that fails is retryable by the user from the UI,
# so automatic retries only need to absorb brief blips.
MAX_PROVIDER_RETRIES = _int("MAX_PROVIDER_RETRIES", 2)

# --- Upload limits --------------------------------------------------------
# The assignment asks for "a reasonable limit on uploaded file type/size".
MAX_UPLOAD_BYTES = _int("MAX_UPLOAD_BYTES", 5 * 1024 * 1024)  # 5 MB
ALLOWED_UPLOAD_TYPES = {"image/png", "image/jpeg", "image/webp"}

# --- Export targets (fixed by the assignment) -----------------------------
SQUARE_SIZE = (1080, 1080)
VERTICAL_SIZE = (1080, 1920)
VIDEO_DURATION_SECONDS = 8  # assignment allows 6-10
VIDEO_FPS = 30

# --- Fixture mode ---------------------------------------------------------
# When on, every provider call is replaced by canned data. Lets a reviewer run
# the full pipeline with no API keys and no cost, and makes tests deterministic.
# Results produced this way are labelled as fixtures in the UI and API so they
# can never be mistaken for live research.
FIXTURE_MODE = os.getenv("FIXTURE_MODE", "0").strip().lower() in {"1", "true", "yes"}


def missing_keys() -> list[str]:
    """Which credentials are absent. Used for a clear startup error."""
    if FIXTURE_MODE:
        return []
    missing = []
    if not OPENROUTER_API_KEY:
        missing.append("OPENROUTER_API_KEY")
    if not TAVILY_API_KEY:
        missing.append("TAVILY_API_KEY")
    return missing
