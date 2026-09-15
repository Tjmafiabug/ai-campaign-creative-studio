"""Stage orchestration: what runs, in what order, and what happens on failure.

The dependency graph
--------------------
    research  ->  spec  ->  images  ->  video
                             (needs a user-selected angle between research and spec)

Everything is sequential here, and that is a deliberate answer to the
assignment's "explain what can run concurrently and what must wait":

  - `spec` needs the angle the *user* picked from `research`, so it waits on a
    human decision, not just on data.
  - `images` needs the spec's scene description.
  - `video` needs the rendered 9:16 image.

The one genuinely parallelisable step is generating the 1:1 and 9:16 variants,
since both derive from the same master. They are run sequentially anyway —
see the note in `run_stage`.

Retry semantics
---------------
`run_stage` is the single entry point for every stage. It claims the stage
atomically (rejecting duplicate submissions), runs it, and records success or
failure. A stage that already succeeded is skipped unless `force=True`, which is
what makes "retry the failed video without re-running research" work.
"""

from __future__ import annotations

import traceback
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any, Callable

from . import agent, config, images, spec as spec_module, store, video
from .models import (
    CreativeAngle,
    CreativeSpec,
    ProductBrief,
    ResearchOutput,
    StageName,
    StageStatus,
)

# Which stage must have succeeded before each stage may run.
STAGE_DEPENDENCIES: dict[StageName, StageName | None] = {
    StageName.RESEARCH: None,
    StageName.SPEC: StageName.RESEARCH,
    StageName.IMAGES: StageName.SPEC,
    StageName.VIDEO: StageName.IMAGES,
}

# Assets discarded before a stage re-runs, so a retry replaces rather than
# accumulates partial output from the failed attempt.
STAGE_ASSETS: dict[StageName, list[str]] = {
    StageName.IMAGES: ["master", "image_1x1", "image_9x16"],
    StageName.VIDEO: ["video"],
}


class StageBlocked(RuntimeError):
    """A stage cannot run yet because its dependency has not succeeded."""


class StageBusy(RuntimeError):
    """A stage is already running. Raised on duplicate submission."""


class StageTimeout(RuntimeError):
    """A stage exceeded STAGE_TIMEOUT_SECONDS and was abandoned."""


def _check_dependency(campaign_id: str, stage: StageName) -> None:
    dependency = STAGE_DEPENDENCIES[stage]
    if dependency is None:
        return
    upstream = store.get_stage(campaign_id, dependency)
    if not upstream or upstream["status"] != StageStatus.SUCCEEDED.value:
        raise StageBlocked(
            f"Cannot run '{stage.value}': the '{dependency.value}' stage has not "
            f"succeeded yet."
        )


def run_stage(
    campaign_id: str,
    stage: StageName,
    *,
    force: bool = False,
    already_claimed: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run one pipeline stage with claim, dependency check, and error capture.

    This is the only path by which a stage executes, which is what makes the
    guarantees uniform: every stage gets the same duplicate protection, the same
    dependency enforcement, and the same failure recording.
    """
    campaign = store.get_campaign(campaign_id)
    if campaign is None:
        raise ValueError(f"No campaign {campaign_id!r}")

    existing = store.get_stage(campaign_id, stage)
    if existing and existing["status"] == StageStatus.SUCCEEDED.value and not force:
        # Reusing a successful stage's output is the assignment's explicit
        # requirement — a retry of a later stage must not redo this work.
        return {"skipped": True, "reason": "already succeeded", "output": existing["output"]}

    _check_dependency(campaign_id, stage)

    # Atomic claim. Returns False when the stage is already RUNNING, which is
    # how a double-clicked button is rejected.
    #
    # `already_claimed` is set by the retry endpoint, which claims in the request
    # so it can return an honest 409 to the losers rather than telling five
    # clients their work started when only one did.
    if not already_claimed and not store.claim_stage(campaign_id, stage):
        raise StageBusy(f"The '{stage.value}' stage is already running.")

    # Clear this stage's previous artifacts so a retry replaces them.
    store.clear_assets(campaign_id, STAGE_ASSETS.get(stage, []))

    def progress(message: str) -> None:
        if on_progress:
            on_progress(message)

    try:
        # Hard stage ceiling. Previously STAGE_TIMEOUT_SECONDS was only checked
        # between research-agent steps, so the images stage had no stage-level
        # bound at all: one hung provider request could consume
        # IMAGE_TIMEOUT x (retries+1) x 3 images — a 27-minute worst case
        # observed as a stage appearing to hang.
        #
        # ponytail: a thread with a timeout, not a cancellation protocol. The
        # worker thread is abandoned rather than killed (Python cannot safely
        # kill a thread mid-request), so an abandoned request keeps its socket
        # until the HTTP timeout fires. Acceptable because the per-request
        # timeouts are tight and the stage is already marked failed and
        # retryable. A process-per-stage model would allow true cancellation.
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(_execute, campaign_id, campaign, stage, progress)
            try:
                output = future.result(timeout=config.STAGE_TIMEOUT_SECONDS)
            except FuturesTimeout:
                raise StageTimeout(
                    f"The '{stage.value}' stage exceeded its "
                    f"{config.STAGE_TIMEOUT_SECONDS}s limit and was stopped. "
                    f"This usually means a provider request hung. Retry it."
                ) from None
        finally:
            # `wait=False` is the whole point: the default ThreadPoolExecutor
            # context manager calls shutdown(wait=True), which blocks until the
            # abandoned worker finishes — so a 3s timeout still took 30s to
            # surface. Not waiting means the timeout is honoured on time.
            pool.shutdown(wait=False)
    except Exception as exc:  # noqa: BLE001 — every failure must be recorded
        # Record the failure with a readable message. The full traceback goes to
        # the server log, not to the user, but the exception text does reach the
        # UI because it usually contains the actionable part (a provider error,
        # a missing dependency, a timeout).
        detail = f"{type(exc).__name__}: {exc}"
        store.finish_stage(
            campaign_id, stage, StageStatus.FAILED, error=detail[:1000]
        )
        print(f"[stage:{stage.value}] FAILED\n{traceback.format_exc()}")
        raise

    store.finish_stage(campaign_id, stage, StageStatus.SUCCEEDED, output=output)
    return {"skipped": False, "output": output}


def _execute(
    campaign_id: str,
    campaign: dict[str, Any],
    stage: StageName,
    progress: Callable[[str], None],
) -> dict[str, Any]:
    """Dispatch to the stage implementation and return its serialised output."""
    brief = ProductBrief(**campaign["brief"])

    if stage is StageName.RESEARCH:
        usage: list[dict[str, Any]] = []
        result = agent.run_research(brief, on_progress=progress, usage_sink=usage)
        store.record_stage_usage(campaign_id, stage.value, usage)
        return result.model_dump(mode="json")

    if stage is StageName.SPEC:
        research_stage = store.get_stage(campaign_id, StageName.RESEARCH)
        research = ResearchOutput(**research_stage["output"])  # type: ignore[index]

        angle_id = campaign.get("selected_angle")
        if not angle_id:
            raise StageBlocked(
                "No creative angle has been selected. Choose one of the three "
                "researched angles before generating the specification."
            )

        angle = next((a for a in research.angles if a.id == angle_id), None)
        if angle is None:
            raise ValueError(f"Selected angle {angle_id!r} is not in the research output")

        progress(f"Building specification from angle: {angle.title}")
        usage = []
        result = spec_module.build_spec(
            brief, CreativeAngle(**angle.model_dump()), usage_sink=usage
        )
        store.record_stage_usage(campaign_id, stage.value, usage)
        payload = result.model_dump(mode="json")
        store.save_spec(campaign_id, payload)
        return payload

    if stage is StageName.IMAGES:
        if not campaign.get("spec"):
            raise StageBlocked("No creative specification found.")
        creative_spec = CreativeSpec(**campaign["spec"])
        # ponytail: the 1:1 and 9:16 edits are independent and could run
        # concurrently. Kept sequential because two simultaneous image requests
        # risk provider rate limits, and the wall-clock saving (~30s) does not
        # justify the added failure modes in a pipeline this size.
        assets = images.generate_images(campaign_id, creative_spec, on_progress=progress)
        return {"assets": [a.model_dump(mode="json") for a in assets]}

    if stage is StageName.VIDEO:
        if not campaign.get("spec"):
            raise StageBlocked("No creative specification found.")
        creative_spec = CreativeSpec(**campaign["spec"])
        asset = video.render_video(campaign_id, creative_spec, on_progress=progress)
        return {"asset": asset.model_dump(mode="json")}

    raise ValueError(f"Unknown stage {stage!r}")


def run_from(
    campaign_id: str,
    start: StageName,
    *,
    on_progress: Callable[[str], None] | None = None,
) -> None:
    """Run `start` and every stage after it. Used by the background worker.

    Stops at the first failure rather than pressing on — a failed spec would
    produce meaningless images, and the user should see the real error rather
    than a cascade of downstream ones.
    """
    order = [StageName.RESEARCH, StageName.SPEC, StageName.IMAGES, StageName.VIDEO]
    for stage in order[order.index(start) :]:
        try:
            run_stage(campaign_id, stage, on_progress=on_progress)
        except (StageBlocked, StageBusy) as exc:
            # Blocked is expected: the pipeline pauses here waiting for the user
            # to select an angle. Not an error worth recording as one.
            print(f"[pipeline] stopped at {stage.value}: {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            print(f"[pipeline] {stage.value} failed: {exc}")
            return
