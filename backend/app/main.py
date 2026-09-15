"""FastAPI application: HTTP surface for the campaign pipeline.

Endpoints follow the pipeline:

    POST /api/campaigns                      create from a brief, start research
    GET  /api/campaigns                      history
    GET  /api/campaigns/{id}                 full state (polled by the UI)
    POST /api/campaigns/{id}/select-angle    user picks 1 of 3, runs spec+images+video
    POST /api/campaigns/{id}/stages/{stage}/retry
    GET  /api/campaigns/{id}/assets/{kind}   download an artifact

Background work uses FastAPI's built-in `BackgroundTasks` rather than Celery or
a queue. The assignment says "a simple background worker is acceptable" and to
avoid adding infrastructure for its own sake. Stage state lives in SQLite, not
in the worker, so a crashed process loses no record of what happened — the
restart sweep marks interrupted stages and the user retries them.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import config, orchestrator, store
from .models import ProductBrief, StageName, StageStatus

app = FastAPI(title="AI Campaign Creative Studio", version="1.0.0")

# The React dev server runs on a different port, so the browser needs CORS.
# Restricted to localhost origins: this app has no auth and should not be
# callable from an arbitrary website.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    """Initialise storage and recover from any unclean shutdown."""
    store.init_db()
    recovered = store.mark_interrupted_stages()
    if recovered:
        print(f"[startup] marked {recovered} interrupted stage(s) from a previous run")
    missing = config.missing_keys()
    if missing:
        print(f"[startup] WARNING missing credentials: {', '.join(missing)}")
        print("[startup] set them in .env, or run with FIXTURE_MODE=1")
    if config.FIXTURE_MODE:
        print("[startup] FIXTURE MODE ON — no provider calls will be made")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class SelectAngleRequest(BaseModel):
    angle_id: str = Field(min_length=1, max_length=64)


# ---------------------------------------------------------------------------
# Health + config
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, Any]:
    """Health and configuration, so the UI can show fixture mode and bounds."""
    return {
        "status": "ok",
        "fixture_mode": config.FIXTURE_MODE,
        "missing_credentials": config.missing_keys(),
        "models": {"agent": config.AGENT_MODEL, "image": config.IMAGE_MODEL},
        "bounds": {
            "max_search_calls": config.MAX_SEARCH_CALLS,
            "max_fetch_calls": config.MAX_FETCH_CALLS,
            "max_agent_steps": config.MAX_AGENT_STEPS,
            "stage_timeout_seconds": config.STAGE_TIMEOUT_SECONDS,
            "max_provider_retries": config.MAX_PROVIDER_RETRIES,
        },
    }


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


@app.post("/api/campaigns", status_code=201)
def create_campaign(brief: ProductBrief, background: BackgroundTasks) -> dict[str, Any]:
    """Create a campaign and start research in the background.

    `brief` is validated by Pydantic before this function body runs — invalid
    input is rejected with a 422 and never reaches the database or a provider.
    """
    campaign_id = store.create_campaign(brief.model_dump(mode="json"))
    background.add_task(_run_stage_safely, campaign_id, StageName.RESEARCH)
    return {"campaign_id": campaign_id, "status": "research_started"}


@app.get("/api/campaigns")
def list_campaigns() -> dict[str, Any]:
    return {"campaigns": store.list_campaigns()}


@app.get("/api/campaigns/{campaign_id}")
def get_campaign(campaign_id: str) -> dict[str, Any]:
    """Full campaign state. The UI polls this to show stage progress."""
    campaign = store.get_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(404, f"No campaign {campaign_id}")
    return campaign


@app.get("/api/campaigns/{campaign_id}/status")
def get_campaign_status(campaign_id: str) -> dict[str, Any]:
    """Lightweight poll target — stage statuses only, ~0.4 KB.

    The UI polls this every 1.5s and fetches the full campaign only when a
    status actually changes. Polling the full record instead transferred ~1.2 MB
    over a 60s stage to observe four state changes.
    """
    status = store.get_campaign_status(campaign_id)
    if status is None:
        raise HTTPException(404, f"No campaign {campaign_id}")
    return status


@app.post("/api/campaigns/{campaign_id}/select-angle")
def select_angle(
    campaign_id: str, request: SelectAngleRequest, background: BackgroundTasks
) -> dict[str, Any]:
    """Record the user's chosen angle, then run spec -> images -> video."""
    campaign = store.get_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(404, f"No campaign {campaign_id}")

    research = campaign["stages"].get(StageName.RESEARCH.value, {})
    if research.get("status") != StageStatus.SUCCEEDED.value:
        raise HTTPException(409, "Research has not completed successfully yet.")

    valid_ids = {a["id"] for a in (research.get("output") or {}).get("angles", [])}
    if request.angle_id not in valid_ids:
        raise HTTPException(
            400,
            f"Angle {request.angle_id!r} is not one of the researched angles: "
            f"{sorted(valid_ids)}",
        )

    store.set_selected_angle(campaign_id, request.angle_id)
    background.add_task(orchestrator.run_from, campaign_id, StageName.SPEC)
    return {"campaign_id": campaign_id, "selected_angle": request.angle_id}


@app.post("/api/campaigns/{campaign_id}/stages/{stage_name}/retry")
def retry_stage(
    campaign_id: str,
    stage_name: str,
    background: BackgroundTasks,
) -> dict[str, Any]:
    """Retry one stage without re-running the stages that already succeeded."""
    try:
        stage = StageName(stage_name)
    except ValueError:
        raise HTTPException(
            400, f"Unknown stage {stage_name!r}. Valid: {[s.value for s in StageName]}"
        ) from None

    if store.get_campaign(campaign_id) is None:
        raise HTTPException(404, f"No campaign {campaign_id}")

    # Claim the stage in the REQUEST, not in the background task.
    #
    # An audit fired 5 simultaneous retries and got 5x 200 "retry_started" while
    # only 1 attempt was recorded: the data was safe, but four clients were told
    # their work had started when it had been silently dropped. Claiming here
    # means the winner gets 202 and every loser gets an honest 409.
    if not store.claim_stage(campaign_id, stage):
        raise HTTPException(
            409,
            f"The '{stage_name}' stage is already running. "
            f"Wait for it to finish before retrying.",
        )

    # An explicit retry always re-runs, even for a stage that already succeeded —
    # that is what the user asked for by pressing the button. The stage is
    # already claimed above, so the worker must not claim it a second time.
    background.add_task(_run_stage_safely, campaign_id, stage, True, True)
    return {
        "campaign_id": campaign_id,
        "stage": stage_name,
        "status": "retry_started",
    }


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


@app.get("/api/campaigns/{campaign_id}/assets/{kind}")
def get_asset(campaign_id: str, kind: str) -> FileResponse:
    """Serve a generated artifact for preview or download."""
    campaign = store.get_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(404, f"No campaign {campaign_id}")

    asset = next((a for a in campaign["assets"] if a["kind"] == kind), None)
    if asset is None:
        raise HTTPException(404, f"No {kind!r} asset for this campaign")

    path = Path(asset["path"])
    # Defence in depth: never serve a file from outside the artifact directory,
    # even if a malformed path were somehow persisted.
    if not path.is_file() or store.ARTIFACT_DIR not in path.resolve().parents:
        raise HTTPException(410, f"The {kind!r} file is no longer on disk")

    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=path.name)


# ---------------------------------------------------------------------------
# Background helper
# ---------------------------------------------------------------------------


def _run_stage_safely(
    campaign_id: str,
    stage: StageName,
    force: bool = False,
    already_claimed: bool = False,
) -> None:
    """Run a stage in the background, swallowing exceptions after recording them.

    `run_stage` already persists the failure before re-raising. A background task
    has nobody to propagate to, so the exception is logged and dropped here — the
    user sees the failure through the campaign status endpoint.
    """
    try:
        orchestrator.run_stage(
            campaign_id, stage, force=force, already_claimed=already_claimed
        )
        # Creating a campaign starts research; once it succeeds the pipeline
        # pauses for the user to select an angle, so nothing auto-continues here.
    except Exception as exc:  # noqa: BLE001
        print(f"[background] {stage.value} for {campaign_id}: {exc}")
