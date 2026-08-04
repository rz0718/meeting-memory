// HTTP client. Server errors carry the typed exception text, which is shown
// verbatim: a lock contention or precondition failure is never retried here.

export class ApiError extends Error {
  constructor(status, body) {
    super((body && body.error) || `request failed with status ${status}`);
    this.status = status;
    this.type = (body && body.type) || "Error";
    this.body = body;
  }
}

async function request(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let parsed = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch (error) {
      parsed = { error: text, type: "ResponseError" };
    }
  }
  if (!response.ok) throw new ApiError(response.status, parsed);
  return parsed;
}

function query(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === null || value === undefined || value === "" || value === "all") {
      continue;
    }
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export const api = {
  session: () => request("GET", "/api/session"),
  setReviewer: (reviewer) => request("POST", "/api/session/reviewer", { reviewer }),

  runs: (params) => request("GET", `/api/runs${query(params)}`),
  run: (runId) => request("GET", `/api/runs/${encodeURIComponent(runId)}`),

  knowledgeIndex: () => request("GET", "/api/knowledge"),
  knowledge: (id) => request("GET", `/api/knowledge/${encodeURIComponent(id)}`),

  reviews: (params) => request("GET", `/api/reviews${query(params)}`),
  review: (id) => request("GET", `/api/reviews/${encodeURIComponent(id)}`),
  resolve: (id, body) =>
    request("POST", `/api/reviews/${encodeURIComponent(id)}/resolve`, body),
  refresh: (id, body) =>
    request("POST", `/api/reviews/${encodeURIComponent(id)}/refresh`, body),
  suggestOne: (id, body) =>
    request("POST", `/api/reviews/${encodeURIComponent(id)}/suggest`, body || {}),

  askContext: (body) => request("POST", "/api/ask/context", body),
  askAnswer: (body) => request("POST", "/api/ask/answer", body),
  askSave: (answerToken) =>
    request("POST", "/api/ask/save", { answer_token: answerToken }),

  askContext: (body) => request("POST", "/api/ask/context", body),
  askAnswer: (body) => request("POST", "/api/ask/answer", body),
  askSave: (answerToken) =>
    request("POST", "/api/ask/save", { answer_token: answerToken }),

  merge: (body) => request("POST", "/api/merge", body),

  basket: () => request("GET", "/api/removal-basket"),
  basketAdd: (objectId) =>
    request("POST", "/api/removal-basket/items", { object_id: objectId }),
  basketRemove: (objectId) =>
    request("DELETE", `/api/removal-basket/items/${encodeURIComponent(objectId)}`),
  basketClear: () => request("DELETE", "/api/removal-basket"),
  basketPreview: (body) => request("POST", "/api/removal-basket/preview", body),
  basketApply: (body) => request("POST", "/api/removal-basket/apply", body),
};

// Batch suggestion generation streams one JSON object per line so a nine-item
// morning queue reports progress instead of blocking on the whole set.
export async function streamSuggestions(body, onEvent) {
  const response = await fetch("/api/reviews/suggest", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const text = await response.text();
    let parsed = null;
    try {
      parsed = JSON.parse(text);
    } catch (error) {
      parsed = { error: text };
    }
    throw new ApiError(response.status, parsed);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let index;
    while ((index = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, index).trim();
      buffer = buffer.slice(index + 1);
      if (line) onEvent(JSON.parse(line));
    }
  }
  if (buffer.trim()) onEvent(JSON.parse(buffer.trim()));
}
