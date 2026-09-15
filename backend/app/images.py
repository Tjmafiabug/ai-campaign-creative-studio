"""Stage 4: produce both image ads from one master scene.

The consistency strategy, concretely
------------------------------------
1. Generate ONE master scene from the spec.
2. Send that master image back to the model as an *input image*, asking it to
   recompose for each target aspect ratio. This is editing, not re-prompting:
   the 1:1 and 9:16 exports are derived from the master's pixels.
3. Crop/resize deterministically to the exact export sizes the assignment
   requires (1080x1080 and 1080x1920) — the model returns its own dimensions.
4. Burn headline and CTA on with Pillow.

Step 2 is what the assignment is asking for when it says "two unrelated
text-to-image requests are not sufficient".

Why text is drawn by Pillow and not the image model
---------------------------------------------------
The assignment explicitly permits and encourages it: "Deterministic text
overlays are acceptable and encouraged when they improve accuracy; text need not
be drawn by an image model." Image models misspell and warp letterforms. Pillow
renders the exact approved copy, every time, at a guaranteed size and position.
"""

from __future__ import annotations

import base64
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

from . import config, providers, store
from .models import Asset, CreativeSpec, StageName

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
FONT_BOLD = ASSETS_DIR / "Overlay-Bold.ttf"
FONT_REGULAR = ASSETS_DIR / "Overlay-Regular.ttf"


def _load_font(path: Path, size: int, weight: int | None = None) -> ImageFont.FreeTypeFont:
    """Load a bundled variable font at a given size and weight.

    Fonts are bundled in the repo rather than read from system paths so the
    pipeline renders identically on macOS, Linux, and in CI. Both are
    SIL Open Font License (Oswald, Inter) — safe to redistribute.
    """
    font = ImageFont.truetype(str(path), size)
    if weight is not None:
        try:
            font.set_variation_by_axes([weight])
        except (AttributeError, OSError):
            pass  # static build of the font; the default weight is fine
    return font


def _master_prompt(spec: CreativeSpec) -> str:
    """Prompt for the single master scene every other asset derives from."""
    brand = (spec.brand_name or "").upper()
    return f"""Professional advertising product photography, 16:9 landscape.

SCENE: {spec.scene_description}

PRODUCT (must appear exactly as described): {spec.product_identity}

BRANDING: the product label carries the brand name "{brand}" in bold sans-serif
capitals, correctly spelled, clean and centred on the label. This is the ONLY
text permitted anywhere in the image.

PALETTE: the overall colour grade should read as {', '.join(spec.palette)} —
apply these as lighting and material colours within the scene. Do NOT draw
colour swatches, chips, bars, or a palette strip anywhere in the frame.

COMPOSITION: {spec.composition_notes}

NO PEOPLE anywhere in the frame — no person, model, hand, arm, face, or
silhouette, in focus or blurred. This is a product photograph.

ONE single photograph from one camera at one moment. Not a split screen, not a
diptych, not a collage, not a grid — no panels, dividers, or colour swatches.

Photorealistic, commercial advertising quality, sharp focus on the product.
No text anywhere except the brand name on the product label."""


def _recompose_prompt(spec: CreativeSpec, aspect: str, headline_zone: str) -> str:
    """Prompt for editing the master into a target aspect ratio.

    Deliberately phrased as a recomposition, not a regeneration. Naming the
    elements that must survive ("the same product, the same lighting") measurably
    improves identity retention.
    """
    brand = (spec.brand_name or "").upper()
    return f"""Recompose this exact photograph into a {aspect} format.

CRITICAL: keep the identical product, the identical scene, the identical
lighting and the identical colour palette. This must read as the same
photograph reframed, not as a new photograph. Do not change the product's
shape, colour, or finish.

The product must remain exactly: {spec.product_identity}
The product label must still read "{brand}", correctly spelled and unchanged.
The colour grade must remain: {', '.join(spec.palette)} — as lighting and
materials only, never as swatches or colour bars in the frame.

Extend the surroundings naturally to fill the new aspect ratio — add more
ceiling, floor, wall or background, but do NOT push the subject or the product
out of frame to make room.

FRAMING RULES:
- NO PEOPLE: no person, hand, arm, face, or silhouette may appear.
- The product must stay clearly visible and must not be covered by anything.
- {headline_zone} should read as calm background — achieved by extending the
  scene, not by shrinking or displacing the subject.

No text anywhere except the brand name on the product label."""


def _fit_to_size(img: Image.Image, target: tuple[int, int]) -> Image.Image:
    """Cover-crop to exactly `target`, centred, preserving aspect ratio.

    The model returns its own dimensions (e.g. 1376x768), never the exact export
    size. Scaling to cover and then centre-cropping avoids the distortion that
    a direct resize would introduce — the assignment warns against "stretching
    the same image".
    """
    tw, th = target
    scale = max(tw / img.width, th / img.height)
    resized = img.resize(
        (max(tw, round(img.width * scale)), max(th, round(img.height * scale))),
        Image.LANCZOS,
    )
    left = (resized.width - tw) // 2
    top = (resized.height - th) // 2
    return resized.crop((left, top, left + tw, top + th))


def _wrap_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    """Wrap text to a pixel width, returning the lines.

    Measuring with `textlength` rather than counting characters means the result
    is correct for any font and any string. The same function is used to measure
    the block before drawing and to draw it, so the scrim can never be sized
    against a different line count than the renderer actually produces — which
    is exactly the bug that put a scrim over a subject's face.
    """
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def _busiest_band(img: Image.Image, top: int, height: int) -> float:
    """Worst-case edge energy inside a horizontal band.

    Returns the energy of the *busiest slice* within the band, not the band's
    mean. The distinction matters and was found the hard way: on a real ad the
    top 500px averaged 5.2 against the bottom's 10.6, so the mean said "put the
    copy at the top" — but that average mixed 80% empty sky with the subject's
    head sitting at the bottom edge of the band, and the CTA landed on her face.

    A band is only safe if *every* slice of it is quiet. Scoring by the worst
    slice is what makes "safe" mean safe.

    ponytail: edge density, not face detection. A face detector would be more
    precise and would mean an ML dependency and a model download for a problem
    this solves. Upgrade only if edge density keeps misplacing copy.
    """
    grey = img.convert("L")
    bottom = min(top + height, img.height)
    if bottom <= top:
        return 0.0

    edges = grey.crop((0, top, img.width, bottom)).filter(ImageFilter.FIND_EDGES)

    # Score in slices so a concentrated subject cannot hide behind empty space.
    slice_h = max(40, (bottom - top) // 6)
    worst = 0.0
    for y in range(0, edges.height, slice_h):
        chunk = edges.crop((0, y, edges.width, min(y + slice_h, edges.height)))
        worst = max(worst, ImageStat.Stat(chunk).mean[0])
    return worst


def _choose_text_anchor(
    img: Image.Image, block_height: int, *, vertical: bool
) -> tuple[int, str]:
    """Pick the top or bottom of the frame — whichever has less going on.

    Returns (y_of_block_top, "top" | "bottom").

    Why only two candidates rather than a free slide: ad copy belongs anchored to
    an edge. A text block floating in the vertical centre reads as a mistake even
    when it sits over empty pixels.
    """
    margin = int(img.width * 0.07)
    top_y = margin if not vertical else int(img.height * 0.06)
    bottom_y = max(margin, img.height - block_height - margin)

    top_energy = _busiest_band(img, top_y, block_height)
    bottom_energy = _busiest_band(img, bottom_y, block_height)

    # Prefer the top when the two are genuinely close: headlines read better
    # above the subject, and Meta crops the bottom of feed creatives more often.
    #
    # The margin is 5%, not 15%. A wider threshold was measured misplacing copy
    # on a real scene where the bottom was 12% quieter — a real difference that
    # the rule discarded. 5% is still enough to stop the block flip-flopping
    # between runs on sensor noise.
    if bottom_energy < top_energy * 0.95:
        return bottom_y, "bottom"
    return top_y, "top"


def _draw_brand_lockup(
    canvas: Image.Image,
    spec: CreativeSpec,
    *,
    vertical: bool,
) -> None:
    """Draw the brand wordmark and tagline bottom-left, on top of a soft scrim.

    Rendered by Pillow rather than described to the image model. That is the
    entire reason brand text is stripped from image prompts upstream: a
    diffusion model spells brand names wrong, while this renders the exact
    string every time.

    Bottom-left is the Meta feed convention — far from the headline block, and
    it reads as a signature rather than competing with the message.
    """
    if not spec.brand_name:
        return

    width, height = canvas.size
    margin = int(width * 0.07)

    mark_size = int(width * (0.036 if vertical else 0.032))
    tag_size = int(width * (0.024 if vertical else 0.021))

    font_mark = _load_font(FONT_BOLD, mark_size, weight=700)
    font_tag = _load_font(FONT_REGULAR, tag_size, weight=400)

    draw = ImageDraw.Draw(canvas)
    ma, md = font_mark.getmetrics()
    ta, td = font_tag.getmetrics()

    wordmark = spec.brand_name.upper()
    tagline = (spec.tagline or "").strip()

    block_h = (ma + md) + (int((ta + td) * 1.1) if tagline else 0)
    baseline = height - margin - block_h

    # A soft dark scrim behind the lockup so it stays legible on a light scene.
    pad = int(width * 0.025)
    scrim = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(scrim).rounded_rectangle(
        [
            margin - pad,
            baseline - pad,
            margin + max(
                draw.textlength(wordmark, font=font_mark),
                draw.textlength(tagline, font=font_tag) if tagline else 0,
            )
            + pad,
            baseline + block_h + pad,
        ],
        radius=int(pad * 0.8),
        fill=(8, 9, 12, 140),
    )
    canvas.alpha_composite(scrim)

    draw = ImageDraw.Draw(canvas)
    draw.text((margin, baseline), wordmark, font=font_mark, fill=(255, 255, 255, 255))
    if tagline:
        draw.text(
            (margin, baseline + (ma + md)),
            tagline,
            font=font_tag,
            fill=(215, 215, 220, 255),
        )


def _apply_overlay(
    img: Image.Image, spec: CreativeSpec, *, vertical: bool
) -> Image.Image:
    """Burn the approved headline, body copy, and CTA onto the image.

    A scrim (semi-transparent dark panel) sits behind the text so copy stays
    legible regardless of what the generated scene looks like underneath. Without
    it, a light headline over a bright window would be unreadable — and the
    assignment requires the copy to be legible.
    """
    canvas = img.convert("RGBA")
    width, height = canvas.size

    margin = int(width * 0.07)
    max_text_width = width - 2 * margin

    # Type sizes are a fraction of width so they scale with any export size.
    headline_size = int(width * (0.072 if vertical else 0.058))
    body_size = int(width * (0.030 if vertical else 0.025))
    cta_size = int(width * (0.034 if vertical else 0.028))

    font_headline = _load_font(FONT_BOLD, headline_size, weight=700)
    font_body = _load_font(FONT_REGULAR, body_size, weight=400)
    font_cta = _load_font(FONT_BOLD, cta_size, weight=600)

    # --- measure the real block height -------------------------------------
    # Previously this estimated line counts with textwrap against an assumed
    # character width, which over-padded the scrim and pushed it down over the
    # subject's face on square crops. Wrapping with the same measurement the
    # renderer uses means the scrim is exactly as tall as the text needs.
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    # Shrink the headline until the whole block fits inside the frame. Copy comes
    # from a model and its length is not fully predictable: an end card built
    # from a 120-character hook once overflowed the frame and rendered
    # "...24G PROTEIN THA", truncated mid-word. Measuring and shrinking removes
    # that whole class of failure rather than fixing one field.
    max_block_fraction = 0.42 if not vertical else 0.38
    headline_lines = _wrap_to_width(
        probe, spec.headline.upper(), font_headline, max_text_width
    )
    while headline_size > int(width * 0.028) and (
        len(headline_lines) * int(sum(font_headline.getmetrics()) * 1.1)
        > height * max_block_fraction * 0.55
    ):
        headline_size = int(headline_size * 0.9)
        font_headline = _load_font(FONT_BOLD, headline_size, weight=700)
        headline_lines = _wrap_to_width(
            probe, spec.headline.upper(), font_headline, max_text_width
        )

    # Body copy is dropped from the square format. A 1080x1080 canvas cannot
    # carry headline + 2 lines of body + a CTA without the block reaching ~36%
    # of the frame, which buries the product no matter where it is anchored.
    # Real Meta feed creatives put the headline and CTA on the image and leave
    # the longer copy in the post text, so this matches the placement rather
    # than shrinking type until it is unreadable. The 9:16 format has the
    # vertical room and keeps it.
    body_lines = (
        _wrap_to_width(probe, spec.body_copy, font_body, max_text_width)
        if vertical
        else []
    )

    gap_after_headline = int(height * 0.016) if body_lines else 0
    gap_before_cta = int(height * 0.024)

    ha, hd = font_headline.getmetrics()
    ba, bd = font_body.getmetrics()
    ca, cd = font_cta.getmetrics()

    headline_h = int((ha + hd) * 1.1) * len(headline_lines)
    body_h = int((ba + bd) * 1.15) * len(body_lines)
    cta_h = (ca + cd) + 2 * int(cta_size * 0.45)

    block_height = (
        headline_h + gap_after_headline + body_h + gap_before_cta + cta_h
    )

    # --- place it where the image is quietest ------------------------------
    scrim_pad = int(width * 0.04)
    top, anchor = _choose_text_anchor(
        img, block_height + 2 * scrim_pad, vertical=vertical
    )

    # --- scrim: a gradient, not a hard band --------------------------------
    # A hard-edged rectangle reads as a UI panel bolted onto a photo. A gradient
    # that fades toward the subject keeps copy legible while letting the image
    # breathe, which is how real ad creative handles this.
    scrim = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    scrim_top = max(0, top - scrim_pad)
    scrim_bottom = min(height, top + block_height + scrim_pad)
    fade = int((scrim_bottom - scrim_top) * 0.35)
    draw_scrim = ImageDraw.Draw(scrim)

    solid_end = scrim_bottom - fade if anchor == "top" else scrim_bottom
    solid_start = scrim_top if anchor == "top" else scrim_top + fade
    draw_scrim.rectangle([0, solid_start, width, solid_end], fill=(8, 9, 12, 190))

    for i in range(fade):
        alpha = int(190 * (1 - i / fade))
        y = (solid_end + i) if anchor == "top" else (solid_start - i)
        draw_scrim.rectangle([0, y, width, y + 1], fill=(8, 9, 12, alpha))

    canvas = Image.alpha_composite(canvas, scrim)

    # --- draw the copy ------------------------------------------------------
    draw = ImageDraw.Draw(canvas)
    y = top

    for line in headline_lines:
        draw.text((margin, y), line, font=font_headline, fill=(255, 255, 255))
        y += int((ha + hd) * 1.1)

    y += gap_after_headline
    for line in body_lines:
        draw.text((margin, y), line, font=font_body, fill=(230, 230, 234))
        y += int((ba + bd) * 1.15)

    y += gap_before_cta
    cta_text = spec.call_to_action.upper()
    text_w = draw.textlength(cta_text, font=font_cta)
    pad_x, pad_y = int(cta_size * 0.9), int(cta_size * 0.45)

    accent = spec.palette[-1] if len(spec.palette) > 2 else "#FFFFFF"
    accent_rgb = tuple(int(accent[i : i + 2], 16) for i in (1, 3, 5))
    luminance = 0.299 * accent_rgb[0] + 0.587 * accent_rgb[1] + 0.114 * accent_rgb[2]
    cta_fill = (17, 17, 17) if luminance > 140 else (255, 255, 255)

    draw.rounded_rectangle(
        [margin, y, margin + text_w + 2 * pad_x, y + (ca + cd) + 2 * pad_y],
        radius=int(cta_size * 0.3),
        fill=accent_rgb,
    )
    draw.text((margin + pad_x, y + pad_y), cta_text, font=font_cta, fill=cta_fill)

    _draw_brand_lockup(canvas, spec, vertical=vertical)

    return canvas.convert("RGB")


def generate_images(
    campaign_id: str,
    spec: CreativeSpec,
    *,
    on_progress=None,
) -> list[Asset]:
    """Produce master, 1:1, and 9:16 assets. Returns saved Asset records."""

    def progress(message: str) -> None:
        if on_progress:
            on_progress(message)

    out_dir = store.ARTIFACT_DIR / campaign_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- 1. master scene -------------------------------------------------
    usage_records: list[dict] = []

    progress("Generating master scene")
    master_prompt = _master_prompt(spec)
    master_bytes = providers.generate_image(master_prompt, usage_sink=usage_records)
    master_path = out_dir / "master.png"
    master_path.write_bytes(master_bytes)

    with Image.open(master_path) as m:
        master_size = m.size

    assets = [
        Asset(
            asset_id=f"{campaign_id}_master",
            kind="master",
            path=str(master_path),
            width=master_size[0],
            height=master_size[1],
            generation_prompt=master_prompt,
        )
    ]

    master_b64 = base64.b64encode(master_bytes).decode()

    # --- 2. recompose into each format ----------------------------------
    formats = [
        ("image_1x1", config.SQUARE_SIZE, "1:1 square", "The upper area", False),
        (
            "image_9x16",
            config.VERTICAL_SIZE,
            "9:16 vertical portrait",
            "The upper area",
            True,
        ),
    ]

    for kind, target, aspect, zone, vertical in formats:
        progress(f"Recomposing master into {aspect}")
        prompt = _recompose_prompt(spec, aspect, zone)
        edited = providers.generate_image(
            prompt, source_image_b64=master_b64, usage_sink=usage_records
        )

        raw_path = out_dir / f"{kind}_raw.png"
        raw_path.write_bytes(edited)

        with Image.open(raw_path) as img:
            sized = _fit_to_size(img.convert("RGB"), target)
            final = _apply_overlay(sized, spec, vertical=vertical)
            final_path = out_dir / f"{kind}.png"
            final.save(final_path, "PNG", optimize=True)

        assets.append(
            Asset(
                asset_id=f"{campaign_id}_{kind}",
                kind=kind,  # type: ignore[arg-type]
                path=str(final_path),
                width=target[0],
                height=target[1],
                generation_prompt=prompt,
            )
        )

    for asset in assets:
        store.save_asset(campaign_id, asset.model_dump(mode="json"))

    store.record_stage_usage(campaign_id, StageName.IMAGES.value, usage_records)

    return assets
