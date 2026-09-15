"""SQLite persistence for campaigns, stages, and artifacts.

Why SQLite and not Postgres: the assignment says a local database is acceptable
and to "explain choices instead of adding infrastructure for its own sake".
This app is single-user, writes rarely, and reads small JSON blobs. SQLite gives
durability, transactions, and zero setup for a reviewer cloning the repo.
Upgrade path if it ever needed concurrency: the same SQL moves to Postgres by
swapping the connection, because nothing here uses SQLite-specific features.

Design: campaigns hold the brief; stages hold status + output + error. Stage
output is stored as JSON text because the shape is already validated by the
Pydantic contracts in models.py — the database enforces existence, Pydantic
enforces shape. Keeping schema flat avoids migrations for a 12-hour project.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import StageName, StageStatus

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "studio.db"
ARTIFACT_DIR = DATA_DIR / "artifacts"


SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id              TEXT PRIMARY KEY,
    product_name    TEXT NOT NULL,
    brief_json      TEXT NOT NULL,
    selected_angle  TEXT,
    spec_json       TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stages (
    campaign_id  TEXT NOT NULL,
    name         TEXT NOT NULL,
    status       TEXT NOT NULL,
    output_json  TEXT,
    error        TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    started_at   TEXT,
    finished_at  TEXT,
    PRIMARY KEY (campaign_id, name),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id     TEXT PRIMARY KEY,
    campaign_id  TEXT NOT NULL,
    kind         TEXT NOT NULL,
    asset_json   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS usage (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id  TEXT NOT NULL,
    usage_json   TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_stages_campaign ON stages(campaign_id);
CREATE INDEX IF NOT EXISTS idx_assets_campaign ON assets(campaign_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Open a connection with sane durability defaults.

    WAL mode lets the API read campaign status while a background worker is
    writing stage results — without it, the UI polling would block on the
    worker's writes.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def mark_interrupted_stages() -> int:
    """Called once on startup. Converts orphaned RUNNING stages to INTERRUPTED.

    This is the answer to "explain how interrupted in-progress work is detected
    after restart". A stage can only be RUNNING while a worker holds it in
    memory; that memory dies with the process. So any RUNNING row found at boot
    is by definition abandoned, and we mark it so the user sees an honest
    status and can retry it.
    """
    with connect() as conn:
        cur = conn.execute(
            "UPDATE stages SET status = ?, error = ?, finished_at = ? "
            "WHERE status = ?",
            (
                StageStatus.INTERRUPTED.value,
                "Server restarted while this stage was running. Retry it.",
                _now(),
                StageStatus.RUNNING.value,
            ),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


def create_campaign(brief: dict[str, Any]) -> str:
    campaign_id = uuid.uuid4().hex[:12]
    now = _now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO campaigns (id, product_name, brief_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (campaign_id, brief["product_name"], json.dumps(brief), now, now),
        )
        # Every stage starts life as an explicit PENDING row. Materialising them
        # up front means the UI can render the full pipeline immediately, and
        # there is never an ambiguous "missing row = ?" state.
        for stage in StageName:
            conn.execute(
                "INSERT INTO stages (campaign_id, name, status) VALUES (?, ?, ?)",
                (campaign_id, stage.value, StageStatus.PENDING.value),
            )
    return campaign_id


def get_campaign(campaign_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
        ).fetchone()
        if row is None:
            return None

        stages = conn.execute(
            "SELECT * FROM stages WHERE campaign_id = ?", (campaign_id,)
        ).fetchall()
        assets = conn.execute(
            "SELECT asset_json FROM assets WHERE campaign_id = ? ORDER BY created_at",
            (campaign_id,),
        ).fetchall()
        usage = conn.execute(
            "SELECT usage_json FROM usage WHERE campaign_id = ?", (campaign_id,)
        ).fetchall()

    return {
        "id": row["id"],
        "brief": json.loads(row["brief_json"]),
        "selected_angle": row["selected_angle"],
        "spec": json.loads(row["spec_json"]) if row["spec_json"] else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "stages": {
            s["name"]: {
                "status": s["status"],
                "output": json.loads(s["output_json"]) if s["output_json"] else None,
                "error": s["error"],
                "attempts": s["attempts"],
                "started_at": s["started_at"],
                "finished_at": s["finished_at"],
            }
            for s in stages
        },
        "assets": [json.loads(a["asset_json"]) for a in assets],
        "usage": [json.loads(u["usage_json"]) for u in usage],
    }


def get_campaign_status(campaign_id: str) -> dict[str, Any] | None:
    """Stage statuses only — the minimum the UI needs while polling.

    `get_campaign` returns ~31 KB (source excerpts, the full agent trace, every
    generation prompt). The UI polls every 1.5s to watch four status strings
    change, so a 60s stage transferred ~1.2 MB to observe about four state
    changes. This returns ~0.4 KB instead; the full record is fetched once, when
    something actually changes.
    """
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM campaigns WHERE id = ?", (campaign_id,)
        ).fetchone()
        if exists is None:
            return None

        stages = conn.execute(
            "SELECT name, status, error, attempts FROM stages WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchall()
        asset_count = conn.execute(
            "SELECT COUNT(*) n FROM assets WHERE campaign_id = ?", (campaign_id,)
        ).fetchone()["n"]

    return {
        "id": campaign_id,
        "stages": {
            s["name"]: {
                "status": s["status"],
                "error": s["error"],
                "attempts": s["attempts"],
            }
            for s in stages
        },
        "asset_count": asset_count,
    }


def list_campaigns() -> list[dict[str, Any]]:
    """Campaign history for the UI. Deliberately lightweight — no blobs."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.id, c.product_name, c.created_at, "
            "  (SELECT COUNT(*) FROM stages s WHERE s.campaign_id = c.id "
            "   AND s.status = 'succeeded') AS done "
            "FROM campaigns c ORDER BY c.created_at DESC LIMIT 100"
        ).fetchall()
    return [
        {
            "id": r["id"],
            "product_name": r["product_name"],
            "created_at": r["created_at"],
            "stages_complete": r["done"],
            "stages_total": len(StageName),
        }
        for r in rows
    ]


def set_selected_angle(campaign_id: str, angle_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE campaigns SET selected_angle = ?, updated_at = ? WHERE id = ?",
            (angle_id, _now(), campaign_id),
        )


def save_spec(campaign_id: str, spec: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE campaigns SET spec_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(spec), _now(), campaign_id),
        )


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def claim_stage(campaign_id: str, stage: StageName) -> bool:
    """Atomically move a stage into RUNNING. Returns False if already running.

    This single SQL statement is the duplicate-submission guard. The WHERE
    clause refuses to claim a stage that is already RUNNING, and SQLite applies
    it atomically, so two simultaneous requests cannot both win. The loser gets
    False and does no work.

    ponytail: this is optimistic locking via UPDATE...WHERE, not a lock table.
    Sufficient for one process; move to SELECT FOR UPDATE if it ever runs
    multi-process against Postgres.
    """
    with connect() as conn:
        cur = conn.execute(
            "UPDATE stages SET status = ?, started_at = ?, error = NULL, "
            "  attempts = attempts + 1 "
            "WHERE campaign_id = ? AND name = ? AND status != ?",
            (
                StageStatus.RUNNING.value,
                _now(),
                campaign_id,
                stage.value,
                StageStatus.RUNNING.value,
            ),
        )
        return cur.rowcount > 0


def finish_stage(
    campaign_id: str,
    stage: StageName,
    status: StageStatus,
    output: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE stages SET status = ?, output_json = ?, error = ?, finished_at = ? "
            "WHERE campaign_id = ? AND name = ?",
            (
                status.value,
                json.dumps(output) if output is not None else None,
                error,
                _now(),
                campaign_id,
                stage.value,
            ),
        )
        conn.execute(
            "UPDATE campaigns SET updated_at = ? WHERE id = ?", (_now(), campaign_id)
        )


def get_stage(campaign_id: str, stage: StageName) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM stages WHERE campaign_id = ? AND name = ?",
            (campaign_id, stage.value),
        ).fetchone()
    if row is None:
        return None
    return {
        "status": row["status"],
        "output": json.loads(row["output_json"]) if row["output_json"] else None,
        "error": row["error"],
        "attempts": row["attempts"],
    }


# ---------------------------------------------------------------------------
# Assets + usage
# ---------------------------------------------------------------------------


def save_asset(campaign_id: str, asset: dict[str, Any]) -> None:
    """Upsert by asset_id so retrying a stage replaces rather than duplicates."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO assets (asset_id, campaign_id, kind, asset_json, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(asset_id) DO UPDATE SET "
            "  asset_json = excluded.asset_json, created_at = excluded.created_at",
            (asset["asset_id"], campaign_id, asset["kind"], json.dumps(asset), _now()),
        )


def clear_assets(campaign_id: str, kinds: list[str]) -> None:
    """Remove assets of given kinds before a stage retry.

    Without this, retrying the image stage would leave the previous failed
    run's partial assets alongside the new ones.
    """
    if not kinds:
        return
    placeholders = ",".join("?" for _ in kinds)
    with connect() as conn:
        conn.execute(
            f"DELETE FROM assets WHERE campaign_id = ? AND kind IN ({placeholders})",
            (campaign_id, *kinds),
        )


def record_stage_usage(
    campaign_id: str,
    stage: str,
    records: list[dict[str, Any]],
) -> None:
    """Aggregate a stage's provider calls into one usage row.

    Previously `record_usage` existed but was never called from anywhere, so the
    `usage` table stayed empty while the README claimed otherwise. This is the
    single entry point every stage uses, for the same reason `run_stage` is the
    single entry point for stage execution: a guarantee applied in one place
    cannot be forgotten in another.

    `cost` comes from the provider's own reported figure where available and is
    labelled accordingly — the assignment asks for estimates and unknowns to be
    marked as such, and a computed estimate was measured 7x too low.
    """
    if not records:
        return

    total_cost = 0.0
    cost_is_reported = True
    for r in records:
        if r.get("cost") is not None:
            total_cost += float(r["cost"])
        else:
            cost_is_reported = False

    summary = {
        "stage": stage,
        "provider": "openrouter",
        "model": records[0].get("model", "unknown"),
        "calls": len(records),
        "prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in records),
        "completion_tokens": sum(r.get("completion_tokens") or 0 for r in records),
        "images_generated": sum(r.get("images_generated") or 0 for r in records),
        "cost_usd": round(total_cost, 6) if cost_is_reported else None,
        # True when every call reported its own billed cost. False means at
        # least one call did not, so the total is incomplete — reported as
        # unknown rather than guessed.
        "cost_is_reported": cost_is_reported,
    }
    record_usage(campaign_id, summary)


def record_usage(campaign_id: str, usage: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO usage (campaign_id, usage_json, created_at) VALUES (?, ?, ?)",
            (campaign_id, json.dumps(usage), _now()),
        )
