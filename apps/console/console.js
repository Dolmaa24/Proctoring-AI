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
};

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
    ...entries.map((entry) => {
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
      msg.textContent = entry.resolved ? `${entry.message}` : entry.message;
      if (entry.duration_ms) {
        const dur = document.createElement("span");
        dur.className = "dur";
        dur.textContent = ` (${duration(entry.duration_ms)})`;
        msg.append(dur);
      }

      li.append(at, rule, msg);
      return li;
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
