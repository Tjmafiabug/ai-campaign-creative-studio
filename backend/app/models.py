"""Data contracts for every stage of the campaign pipeline.

These Pydantic models are the "contracts" the assignment asks for: every stage
declares exactly what it accepts and what it returns, and invalid data is
rejected at the boundary rather than failing deep inside a provider call.

Read this file top-to-bottom to understand the whole app: it follows the
pipeline order — Brief -> Research -> CreativeSpec -> Assets.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Stage status — the backbone of recovery.
# ---------------------------------------------------------------------------


class StageStatus(str, Enum):
    """Lifecycle of a single pipeline stage.

    RUNNING is what makes restart-recovery possible: on boot we look for stages
    still marked RUNNING. Nothing can be running right after a restart, so any
    such row is an interrupted job and gets marked INTERRUPTED (see
    `store.mark_interrupted_stages`). Without this, a crashed campaign would
    look "in progress" forever.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class StageName(str, Enum):
    RESEARCH = "research"
    SPEC = "spec"
    IMAGES = "images"
    VIDEO = "video"


# ---------------------------------------------------------------------------
# Stage 1 — the product brief (user input).
# ---------------------------------------------------------------------------


class ProductBrief(BaseModel):
    """What the user types in. Validated server-side before anything runs."""

    product_name: str = Field(min_length=2, max_length=120)
    description: str = Field(min_length=10, max_length=2000)
    target_audience: str = Field(min_length=3, max_length=500)
    objective: str = Field(min_length=3, max_length=500)
    tone: str = Field(min_length=3, max_length=200)
    call_to_action: str = Field(min_length=2, max_length=120)

    # Optional. "Verified claims" are facts the user attests to. The agent and
    # the copywriter may ONLY use these — they must not invent benefits,
    # certifications, discounts, or performance numbers. This is the guardrail
    # the assignment asks for against fabricated claims.
    verified_claims: list[str] = Field(default_factory=list, max_length=10)

    # Optional brand tagline, rendered under the wordmark on every creative.
    # Kept on the brief rather than generated, because a tagline is a brand
    # asset the user owns — the model must not invent one.
    tagline: str | None = Field(default=None, max_length=60)

    # Optional reference packshot, stored as a path on disk (not raw bytes).
    reference_image_path: str | None = None

    @field_validator("verified_claims")
    @classmethod
    def _claims_not_empty(cls, v: list[str]) -> list[str]:
        cleaned = [c.strip() for c in v if c and c.strip()]
        for c in cleaned:
            if len(c) > 300:
                raise ValueError("each verified claim must be under 300 characters")
        return cleaned


# ---------------------------------------------------------------------------
# Stage 2 — research output.
# ---------------------------------------------------------------------------


class Source(BaseModel):
    """One real page the agent actually read.

    `accessed_at` and `excerpt` exist because the assignment requires recording
    page titles, URLs, access time, and supporting excerpts — evidence that the
    research was real rather than recalled from model memory.
    """

    title: str
    url: HttpUrl
    accessed_at: datetime = Field(default_factory=utcnow)
    excerpt: str = Field(max_length=1200)


class CreativeAngle(BaseModel):
    """One recommended creative direction.

    Note the split the assignment demands: `audience_insight` must be grounded
    in `source_urls`, while `hook` / `visual_direction` are openly labelled as
    the model's creative interpretation. Sourced observation and invention are
    kept in separate fields on purpose.
    """

    id: str
    title: str
    audience_insight: str  # grounded in sources
    hook: str  # creative interpretation
    visual_direction: str  # creative interpretation
    rationale: str
    source_urls: list[HttpUrl] = Field(default_factory=list)

    # Statistics found in this angle that do NOT appear in any retrieved source.
    # Populated by the grounding check in agent.py and surfaced in the UI so a
    # human reviewer can see which figures are ungrounded before approving.
    unsupported_numbers: list[str] = Field(default_factory=list)

    # Set when this angle rests on fewer than 2 retrieved sources. The
    # assignment asks for "supporting source links" (plural) per angle and says
    # to "report the gap" when sources are thin — so a thin angle is labelled,
    # never padded with unrelated citations.
    thin_sourcing: bool = False


class ToolCall(BaseModel):
    """A single agent tool invocation, recorded for the observable trace.

    The assignment wants tool calls, results, and decision summaries visible.
    This is the unit of that trace — it is persisted and shown in the UI.
    """

    step: int
    tool: Literal["web_search", "fetch_page"]
    arguments: dict
    result_summary: str
    decision: str  # the agent's stated reason for the next move
    started_at: datetime = Field(default_factory=utcnow)
    duration_ms: int = 0
    error: str | None = None

    # Injection-like phrases detected in the retrieved page for this call.
    # Recorded so an attempt is visible in the trace rather than handled
    # silently. See agent.detect_injection.
    injection_flags: list[str] = Field(default_factory=list)


class ResearchOutput(BaseModel):
    """Contract for the research stage."""

    angles: list[CreativeAngle] = Field(min_length=3, max_length=3)
    sources: list[Source]
    tool_calls: list[ToolCall] = Field(default_factory=list)
    # Honest reporting: if the agent could not reach 3 sources, it says so here
    # rather than padding with invented references.
    source_gap_note: str | None = None

    @field_validator("sources")
    @classmethod
    def _dedupe_sources(cls, v: list[Source]) -> list[Source]:
        seen: set[str] = set()
        out: list[Source] = []
        for s in v:
            key = str(s.url)
            if key not in seen:
                seen.add(key)
                out.append(s)
        return out


# ---------------------------------------------------------------------------
# Stage 3 — the shared creative specification.
# ---------------------------------------------------------------------------


class CreativeSpec(BaseModel):
    """The single source of truth for every generated asset.

    This is the consistency mechanism at the data level: images and video all
    read from ONE spec, so palette, copy, and product identity cannot drift
    between formats. It is versioned and stored so a rerun of a single stage
    reuses the exact same approved inputs.
    """

    spec_id: str
    version: int = 1
    angle_id: str

    hook: str = Field(max_length=120)
    headline: str = Field(max_length=80)  # burned onto images deterministically
    body_copy: str = Field(max_length=300)
    call_to_action: str = Field(max_length=40)

    product_identity: str  # how the product must look in every frame
    scene_description: str  # the master scene, shared by all assets
    palette: list[str] = Field(min_length=2, max_length=6)  # hex colors
    composition_notes: str
    video_outline: str

    # Brand identity, carried on the spec so every asset renders it identically.
    # These are burned on by Pillow, never described to the image model — the
    # whole reason brand text is stripped from image prompts is so it can be
    # rendered here, correctly spelled, instead of garbled by the model.
    brand_name: str = ""
    tagline: str | None = None

    # Text-rendering cues ("typography", "logo", the brand name) that were found
    # in model output and neutralised before this spec reached an image prompt.
    # Recorded so the substitution is visible rather than silent.
    text_cues_removed: list[str] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("palette")
    @classmethod
    def _valid_hex(cls, v: list[str]) -> list[str]:
        out = []
        for c in v:
            c = c.strip()
            if not c.startswith("#"):
                c = "#" + c
            if len(c) != 7:
                raise ValueError(f"palette color must be #RRGGBB, got {c!r}")
            int(c[1:], 16)  # raises if not valid hex
            out.append(c.upper())
        return out


# ---------------------------------------------------------------------------
# Stage 4/5 — generated assets.
# ---------------------------------------------------------------------------


class Asset(BaseModel):
    """A produced file plus the exact prompt that produced it.

    Retaining `generation_prompt` is an explicit assignment requirement and is
    what makes a result reproducible and auditable after the fact.
    """

    asset_id: str
    kind: Literal["master", "image_1x1", "image_9x16", "video"]
    path: str
    width: int
    height: int
    duration_seconds: float | None = None
    generation_prompt: str
    created_at: datetime = Field(default_factory=utcnow)


class UsageRecord(BaseModel):
    """Model/provider usage for the bounds + cost reporting requirement.

    Cost is labelled an estimate because providers do not always return billing
    data; unknowns are reported as unknown rather than guessed.
    """

    stage: StageName
    provider: str
    model: str
    calls: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    images_generated: int = 0
    estimated_cost_usd: float | None = None
    cost_is_estimate: bool = True
