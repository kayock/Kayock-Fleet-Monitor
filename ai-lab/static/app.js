(() => {
  const PREF_CHAT_MODE = "ailab.max_tokens.chat.mode";
  const PREF_CHAT_CUSTOM = "ailab.max_tokens.chat.custom";
  const PREF_CMP_MODE = "ailab.max_tokens.compare.mode";
  const PREF_CMP_CUSTOM = "ailab.max_tokens.compare.custom";
  const PREF_TIMEOUT_FIRST = "ailab.timeout.first_token";
  const PREF_TIMEOUT_IDLE = "ailab.timeout.read_inactivity";
  const FLEET_MONITOR = "http://10.0.0.202:5000";

  const state = {
    discovery: null,
    chatHistory: [],
    servers: [],
    compareModels: [], // [{endpoint, engine, model, machine, label, loaded, serverId}]
    chatAbort: null,
  };

  const byId = (id) => document.getElementById(id);

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function loadPref(key, fallback) {
    try {
      const v = localStorage.getItem(key);
      return v == null ? fallback : v;
    } catch {
      return fallback;
    }
  }

  function savePref(key, value) {
    try { localStorage.setItem(key, String(value)); } catch { /* ignore */ }
  }

  function bindMaxTokenControls(modeId, customId, wrapId, modeKey, customKey) {
    const modeEl = byId(modeId);
    const customEl = byId(customId);
    const wrap = byId(wrapId);
    if (!modeEl || !customEl || !wrap) return;
    modeEl.value = loadPref(modeKey, "default") === "custom" ? "custom" : "default";
    customEl.value = loadPref(customKey, "") || "";
    const sync = () => {
      const custom = modeEl.value === "custom";
      wrap.hidden = !custom;
      savePref(modeKey, custom ? "custom" : "default");
      if (custom && customEl.value) savePref(customKey, customEl.value);
    };
    modeEl.addEventListener("change", sync);
    customEl.addEventListener("change", () => {
      if (customEl.value) savePref(customKey, customEl.value);
    });
    sync();
  }

  /** @returns {{ ok: true, value: number|null, label: string } | { ok: false, error: string }} */
  function readMaxTokens(modeId, customId) {
    const mode = byId(modeId).value;
    if (mode !== "custom") {
      return { ok: true, value: null, label: "Model default" };
    }
    const raw = String(byId(customId).value || "").trim();
    if (!raw) return { ok: false, error: "Enter a positive custom max-token value." };
    const n = Number(raw);
    if (!Number.isInteger(n) || n < 1) {
      return { ok: false, error: "Custom max tokens must be a positive integer." };
    }
    return { ok: true, value: n, label: String(n) };
  }

  function diagWarning(body) {
    const stop = body.stop_reason || {};
    const code = stop.code || "";
    const kind = body.timeout_kind || "";
    if (code === "output_limit_reached") return "⚠️ Output token limit reached";
    if (code === "streaming_interrupted") return "⚠️ Response stream interrupted";
    if (code === "request_timed_out" || body.timed_out) {
      if (kind === "first_token") return "⚠️ Timed out waiting for first token";
      if (kind === "stream_inactivity") return "⚠️ Stream stalled (inactivity timeout)";
      if (kind === "connection") return "⚠️ Connection timed out";
      if (kind === "total") return "⚠️ Total request timeout";
      return "⚠️ Request timed out";
    }
    if (code === "connection_lost") return "⚠️ Connection lost";
    if (body.partial) return "⚠️ Incomplete response (partial text preserved)";
    if (code === "unknown" && body.error) return "Completion reason unavailable";
    if (body.done_reason === "length" || body.finish_reason === "length") {
      return "⚠️ Output token limit reached";
    }
    return "";
  }

  function readTimeoutOverrides() {
    const out = {};
    const first = byId("chat-timeout-first");
    const idle = byId("chat-timeout-idle");
    if (first && first.value) {
      const n = Number(first.value);
      if (Number.isFinite(n) && n > 0) out.first_token = n;
    }
    if (idle && idle.value) {
      const n = Number(idle.value);
      if (Number.isFinite(n) && n > 0) out.read_inactivity = n;
    }
    return out;
  }

  function bindTimeoutPrefs() {
    const first = byId("chat-timeout-first");
    const idle = byId("chat-timeout-idle");
    if (first) {
      first.value = loadPref(PREF_TIMEOUT_FIRST, "") || "";
      first.addEventListener("change", () => savePref(PREF_TIMEOUT_FIRST, first.value || ""));
    }
    if (idle) {
      idle.value = loadPref(PREF_TIMEOUT_IDLE, "") || "";
      idle.addEventListener("change", () => savePref(PREF_TIMEOUT_IDLE, idle.value || ""));
    }
  }

  function diagSummaryHtml(body, modelLabel, outputLimitLabel) {
    const stop = body.stop_reason || {};
    const kind = body.timeout_kind || "";
    const kindLabel = kind
      ? ({ first_token: "first-token timeout", stream_inactivity: "stream inactivity",
           connection: "connection timeout", total: "total request timeout",
           nonstream_total: "non-stream total timeout" }[kind] || kind)
      : "";
    const stopLabel = stop.label
      || (body.timed_out && kindLabel ? `Request Timed Out (${kindLabel})` : "")
      || body.done_reason
      || body.finish_reason
      || (body.ok ? "Completion reason unavailable" : (body.error || "error"));
    const gen = body.completion_tokens != null ? String(body.completion_tokens) : "Unknown — backend did not report";
    const speed = body.tokens_per_sec != null ? `${body.tokens_per_sec} tok/s` : "—";
    const dur = body.elapsed_ms != null ? `${body.elapsed_ms} ms` : "—";
    const warn = diagWarning(body);
    const link = body.diagnostic_id
      ? `<a href="${esc(body.diagnostic_url || (FLEET_MONITOR + "/#mcc?diag=" + body.diagnostic_id))}" target="_blank" rel="noreferrer">Open diagnostics ${esc(body.diagnostic_id)}</a>`
      : "";
    const reqMode = (body.request && body.request.max_tokens_mode) || null;
    const limitShown = outputLimitLabel
      || (reqMode === "model_default" || body.request?.max_tokens == null
        ? "Model default"
        : String(body.request.max_tokens));
    return `<div class="diag-summary">
      <div><b>Model:</b> ${esc(modelLabel || body.model || "—")}</div>
      <div><b>Output limit:</b> ${esc(limitShown)}</div>
      <div><b>Generated:</b> ${esc(gen)} <span title="Backend-reported completion tokens; not character length">(backend tokens)</span></div>
      <div><b>Stop reason:</b> ${esc(stopLabel)}${stop.confidence ? ` (${esc(stop.confidence)})` : ""}</div>
      ${kind ? `<div><b>Timeout kind:</b> ${esc(kindLabel)}</div>` : ""}
      ${body.partial ? `<div><b>Partial:</b> yes — incomplete text preserved</div>` : ""}
      <div><b>Speed:</b> ${esc(speed)} · <b>Duration:</b> ${esc(dur)}</div>
      ${warn ? `<div class="diag-warn">${esc(warn)}</div>` : ""}
      ${link ? `<div>${link}</div>` : ""}
    </div>`;
  }

  function setChatBusy(busy) {
    const form = byId("chat-form");
    const cancel = byId("chat-cancel");
    if (form) {
      const btn = form.querySelector('button[type="submit"]');
      if (btn) btn.disabled = !!busy;
    }
    if (cancel) cancel.hidden = !busy;
  }

  function cancelDirectChat() {
    if (state.chatAbort) {
      state.chatAbort.abort();
      state.chatAbort = null;
    }
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
      const diag = m.diagHtml || "";
      return `<div class="msg ${esc(m.role)}">
        <div class="role">${esc(m.role)}</div>
        ${thinking}
        <div class="answer">${esc(m.content || "")}</div>
        ${diag}
      </div>`;
    }).join("") || `<div class="note">No messages yet.</div>`;
    log.scrollTop = log.scrollHeight;
  }

  byId("chat-server").addEventListener("change", loadModelsForServer);
  byId("chat-clear").addEventListener("click", () => {
    if (state.chatAbort) cancelDirectChat();
    state.chatHistory = [];
    renderChat();
    byId("chat-stats").textContent = "";
    byId("chat-meta").textContent = "";
  });
  const chatCancelBtn = byId("chat-cancel");
  if (chatCancelBtn) chatCancelBtn.addEventListener("click", cancelDirectChat);

  byId("chat-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (state.chatAbort) {
      byId("chat-stats").textContent = "A chat request is already in progress.";
      return;
    }
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
    const mt = readMaxTokens("chat-max-mode", "chat-max-custom");
    if (!mt.ok) {
      byId("chat-stats").textContent = mt.error;
      return;
    }
    const timeouts = readTimeoutOverrides();
    state.chatHistory.push({ role: "user", content: text });
    byId("chat-input").value = "";
    const assistantIdx = state.chatHistory.length;
    state.chatHistory.push({ role: "assistant", content: "", thinking: "" });
    renderChat();
    byId("chat-stats").textContent = "Waiting for first token…";
    byId("chat-meta").textContent = "Streaming…";
    setChatBusy(true);
    const started = performance.now();
    const abort = new AbortController();
    state.chatAbort = abort;

    const payload = {
      endpoint: s.endpoint,
      engine: s.engine,
      model,
      messages: state.chatHistory.slice(0, -1).map(({ role, content }) => ({ role, content })),
      think: byId("chat-think").value,
      require_loaded: true,
      stream: true,
    };
    if (mt.value != null) payload.max_tokens = mt.value;
    if (Object.keys(timeouts).length) payload.timeouts = timeouts;

    let gotPartial = false;
    let finalMeta = {
      ok: true,
      model,
      elapsed_ms: null,
      completion_tokens: null,
      tokens_per_sec: null,
      done_reason: null,
      finish_reason: null,
      stop_reason: null,
      diagnostic_id: null,
      diagnostic_url: null,
      timed_out: false,
      timeout_kind: null,
      partial: false,
      error: null,
    };

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: abort.signal,
        body: JSON.stringify(payload),
      });
      if (!res.ok && !res.body) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `HTTP ${res.status}`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let sseMode = String(s.engine || "").toLowerCase().includes("ollama") === false;

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n");
        buf = lines.pop() || "";
        for (const rawLine of lines) {
          const line = rawLine.trim();
          if (!line) continue;

          // OpenAI SSE passthrough
          if (line.startsWith("data:")) {
            sseMode = true;
            const data = line.slice(5).trim();
            if (data === "[DONE]") {
              finalMeta.ok = finalMeta.ok !== false;
              continue;
            }
            let chunk;
            try { chunk = JSON.parse(data); } catch { continue; }
            const choices = chunk.choices || [];
            if (choices.length) {
              const delta = choices[0].delta || {};
              if (delta.content) {
                gotPartial = true;
                state.chatHistory[assistantIdx].content += delta.content;
                renderChat();
                byId("chat-stats").textContent = "Generating…";
              }
              if (choices[0].finish_reason) {
                finalMeta.finish_reason = choices[0].finish_reason;
              }
            }
            if (chunk.usage) {
              finalMeta.completion_tokens = chunk.usage.completion_tokens;
              finalMeta.prompt_tokens = chunk.usage.prompt_tokens;
            }
            continue;
          }

          let body;
          try { body = JSON.parse(line); } catch { continue; }

          // AI Lab error / diagnostic_meta / Ollama NDJSON
          if (body.type === "diagnostic_meta") {
            Object.assign(finalMeta, {
              diagnostic_id: body.diagnostic_id || finalMeta.diagnostic_id,
              diagnostic_url: body.diagnostic_url || finalMeta.diagnostic_url,
              stop_reason: body.stop_reason || finalMeta.stop_reason,
              timeout_kind: body.timeout_kind || finalMeta.timeout_kind,
              request_id: body.request_id,
              elapsed_ms: body.elapsed_ms != null ? body.elapsed_ms : finalMeta.elapsed_ms,
              completion_tokens: body.completion_tokens != null
                ? body.completion_tokens : finalMeta.completion_tokens,
              done_reason: body.done_reason || finalMeta.done_reason,
              finish_reason: body.finish_reason || finalMeta.finish_reason,
              partial: !!body.partial || finalMeta.partial,
              error: body.error || finalMeta.error,
              ok: body.ok !== false && !body.error,
              timed_out: !!(body.timeout_kind || (body.error || "").toLowerCase().includes("timeout")),
            });
            continue;
          }

          if (body.ok === false) {
            finalMeta.ok = false;
            finalMeta.error = body.error || "chat failed";
            finalMeta.timeout_kind = body.timeout_kind || finalMeta.timeout_kind;
            finalMeta.partial = !!body.partial || gotPartial;
            finalMeta.timed_out = !!(body.timeout_kind
              || String(body.error || "").toLowerCase().includes("timeout"));
            continue;
          }

          // Ollama NDJSON chunk
          if (body.message) {
            const msg = body.message;
            if (msg.content) {
              gotPartial = true;
              state.chatHistory[assistantIdx].content += msg.content;
              renderChat();
              byId("chat-stats").textContent = "Generating…";
            }
            if (msg.thinking) {
              state.chatHistory[assistantIdx].thinking =
                (state.chatHistory[assistantIdx].thinking || "") + msg.thinking;
            }
          }
          if (body.done === true) {
            finalMeta.done_reason = body.done_reason || finalMeta.done_reason;
            finalMeta.completion_tokens = body.eval_count != null
              ? body.eval_count : finalMeta.completion_tokens;
            if (body.eval_count && body.eval_duration) {
              finalMeta.tokens_per_sec = Math.round(
                (body.eval_count / (body.eval_duration / 1e9)) * 100) / 100;
            }
          }
        }
      }

      const elapsed = Math.round(performance.now() - started);
      if (finalMeta.elapsed_ms == null) finalMeta.elapsed_ms = elapsed;
      finalMeta.partial = finalMeta.partial || (gotPartial && finalMeta.ok === false);
      const content = state.chatHistory[assistantIdx].content || "";
      if (!content && finalMeta.error) {
        state.chatHistory[assistantIdx].content = `Error: ${finalMeta.error}`;
      } else if (!content) {
        state.chatHistory[assistantIdx].content = "(empty response)";
      }
      if (finalMeta.partial && content && !String(content).startsWith("Error:")) {
        // keep partial text; mark incomplete in diag
      }
      state.chatHistory[assistantIdx].diagnostic_id = finalMeta.diagnostic_id || null;
      state.chatHistory[assistantIdx].diagHtml = diagSummaryHtml(finalMeta, model, mt.label);
      renderChat();

      const stop = finalMeta.stop_reason || {};
      const stopBit = stop.label
        ? ` · ${stop.label}${stop.confidence ? ` (${stop.confidence})` : ""}`
        : (finalMeta.timeout_kind
          ? ` · timeout=${finalMeta.timeout_kind}`
          : (finalMeta.done_reason || finalMeta.finish_reason
            ? ` · stop=${finalMeta.done_reason || finalMeta.finish_reason}` : ""));
      byId("chat-stats").innerHTML =
        `${finalMeta.elapsed_ms} ms · ${finalMeta.completion_tokens != null ? finalMeta.completion_tokens + " tokens" : "tokens n/a"}` +
        (finalMeta.tokens_per_sec != null ? ` · ${finalMeta.tokens_per_sec} tok/s` : "") +
        ` · output=${esc(mt.label)}` +
        (finalMeta.partial ? " · partial" : "") +
        stopBit +
        ` · ${s.engine} · ${model}` +
        (finalMeta.diagnostic_id
          ? ` · <a href="${esc(finalMeta.diagnostic_url || (FLEET_MONITOR + "/#mcc?diag=" + finalMeta.diagnostic_id))}" target="_blank" rel="noreferrer">Diagnostics ${esc(finalMeta.diagnostic_id)}</a>`
          : "");
      byId("chat-meta").textContent = finalMeta.timed_out
        ? `Timed out (${finalMeta.timeout_kind || "unknown"})`
        : (finalMeta.error ? String(finalMeta.error) : "Stream complete");
      if (finalMeta.timed_out || finalMeta.error) {
        byId("chat-meta").textContent =
          diagWarning(finalMeta) || finalMeta.error || "Chat incomplete";
      }
    } catch (err) {
      if (err.name === "AbortError") {
        const content = state.chatHistory[assistantIdx].content || "";
        if (content) {
          state.chatHistory[assistantIdx].diagHtml = diagSummaryHtml({
            ok: false, cancelled: true, partial: true, model,
            elapsed_ms: Math.round(performance.now() - started),
            stop_reason: { label: "Streaming Interrupted", code: "streaming_interrupted" },
          }, model, mt.label);
          byId("chat-stats").textContent = "Cancelled · partial response preserved";
        } else {
          state.chatHistory.splice(assistantIdx, 1);
          state.chatHistory.pop();
          byId("chat-stats").textContent = "Cancelled";
        }
        renderChat();
        byId("chat-meta").textContent = "Cancelled by user";
      } else {
        const content = state.chatHistory[assistantIdx].content || "";
        if (gotPartial && content) {
          state.chatHistory[assistantIdx].diagHtml = diagSummaryHtml({
            ok: false, error: String(err.message || err), partial: true, model,
            elapsed_ms: Math.round(performance.now() - started),
          }, model, mt.label);
          byId("chat-stats").textContent = `Error · partial preserved · ${err.message || err}`;
        } else {
          state.chatHistory[assistantIdx].content = `Error: ${err.message || err}`;
          byId("chat-stats").textContent = "Network or timeout error";
        }
        renderChat();
      }
    } finally {
      state.chatAbort = null;
      setChatBusy(false);
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

  function renderCompareSlot(slotEl, result, pending, outputLimitLabel) {
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
        <div class="note">${esc((result && result.elapsed_ms) || "—")} ms · ${esc(t.endpoint || "")}</div>
        ${diagSummaryHtml(result || {}, t.model, outputLimitLabel)}`;
      return;
    }
    slotEl.innerHTML = `<h3>${esc(title)}</h3>
      <div class="badge online">ok</div>
      ${result.thinking ? `<div class="thinking">${esc(result.thinking)}</div>` : ""}
      <pre class="answer">${esc(result.content || "")}</pre>
      <div class="note">${esc(result.elapsed_ms)} ms · tokens ${esc(result.completion_tokens ?? "—")} · ${esc(result.tokens_per_sec ?? "—")} tok/s · ${esc(t.model)} @ ${esc(hostPort(t.endpoint || ""))}</div>
      ${diagSummaryHtml(result, t.model, outputLimitLabel)}`;
  }

  byId("cmp-run").addEventListener("click", async () => {
    const prompt = byId("cmp-prompt").value.trim();
    const raw = [byId("cmp-a").value, byId("cmp-b").value, byId("cmp-c").value].filter(Boolean);
    if (!prompt || raw.length < 2) {
      byId("cmp-results").innerHTML = `<div class="card note">Provide a prompt and at least two models.</div>`;
      return;
    }
    const mt = readMaxTokens("cmp-max-mode", "cmp-max-custom");
    if (!mt.ok) {
      byId("cmp-results").innerHTML = `<div class="card note">${esc(mt.error)}</div>`;
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
      renderCompareSlot(el, { target: t }, true, mt.label);
      byId("cmp-results").appendChild(el);
      return el;
    });

    // Independent requests so a slow/failed model never blocks others from rendering.
    await Promise.all(targets.map(async (t, i) => {
      try {
        const payload = {
          endpoint: t.endpoint,
          engine: t.engine,
          model: t.model,
          messages: [{ role: "user", content: prompt }],
          think: "disabled",
          require_loaded: true,
          stream: false,
        };
        if (mt.value != null) payload.max_tokens = mt.value;
        const res = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const body = await res.json();
        body.target = t;
        renderCompareSlot(slots[i], body, false, mt.label);
      } catch (err) {
        renderCompareSlot(slots[i], {
          ok: false,
          error: String(err.message || err),
          target: t,
        }, false, mt.label);
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

  bindMaxTokenControls("chat-max-mode", "chat-max-custom", "chat-max-custom-wrap",
    PREF_CHAT_MODE, PREF_CHAT_CUSTOM);
  bindMaxTokenControls("cmp-max-mode", "cmp-max-custom", "cmp-max-custom-wrap",
    PREF_CMP_MODE, PREF_CMP_CUSTOM);
  bindTimeoutPrefs();

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
