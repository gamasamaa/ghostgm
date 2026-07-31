// GhostGM Visual Suite — Client Logic & Canvas Battle Engine

let ws = null;
let sessionId = null;
let gameState = {
  grid_cols: 12,
  grid_rows: 12,
  tokens: {},
  enemies: {}
};

let selectedTokenId = null;
let draggedTokenId = null;

// Canvas DOM & Context
const canvas = document.getElementById("battle-map");
const ctx = canvas.getContext("2d");

const cellWidth = () => canvas.width / (gameState.grid_cols || 12);
const cellHeight = () => canvas.height / (gameState.grid_rows || 12);

// ---------------------------------------------------------------------------
// WebSocket Connection Management
// ---------------------------------------------------------------------------
function initWebSocket() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  // Reconnects hand the session id back so we resume the same conversation
  // instead of starting a fresh transcript every time the socket blips.
  const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
  ws = new WebSocket(`${protocol}//${window.location.host}/ws${query}`);

  ws.onopen = () => {
    console.log("Connected to GhostGM Visual WebSocket server.");
    document.getElementById("connection-status").innerHTML = '<span class="dot"></span> Online';
  };

  ws.onmessage = (event) => {
    handleServerMessage(JSON.parse(event.data));
  };

  ws.onclose = () => {
    console.warn("WebSocket connection closed. Retrying in 2 seconds...");
    document.getElementById("connection-status").innerHTML = '<span class="dot" style="background:#ef4444;box-shadow:0 0 8px #ef4444"></span> Disconnected';
    setTimeout(initWebSocket, 2000);
  };
}

let activeStreamMsgEl = null;

function handleServerMessage(data) {
  if (data.type === "init") {
    sessionId = data.session_id;
    gameState = data.state;
    document.getElementById("session-tag").textContent = `session ${data.session_id} → ${data.transcript}`;
    renderRoster();
    renderMap();
  } else if (data.type === "stream_start") {
    activeStreamMsgEl = createMessageElement("GM", "🧙‍♂️", "gm-msg");
  } else if (data.type === "chunk") {
    if (activeStreamMsgEl) {
      activeStreamMsgEl.textContent += data.text;
      const feed = document.getElementById("narrative-feed");
      feed.scrollTop = feed.scrollHeight;
    }
  } else if (data.type === "stream_done") {
    activeStreamMsgEl = null;
    if (data.state) {
      gameState = data.state;
      renderRoster();
      renderMap();
    }
    renderStateLog(data.applied);
    renderMetrics(data.metrics);
  } else if (data.type === "error") {
    activeStreamMsgEl = null;
    createMessageElement("System Error", "⚠️", "system-msg").textContent = `Error: ${data.message}`;
  }
}

function createMessageElement(sender, avatar, msgClass) {
  const feed = document.getElementById("narrative-feed");
  const msgDiv = document.createElement("div");
  msgDiv.className = `msg ${msgClass}`;

  const avatarDiv = document.createElement("div");
  avatarDiv.className = "avatar";
  avatarDiv.textContent = avatar;

  const contentDiv = document.createElement("div");
  contentDiv.className = "msg-content";
  contentDiv.innerHTML = `<strong>${sender}</strong><br><span class="text-body"></span>`;

  msgDiv.appendChild(avatarDiv);
  msgDiv.appendChild(contentDiv);
  feed.appendChild(msgDiv);

  feed.scrollTop = feed.scrollHeight;
  return contentDiv.querySelector(".text-body");
}

// Every automatic HP change is shown with the phrase that triggered it — the
// narration reader is a heuristic, so it has to show its work.
function renderStateLog(applied) {
  if (!applied || !applied.length) return;

  const body = createMessageElement("State Tracker", "⚡", "system-msg");
  applied.forEach((change) => {
    const line = document.createElement("div");
    line.className = "state-line";

    const sign = change.hp_delta < 0 ? "−" : "+";
    const label = document.createElement("span");
    label.className = change.hp_delta < 0 ? "delta-down" : "delta-up";
    label.textContent = `${change.name} ${sign}${Math.abs(change.hp_delta)} → ${change.hp}/${change.max_hp}`;

    const why = document.createElement("span");
    why.className = "state-why";
    why.textContent = `“${change.phrase}”`;

    line.appendChild(label);
    line.appendChild(why);
    body.appendChild(line);
  });
}

function renderMetrics(metrics) {
  if (!metrics) return;
  const el = document.getElementById("turn-stats");
  const ttft = metrics.ttft_ms === null ? "—" : `${Math.round(metrics.ttft_ms)}ms`;
  el.textContent = `turn ${metrics.turn} · ${metrics.input_tokens}→${metrics.output_tokens} tok · ttft ${ttft} · ${Math.round(metrics.latency_ms)}ms`;
}

function sendMessage() {
  const inputEl = document.getElementById("user-input");
  const text = inputEl.value.trim();

  if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;

  // Add user prompt to chat UI
  const userTextSpan = createMessageElement("Player", "⚔️", "user-msg");
  userTextSpan.textContent = text;

  // Send over WebSocket
  ws.send(JSON.stringify({ text }));
  inputEl.value = "";
}

// ---------------------------------------------------------------------------
// Roster UI Rendering
// ---------------------------------------------------------------------------
function allTokens() {
  return [...Object.values(gameState.tokens), ...Object.values(gameState.enemies)];
}

function renderRoster() {
  const container = document.getElementById("roster-list");
  container.innerHTML = "";

  allTokens().forEach((token) => {
    const card = document.createElement("div");
    card.className = `card ${token.is_enemy ? "enemy-card" : ""}`;
    card.style.borderLeftColor = token.color;

    const hpPct = Math.max(0, Math.min(100, (token.hp / token.max_hp) * 100));

    card.innerHTML = `
      <div class="card-header">
        <span class="card-title">${token.name}</span>
        <span class="ac-badge">AC ${token.ac}</span>
      </div>
      <div class="hp-container">
        <div class="hp-bar-bg">
          <div class="hp-bar-fill" style="width: ${hpPct}%; background: ${token.color};"></div>
        </div>
        <span class="hp-text">${token.hp}/${token.max_hp}</span>
        <div class="hp-actions">
          <button class="hp-btn" onclick="adjustHP('${token.id}', -1)">-</button>
          <button class="hp-btn" onclick="adjustHP('${token.id}', 1)">+</button>
        </div>
      </div>
    `;

    container.appendChild(card);
  });
}

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, ...body })
  });
  return res.json();
}

async function adjustHP(tokenId, delta) {
  if (!sessionId) return;
  try {
    const data = await post("/api/token/hp", { token_id: tokenId, delta });
    if (data.status === "ok") {
      gameState = data.state;
      renderRoster();
      renderMap();
    }
  } catch (err) {
    console.error("Failed to adjust HP:", err);
  }
}

// ---------------------------------------------------------------------------
// Canvas Battle Grid Renderer & Interactive Drag engine
// ---------------------------------------------------------------------------
function renderMap() {
  const cw = cellWidth();
  const ch = cellHeight();

  ctx.clearRect(0, 0, canvas.width, canvas.height);

  // 1. Draw Grid Lines
  ctx.strokeStyle = "rgba(255, 255, 255, 0.08)";
  ctx.lineWidth = 1;

  for (let c = 0; c <= gameState.grid_cols; c++) {
    ctx.beginPath();
    ctx.moveTo(c * cw, 0);
    ctx.lineTo(c * cw, canvas.height);
    ctx.stroke();
  }
  for (let r = 0; r <= gameState.grid_rows; r++) {
    ctx.beginPath();
    ctx.moveTo(0, r * ch);
    ctx.lineTo(canvas.width, r * ch);
    ctx.stroke();
  }

  // 2. Render Tokens
  allTokens().forEach((token) => {
    const centerX = token.x * cw + cw / 2;
    const centerY = token.y * ch + ch / 2;
    const radius = Math.min(cw, ch) * 0.38;

    // Draw Selection Glow
    if (token.id === selectedTokenId) {
      ctx.beginPath();
      ctx.arc(centerX, centerY, radius + 5, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(255, 255, 255, 0.3)";
      ctx.fill();
    }

    // Token Outer Circle
    ctx.beginPath();
    ctx.arc(centerX, centerY, radius, 0, Math.PI * 2);
    ctx.fillStyle = token.color;
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = token.hp === 0 ? "#64748b" : "#ffffff";
    ctx.stroke();

    // Token Initial Letter Label
    ctx.fillStyle = "#ffffff";
    ctx.font = "bold 16px Outfit, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(token.name.charAt(0).toUpperCase(), centerX, centerY);

    // Small Name Tag beneath
    ctx.font = "10px Outfit, sans-serif";
    ctx.fillStyle = "#94a3b8";
    ctx.fillText(token.name, centerX, centerY + radius + 12);
  });
}

// ---------------------------------------------------------------------------
// Canvas Interaction (Click & Drag to Move Tokens)
// ---------------------------------------------------------------------------
// The canvas has a fixed backing size but is laid out by CSS, so pointer
// coordinates have to be scaled into canvas space before they mean anything.
function cellFromEvent(e) {
  const rect = canvas.getBoundingClientRect();
  const x = (e.clientX - rect.left) * (canvas.width / rect.width);
  const y = (e.clientY - rect.top) * (canvas.height / rect.height);
  return { col: Math.floor(x / cellWidth()), row: Math.floor(y / cellHeight()) };
}

canvas.addEventListener("mousedown", (e) => {
  const { col, row } = cellFromEvent(e);
  const found = allTokens().find((t) => t.x === col && t.y === row);

  if (found) {
    selectedTokenId = found.id;
    draggedTokenId = found.id;
    renderRoster();
    renderMap();
  }
});

canvas.addEventListener("mouseup", async (e) => {
  if (!draggedTokenId || !sessionId) return;

  const { col, row } = cellFromEvent(e);
  const targetId = draggedTokenId;
  draggedTokenId = null;

  try {
    const data = await post("/api/token/move", { token_id: targetId, x: col, y: row });
    if (data.status === "ok") {
      gameState = data.state;
      renderMap();
    }
  } catch (err) {
    console.error("Failed to move token:", err);
  }
});

// ---------------------------------------------------------------------------
// Event Listeners
// ---------------------------------------------------------------------------
document.getElementById("btn-send").addEventListener("click", sendMessage);
document.getElementById("user-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

document.getElementById("btn-reset").addEventListener("click", async () => {
  if (!sessionId) return;
  const data = await post("/api/state/reset", {});
  if (data.status === "ok") {
    gameState = data.state;
    renderRoster();
    renderMap();
  }
});

// Initial boot
window.onload = () => {
  initWebSocket();
};
