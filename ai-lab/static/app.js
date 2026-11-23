(() => {
  const state = {
    discovery: null,
    chatHistory: [],
    servers: [],
    compareModels: [], // [{endpoint, engine, model, machine, label, loaded, serverId}]
  };

  const byId = (id) => document.getElementById(id);

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtBytes(n) {
    if (n == null || !Number.isFinite(n)) return "—";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0; let v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${u[i]}`;
  }

  function badge(status) {
    const s = (status || "unknown").toLowerCase();
    return `<span class="badge ${esc(s)}">${esc(status || "unknown")}</span>`;
  }

  function targetKey(t) {
    return `${t.endpoint || ""}|${t.model || ""}`;
  }

  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      byId(`panel-${btn.dataset.panel}`).classList.add("active");
      if (history.replaceState) history.replaceState(null, "", "#" + btn.dataset.panel);
    });
  });

  async function refreshDiscovery() {
    byId("servers-status").textContent = "Refreshing Fleet Monitor discovery…";
    try {
      const res = await fetch("/api/servers", { cache: "no-store" });
      const body = await res.json();
      state.discovery = body.data;
      state.servers = (body.data && body.data.servers) || [];
      renderMachines();
      renderServers();
      populateChatServers();
      await populateCompare();
      const loadedCount = state.compareModels.filter((m) => m.loaded).length;
      byId("servers-status").textContent =
        `Updated ${new Date().toLocaleTimeString()} · ${state.servers.length} servers · ${loadedCount} loaded model(s)`;
      byId("foot-ts").textContent = new Date().toISOString();
    } catch (err) {
      byId("servers-status").textContent = `Discovery failed: ${err.message || err}`;
    }
  }

  function renderMachines() {
    const strip = byId("machine-strip");
    const machines = (state.discovery && state.discovery.machines) || [];
    strip.innerHTML = machines.map((m) => {
      const cpu = m.cpu && m.cpu.percent != null ? `${Math.round(m.cpu.percent)}% CPU` : "CPU —";
      const mem = m.memory && m.memory.percent != null ? `${Math.round(m.memory.percent)}% RAM` : "RAM —";
      const samples = m.sampler ? `${m.sampler.samples} samples` : "no sampler";
      return `<div class="machine-pill">
        <strong>${esc(m.machine)}</strong><br>
        ${badge(m.monitor_ok ? "online" : "offline")}
        <div class="note">${esc(cpu)} · ${esc(mem)} · ${esc(samples)}</div>
        ${m.error ? `<div class="note">${esc(m.error)}</div>` : ""}
      </div>`;
    }).join("");
  }

  function modelSummary(s) {
    const models = s.models || [];
    if (models.length) {
      const loaded = models.filter((m) => m.loaded && m.chat !== false).map((m) => m.id);
      const unloaded = models.filter((m) => !m.loaded && m.chat !== false).map((m) => m.id);
      const parts = [];
      if (loaded.length) parts.push(`loaded: ${loaded.join(", ")}`);
      if (unloaded.length) parts.push(`installed (not loaded): ${unloaded.slice(0, 4).join(", ")}${unloaded.length > 4 ? "…" : ""}`);
      return parts.join(" · ") || "—";
    }
    return s.model || ((s.loaded_models || [])[0] || {}).name || "—";
  }

  function renderServers() {
    const host = byId("server-cards");
    if (!state.servers.length) {
      host.innerHTML = `<div class="card"><p class="note">No servers discovered yet.</p></div>`;
      return;
    }
    host.innerHTML = state.servers.map((s) => {
      const endpoint = s.endpoint || "—";
      const chatBtn = s.chat_compatible && s.lan_api_ok
        ? `<button type="button" data-chat="${esc(s.id)}">Open chat</button>`
        : `<button type="button" disabled title="Not chat-ready from Station">Open chat</button>`;
      const statusBits = [
        badge(s.status),
        s.lan_api_ok ? badge("api-ok") : badge("api-down"),
        s.chat_compatible ? "" : badge("no-chat"),
      ].join(" ");
      return `<article class="card">
        <h3>${esc(s.machine)} · ${esc(s.engine || "unknown")}</h3>
        <div>${statusBits}</div>
        <div class="meta">
          <div><span>Endpoint</span> <b class="ellip">${esc(endpoint)}</b></div>
          <div><span>Models</span> <b>${esc(modelSummary(s))}</b></div>
          <div><span>Quant</span> <b>${esc(s.quantization || "—")}</b></div>
          <div><span>Context</span> <b>${esc(s.context_tokens != null ? s.context_tokens : "—")}</b></div>
          <div><span>CPU / RAM</span> <b>${esc(s.cpu_percent != null ? s.cpu_percent + "%" : "—")} / ${esc(fmtBytes(s.ram_bytes))}</b></div>
          <div><span>VRAM</span> <b>${esc(fmtBytes(s.vram_bytes))}</b></div>
          <div><span>Active req</span> <b>${esc(s.active_requests != null ? s.active_requests : "—")}</b></div>
          <div><span>Tok/s</span> <b>${esc(s.tokens_per_sec != null ? s.tokens_per_sec : "—")}</b></div>
          <div><span>Last check</span> <b>${esc(s.last_check ? new Date(s.last_check * 1000).toLocaleTimeString() : "—")}</b></div>
          <div><span>Detail</span> <b>${esc(s.status_detail || s.lan_detail || s.note || "—")}</b></div>
        </div>
        <div class="card-actions">
          ${chatBtn}
          <button type="button" data-refresh-one="${esc(s.id)}">Refresh status</button>
          ${s.endpoint ? `<a class="btn" href="${esc(s.endpoint)}" target="_blank" rel="noreferrer">Server UI</a>` : ""}
          ${s.fleet_monitor_url ? `<a class="btn" href="${esc(s.fleet_monitor_url)}" target="_blank" rel="noreferrer">Fleet diagnostics</a>` : ""}
        </div>
      </article>`;
    }).join("");

    host.querySelectorAll("[data-chat]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = btn.getAttribute("data-chat");
        const s = state.servers.find((x) => x.id === id);
        if (!s) return;
        document.querySelector('.tab[data-panel="chat"]').click();
        byId("chat-server").value = id;
        byId("chat-server").dispatchEvent(new Event("change"));
      });
    });
    host.querySelectorAll("[data-refresh-one]").forEach((btn) => {
      btn.addEventListener("click", () => refreshDiscovery());
    });
  }

  function chatableServers() {
    return state.servers.filter((s) => s.chat_compatible && s.endpoint && s.lan_api_ok);
  }

  function populateChatServers() {
    const sel = byId("chat-server");
    const list = chatableServers();
    sel.innerHTML = list.length
      ? list.map((s) => `<option value="${esc(s.id)}">${esc(s.machine)} · ${esc(s.engine)} · ${esc(s.endpoint)}</option>`).join("")
      : `<option value="">No LAN-reachable chat servers</option>`;
    sel.dispatchEvent(new Event("change"));
  }

  async function loadModelsForServer() {
    const id = byId("chat-server").value;
    const s = state.servers.find((x) => x.id === id);
    const modelSel = byId("chat-model");
    if (!s) {
      modelSel.innerHTML = `<option value="">—</option>`;
      byId("chat-meta").textContent = "Select a reachable server.";
      return;
    }
    byId("chat-meta").textContent = "Loading model list…";
    try {
      const res = await fetch(`/api/models?endpoint=${encodeURIComponent(s.endpoint)}&engine=${encodeURIComponent(s.engine || "")}`);
      const body = await res.json();
      const loaded = (body.loaded || []).filter((m) => m.chat !== false);
      const available = (body.available || []).filter((m) => m.chat !== false);
      const opts = [];
      for (const m of loaded) {
        opts.push(`<option value="${esc(m.id)}">${esc(m.id)} (loaded)</option>`);
      }
      for (const m of available) {
        if (loaded.some((l) => l.id === m.id)) continue;
        opts.push(`<option value="${esc(m.id)}" data-unloaded="1">${esc(m.id)} (installed, not loaded)</option>`);
      }
      if (opts.length) {
        modelSel.innerHTML = opts.join("");
        byId("chat-meta").textContent =
          `${loaded.length} loaded · ${Math.max(0, available.length - loaded.length)} installed not loaded. Embeddings excluded. AI Lab will not auto-load.`;
      } else {
        modelSel.innerHTML = `<option value="">No chat models on this server</option>`;
        byId("chat-meta").textContent = body.error
          ? String(body.error)
          : "No chat models reported by this endpoint.";
      }
      byId("chat-think").disabled = !String(body.engine || s.engine || "").includes("ollama");
    } catch (err) {
      modelSel.innerHTML = `<option value="">error</option>`;
      byId("chat-meta").textContent = String(err.message || err);
    }
  }

  function renderChat() {
    const log = byId("chat-log");
    log.innerHTML = state.chatHistory.map((m) => {
      const thinking = m.thinking
        ? `<div class="thinking"><div class="role">Thinking</div>${esc(m.thinking)}</div>`
        : "";
      return `<div class="msg ${esc(m.role)}">
        <div class="role">${esc(m.role)}</div>
        ${thinking}
        <div class="answer">${esc(m.content || "")}</div>
      </div>`;
    }).join("") || `<div class="note">No messages yet.</div>`;
    log.scrollTop = log.scrollHeight;
  }

  byId("chat-server").addEventListener("change", loadModelsForServer);
  byId("chat-clear").addEventListener("click", () => {
    state.chatHistory = [];
    renderChat();
    byId("chat-stats").textContent = "";
  });

  byId("chat-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const id = byId("chat-server").value;
    const s = state.servers.find((x) => x.id === id);
    const model = byId("chat-model").value;
    const text = byId("chat-input").value.trim();
    if (!s || !model || !text) {
      byId("chat-stats").textContent = "Choose a model and enter a message.";
      return;
    }
    const opt = byId("chat-model").selectedOptions[0];
    if (opt && opt.dataset.unloaded === "1") {
      byId("chat-stats").textContent =
        `Model ${model} is installed but not loaded. Load it manually first; AI Lab will not auto-load.`;
      return;
    }
    state.chatHistory.push({ role: "user", content: text });
    byId("chat-input").value = "";
    renderChat();
    byId("chat-stats").textContent = "Waiting for response…";
    const started = performance.now();
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          endpoint: s.endpoint,
          engine: s.engine,
          model,
          messages: state.chatHistory.map(({ role, content }) => ({ role, content })),
          think: byId("chat-think").value,
          max_tokens: 96,
          require_loaded: true,
          stream: false,
        }),
      });
      const body = await res.json();
      const elapsed = Math.round(performance.now() - started);
      if (!body.ok) {
        state.chatHistory.push({ role: "assistant", content: `Error: ${body.error || "request failed"}` });
        byId("chat-stats").textContent = `Failed in ${elapsed} ms`;
      } else {
        state.chatHistory.push({
          role: "assistant",
          content: body.content || "",
          thinking: body.thinking || "",
          diagnostic_id: body.diagnostic_id || null,
        });
        const stop = body.stop_reason || {};
        const stopBit = stop.label
          ? ` · ${stop.label}${stop.confidence ? ` (${stop.confidence})` : ""}`
          : (body.done_reason || body.finish_reason
            ? ` · stop=${body.done_reason || body.finish_reason}` : "");
        const maxTok = (body.request && body.request.max_tokens) || 96;
        byId("chat-stats").innerHTML =
          `${elapsed} ms · ${body.completion_tokens != null ? body.completion_tokens + " tokens" : "tokens n/a"}` +
          (body.tokens_per_sec != null ? ` · ${body.tokens_per_sec} tok/s` : "") +
          ` · max_tokens=${maxTok}` +
          stopBit +
          ` · ${s.engine} · ${model}` +
          (body.diagnostic_id
            ? ` · <a href="${esc(body.diagnostic_url || ("http://10.0.0.202:5000/#mcc?diag=" + body.diagnostic_id))}" target="_blank" rel="noreferrer">Diagnostics ${esc(body.diagnostic_id)}</a>`
            : "");
      }
      renderChat();
    } catch (err) {
      state.chatHistory.push({ role: "assistant", content: `Error: ${err.message || err}` });
      renderChat();
      byId("chat-stats").textContent = "Network or timeout error";
    }
  });

  function hostPort(endpoint) {
    try {
      const u = new URL(endpoint);
      return `${u.hostname}:${u.port || (u.protocol === "https:" ? "443" : "80")}`;
    } catch {
      return endpoint || "?";
    }
  }

  function buildCompareCatalogFromDiscovery() {
    const catalog = [];
    const seen = new Set();
    for (const s of chatableServers()) {
      const models = Array.isArray(s.models) && s.models.length
        ? s.models
        : [];
      let entries = models.filter((m) => m && m.id && m.chat !== false);
      if (!entries.length) {
        const loaded = (s.loaded_models_lan || []).length
          ? s.loaded_models_lan
          : (s.loaded_models || []).map((m) => (typeof m === "string" ? m : m.name || m.id)).filter(Boolean);
        const installed = s.installed_models || [];
        const loadedSet = new Set(loaded);
        entries = [
          ...loaded.map((id) => ({ id, loaded: true, chat: true })),
          ...installed.filter((id) => !loadedSet.has(id)).map((id) => ({
            id, loaded: false, chat: !String(id).toLowerCase().includes("embed"),
          })),
        ].filter((m) => m.chat !== false);
      }
      for (const m of entries) {
        const key = `${s.endpoint}|${m.id}`;
        if (seen.has(key)) continue;
        seen.add(key);
        catalog.push({
          endpoint: s.endpoint,
          engine: s.engine,
          model: m.id,
          machine: s.machine,
          loaded: !!m.loaded,
          serverId: s.id,
          label: `${m.id} · ${s.engine} @ ${hostPort(s.endpoint)} · ${s.machine}${m.loaded ? " · loaded" : " · not loaded"}`,
        });
      }
    }
    // Prefer loaded models first in selectors
    catalog.sort((a, b) => Number(b.loaded) - Number(a.loaded) || a.label.localeCompare(b.label));
    return catalog;
  }

  async function enrichCompareCatalog(catalog) {
    // Refresh live model lists for each unique chatable endpoint so labels stay accurate.
    const byEndpoint = new Map();
    for (const s of chatableServers()) {
      if (!byEndpoint.has(s.endpoint)) byEndpoint.set(s.endpoint, s);
    }
    const live = [];
    const seen = new Set();
    await Promise.all([...byEndpoint.values()].map(async (s) => {
      try {
        const res = await fetch(`/api/models?endpoint=${encodeURIComponent(s.endpoint)}&engine=${encodeURIComponent(s.engine || "")}`);
        const body = await res.json();
        const loaded = (body.loaded || []).filter((m) => m.chat !== false);
        const available = (body.available || []).filter((m) => m.chat !== false);
        const loadedIds = new Set(loaded.map((m) => m.id));
        for (const m of loaded) {
          const key = `${s.endpoint}|${m.id}`;
          if (seen.has(key)) continue;
          seen.add(key);
          live.push({
            endpoint: s.endpoint, engine: body.engine || s.engine, model: m.id,
            machine: s.machine, loaded: true, serverId: s.id,
            label: `${m.id} · ${s.engine} @ ${hostPort(s.endpoint)} · ${s.machine} · loaded`,
          });
        }
        for (const m of available) {
          if (loadedIds.has(m.id)) continue;
          const key = `${s.endpoint}|${m.id}`;
          if (seen.has(key)) continue;
          seen.add(key);
          live.push({
            endpoint: s.endpoint, engine: body.engine || s.engine, model: m.id,
            machine: s.machine, loaded: false, serverId: s.id,
            label: `${m.id} · ${s.engine} @ ${hostPort(s.endpoint)} · ${s.machine} · not loaded`,
          });
        }
      } catch {
        // Fall back to discovery snapshot for this endpoint.
      }
    }));
    if (live.length) {
      live.sort((a, b) => Number(b.loaded) - Number(a.loaded) || a.label.localeCompare(b.label));
      return live;
    }
    return catalog;
  }

  function modelOptionsHtml(catalog, includeEmpty) {
    const opts = [];
    if (includeEmpty) opts.push(`<option value="">—</option>`);
    for (const m of catalog) {
      const val = JSON.stringify({
        endpoint: m.endpoint, engine: m.engine, model: m.model,
        machine: m.machine, label: m.label, loaded: m.loaded,
      });
      opts.push(`<option value='${esc(val)}'>${esc(m.label)}</option>`);
    }
    if (opts.length === (includeEmpty ? 1 : 0)) {
      opts.push(`<option value="">No discovered chat models</option>`);
    }
    return opts.join("");
  }

  function updateSameModelWarning() {
    const note = byId("cmp-same-note");
    if (!note) return;
    const raw = [byId("cmp-a").value, byId("cmp-b").value, byId("cmp-c").value].filter(Boolean);
    const targets = raw.map((v) => {
      try { return JSON.parse(v); } catch { return null; }
    }).filter(Boolean);
    const keys = targets.map(targetKey);
    const unloaded = targets.filter((t) => t.loaded === false);
    const msgs = [];
    if (keys.length >= 2 && new Set(keys).size < keys.length) {
      msgs.push("Two or more selectors point at the same model endpoint — comparison will intentionally run the same model more than once.");
    }
    if (unloaded.length) {
      msgs.push(`${unloaded.length} selected model(s) are not loaded; those slots will error without auto-loading.`);
    }
    note.textContent = msgs.join(" ");
  }

  async function populateCompare() {
    const base = buildCompareCatalogFromDiscovery();
    state.compareModels = await enrichCompareCatalog(base);
    byId("cmp-a").innerHTML = modelOptionsHtml(state.compareModels, false);
    byId("cmp-b").innerHTML = modelOptionsHtml(state.compareModels, false);
    byId("cmp-c").innerHTML = modelOptionsHtml(state.compareModels, true);
    // Prefer distinct loaded models for A/B when possible
    const loaded = state.compareModels.filter((m) => m.loaded);
    if (loaded.length >= 2) {
      byId("cmp-a").selectedIndex = state.compareModels.indexOf(loaded[0]);
      byId("cmp-b").selectedIndex = state.compareModels.indexOf(loaded[1]);
    } else if (loaded.length === 1) {
      const idx = state.compareModels.indexOf(loaded[0]);
      byId("cmp-a").selectedIndex = idx;
      byId("cmp-b").selectedIndex = idx;
    }
    updateSameModelWarning();
  }

  ["cmp-a", "cmp-b", "cmp-c"].forEach((id) => {
    byId(id).addEventListener("change", updateSameModelWarning);
  });

  function renderCompareSlot(slotEl, result, pending) {
    const t = (result && result.target) || {};
    const title = t.label || `${t.model || "?"} · ${t.engine || "?"} @ ${hostPort(t.endpoint || "")}`;
    if (pending) {
      slotEl.innerHTML = `<h3>${esc(title)}</h3>
        <div class="badge listening">running</div>
        <pre class="answer">Waiting for response…</pre>`;
      return;
    }
    if (!result || !result.ok) {
      slotEl.innerHTML = `<h3>${esc(title)}</h3>
        <div class="badge error">error</div>
        <pre class="answer">${esc((result && result.error) || "failed")}</pre>
        <div class="note">${esc((result && result.elapsed_ms) || "—")} ms · ${esc(t.endpoint || "")}</div>`;
      return;
    }
    slotEl.innerHTML = `<h3>${esc(title)}</h3>
      <div class="badge online">ok</div>
      ${result.thinking ? `<div class="thinking">${esc(result.thinking)}</div>` : ""}
      <pre class="answer">${esc(result.content || "")}</pre>
      <div class="note">${esc(result.elapsed_ms)} ms · tokens ${esc(result.completion_tokens ?? "—")} · ${esc(result.tokens_per_sec ?? "—")} tok/s · ${esc(t.model)} @ ${esc(hostPort(t.endpoint || ""))}</div>`;
  }

  byId("cmp-run").addEventListener("click", async () => {
    const prompt = byId("cmp-prompt").value.trim();
    const raw = [byId("cmp-a").value, byId("cmp-b").value, byId("cmp-c").value].filter(Boolean);
    if (!prompt || raw.length < 2) {
      byId("cmp-results").innerHTML = `<div class="card note">Provide a prompt and at least two models.</div>`;
      return;
    }
    const targets = raw.map((v) => JSON.parse(v));
    const keys = targets.map(targetKey);
    const same = keys.length >= 2 && new Set(keys).size < keys.length;

    byId("cmp-results").innerHTML = "";
    if (same) {
      const warn = document.createElement("div");
      warn.className = "card note";
      warn.textContent = "Notice: multiple selectors refer to the same model endpoint.";
      byId("cmp-results").appendChild(warn);
    }

    const slots = targets.map((t, i) => {
      const el = document.createElement("article");
      el.className = "card";
      el.dataset.slot = String(i);
      renderCompareSlot(el, { target: t }, true);
      byId("cmp-results").appendChild(el);
      return el;
    });

    // Independent requests so a slow/failed model never blocks others from rendering.
    await Promise.all(targets.map(async (t, i) => {
      try {
        const res = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            endpoint: t.endpoint,
            engine: t.engine,
            model: t.model,
            messages: [{ role: "user", content: prompt }],
            think: "disabled",
            max_tokens: 64,
            require_loaded: true,
            stream: false,
          }),
        });
        const body = await res.json();
        body.target = t;
        renderCompareSlot(slots[i], body, false);
      } catch (err) {
        renderCompareSlot(slots[i], {
          ok: false,
          error: String(err.message || err),
          target: t,
        }, false);
      }
    }));
  });

  async function loadQrLinks() {
    const sel = byId("qr-link-select");
    try {
      const res = await fetch("/api/homepage-links");
      const body = await res.json();
      const links = body.data || [];
      const serverLinks = state.servers
        .filter((s) => s.endpoint && s.lan_api_ok)
        .map((s) => ({ name: `AI · ${s.machine} · ${s.engine}`, href: s.endpoint }));
      const all = [
        { name: "AI Lab Command Center", href: "http://10.0.0.202:3010/" },
        { name: "Fleet Monitor (Station)", href: "http://10.0.0.202:5000/" },
        { name: "LocalForge", href: "http://10.0.0.202:3473/" },
        ...links,
        ...serverLinks,
      ];
      sel.innerHTML = all.map((l) => `<option value="${esc(l.href)}">${esc(l.name)} — ${esc(l.href)}</option>`).join("");
    } catch (err) {
      sel.innerHTML = `<option value="">failed to load links</option>`;
    }
  }

  byId("qr-link-make").addEventListener("click", async () => {
    const href = byId("qr-link-select").value;
    const box = byId("qr-link-preview");
    const status = byId("qr-link-status");
    if (!href || typeof QRCode === "undefined") {
      status.textContent = "QR library or link unavailable.";
      return;
    }
    try {
      const canvas = document.createElement("canvas");
      await QRCode.toCanvas(canvas, href, {
        width: 240, margin: 2,
        color: { dark: "#6d28d9", light: "#f8f5ff" },
      });
      box.replaceChildren(canvas);
      status.textContent = `Encoded: ${href}`;
    } catch (err) {
      status.textContent = String(err.message || err);
    }
  });

  byId("refresh-all").addEventListener("click", () => {
    refreshDiscovery().then(loadQrLinks);
  });

  refreshDiscovery().then(loadQrLinks);
  renderChat();

  function applyHash() {
    const h = (location.hash || "").replace(/^#/, "");
    if (!h) return;
    const btn = document.querySelector('.tab[data-panel="' + h + '"]');
    if (btn) btn.click();
  }
  window.addEventListener("hashchange", applyHash);
  applyHash();
})();
