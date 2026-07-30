/**
 * Proctor console.
 *
 * Renders the triage board and follows the authenticated event stream. All
 * ranking happens server-side (see proctor_gateway/triage.py) so that two
 * proctors see the same queue and the logic deciding whose name floats to
 * the top is unit tested rather than buried here.
 *
 * One rule this file follows deliberately: the numeric score is never
 * displayed. It is used for ordering only, and the coarse band is what a
 * human sees. A number beside a person's name gets read as a confidence
 * value no matter how it is labelled.
 *
 * Evidence (the raw signal samples, similarity scores, or a transcript
 * reference behind a flag) is fetched lazily, one violation at a time, only
 * when a proctor opens it — never embedded in the board snapshot. See
 * TimelineEntry.evidence_count in proctor_gateway/triage.py for why: that
 * snapshot is refetched in full on every WS message this file receives, and
 * embedding up to 64 samples per flag in every one of those refetches would
 * make an ordinary exam room's traffic pattern expensive for no benefit.
 */

const els = {
  link: document.getElementById("link"),
  counts: document.getElementById("counts"),
  sessions: document.getElementById("sessions"),
  empty: document.getElementById("empty"),
  detail: document.getElementById("detail"),
  detailEmpty: document.getElementById("detail-empty"),
  ref: document.getElementById("detail-ref"),
  meta: document.getElementById("detail-meta"),
  timeline: document.getElementById("timeline"),
  auth: document.getElementById("auth"),
  token: document.getElementById("token"),
};

const state = {
  sessions: new Map(),
  selected: null,
  token: sessionStorage.getItem("proctor.token") ?? "",
  // Evidence and transcripts are fetched lazily, per entry a proctor
  // actually opens — see TimelineEntry.evidence_count in triage.py for
  // why the board snapshot itself never carries the raw samples. Cached
  // client-side and keyed by session so re-selecting a session does not
  // refetch, and `expanded` persists across the frequent snapshot
  // refetches this file already does on every WS message.
  evidenceCache: new Map(),
  transcriptCache: new Map(),
  expanded: new Set(),
};

const EVIDENCE_DISPLAY_LIMIT = 20;

const clockTime = (ms) =>
  new Date(ms).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });

const duration = (ms) => (ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`);

function setLink(stateName, label) {
  els.link.dataset.state = stateName;
  els.link.textContent = label;
}

function formatValue(value) {
  if (Array.isArray(value) || (value && typeof value === "object")) return JSON.stringify(value);
  return String(value);
}

const cacheKey = (sessionId, id) => `${sessionId}::${id}`;

// -- evidence and transcripts (fetched on demand) ----------------------------

async function fetchJson(url, cache, key) {
  if (cache.has(key)) return cache.get(key);
  let result;
  try {
    const response = await fetch(url, { headers: { authorization: `Bearer ${state.token}` } });
    result = response.ok ? await response.json() : { error: `unavailable (${response.status})` };
  } catch {
    result = { error: "network error" };
  }
  cache.set(key, result);
  return result;
}

async function toggleEvidence(sessionId, violationId) {
  if (state.expanded.has(violationId)) {
    state.expanded.delete(violationId);
  } else {
    state.expanded.add(violationId);
    await fetchJson(
      `/v1/proctor/sessions/${sessionId}/violations/${violationId}`,
      state.evidenceCache,
      cacheKey(sessionId, violationId),
    );
  }
  renderDetail();
}

async function toggleTranscript(sessionId, transcriptId) {
  const key = `transcript:${transcriptId}`;
  if (state.expanded.has(key)) {
    state.expanded.delete(key);
  } else {
    state.expanded.add(key);
    await fetchJson(
      `/v1/proctor/sessions/${sessionId}/audio/transcripts/${transcriptId}`,
      state.transcriptCache,
      cacheKey(sessionId, transcriptId),
    );
  }
  renderDetail();
}

function renderEvidence(sessionId, record) {
  const wrap = document.createElement("div");
  wrap.className = "evidence";

  if (record.error) {
    wrap.classList.add("evidence-error");
    wrap.textContent = record.error;
    return wrap;
  }

  const items = record.evidence ?? [];
  const shown = items.slice(-EVIDENCE_DISPLAY_LIMIT);
  if (items.length > shown.length) {
    const note = document.createElement("p");
    note.className = "hint";
    note.textContent = `showing the most recent ${shown.length} of ${items.length} samples`;
    wrap.append(note);
  }
  if (!items.length) {
    wrap.append(document.createTextNode("no samples recorded"));
    return wrap;
  }

  const list = document.createElement("ol");
  list.className = "evidence-list";
  for (const item of shown) {
    const li = document.createElement("li");

    const at = document.createElement("span");
    at.className = "at";
    at.textContent = clockTime(item.server_ts_ms);

    const fields = document.createElement("span");
    fields.className = "fields";
    fields.textContent = Object.entries(item.payload ?? {})
      .map(([field, value]) => `${field}=${formatValue(value)}`)
      .join("  ");

    li.append(at, fields);

    // Audio findings reference a transcript rather than embedding it — see
    // _audio_violation in proctor_gateway/app.py. This is the other end of
    // that split: dereference it here, on request, while it still exists.
    const transcriptRef = item.payload?.transcript_ref;
    if (transcriptRef) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "link-btn";
      btn.textContent = "view transcript";
      btn.addEventListener("click", () => toggleTranscript(sessionId, transcriptRef));
      li.append(document.createTextNode(" "), btn);

      if (state.expanded.has(`transcript:${transcriptRef}`)) {
        const cached = state.transcriptCache.get(cacheKey(sessionId, transcriptRef));
        const box = document.createElement("div");
        box.className = "transcript-box";
        box.textContent = cached ? (cached.error ?? cached.transcript) : "loading…";
        li.append(box);
      }
    }

    list.append(li);
  }
  wrap.append(list);
  return wrap;
}

// -- rendering --------------------------------------------------------------

function renderQueue() {
  const rows = [...state.sessions.values()].sort(
    (a, b) => b.score - a.score || Number(a.connected) - Number(b.connected),
  );

  els.empty.hidden = rows.length > 0;
  els.sessions.replaceChildren(
    ...rows.map((session) => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.className = "session";
      button.type = "button";
      if (session.session_id === state.selected) button.setAttribute("aria-current", "true");
      button.addEventListener("click", () => select(session.session_id));

      const band = document.createElement("span");
      band.className = "band";
      band.dataset.band = session.band;
      band.textContent = session.band;

      const who = document.createElement("span");
      who.className = "who";
      const ref = document.createElement("div");
      ref.className = "ref";
      ref.textContent = session.candidate_ref || session.session_id.slice(0, 18);
      const sub = document.createElement("div");
      sub.className = "sub";
      sub.textContent = session.connected
        ? session.exam_id || "in progress"
        : session.ended_cleanly
          ? "finished"
          : "client disconnected";
      if (!session.connected && !session.ended_cleanly) sub.classList.add("offline");
      who.append(ref, sub);

      const tally = document.createElement("span");
      tally.className = "tally";
      const open = session.open_violations?.length ?? 0;
      tally.textContent = open ? `${open} open` : "—";

      button.append(band, who, tally);
      item.append(button);
      return item;
    }),
  );

  const connected = rows.filter((s) => s.connected).length;
  const needing = rows.filter((s) => s.band === "review").length;
  els.counts.textContent = `${rows.length} sessions · ${connected} live · ${needing} to review`;
}

function renderDetail() {
  const session = state.selected ? state.sessions.get(state.selected) : null;
  els.detail.hidden = !session;
  els.detailEmpty.hidden = Boolean(session);
  if (!session) return;

  els.ref.textContent = session.candidate_ref || session.session_id;

  const meta = [
    ["session", session.session_id],
    ["exam", session.exam_id || "—"],
    ["state", session.connected ? "connected" : session.ended_cleanly ? "finished" : "disconnected"],
    ["events", String(session.events_received ?? 0)],
    ["open flags", String(session.open_violations?.length ?? 0)],
  ];
  els.meta.replaceChildren(
    ...meta.flatMap(([term, value]) => {
      const dt = document.createElement("dt");
      dt.textContent = term;
      const dd = document.createElement("dd");
      dd.textContent = value;
      return [dt, dd];
    }),
  );

  const entries = session.timeline ?? [];
  els.timeline.replaceChildren(
    ...entries.flatMap((entry) => {
      const li = document.createElement("li");
      li.className = "entry";
      li.dataset.severity = entry.severity;
      li.dataset.resolved = String(entry.resolved);

      const at = document.createElement("span");
      at.className = "at";
      at.textContent = clockTime(entry.at_ms);

      const rule = document.createElement("span");
      rule.className = "rule";
      rule.textContent = entry.rule_id;

      const msg = document.createElement("span");
      msg.className = "msg";
      msg.textContent = entry.message;
      if (entry.duration_ms) {
        const dur = document.createElement("span");
        dur.className = "dur";
        dur.textContent = ` (${duration(entry.duration_ms)})`;
        msg.append(dur);
      }

      const isOpen = state.expanded.has(entry.violation_id);
      if (entry.evidence_count > 0) {
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "link-btn";
        toggle.setAttribute("aria-expanded", String(isOpen));
        const noun = entry.evidence_count === 1 ? "sample" : "samples";
        toggle.textContent = `${entry.evidence_count} ${noun} ${isOpen ? "▾" : "▸"}`;
        toggle.addEventListener("click", () => toggleEvidence(session.session_id, entry.violation_id));
        msg.append(document.createTextNode(" "), toggle);
      }

      li.append(at, rule, msg);

      if (entry.evidence_count === 0 || !isOpen) return [li];

      const record = state.evidenceCache.get(cacheKey(session.session_id, entry.violation_id));
      const evidenceRow = document.createElement("li");
      evidenceRow.className = "entry-evidence";
      evidenceRow.append(
        record ? renderEvidence(session.session_id, record) : document.createTextNode("loading…"),
      );
      return [li, evidenceRow];
    }),
  );

  if (!entries.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "No flags recorded for this session.";
    els.timeline.append(li);
  }
}

function select(sessionId) {
  state.selected = sessionId;
  renderQueue();
  renderDetail();
}

function applySnapshot(sessions) {
  state.sessions = new Map(sessions.map((s) => [s.session_id, s]));
  renderQueue();
  renderDetail();
}

// -- transport --------------------------------------------------------------

async function refresh() {
  const response = await fetch("/v1/proctor/sessions", {
    headers: { authorization: `Bearer ${state.token}` },
  });
  if (response.status === 401) throw new Error("unauthorised");
  const body = await response.json();
  applySnapshot(body.sessions);
}

function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  // The token rides as a subprotocol: browsers cannot set headers on a
  // WebSocket handshake, and a query string would land in access logs.
  const socket = new WebSocket(`${scheme}://${location.host}/v1/proctor/stream`, [
    "proctor.console.v1",
    `token.${state.token}`,
  ]);

  socket.addEventListener("open", () => setLink("on", "live"));

  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.kind === "hello") {
      applySnapshot(message.sessions ?? []);
      return;
    }
    // Every other message mutates board state server-side. Re-reading the
    // snapshot keeps one source of truth rather than reimplementing the
    // decay and ordering rules here, where they would drift.
    void refresh().catch(() => {});
  });

  socket.addEventListener("close", (event) => {
    if (event.code === 1008) {
      setLink("error", "unauthorised");
      promptForToken();
      return;
    }
    setLink("off", "reconnecting");
    setTimeout(connect, 2000);
  });

  socket.addEventListener("error", () => setLink("error", "error"));
}

function promptForToken() {
  sessionStorage.removeItem("proctor.token");
  els.auth.showModal();
}

els.auth.addEventListener("close", () => {
  state.token = els.token.value.trim();
  if (!state.token) return promptForToken();
  sessionStorage.setItem("proctor.token", state.token);
  start();
});

function start() {
  setLink("off", "connecting");
  refresh()
    .then(connect)
    .catch(() => {
      setLink("error", "unauthorised");
      promptForToken();
    });
}

if (state.token) start();
else promptForToken();
