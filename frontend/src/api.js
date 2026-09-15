// All backend calls in one place, so error handling and the base URL are
// defined once rather than repeated at every call site.

const BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";

async function request(path, options = {}) {
  const response = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });

  if (!response.ok) {
    // FastAPI puts the useful message in `detail`. For validation errors that
    // is a list of per-field objects, so flatten it into something readable
    // rather than showing the user "[object Object]".
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (Array.isArray(body.detail)) {
        message = body.detail
          .map((e) => `${e.loc?.slice(1).join(".") || "field"}: ${e.msg}`)
          .join("; ");
      } else if (body.detail) {
        message = body.detail;
      }
    } catch {
      // Response had no JSON body; keep the status-based message.
    }
    throw new Error(message);
  }

  return response.json();
}

export const api = {
  health: () => request("/api/health"),
  listCampaigns: () => request("/api/campaigns"),
  getCampaign: (id) => request(`/api/campaigns/${id}`),

  // Lightweight poll target: stage statuses only, ~0.3 KB against the full
  // record's ~13 KB. The UI polls this and refetches the full campaign only
  // when a status actually changes.
  getStatus: (id) => request(`/api/campaigns/${id}/status`),

  createCampaign: (brief) =>
    request("/api/campaigns", { method: "POST", body: JSON.stringify(brief) }),

  selectAngle: (id, angleId) =>
    request(`/api/campaigns/${id}/select-angle`, {
      method: "POST",
      body: JSON.stringify({ angle_id: angleId }),
    }),

  retryStage: (id, stage) =>
    request(`/api/campaigns/${id}/stages/${stage}/retry`, { method: "POST" }),

  assetUrl: (id, kind) => `${BASE}/api/campaigns/${id}/assets/${kind}`,
};
