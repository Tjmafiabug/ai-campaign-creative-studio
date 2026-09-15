"""Stage 5: render a vertical MP4 from the approved images.

Pipeline shape
--------------
No new image generation happens here. The video is built from the *already
approved* 9:16 asset, so it cannot drift from what the user signed off on — the
same consistency guarantee that holds between the two image formats.

  approved 9:16 image
      -> Pillow renders each distinct frame state (clean / headline / CTA)
      -> FFmpeg animates a slow push-in and cross-fades between them
      -> H.264 MP4, 1080x1920, 8 seconds

Why Pillow renders the text and not FFmpeg
------------------------------------------
FFmpeg's `drawtext` filter requires a build compiled with libfreetype. The build
on this machine — and plenty of default installs, including many Docker images —
does not have it:

    $ ffmpeg -filters | grep drawtext
    (no output)
    libfreetype NOT compiled in

Rather than depend on a filter that may be absent, text frames are rendered with
Pillow (already a dependency, already used for the image overlays) and FFmpeg
only composites and encodes. Two benefits beyond portability: the video's
typography is produced by the *same code* as the images, so it matches exactly;
and the pipeline works on any FFmpeg build.

The assignment requires the video to be rendered by the application's pipeline,
which this is — FFmpeg is driven programmatically, nothing is hand-assembled.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from . import config, store
from .images import (
    FONT_BOLD,
    FONT_REGULAR,
    _apply_overlay,
    _fit_to_size,
    _load_font,
    _wrap_to_width,
)
from .models import Asset, CreativeSpec


class VideoError(RuntimeError):
    """Raised when rendering fails. Message is shown to the user verbatim."""


def _ffmpeg_path() -> str:
    """Locate ffmpeg, failing with an actionable message rather than a traceback."""
    path = shutil.which("ffmpeg")
    if not path:
        raise VideoError(
            "ffmpeg is not installed or not on PATH. Install it with "
            "`brew install ffmpeg` (macOS) or `apt install ffmpeg` (Linux)."
        )
    return path


def _text_layer(
    size: tuple[int, int],
    render,
) -> Image.Image:
    """Render one transparent RGBA layer that FFmpeg can animate on its own.

    Each text element becomes a separate layer so it can enter at its own time
    with its own motion. FFmpeg's `drawtext` filter would normally do this, but
    it needs a build compiled with libfreetype and this one is not:

        $ ffmpeg -filters | grep drawtext
        (no output)

    Rendering layers with Pillow instead keeps the pipeline portable AND makes
    the video's typography identical to the image ads, because it is the same
    font loading and the same wrapping code.
    """
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    render(ImageDraw.Draw(layer), layer)
    return layer


def _render_frames(spec: CreativeSpec, source: Path, out_dir: Path) -> dict[str, Path]:
    """Render the still and the transparent type layers the video animates.

    Returns paths keyed by role:
      scene    - the clean product photograph, no copy
      headline - transparent layer, headline only
      body     - transparent layer, body copy only
      cta      - transparent layer, CTA pill only
      endcard  - solid end frame

    Splitting the copy into layers is what allows staggered entrances: the
    headline slides up, the body fades in beneath it, the CTA pops last.
    """
    with Image.open(source) as img:
        base = _fit_to_size(img.convert("RGB"), config.VERTICAL_SIZE)

    width, height = config.VERTICAL_SIZE
    margin = int(width * 0.07)
    max_text_width = width - 2 * margin

    frames: dict[str, Path] = {}

    scene_path = out_dir / "v_scene.png"
    base.save(scene_path, "PNG")
    frames["scene"] = scene_path

    # --- type sizes, matched to the 9:16 image ad ------------------------
    headline_size = int(width * 0.072)
    body_size = int(width * 0.030)
    cta_size = int(width * 0.034)

    font_headline = _load_font(FONT_BOLD, headline_size, weight=700)
    font_body = _load_font(FONT_REGULAR, body_size, weight=400)
    font_cta = _load_font(FONT_BOLD, cta_size, weight=600)

    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    headline_lines = _wrap_to_width(
        probe, spec.headline.upper(), font_headline, max_text_width
    )
    body_lines = _wrap_to_width(probe, spec.body_copy, font_body, max_text_width)

    ha, hd = font_headline.getmetrics()
    ba, bd = font_body.getmetrics()
    ca, cd = font_cta.getmetrics()

    headline_h = int((ha + hd) * 1.1) * len(headline_lines)
    body_h = int((ba + bd) * 1.15) * len(body_lines)

    top = int(height * 0.10)
    body_top = top + headline_h + int(height * 0.02)
    cta_top = body_top + body_h + int(height * 0.03)

    # --- scrim, rendered as its own layer so it can fade in first --------
    scrim_bottom = cta_top + (ca + cd) + 2 * int(cta_size * 0.45) + int(width * 0.05)

    def draw_scrim(draw, layer):
        fade = int(scrim_bottom * 0.30)
        draw.rectangle([0, 0, width, scrim_bottom - fade], fill=(8, 9, 12, 190))
        for i in range(fade):
            alpha = int(190 * (1 - i / fade))
            y = scrim_bottom - fade + i
            draw.rectangle([0, y, width, y + 1], fill=(8, 9, 12, alpha))

    scrim_path = out_dir / "v_scrim.png"
    _text_layer((width, height), draw_scrim).save(scrim_path, "PNG")
    frames["scrim"] = scrim_path

    # --- headline layer ---------------------------------------------------
    def draw_headline(draw, layer):
        y = top
        for line in headline_lines:
            draw.text((margin, y), line, font=font_headline, fill=(255, 255, 255, 255))
            y += int((ha + hd) * 1.1)

    headline_path = out_dir / "v_headline.png"
    _text_layer((width, height), draw_headline).save(headline_path, "PNG")
    frames["headline"] = headline_path

    # --- body layer -------------------------------------------------------
    def draw_body(draw, layer):
        y = body_top
        for line in body_lines:
            draw.text((margin, y), line, font=font_body, fill=(230, 230, 234, 255))
            y += int((ba + bd) * 1.15)

    body_path = out_dir / "v_body.png"
    _text_layer((width, height), draw_body).save(body_path, "PNG")
    frames["body"] = body_path

    # --- CTA layer --------------------------------------------------------
    accent = spec.palette[-1] if len(spec.palette) > 2 else "#FFFFFF"
    accent_rgb = _hex_to_rgb(accent)
    luminance = (
        0.299 * accent_rgb[0] + 0.587 * accent_rgb[1] + 0.114 * accent_rgb[2]
    )
    cta_fill = (17, 17, 17, 255) if luminance > 140 else (255, 255, 255, 255)

    def draw_cta(draw, layer):
        text = spec.call_to_action.upper()
        text_w = draw.textlength(text, font=font_cta)
        pad_x, pad_y = int(cta_size * 0.9), int(cta_size * 0.45)
        draw.rounded_rectangle(
            [margin, cta_top, margin + text_w + 2 * pad_x,
             cta_top + (ca + cd) + 2 * pad_y],
            radius=int(cta_size * 0.3),
            fill=(*accent_rgb, 255),
        )
        draw.text(
            (margin + pad_x, cta_top + pad_y), text, font=font_cta, fill=cta_fill
        )

    cta_path = out_dir / "v_cta.png"
    _text_layer((width, height), draw_cta).save(cta_path, "PNG")
    frames["cta"] = cta_path

    # --- end card ---------------------------------------------------------
    endcard = Image.new("RGB", config.VERTICAL_SIZE, _hex_to_rgb(spec.palette[0]))
    end_spec = spec.model_copy(update={"headline": spec.headline, "body_copy": ""})
    endcard_path = out_dir / "v_endcard.png"
    _apply_overlay(endcard, end_spec, vertical=True).save(endcard_path, "PNG")
    frames["endcard"] = endcard_path

    return frames


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _build_filter(duration: int, fps: int) -> str:
    """Build the animated filter graph.

    Motion design, in order:

      0.0s  scene fades up from black and begins a slow continuous push-in
      1.2s  scrim fades in
      1.5s  headline slides up into place while fading in
      2.4s  body copy fades in beneath it
      3.1s  CTA pops in
      6.0s  cross-fade to the end card

    Everything is expressed with `overlay` and its time expressions rather than
    `drawtext`, because this FFmpeg build has no libfreetype. Each type element
    is a pre-rendered transparent PNG, so it can be animated independently —
    which is what makes the entrances staggered rather than one hard cut.

    Two filter details worth knowing:
      - `overlay` alpha is animated by pre-multiplying the layer with
        `colorchannelmixer=aa=<expr>`; `enable=` alone would pop it on instantly.
      - `zoompan` `d` is in FRAMES while `xfade` `offset` is in SECONDS. Mixing
        those units is the classic FFmpeg mistake, so both derive from `fps`.
    """
    w, h = config.VERTICAL_SIZE
    total = duration * fps
    end_at = duration - 2.0
    fade = 0.5

    # Slow continuous push-in across the whole scene segment.
    zoom_step = round(0.12 / total, 6)

    # Entrance timings (seconds).
    t_scrim, t_head, t_body, t_cta = 1.2, 1.5, 2.4, 3.1
    rise = 0.45      # how long the headline takes to slide up
    rise_px = 60     # how far it travels

    return (
        # --- scene: push in, vignette, fade up from black -------------------
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
        f"zoompan=z='min(zoom+{zoom_step},1.12)':d={total}:s={w}x{h}:fps={fps},"
        f"vignette=PI/5,"
        f"fade=t=in:st=0:d=0.6,"
        f"trim=duration={end_at + fade},setpts=PTS-STARTPTS,format=yuv420p[scene];"

        # --- scrim ----------------------------------------------------------
        # `fade` with alpha=1 is the correct way to ramp a transparent layer.
        # An earlier attempt used colorchannelmixer=aa='<time expression>',
        # which fails: `aa` takes a static number, not an expression —
        #   "Undefined constant or missing '(' in 't-1.2)/0.5,1))'"
        f"[1:v]format=rgba,fade=t=in:st={t_scrim}:d=0.5:alpha=1[scrim];"
        f"[scene][scrim]overlay=0:0:format=auto[s1];"

        # --- headline: slides up while fading in ----------------------------
        f"[2:v]format=rgba,fade=t=in:st={t_head}:d={rise}:alpha=1[head];"
        f"[s1][head]overlay="
        f"x=0:y='if(lt(t,{t_head}),{rise_px},"
        f"{rise_px}-{rise_px}*min((t-{t_head})/{rise},1))':format=auto[s2];"

        # --- body copy: fades in --------------------------------------------
        f"[3:v]format=rgba,fade=t=in:st={t_body}:d=0.5:alpha=1[body];"
        f"[s2][body]overlay=0:0:format=auto[s3];"

        # --- CTA: pops in with a short rise ---------------------------------
        f"[4:v]format=rgba,fade=t=in:st={t_cta}:d=0.35:alpha=1[cta];"
        f"[s3][cta]overlay="
        f"x=0:y='if(lt(t,{t_cta}),24,24-24*min((t-{t_cta})/0.35,1))':format=auto"
        f"[body_seg];"

        # --- end card --------------------------------------------------------
        f"[5:v]scale={w}:{h},fps={fps},"
        f"trim=duration={duration - end_at},setpts=PTS-STARTPTS,"
        f"format=yuv420p[end];"

        # --- final cross-fade -------------------------------------------------
        f"[body_seg][end]xfade=transition=fade:duration={fade}:offset={end_at}[v]"
    )


def render_video(
    campaign_id: str,
    spec: CreativeSpec,
    *,
    on_progress=None,
) -> Asset:
    """Render the campaign video. Raises VideoError with a readable message."""

    def progress(message: str) -> None:
        if on_progress:
            on_progress(message)

    out_dir = store.ARTIFACT_DIR / campaign_id

    # Use the *un-overlaid* recomposed scene as the video's base. `image_9x16.png`
    # is the finished ad with copy already burned in; building the video from it
    # produced a "clean" opening frame that already carried the headline, so the
    # reveal had nothing to reveal. The raw file is the same recomposed scene
    # before the overlay pass.
    source = out_dir / "image_9x16_raw.png"
    if not source.exists():
        source = out_dir / "image_9x16.png"  # older campaigns, pre-raw-file
    if not source.exists():
        raise VideoError(
            "The 9:16 image has not been generated yet. Run the image stage first."
        )

    progress("Rendering video frames")
    frames = _render_frames(spec, source, out_dir)

    output = out_dir / "campaign.mp4"
    duration = config.VIDEO_DURATION_SECONDS
    fps = config.VIDEO_FPS

    # Input order must match the [0:v]..[5:v] labels in the filter graph.
    cmd = [
        _ffmpeg_path(),
        "-y",
        "-loop", "1", "-t", str(duration), "-i", str(frames["scene"]),
        "-loop", "1", "-t", str(duration), "-i", str(frames["scrim"]),
        "-loop", "1", "-t", str(duration), "-i", str(frames["headline"]),
        "-loop", "1", "-t", str(duration), "-i", str(frames["body"]),
        "-loop", "1", "-t", str(duration), "-i", str(frames["cta"]),
        "-loop", "1", "-t", str(duration), "-i", str(frames["endcard"]),
        "-filter_complex", _build_filter(duration, fps),
        "-map", "[v]",
        "-t", str(duration),
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        # faststart moves the index to the front so the file streams before it
        # has fully downloaded — required for sane playback in a browser.
        "-movflags", "+faststart",
        "-r", str(fps),
        str(output),
    ]

    progress(f"Encoding {duration}s video at {config.VERTICAL_SIZE[0]}x{config.VERTICAL_SIZE[1]}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.STAGE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoError(
            f"ffmpeg timed out after {config.STAGE_TIMEOUT_SECONDS}s. "
            f"The stage can be retried."
        ) from exc

    if result.returncode != 0:
        # Surface the last few lines of stderr — ffmpeg puts the actual cause
        # there, and a bare "returncode 1" is useless to whoever has to fix it.
        tail = "\n".join(result.stderr.strip().splitlines()[-6:])
        raise VideoError(f"ffmpeg failed (exit {result.returncode}):\n{tail}")

    if not output.exists() or output.stat().st_size == 0:
        raise VideoError("ffmpeg reported success but produced no output file.")

    asset = Asset(
        asset_id=f"{campaign_id}_video",
        kind="video",
        path=str(output),
        width=config.VERTICAL_SIZE[0],
        height=config.VERTICAL_SIZE[1],
        duration_seconds=float(duration),
        generation_prompt=(
            f"FFmpeg render from approved 9:16 asset. "
            f"Outline: {spec.video_outline}"
        ),
    )
    store.save_asset(campaign_id, asset.model_dump(mode="json"))
    return asset
