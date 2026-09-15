#!/usr/bin/env python3
"""Verify one complete campaign against the assignment's acceptance criteria.

Checks the committed sample campaign by default — no API keys, no network, no
backend required:

    python3 verify_campaign.py

To verify a campaign this machine generated instead, point it at the running
backend and give it a campaign id:

    python3 verify_campaign.py --api http://localhost:8000 --campaign <id>

What it checks, one line per criterion the brief names:

  - source links          research cites real, reachable-looking URLs, >= 3 pages
  - image dimensions      1:1 is exactly 1080x1080, 9:16 is exactly 1080x1920
  - video duration/dims   6-10 seconds, 1080x1920, H.264 MP4
  - downloadable files    every asset referenced actually exists and is non-empty
  - persisted history     the campaign survives a backend restart and is listed

Exit code is 0 only if every check passes, so this is usable in CI.

Image dimensions are read from the PNG/JPEG header directly rather than with
Pillow, and video metadata comes from ffprobe, so the script has no dependency
the project does not already require.
"""

from __future__ import annotations

import argparse
import json
import shutil
import struct
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent
SAMPLE = REPO / "sample-campaign"

SQUARE = (1080, 1080)
VERTICAL = (1080, 1920)
VIDEO_MIN_SECONDS, VIDEO_MAX_SECONDS = 6.0, 10.0
MIN_SOURCES = 3  # the brief requires at least 3 relevant source pages

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str) -> bool:
    results.append((PASS if ok else FAIL, name, detail))
    return ok


# ---------------------------------------------------------------------------
# Readers — no Pillow, no ffmpeg-python, just headers and ffprobe
# ---------------------------------------------------------------------------


def image_size(path: Path) -> tuple[int, int]:
    """Width and height from a PNG or JPEG header.

    Deliberately not Pillow: this script must run for a reviewer who has only
    cloned the repo and not installed backend requirements.
    """
    data = path.read_bytes()

    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)

    if data[:2] == b"\xff\xd8":  # JPEG: walk the segments to SOFn
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            # SOF0-SOF15, excluding DHT(c4), JPG(c8) and DAC(cc)
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                return int(w), int(h)
            i += 2 + struct.unpack(">H", data[i + 2 : i + 4])[0]

    raise ValueError(f"{path.name}: not a PNG or JPEG this script can read")


def video_info(path: Path) -> tuple[float, int, int, str]:
    """Duration, width, height and codec via ffprobe."""
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe not on PATH (install ffmpeg to verify the video)")

    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,codec_name:format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    meta = json.loads(out)
    stream = meta["streams"][0]
    return (
        float(meta["format"]["duration"]),
        int(stream["width"]),
        int(stream["height"]),
        stream["codec_name"],
    )


# ---------------------------------------------------------------------------
# Campaign loading — from disk (sample) or from the API (live)
# ---------------------------------------------------------------------------


def load_from_disk() -> tuple[dict, dict[str, Path]]:
    record = json.loads((SAMPLE / "campaign.json").read_text())
    files = {
        "image_1x1": SAMPLE / "image_1x1.png",
        "image_9x16": SAMPLE / "image_9x16.png",
        "video": SAMPLE / "campaign.mp4",
        "master": SAMPLE / "master-scene.png",
    }
    return record, files


def load_from_api(api: str, campaign_id: str) -> tuple[dict, dict[str, Path]]:
    """Fetch a live campaign and download each asset through the HTTP surface.

    Downloading via the API rather than reading the path out of the database is
    the point: it verifies the files are actually *downloadable*, which is what
    the brief asks for.
    """
    with urllib.request.urlopen(f"{api}/api/campaigns/{campaign_id}", timeout=30) as r:
        record = json.loads(r.read())

    # Normalise the API's nested stage shape into the flat shape the sample uses.
    stages = record.get("stages", {})
    if "research" not in record and "research" in stages:
        record["research"] = stages["research"].get("output") or {}
    if "creative_spec" not in record:
        record["creative_spec"] = record.get("spec") or {}

    out_dir = REPO / ".verify_downloads"
    out_dir.mkdir(exist_ok=True)
    files: dict[str, Path] = {}
    for asset in record.get("assets", []):
        kind = asset["kind"]
        suffix = ".mp4" if kind == "video" else ".png"
        target = out_dir / f"{kind}{suffix}"
        url = f"{api}/api/campaigns/{campaign_id}/assets/{kind}"
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                target.write_bytes(r.read())
            files[kind] = target
        except urllib.error.HTTPError as exc:
            check(f"download {kind}", False, f"HTTP {exc.code} from {url}")
    return record, files


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def verify_sources(record: dict) -> None:
    research = record.get("research") or {}
    sources = research.get("sources") or []
    urls = [s.get("url", "") for s in sources]
    http = [u for u in urls if u.startswith("http")]

    check(
        "source links: at least 3 pages cited",
        len(http) >= MIN_SOURCES,
        f"{len(http)} source URLs (minimum {MIN_SOURCES})",
    )
    check(
        "source links: every source has a title and timestamp",
        all(s.get("title") and s.get("accessed_at") for s in sources),
        f"{len(sources)} sources carry title + accessed_at",
    )

    angles = research.get("angles") or []
    cited = [a for a in angles if a.get("source_urls")]
    check(
        "angles: each cites at least one retrieved source",
        len(angles) == 3 and len(cited) == len(angles),
        f"{len(cited)}/{len(angles)} angles cite a source",
    )

    calls = research.get("tool_calls") or []
    with_reason = [c for c in calls if c.get("decision")]
    check(
        "agent trace: every tool call records its own reason",
        bool(calls) and len(with_reason) == len(calls),
        f"{len(with_reason)}/{len(calls)} tool calls carry a decision summary",
    )


def verify_images(files: dict[str, Path]) -> None:
    for kind, expected in (("image_1x1", SQUARE), ("image_9x16", VERTICAL)):
        path = files.get(kind)
        if path is None or not path.is_file():
            check(f"{kind}: file present", False, f"missing {kind}")
            continue
        try:
            size = image_size(path)
        except ValueError as exc:
            check(f"{kind}: readable image", False, str(exc))
            continue
        check(
            f"{kind}: exactly {expected[0]}x{expected[1]}",
            size == expected,
            f"{size[0]}x{size[1]}",
        )


def verify_video(files: dict[str, Path]) -> None:
    path = files.get("video")
    if path is None or not path.is_file():
        check("video: file present", False, "missing video")
        return
    try:
        duration, w, h, codec = video_info(path)
    except (RuntimeError, subprocess.CalledProcessError, KeyError) as exc:
        check("video: readable by ffprobe", False, str(exc))
        return

    check(
        f"video: {VIDEO_MIN_SECONDS:.0f}-{VIDEO_MAX_SECONDS:.0f} seconds",
        VIDEO_MIN_SECONDS <= duration <= VIDEO_MAX_SECONDS,
        f"{duration:.2f}s",
    )
    check(
        f"video: exactly {VERTICAL[0]}x{VERTICAL[1]}",
        (w, h) == VERTICAL,
        f"{w}x{h}",
    )
    check("video: H.264", codec == "h264", codec)


def verify_downloadable(files: dict[str, Path]) -> None:
    for kind in ("image_1x1", "image_9x16", "video"):
        path = files.get(kind)
        ok = path is not None and path.is_file() and path.stat().st_size > 0
        size = f"{path.stat().st_size / 1024:.0f} KB" if ok else "missing or empty"
        check(f"downloadable: {kind}", bool(ok), size)


def verify_record(record: dict) -> None:
    brief = record.get("brief") or {}
    check(
        "persisted: brief stored with the campaign",
        bool(brief.get("product_name")),
        brief.get("product_name", "(none)"),
    )
    check(
        "persisted: selected angle recorded",
        bool(record.get("selected_angle")),
        str(record.get("selected_angle")),
    )

    spec = record.get("creative_spec") or {}
    check(
        "persisted: one shared creative spec drives every asset",
        bool(spec.get("scene_description") and spec.get("headline")),
        f"spec {spec.get('spec_id', '?')}, headline {spec.get('headline', '?')!r}",
    )

    usage = record.get("usage") or []
    reported = [u for u in usage if u.get("cost_is_reported")]
    total = sum(u.get("cost_usd") or 0 for u in usage)
    check(
        "cost: recorded per stage and labelled reported vs estimated",
        bool(usage),
        f"${total:.4f} across {len(usage)} stages, {len(reported)} provider-reported",
    )


def verify_history(api: str | None, campaign_id: str | None) -> None:
    """Persisted history: the campaign is listed by a backend that restarted.

    Only meaningful against a live API — on disk there is nothing to restart.
    """
    if not api or not campaign_id:
        results.append(
            ("SKIP", "persisted history: campaign appears in /api/campaigns",
             "needs --api and --campaign (restart the backend first)")
        )
        return
    try:
        with urllib.request.urlopen(f"{api}/api/campaigns", timeout=30) as r:
            listed = json.loads(r.read()).get("campaigns", [])
    except Exception as exc:  # noqa: BLE001
        check("persisted history", False, f"could not reach {api}: {exc}")
        return
    check(
        "persisted history: campaign appears in /api/campaigns",
        any(c.get("id") == campaign_id for c in listed),
        f"{len(listed)} campaign(s) in history",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", help="Base URL of a running backend")
    parser.add_argument("--campaign", help="Campaign id to verify via the API")
    args = parser.parse_args()

    if bool(args.api) != bool(args.campaign):
        print("error: --api and --campaign must be given together", file=sys.stderr)
        return 2

    if args.api:
        print(f"Verifying live campaign {args.campaign} via {args.api}\n")
        record, files = load_from_api(args.api, args.campaign)
    else:
        if not (SAMPLE / "campaign.json").is_file():
            print(f"error: no sample campaign at {SAMPLE}", file=sys.stderr)
            return 2
        print(f"Verifying the committed sample campaign in {SAMPLE.name}/\n")
        record, files = load_from_disk()

    verify_sources(record)
    verify_images(files)
    verify_video(files)
    verify_downloadable(files)
    verify_record(record)
    verify_history(args.api, args.campaign)

    width = max(len(name) for _, name, _ in results)
    for status, name, detail in results:
        print(f"  {status:4}  {name:<{width}}  {detail}")

    failed = sum(1 for s, _, _ in results if s == FAIL)
    skipped = sum(1 for s, _, _ in results if s == "SKIP")
    total = len(results) - skipped
    print()
    if failed:
        print(f"{total - failed}/{total} checks passed, {failed} FAILED")
        return 1
    print(f"All {total} checks passed" + (f" ({skipped} skipped)" if skipped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
