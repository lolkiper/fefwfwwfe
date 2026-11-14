const state = {
  sessionId: null,
  proxyInterval: 60,
  pollTimer: null,
  countdownTimer: null,
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
    throw new Error(data.detail || data.message || res.statusText);
  }
  return data;
}

function updateProxyUI(proxy) {
  const enabled = proxy && proxy.enabled;
  $("proxyLabel").textContent = enabled ? proxy.current || "—" : "Без прокси (прямое соединение)";
  $("proxyBadge").textContent = enabled ? `${proxy.index + 1}/${proxy.total}` : "—";
  const remain = enabled ? proxy.seconds_until_rotation : 0;
  const interval = enabled ? proxy.interval_sec : state.proxyInterval || 60;
  $("proxyCountdown").textContent = enabled ? remain.toFixed(0) : "—";
  const pct = enabled && interval > 0 ? (remain / interval) * 100 : 0;
  $("proxyBar").style.width = `${Math.max(0, Math.min(100, pct))}%`;
  state.proxyInterval = interval;
}

function renderCards(containerId, items, kind) {
  const root = $(containerId);
  root.innerHTML = "";
  if (!items.length) {
    root.innerHTML = '<p class="empty">Ничего не найдено.</p>';
    return;
  }
  for (const item of items) {
    const card = document.createElement("article");
    card.className = "card";
    const extra =
      kind === "app"
        ? (item.scopes || []).map((s) => `<span class="tag">${escapeHtml(s)}</span>`).join("")
        : kind === "org"
          ? `<span class="tag">${escapeHtml(item.role)}</span>`
          : `<span class="tag">${escapeHtml(item.detail || "")}</span>`;
    const disabled = kind === "org" && !item.can_leave;
    card.innerHTML = `
      <h3 class="card__title">${escapeHtml(item.name)}</h3>
      <div class="card__meta">${escapeHtml(item.id)}</div>
      <div class="card__tags">${extra}</div>
      <div class="card__actions">
        <button type="button" class="btn btn--danger btn--sm" ${disabled ? "disabled" : ""} data-id="${escapeHtml(item.id)}" data-kind="${kind}">
          ${kind === "org" ? "Выйти" : "Отвязать"}
        </button>
      </div>
    `;
    const btn = card.querySelector("button");
    if (!disabled) {
      btn.addEventListener("click", () => disconnect(item.id, kind, item.name));
    }
    root.appendChild(card);
  }
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function refreshConnections() {
  if (!state.sessionId) return;
  const q = $("searchInput").value.trim();
  const url = `/api/connections/${state.sessionId}${q ? `?q=${encodeURIComponent(q)}` : ""}`;
  try {
    const data = await api(url);
    renderCards("cardsApps", data.apps, "app");
    renderCards("cardsSocial", data.social, "social");
    renderCards("cardsOrgs", data.organizations, "org");
    updateProxyUI(data.proxy);
    hide($("workspaceError"));
  } catch (e) {
    $("workspaceError").textContent = e.message;
    show($("workspaceError"));
  }
}

async function disconnect(itemId, kind, label) {
  if (!state.sessionId) return;
  const action = kind === "org" ? "выйти из" : "отвязать";
  if (!confirm(`${action} «${label}»?`)) return;
  try {
    const data = await api("/api/disconnect", {
      method: "POST",
      body: JSON.stringify({ session_id: state.sessionId, kind, item_id: itemId }),
    });
    updateProxyUI(data.proxy);
    toast("Готово");
    await refreshConnections();
  } catch (e) {
    toast(e.message);
  }
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(refreshConnections, 5000);
  state.countdownTimer = setInterval(async () => {
    if (!state.sessionId) return;
    try {
      const data = await api(`/api/session/${state.sessionId}`);
      updateProxyUI(data.proxy);
    } catch {
      /* ignore transient */
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
    await refreshConnections();
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

function setupTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => {
        t.classList.remove("tab--active");
        t.setAttribute("aria-selected", "false");
      });
      tab.classList.add("tab--active");
      tab.setAttribute("aria-selected", "true");
      const name = tab.dataset.tab;
      hide($("cardsApps"));
      hide($("cardsSocial"));
      hide($("cardsOrgs"));
      show($({ apps: "cardsApps", social: "cardsSocial", orgs: "cardsOrgs" }[name]));
    });
  });
}

$("loginBtn").addEventListener("click", login);
$("refreshBtn").addEventListener("click", refreshConnections);
$("logoutBtn").addEventListener("click", logout);
$("searchInput").addEventListener("input", () => {
  clearTimeout(state.searchDebounce);
  state.searchDebounce = setTimeout(refreshConnections, 280);
});
$("manualBtn").addEventListener("click", () => {
  const id = $("manualId").value.trim();
  const kind = $("manualKind").value;
  if (!id) {
    toast("Укажите ID");
    return;
  }
  disconnect(id, kind, id);
});

setupTabs();