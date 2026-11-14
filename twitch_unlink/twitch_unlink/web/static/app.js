const state = {
  sessionId: null,
  proxyInterval: 60,
  pollTimer: null,
  countdownTimer: null,
  searchDebounce: null,
};

const $ = (id) => document.getElementById(id);

function show(el) {
  el.classList.remove("hidden");
}
function hide(el) {
  el.classList.add("hidden");
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  show(t);
  setTimeout(() => hide(t), 3200);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data.detail;
    throw new Error(Array.isArray(detail) ? detail.map((d) => d.msg).join(", ") : detail || res.statusText);
  }
  return data;
}

function updateProxyUI(proxy) {
  const enabled = proxy && proxy.enabled;
  $("proxyLabel").textContent = enabled ? proxy.current || "—" : "Без прокси";
  $("proxyBadge").textContent = enabled ? `${proxy.index + 1}/${proxy.total}` : "—";
  const remain = enabled ? proxy.seconds_until_rotation : 0;
  const interval = enabled ? proxy.interval_sec : state.proxyInterval || 60;
  $("proxyCountdown").textContent = enabled ? remain.toFixed(0) : "—";
  const pct = enabled && interval > 0 ? (remain / interval) * 100 : 0;
  $("proxyBar").style.width = `${Math.max(0, Math.min(100, pct))}%`;
  state.proxyInterval = interval;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderGames(games) {
  const root = $("cardsGames");
  root.innerHTML = "";
  if (!games.length) {
    root.innerHTML =
      '<p class="empty">Нет привязанных игр Drops. Если игра всё ещё в настройках Twitch — укажите client_id вручную ниже.</p>';
    return;
  }
  for (const g of games) {
    const card = document.createElement("article");
    card.className = "card";
    const badge = g.in_progress
      ? '<span class="tag tag--live">В инвентаре</span>'
      : '<span class="tag">Привязка</span>';
    const camp = g.campaign_name
      ? `<div class="card__meta">Кампания: ${escapeHtml(g.campaign_name)}</div>`
      : "";
    card.innerHTML = `
      <h3 class="card__title">${escapeHtml(g.name)}</h3>
      ${camp}
      <div class="card__meta">client_id: ${escapeHtml(g.client_id)}</div>
      <div class="card__tags">${badge}</div>
      <div class="card__actions">
        <button type="button" class="btn btn--danger btn--sm" data-id="${escapeHtml(g.client_id)}">
          Отвязать игру
        </button>
      </div>
    `;
    card.querySelector("button").addEventListener("click", () => disconnect(g.client_id, g.name));
    root.appendChild(card);
  }
}

async function refreshGames() {
  if (!state.sessionId) return;
  const q = $("searchInput").value.trim();
  const url = `/api/drop-games/${state.sessionId}${q ? `?q=${encodeURIComponent(q)}` : ""}`;
  try {
    const data = await api(url);
    renderGames(data.games);
    updateProxyUI(data.proxy);
    hide($("workspaceError"));
  } catch (e) {
    $("workspaceError").textContent = e.message;
    show($("workspaceError"));
  }
}

async function disconnect(clientId, label) {
  if (!state.sessionId) return;
  if (!confirm(`Отвязать игру «${label}» от Twitch Drops?`)) return;
  try {
    const data = await api("/api/disconnect", {
      method: "POST",
      body: JSON.stringify({ session_id: state.sessionId, client_id: clientId }),
    });
    updateProxyUI(data.proxy);
    toast("Игра отвязана");
    await refreshGames();
  } catch (e) {
    toast(e.message);
  }
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(refreshGames, 8000);
  state.countdownTimer = setInterval(async () => {
    if (!state.sessionId) return;
    try {
      const data = await api(`/api/session/${state.sessionId}`);
      updateProxyUI(data.proxy);
    } catch {
      /* ignore */
    }
  }, 1000);
}

function stopPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  if (state.countdownTimer) clearInterval(state.countdownTimer);
}

async function login() {
  const token = $("tokenInput").value.trim();
  const proxies = $("proxyInput").value;
  const proxy_interval_sec = Number($("proxyInterval").value) || 60;
  $("loginBtn").disabled = true;
  hide($("loginError"));
  try {
    const data = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ token, proxies, proxy_interval_sec }),
    });
    state.sessionId = data.session_id;
    $("userLogin").textContent = data.login;
    show($("userChip"));
    hide($("loginPanel"));
    show($("workspace"));
    updateProxyUI(data.proxy);
    await refreshGames();
    startPolling();
    toast(`Вошли как ${data.login}`);
  } catch (e) {
    $("loginError").textContent = e.message;
    show($("loginError"));
  } finally {
    $("loginBtn").disabled = false;
  }
}

async function logout() {
  stopPolling();
  if (state.sessionId) {
    try {
      await api(`/api/session/${state.sessionId}`, { method: "DELETE" });
    } catch {
      /* ignore */
    }
  }
  state.sessionId = null;
  $("tokenInput").value = "";
  hide($("workspace"));
  hide($("userChip"));
  show($("loginPanel"));
}

$("loginBtn").addEventListener("click", login);
$("refreshBtn").addEventListener("click", refreshGames);
$("logoutBtn").addEventListener("click", logout);
$("searchInput").addEventListener("input", () => {
  clearTimeout(state.searchDebounce);
  state.searchDebounce = setTimeout(refreshGames, 280);
});
$("manualBtn").addEventListener("click", () => {
  const id = $("manualId").value.trim();
  if (!id) {
    toast("Укажите client_id");
    return;
  }
  disconnect(id, id);
});