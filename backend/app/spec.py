"""Stage 3: turn the selected angle into one shared creative specification.

Why this stage exists at all
----------------------------
It would be possible to send the angle straight to the image model twice. The
assignment rules that out: "Two unrelated text-to-image requests are not
sufficient without a deliberate consistency strategy."

The spec is that strategy at the data level. Every downstream asset — the master
image, both format exports, and the video — reads from this one object. Palette,
product identity, and approved copy therefore cannot drift between formats,
because there is only one place they are written down.

Copy safety
-----------
`headline`, `body_copy`, and `call_to_action` are the exact strings later burned
onto the images by Pillow. They are validated here, before generation, because
fixing copy after four assets have been rendered means re-rendering all four.
"""

from __future__ import annotations

import json
import re
import uuid

from . import providers
from .models import CreativeAngle, CreativeSpec, ProductBrief

SPEC_PROMPT = """RESPOND_WITH_SPEC

You are an art director converting an approved creative angle into a production
specification. Every downstream asset is generated from this spec alone, so it
must be self-contained and unambiguous.

PRODUCT
Name: {product_name}
Description: {description}
Audience: {target_audience}
Objective: {objective}
Tone: {tone}
Required call to action: {cta}

VERIFIED CLAIMS — the ONLY product facts you may state in copy:
{claims}

APPROVED ANGLE
Title: {angle_title}
Audience insight: {insight}
Hook: {hook}
Visual direction: {visual}

WRITE THE SPECIFICATION

Copy rules:
- `headline`: max 60 characters. It is rendered as real text over the image, so
  it must be short enough to read at a glance on a phone. No statistics unless
  they appear in the verified claims above.
- `body_copy`: max 200 characters. One or two short sentences.
- `call_to_action`: use the required CTA above, or a close variant under 30
  characters.
- Never invent certifications, discounts, clinical results, or comparisons to
  competitors.

Scene rules:
- `scene_description` is ONE single photograph taken by one camera at one moment
  in one place. It will be generated once as a master image and then recomposed
  into other formats, so it must work both wide and tall. Describe subject,
  setting, lighting, and mood.
- **NO PEOPLE.** No person, model, athlete, hand, arm, face, silhouette, or
  figure of any kind — not in focus, not blurred in the background. This is a
  product photograph.

  The scene is built from the product plus supporting objects and environment:
  a gym bench, a kitchen counter, a gym bag, a towel, a shaker bottle, a water
  bottle, weights, a phone, morning light through a window, a locker room, a
  commuter setting shown through its objects rather than its people.

  Convey the audience insight through *context and styling*, not by showing a
  person. "Built for the 5AM crowd" becomes pre-dawn light and a packed gym bag,
  not someone holding the product.
- **Never describe a split screen, a diptych, a collage, a grid, a before/after,
  a montage, side-by-side panels, or two scenes in one frame.** These produce a
  moodboard, not an advertisement. If the angle contrasts two moments, choose
  the single stronger moment and photograph that one.
- The product label carries the brand name, and that is the ONLY text in the
  scene. Do not describe any other text, words, slogans, sub-branding, signage,
  or typography — headline and CTA are added afterwards by the rendering
  pipeline, and invented packaging copy comes out misspelled.
- `product_identity` describes how the product must look in every single frame:
  shape, colour, finish, label treatment. This is what keeps the product
  recognisable across formats.
- `composition_notes` must specify where empty space is reserved for the
  headline and CTA overlays.
- `palette`: 3 or 4 hex colours (#RRGGBB) that the scene should read as.

Respond with JSON only:
{{"headline": "...", "body_copy": "...", "call_to_action": "...",
  "product_identity": "...", "scene_description": "...",
  "palette": ["#RRGGBB", "..."], "composition_notes": "...",
  "video_outline": "..."}}"""


# Text that must never reach an image prompt: asking a diffusion model for
# words produces garbled glyphs. All copy is overlaid deterministically instead.
_TEXT_IN_SCENE_RE = re.compile(
    r"\b(?:text|words?|headline|slogan|tagline|logos?|label reading|"
    r"typography|lettering|caption|written|says|reads|branding|"
    r"sans-serif|serif|font)\b",
    re.I,
)

# Layout words that turn a photograph into a moodboard. A live run produced a
# scene beginning "A high-contrast split-screen composition. Left side... Right
# side..." and the image model rendered exactly that — two photos in one frame
# with a colour-swatch strip. An ad is one photograph.
_MULTI_PANEL_RE = re.compile(
    r"\b(?:split[- ]screen|split screen|diptych|triptych|collage|montage|"
    r"side[- ]by[- ]side|grid of|before and after|before/after|two panels?|"
    r"left side|right side|left half|right half|mood ?board)\b",
    re.I,
)

_SINGLE_FRAME_CLAUSE = (
    " This is ONE single photograph from one camera at one moment — not a split "
    "screen, diptych, collage, or montage, and with no panels, dividers, or "
    "colour swatches anywhere in the frame."
)

# People in a generated scene are the single largest source of visible defects:
# warped hands, wrong joint counts, cropped heads. Measured cost is identical
# with or without them ($0.03361 either way — image pricing tracks output
# resolution, not scene complexity), so removing people buys reliability for
# free. Detection is here because the prompt asking for it is a request, not a
# guarantee.
_PEOPLE_RE = re.compile(
    r"\b(?:person|people|man|woman|men|women|athlete|model|figure|someone|"
    r"individual|professional|commuter|gym-?goer|trainer|guy|girl|"
    r"hand|hands|arm|arms|face|faces|silhouette|human)\b",
    re.I,
)

_NO_PEOPLE_CLAUSE = (
    " NO PEOPLE: no person, model, hand, arm, face, silhouette, or human figure "
    "anywhere in the frame, in focus or blurred. This is a product photograph."
)

# The brand name on the product label is the ONE permitted piece of rendered
# text, added back deliberately in the image prompt. Everything else — invented
# sub-branding, slogans on packaging, signage in the scene — is still stripped,
# because that is what the model gets wrong.
#
# This was originally a blanket "no text at all" rule, written on the assumption
# that the image model could not spell. Re-tested against the current model:
# 3/3 generations rendered "BEASTLIFE" and "BEASTLIFE WHEY CORE" correctly, so
# the blanket ban was costing a real brand asset for no reason.
_NO_OTHER_TEXT_CLAUSE = (
    " Apart from the brand name on the product label, render no other text, "
    "words, letters, numbers, logos, or typography anywhere in the image."
)

def _truncate_words(text: str, limit: int) -> str:
    """Trim to `limit` characters without cutting a word in half.

    A plain slice produced "BeastLife Whey Core mixes in wa" on a live run,
    which was then rendered onto an ad. Ending on a word boundary keeps the
    copy readable even when the model overruns its length budget.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut or text[:limit]


def _strip_text_cues(value: str) -> tuple[str, list[str]]:
    """Remove text-rendering cues from any string bound for an image prompt.

    Catches typography vocabulary ("sans-serif", "label reading", "lettering")
    in every field that reaches the image prompt, not just `scene_description` —
    a live run put "sans-serif typography" in `product_identity`, which was not
    being checked at the time.

    The brand name is deliberately NOT removed. An earlier version replaced it
    with "the product", on the assumption that naming the brand would make the
    model letter it onto the label badly. That assumption no longer holds: the
    model spells the brand correctly (3/3 on re-test), and `_master_prompt` now
    asks for it on the label explicitly. Stripping it here meant the prompt
    simultaneously said "render no letters anywhere" and "put BEASTLIFE on the
    label" — while degrading the scene description to "the product tub" on the
    way. The substitution and its cleanup passes are gone with it.

    Returns the cleaned string and the cues that were found, so the change is
    observable rather than silent.
    """
    flags = _TEXT_IN_SCENE_RE.findall(value)

    # Quoted fragments are lettering the model is being asked to render — e.g.
    # "a bold white 'Whey Core' sub-branding". The brand name on the label is
    # requested separately and deliberately in _master_prompt; anything else
    # quoted here is invented packaging copy and comes out misspelled.
    cleaned = re.sub(r"['‘’\"“”][^'‘’\"“”]{1,40}['‘’\"“”]", "", value)

    if _PEOPLE_RE.search(cleaned):
        flags.append("people")
        cleaned = cleaned.rstrip(". ") + "." + _NO_PEOPLE_CLAUSE

    if flags:
        cleaned = cleaned.rstrip(". ") + "." + _NO_OTHER_TEXT_CLAUSE

    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()

    return cleaned, sorted(set(f.lower() for f in flags))


def build_spec(
    brief: ProductBrief,
    angle: CreativeAngle,
    *,
    version: int = 1,
    usage_sink: list[dict] | None = None,
) -> CreativeSpec:
    """Generate and validate the shared creative specification.

    Raises RuntimeError if the model output cannot be coerced into a valid spec —
    the stage fails loudly here rather than letting a malformed spec reach four
    downstream generation calls.
    """
    claims = (
        "\n".join(f"- {c}" for c in brief.verified_claims)
        if brief.verified_claims
        else "(none supplied — state no product benefit as fact)"
    )

    prompt = SPEC_PROMPT.format(
        product_name=brief.product_name,
        description=brief.description,
        target_audience=brief.target_audience,
        objective=brief.objective,
        tone=brief.tone,
        cta=brief.call_to_action,
        claims=claims,
        angle_title=angle.title,
        insight=angle.audience_insight,
        hook=angle.hook,
        visual=angle.visual_direction,
    )

    raw, _usage = providers.chat(
        [{"role": "user", "content": prompt}],
        max_tokens=1500,
        temperature=0.6,
    )
    if usage_sink is not None:
        usage_sink.append(_usage)

    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError(f"Spec generation returned no JSON: {raw[:200]!r}")

    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Spec JSON was malformed: {exc}") from exc

    scene = str(data.get("scene_description", "")).strip()
    if not scene:
        raise RuntimeError("Spec is missing a scene description")

    # Every field that reaches an image prompt gets the same sanitising pass.
    # The prompt already asks for no text; this enforces it rather than trusting
    # it, and records what was found.
    scene, scene_flags = _strip_text_cues(scene)

    # Enforce single-frame composition. The prompt asks for it; this checks it.
    # A live run returned "A high-contrast split-screen composition. Left side:
    # ... Right side: ..." and the image model produced a two-panel moodboard
    # with a colour-swatch strip instead of an advertisement.
    if _MULTI_PANEL_RE.search(scene):
        scene_flags.append("multi-panel")
        scene = scene.rstrip(". ") + "." + _SINGLE_FRAME_CLAUSE

    identity = str(data.get("product_identity", "")).strip() or brief.product_name
    identity, identity_flags = _strip_text_cues(identity)

    composition = str(data.get("composition_notes", "")).strip()
    composition, composition_flags = _strip_text_cues(composition)
    text_cue_flags = sorted(set(scene_flags + identity_flags + composition_flags))

    # Truncate rather than reject: an over-long headline is a formatting problem,
    # not a reason to fail a stage the user has already paid a model call for.
    # Truncation is on a WORD boundary — a plain [:80] slice produced
    # "...BeastLife Whey Core mixes in wa" on a real run, which rendered onto an
    # ad as a visibly cut-off word.
    headline = _truncate_words(str(data.get("headline", "")).strip(), 80)
    if not headline:
        raise RuntimeError("Spec is missing a headline")

    cta = str(data.get("call_to_action", "")).strip()[:40] or brief.call_to_action[:40]

    palette = data.get("palette") or ["#111111", "#F5F5F3", "#D8FF3E"]
    if len(palette) < 2:
        palette = list(palette) + ["#111111", "#F5F5F3"]

    return CreativeSpec(
        spec_id=uuid.uuid4().hex[:12],
        version=version,
        angle_id=angle.id,
        hook=angle.hook[:120],
        headline=headline,
        body_copy=str(data.get("body_copy", "")).strip()[:300],
        call_to_action=cta,
        product_identity=identity,
        scene_description=scene,
        palette=palette[:6],
        composition_notes=composition,
        video_outline=str(data.get("video_outline", "")).strip(),
        brand_name=brief.product_name,
        tagline=brief.tagline,
        text_cues_removed=text_cue_flags,
    )
