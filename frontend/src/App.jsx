import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import "./App.css";

// The UI the assignment asks for: brief entry, research review, angle selection,
// generation status, asset previews, download, and campaign history.
// Deliberately one screen with sections rather than a router — there is only
// ever one campaign in view, and "a polished dashboard is not required".

// Inline SVG rather than an icon package: the design reference is explicit that
// emoji must not be used as structural icons, and four inline paths do not
// justify a dependency.
const Icon = {
  spark: (p) => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...p}>
      <path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1" />
    </svg>
  ),
  download: (p) => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...p}>
      <path d="M12 3v12M7 11l5 5 5-5M4 20h16" />
    </svg>
  ),
  retry: (p) => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...p}>
      <path d="M3 12a9 9 0 1 0 3-6.7M3 4v5h5" />
    </svg>
  ),
  empty: (p) => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...p}>
      <path d="M4 7h16v13H4zM4 7l2-3h12l2 3M9 12h6" />
    </svg>
  ),
};

const STAGES = ["research", "spec", "images", "video"];

const STAGE_LABELS = {
  research: "Research",
  spec: "Creative spec",
  images: "Image ads",
  video: "Video",
};

const EXAMPLE_BRIEF = {
  product_name: "BeastLife Whey Core",
  description:
    "A whey protein powder that mixes in water without a blender, made for people who train before work and commute straight after.",
  target_audience:
    "Busy gym-goers aged 25-40 in Indian metros who train early mornings",
  objective: "Introduce the product to a new audience",
  tone: "Practical and energetic",
  call_to_action: "Explore the range",
  verified_claims: "24g protein per serving\nMixes in water without a blender\nNo added sugar",
};

function StatusPill({ status }) {
  return <span className={`pill pill--${status}`}>{status}</span>;
}

function BriefForm({ onSubmit, busy }) {
  const [form, setForm] = useState(EXAMPLE_BRIEF);
  const [error, setError] = useState(null);

  const update = (field) => (event) =>
    setForm((prev) => ({ ...prev, [field]: event.target.value }));

  const submit = async (event) => {
    event.preventDefault();
    setError(null);
    try {
      await onSubmit({
        ...form,
        // Claims are entered one per line and sent as an array.
        verified_claims: form.verified_claims
          .split("\n")
          .map((line) => line.trim())
          .filter(Boolean),
      });
    } catch (err) {
      setError(err.message);
    }
  };

  return (
    <form className="card" onSubmit={submit}>
      <h2><span className="step">1</span> Product brief</h2>

      <label>
        Product name
        <input value={form.product_name} onChange={update("product_name")} required />
      </label>

      <label>
        Description
        <textarea
          rows={3}
          value={form.description}
          onChange={update("description")}
          required
        />
      </label>

      <div className="row">
        <label>
          Target audience
          <input
            value={form.target_audience}
            onChange={update("target_audience")}
            required
          />
        </label>
        <label>
          Objective
          <input value={form.objective} onChange={update("objective")} required />
        </label>
      </div>

      <div className="row">
        <label>
          Tone
          <input value={form.tone} onChange={update("tone")} required />
        </label>
        <label>
          Call to action
          <input
            value={form.call_to_action}
            onChange={update("call_to_action")}
            required
          />
        </label>
      </div>

      <label>
        Tagline (optional)
        <input
          value={form.tagline}
          onChange={update("tagline")}
          placeholder="Fuel the grind."
        />
        <small>
          Rendered under the brand name on every creative. Left blank, no
          tagline is shown — the system never invents one.
        </small>
      </label>

      <label>
        Verified claims — one per line
        <textarea
          rows={3}
          value={form.verified_claims}
          onChange={update("verified_claims")}
        />
        <small>
          The only product facts the system may state. Nothing else is asserted
          as fact.
        </small>
      </label>

      {error && <p className="error">{error}</p>}

      <button type="submit" disabled={busy}>
        {busy ? "Starting research…" : "Start research"}
      </button>
    </form>
  );
}

function ResearchTrace({ research }) {
  const [open, setOpen] = useState(false);
  const output = research.output;
  if (!output) return null;

  return (
    <div className="trace">
      <button className="link" onClick={() => setOpen((v) => !v)}>
        {open ? "▾" : "▸"} Agent trace — {output.tool_calls.length} tool calls,{" "}
        {output.sources.length} sources
      </button>

      {open && (
        <>
          <ol className="steps">
            {output.tool_calls.map((call) => (
              <li key={call.step}>
                <code>{call.tool}</code>
                <span className="muted"> · {call.duration_ms}ms</span>
                <div className="decision">“{call.decision}”</div>
                <div className="muted small">
                  {JSON.stringify(call.arguments)} → {call.result_summary}
                </div>
                {call.error && <div className="error small">{call.error}</div>}
                {call.injection_flags?.length > 0 && (
                  <div className="warn small">
                    Injection phrases detected and ignored:{" "}
                    {call.injection_flags.join(", ")}
                  </div>
                )}
              </li>
            ))}
          </ol>

          <h4>Sources read</h4>
          <ul className="sources">
            {output.sources.map((source) => (
              <li key={source.url}>
                <a href={source.url} target="_blank" rel="noreferrer">
                  {source.title}
                </a>
                <div className="muted small">
                  accessed {new Date(source.accessed_at).toLocaleString()}
                </div>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function AnglePicker({ campaign, onSelect, busy }) {
  const research = campaign.stages.research;
  if (research?.status !== "succeeded") return null;

  const { angles, source_gap_note: gapNote } = research.output;
  const selected = campaign.selected_angle;

  return (
    <div className="card">
      <h2><span className="step">2</span> Choose a creative angle</h2>

      {gapNote && <p className="warn">{gapNote}</p>}
      <ResearchTrace research={research} />

      <div className="angles">
        {angles.map((angle) => (
          <div
            key={angle.id}
            className={`angle ${selected === angle.id ? "angle--selected" : ""}`}
          >
            <h3>{angle.title}</h3>

            <p className="label">Audience insight (from sources)</p>
            <p>{angle.audience_insight}</p>

            <p className="label">Hook (creative interpretation)</p>
            <p className="hook">{angle.hook}</p>

            <p className="label">Visual direction</p>
            <p className="muted">{angle.visual_direction}</p>

            {angle.unsupported_numbers?.length > 0 && (
              <p className="warn small">
                Ungrounded figures flagged: {angle.unsupported_numbers.join(", ")}
                . These do not appear in any retrieved source.
              </p>
            )}

            {angle.thin_sourcing && (
              <p className="warn small">
                Single-source angle — less corroborated than the others.
              </p>
            )}

            <p className="muted small">
              {angle.source_urls.length} cited source
              {angle.source_urls.length === 1 ? "" : "s"}
            </p>

            <button
              onClick={() => onSelect(angle.id)}
              disabled={busy || Boolean(selected)}
            >
              {selected === angle.id ? "Selected" : "Use this angle"}
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

function StageList({ campaign, onRetry }) {
  return (
    <div className="stages">
      {STAGES.map((stage) => {
        const state = campaign.stages[stage] || {};
        const canRetry = ["failed", "interrupted", "succeeded"].includes(
          state.status
        );
        return (
          <div key={stage} className={`stage stage--${state.status}`}>
            <div className="stage__head">
              <strong>{STAGE_LABELS[stage]}</strong>
              <StatusPill status={state.status} />
            </div>
            {state.attempts > 1 && (
              <span className="muted small">{state.attempts} attempts</span>
            )}
            {state.error && <div className="error small">{state.error}</div>}
            {canRetry && (
              <button
                className="ghost"
                onClick={() => onRetry(stage)}
                aria-label={`Retry the ${STAGE_LABELS[stage]} stage`}
              >
                <Icon.retry width="13" height="13" />
                Retry
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}

function Assets({ campaign }) {
  const byKind = Object.fromEntries(campaign.assets.map((a) => [a.kind, a]));
  const has = (kind) => Boolean(byKind[kind]);

  if (campaign.assets.length === 0) return null;

  return (
    <div className="card">
      <h2><span className="step">3</span> Generated assets</h2>

      <div className="assets">
        {["image_1x1", "image_9x16"].map(
          (kind) =>
            has(kind) && (
              <figure key={kind} className="asset">
                <img
                  src={api.assetUrl(campaign.id, kind)}
                  alt={`${kind} ad creative`}
                />
                <figcaption>
                  {kind === "image_1x1" ? "1:1 — 1080×1080" : "9:16 — 1080×1920"}
                  <a
                    href={api.assetUrl(campaign.id, kind)}
                    download
                    className="link"
                    aria-label={`Download the ${kind} creative`}
                  >
                    <Icon.download width="13" height="13" /> Download
                  </a>
                </figcaption>
              </figure>
            )
        )}

        {has("video") && (
          <figure className="asset">
            <video
              src={api.assetUrl(campaign.id, "video")}
              controls
              playsInline
              muted
            />
            <figcaption>
              Video — {byKind.video.duration_seconds}s, 1080×1920
              <a
                href={api.assetUrl(campaign.id, "video")}
                download
                className="link"
                aria-label="Download the campaign video"
              >
                <Icon.download width="13" height="13" /> Download
              </a>
            </figcaption>
          </figure>
        )}
      </div>

      <details className="prompts">
        <summary>Generation prompts used ({campaign.assets.length})</summary>
        {campaign.assets.map((asset) => (
          <div key={asset.asset_id}>
            <strong>{asset.kind}</strong>
            <pre>{asset.generation_prompt}</pre>
          </div>
        ))}
      </details>
    </div>
  );
}

export default function App() {
  const [health, setHealth] = useState(null);
  const [campaignId, setCampaignId] = useState(null);
  const [campaign, setCampaign] = useState(null);
  const [history, setHistory] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const pollRef = useRef(null);

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
    api.listCampaigns().then((r) => setHistory(r.campaigns)).catch(() => {});
  }, []);

  const refresh = useCallback(async (id) => {
    try {
      setCampaign(await api.getCampaign(id));
    } catch (err) {
      setError(err.message);
    }
  }, []);

  // Poll the lightweight status endpoint and only refetch the full campaign
  // when a stage status actually changes. Polling the full record transferred
  // ~1.2 MB over a 60s stage to watch four strings change; this is ~0.3 KB a
  // tick, and the expensive fetch happens on transition rather than on timer.
  const lastStatusRef = useRef("");
  const pollStatus = useCallback(
    async (id) => {
      try {
        const status = await api.getStatus(id);
        const signature = JSON.stringify(status.stages);
        if (signature !== lastStatusRef.current) {
          lastStatusRef.current = signature;
          await refresh(id);
        }
      } catch (err) {
        setError(err.message);
      }
    },
    [refresh]
  );

  // Poll while any stage is still running. Polling (rather than websockets)
  // keeps the backend a plain request/response service; the tradeoff is up to
  // 1.5s of latency on a status change, which is invisible next to a 70s stage.
  useEffect(() => {
    if (!campaignId) return undefined;

    refresh(campaignId);
    pollRef.current = setInterval(() => pollStatus(campaignId), 1500);
    return () => clearInterval(pollRef.current);
  }, [campaignId, refresh, pollStatus]);

  useEffect(() => {
    if (!campaign) return;
    const running = STAGES.some(
      (s) => campaign.stages[s]?.status === "running"
    );
    if (!running && pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
      api.listCampaigns().then((r) => setHistory(r.campaigns)).catch(() => {});
    }
  }, [campaign]);

  const createCampaign = async (brief) => {
    setBusy(true);
    setError(null);
    try {
      const { campaign_id: id } = await api.createCampaign(brief);
      setCampaignId(id);
    } finally {
      setBusy(false);
    }
  };

  const selectAngle = async (angleId) => {
    setBusy(true);
    setError(null);
    try {
      await api.selectAngle(campaignId, angleId);
      // Restart polling: the pipeline is running again.
      if (!pollRef.current) {
        pollRef.current = setInterval(() => pollStatus(campaignId), 1500);
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const retryStage = async (stage) => {
    setError(null);
    try {
      await api.retryStage(campaignId, stage);
      if (!pollRef.current) {
        pollRef.current = setInterval(() => pollStatus(campaignId), 1500);
      }
    } catch (err) {
      setError(err.message);
    }
  };

  return (
    <main>
      <header>
        <div className="brand">
          <span className="brand__mark">
            <Icon.spark width="18" height="18" style={{ color: "#fff" }} />
          </span>
          <div>
            <h1>Campaign Creative Studio</h1>
            <p className="brand__sub">
              Research-grounded ad concepts, rendered in two formats and a video
            </p>
          </div>
        </div>
        {health?.fixture_mode && (
          <p className="warn banner banner--warn" role="status">
            FIXTURE MODE — all provider calls are mocked. Research shown is
            canned data, not live browsing.
          </p>
        )}
        {health?.missing_credentials?.length > 0 && (
          <p className="error">
            Missing credentials: {health.missing_credentials.join(", ")}
          </p>
        )}
      </header>

      {error && (
        <p className="error banner" role="alert">
          {error}
        </p>
      )}

      {!campaignId && <BriefForm onSubmit={createCampaign} busy={busy} />}

      {campaign && (
        <>
          <div className="card">
            <h2>Pipeline</h2>
            <StageList campaign={campaign} onRetry={retryStage} />
          </div>

          <AnglePicker
            campaign={campaign}
            onSelect={selectAngle}
            busy={busy}
          />

          <Assets campaign={campaign} />
        </>
      )}

      {!campaignId && history.length === 0 && (
        <div className="card">
          <div className="empty">
            <Icon.empty width="34" height="34" className="muted" />
            <p>
              No campaigns yet. Fill in the brief above to research angles and
              generate a campaign.
            </p>
          </div>
        </div>
      )}

      {history.length > 0 && (
        <div className="card">
          <h2>Campaign history</h2>
          <ul className="history">
            {history.map((item) => (
              <li key={item.id}>
                <button className="link" onClick={() => setCampaignId(item.id)}>
                  {item.product_name}
                </button>
                <span className="muted small">
                  {" "}
                  {item.stages_complete}/{item.stages_total} stages ·{" "}
                  {new Date(item.created_at).toLocaleString()}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </main>
  );
}
