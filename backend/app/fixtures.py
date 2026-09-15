"""Canned provider responses for FIXTURE_MODE.

Why this exists: the assignment requires "a reproducible fixture/mock mode for
review" and insists fixture research must never be presented as live browsing.

Two uses:
  1. A reviewer with no API keys can run the entire pipeline end to end.
  2. Tests become deterministic and free.

Everything produced here is explicitly marked as a fixture. The URLs below are
real, publicly reachable pages, but in fixture mode they are NOT fetched — the
text is canned. The UI labels any fixture-mode campaign accordingly so a fixture
run can never be mistaken for real research.
"""

from __future__ import annotations

import hashlib
import io
import json
from typing import Any

from PIL import Image, ImageDraw

FIXTURE_NOTICE = "FIXTURE MODE — canned data, no live network call was made."


# ---------------------------------------------------------------------------
# Search + page fetch
# ---------------------------------------------------------------------------

_FIXTURE_PAGES = [
    {
        "title": "Protein Intake for Active Adults — Position Stand",
        "url": "https://jissn.biomedcentral.com/articles/10.1186/s12970-017-0177-8",
        "content": (
            "For building and maintaining muscle mass, an overall daily protein "
            "intake in the range of 1.4 to 2.0 g protein per kg bodyweight per day "
            "is sufficient for most exercising individuals. Protein doses should "
            "ideally be evenly distributed across the day, every 3 to 4 hours. "
            "Timing relative to training is less critical than total daily intake "
            "for most people, which means convenience and consistency matter more "
            "than a narrow post-workout window."
        ),
    },
    {
        "title": "Why Busy Professionals Skip Post-Workout Nutrition",
        "url": "https://www.garagegymreviews.com/guide-to-protein-powder",
        "content": (
            "Survey responses repeatedly cite time as the main barrier to "
            "post-workout nutrition. Gym-goers training before work report leaving "
            "the gym and heading straight to a commute, with no practical way to "
            "prepare food. Powder formats that mix in water without a blender are "
            "consistently rated as the most realistic option for this group."
        ),
    },
    {
        "title": "Supplement Category Trends: Convenience Formats",
        "url": "https://www.nutraingredients.com/Article/2024/01/15/protein-trends",
        "content": (
            "Category data shows sustained growth in single-serve and "
            "easy-mixing formats. Consumers increasingly describe supplement "
            "routines in terms of friction: the fewer steps between finishing a "
            "session and consuming protein, the more likely the routine is to be "
            "sustained over months rather than weeks."
        ),
    },
]


def fixture_search(query: str, max_results: int = 4) -> list[dict[str, Any]]:
    """Return canned results regardless of the query.

    `query` is accepted but deliberately ignored: fixture mode must return the
    same pages every run so that tests and reviewer walkthroughs are
    reproducible. The parameter exists to match the real `web_search` signature
    so callers need no branching.
    """
    del query  # intentionally unused — see docstring
    return [
        {**page, "content": f"[{FIXTURE_NOTICE}] {page['content']}"}
        for page in _FIXTURE_PAGES[:max_results]
    ]


def fixture_fetch(url: str) -> dict[str, Any]:
    for page in _FIXTURE_PAGES:
        if page["url"] == url:
            return {**page, "content": f"[{FIXTURE_NOTICE}] {page['content']}"}
    return {
        "title": "Fixture page",
        "url": url,
        "content": f"[{FIXTURE_NOTICE}] No fixture content for this URL.",
    }


# ---------------------------------------------------------------------------
# Text model
# ---------------------------------------------------------------------------


def fixture_chat(messages: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Return canned model output shaped to whatever the caller asked for.

    Dispatches on a marker in the FINAL user message. Inspecting only the last
    message (rather than a truncated blob of the whole conversation) matters:
    an earlier version searched the last 4000 characters and the marker scrolled
    out of that window once the conversation grew, so synthesis silently fell
    through to the agent-loop branch.
    """
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "fixture": True}

    last = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content")
            last = content if isinstance(content, str) else json.dumps(content)
            break

    if "RESPOND_WITH_ANGLES" in last:
        return json.dumps({"angles": _FIXTURE_ANGLES}), usage

    if "RESPOND_WITH_SPEC" in last:
        return json.dumps(_FIXTURE_SPEC), usage

    # Agent loop. Count how many searches have already happened by looking for
    # evidence blocks the loop appended, then finish once we have enough.
    evidence_turns = sum(
        1
        for m in messages
        if m.get("role") == "user"
        and isinstance(m.get("content"), str)
        and "<untrusted_content" in m["content"]
    )

    if evidence_turns == 0:
        return (
            json.dumps(
                {
                    "action": "web_search",
                    "arguments": {"query": "protein powder busy gym goers convenience"},
                    "decision": "Starting with a broad search on the core audience "
                    "friction before narrowing.",
                }
            ),
            usage,
        )

    if evidence_turns == 1:
        return (
            json.dumps(
                {
                    "action": "fetch_page",
                    "arguments": {"url": _FIXTURE_PAGES[0]["url"]},
                    "decision": "The position-stand snippet looks authoritative but "
                    "is too short to draw a conclusion from; reading it in full.",
                }
            ),
            usage,
        )

    return (
        json.dumps(
            {
                "action": "finish",
                "arguments": {},
                "decision": "Three sources gathered covering intake guidance, the "
                "time barrier, and category format trends. Enough to ground three "
                "distinct angles.",
            }
        ),
        usage,
    )


_FIXTURE_ANGLES = [
    {
        "id": "angle_1",
        "title": "The 3-Hour Window, Not the 30-Minute One",
        "audience_insight": "Published position-stand guidance says even "
        "distribution across the day matters more than a narrow post-workout "
        "window, yet busy gym-goers still believe they have thirty minutes.",
        "hook": "You don't have 30 minutes. You have all day.",
        "visual_direction": "Calm morning gym light, tub on a clean surface, "
        "wide negative space, no clock imagery.",
        "rationale": "Removes the urgency anxiety that makes people skip protein "
        "entirely when they cannot hit an imagined deadline.",
        "source_urls": [
            "https://jissn.biomedcentral.com/articles/10.1186/s12970-017-0177-8"
        ],
    },
    {
        "id": "angle_2",
        "title": "Fewer Steps, More Months",
        "audience_insight": "Category data frames supplement adherence as a "
        "friction problem: fewer steps between session and serving predicts "
        "routines lasting months rather than weeks.",
        "hook": "Mixes in water. That's the whole routine.",
        "visual_direction": "Single-shot product on concrete, one hand, no "
        "blender, no clutter.",
        "rationale": "Sells sustainability rather than intensity, which suits "
        "an introduction objective.",
        "source_urls": [
            "https://www.nutraingredients.com/Article/2024/01/15/protein-trends"
        ],
    },
    {
        "id": "angle_3",
        "title": "Built For The Commute",
        "audience_insight": "Gym-goers training before work report going "
        "straight to a commute with no practical way to prepare food.",
        "hook": "From the rack to the train, handled.",
        "visual_direction": "Early light, gym bag and tub, muted industrial "
        "palette, motion implied not shown.",
        "rationale": "Speaks to a specific, verifiable moment in the target "
        "audience's day rather than a generic fitness aspiration.",
        "source_urls": ["https://www.garagegymreviews.com/guide-to-protein-powder"],
    },
]


_FIXTURE_SPEC = {
    "hook": "You don't have 30 minutes. You have all day.",
    "headline": "PROTEIN ON YOUR SCHEDULE",
    "body_copy": "Even daily intake beats a rushed window. Mixes in water, "
    "wherever the day takes you.",
    "call_to_action": "Explore the range",
    "product_identity": "A matte black cylindrical protein tub with a ribbed "
    "screw lid and a clean unbranded label band.",
    "scene_description": "The matte black protein tub standing on a polished "
    "concrete gym floor, bright diffused morning light from tall windows, a "
    "blurred athlete training in the far background, shallow depth of field.",
    "palette": ["#111111", "#F2F2F0", "#7A7A78", "#D8FF3E"],
    "composition_notes": "Product sits low and centre-weighted with generous "
    "empty space above for the headline. No text rendered in the image itself.",
    "video_outline": "Open on the product in morning light, slow push in, "
    "headline fades up, cut to the vertical framing, end on the CTA frame.",
}


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def fixture_image(prompt: str, source_image_b64: str | None = None) -> bytes:
    """Deterministic placeholder image.

    Colour is derived from a hash of the prompt so the same prompt always yields
    the same image (tests stay stable) while different prompts are visually
    distinguishable. Clearly labelled FIXTURE so it cannot be mistaken for a
    real generation.
    """
    digest = hashlib.sha256(prompt.encode()).digest()
    base = (digest[0] // 3 + 20, digest[1] // 3 + 20, digest[2] // 3 + 20)

    # Landscape when generating fresh, portrait when "editing" — mirrors the
    # shape change the real model produces, so downstream crop logic is tested.
    size = (768, 1376) if source_image_b64 else (1408, 768)
    img = Image.new("RGB", size, base)
    draw = ImageDraw.Draw(img)

    draw.rectangle(
        [size[0] * 0.3, size[1] * 0.45, size[0] * 0.7, size[1] * 0.8],
        fill=(base[0] // 2, base[1] // 2, base[2] // 2),
    )
    label = "FIXTURE IMAGE (edited)" if source_image_b64 else "FIXTURE IMAGE (master)"
    draw.text((30, 30), label, fill=(255, 255, 255))
    draw.text((30, 50), prompt[:90], fill=(210, 210, 210))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
