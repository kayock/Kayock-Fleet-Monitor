/* Windows System Monitor — dashboard logic.
 *
 * Data flow: setTimeout loop -> GET /metrics (cached server snapshot) ->
 * render text/tables -> Plotly.react for every chart with FRESH trace objects
 * built from the server's history arrays. Using Plotly.react with one trace
 * object per series (each carrying its own y-array) makes clobbered traces
 * impossible, unlike hand-managed restyle indices.
 *
 * All assets (plotly.min.js, css) are served locally — the page works with
 * no Internet connection.
 */
(function () {
  "use strict";

  var CFG = window.MONITOR_CONFIG || {};
  var REFRESH_MS = Math.max(500, parseInt(CFG.refreshMs, 10) || 2000);
  var HISTORY_LEN = parseInt(CFG.historyLen, 10) || 60;

  var state = {
    data: null,
    sortBy: "cpu",
    connected: false,
    fetchTimer: null,
    inFlight: false,
    lastTickAt: 0,
    failures: 0,
    lastAI: null,
    lastFleetMachines: [],
    view: { mode: "all", machineId: null },
    peerAllowControl: false,
    inventoryQuery: "",
    inventoryFilter: "all",
    _tabsSig: null,
    _discoTick: 0
  };

  /* ---------------- formatters ---------------- */
  function fmtBytes(v) {
    if (v === null || v === undefined || isNaN(v) || v < 0) return "—";
    if (v < 1024) return Math.round(v) + " B";
    var units = ["KB", "MB", "GB", "TB", "PB"];
    var x = v, i = -1;
    while (x >= 1024 && i < units.length - 1) { x /= 1024; i++; }
    return x.toFixed(2) + " " + units[i];
  }
  function fmtRate(bps) {
    if (bps === null || bps === undefined) return "measuring…";
    return fmtBytes(bps) + "/s";
  }
  function fmtClock(mhz) {
    if (mhz === null || mhz === undefined || isNaN(mhz) || mhz <= 0) return "—";
    return mhz >= 1000 ? (mhz / 1000).toFixed(2) + " GHz" : Math.round(mhz) + " MHz";
  }
  function fmtPct(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return (Math.round(v * 10) / 10).toFixed(1) + "%";
  }
  function fmtUptime(seconds) {
    if (seconds === null || seconds === undefined || seconds < 0) return "—";
    var s = Math.floor(seconds);
    var d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600),
        m = Math.floor(s % 3600 / 60), sec = s % 60;
    function pad(n) { return (n < 10 ? "0" : "") + n; }
    return d > 0 ? d + "d " + pad(h) + ":" + pad(m) + ":" + pad(sec)
                 : pad(h) + ":" + pad(m) + ":" + pad(sec);
  }
  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function setText(id, text) {
    var el = document.getElementById(id);
    if (el && el.textContent !== text) el.textContent = text;
  }
  function pctColorClass(v) {
    return v > 90 ? "hot" : v > 70 ? "warm" : "";
  }

  /* ---------------- charts ---------------- */
  var PALETTE = {
    cpu: "#4f8cff", mem: "#f472b6",
    down: "#22d3ee", up: "#a78bfa",
    read: "#34d399", write: "#f59e0b",
    gpu: "#fbbf24"
  };
  var CHART_BASE_LAYOUT = {
    margin: { l: 46, r: 8, t: 6, b: 24 },
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: "#8fa0bf", size: 10, family: "Segoe UI, system-ui, sans-serif" },
    xaxis: { type: "date", showgrid: false, tickformat: "%H:%M:%S",
             nticks: 5, fixedrange: false },
    yaxis: { showgrid: true, gridcolor: "rgba(34,48,82,.55)", rangemode: "tozero" },
    showlegend: true,
    legend: { orientation: "h", x: 0, y: 1.12, font: { size: 10 } },
    uirevision: "keep"
  };
  var PLOTLY_CONFIG = { responsive: true, displayModeBar: false, displaylogo: false };
  var CHARTS = [
    { id: "cpu-chart", traces: [{ key: "cpu", name: "CPU %", color: PALETTE.cpu }], yTitle: "%" },
    { id: "mem-chart", traces: [{ key: "mem", name: "Memory %", color: PALETTE.mem }], yTitle: "%" },
    { id: "net-chart", traces: [{ key: "net_recv_bps", name: "Download", color: PALETTE.down },
                                { key: "net_sent_bps", name: "Upload", color: PALETTE.up }], yTitle: "bytes/s" },
    { id: "disk-chart", traces: [{ key: "disk_read_bps", name: "Disk read", color: PALETTE.read },
                                 { key: "disk_write_bps", name: "Disk write", color: PALETTE.write }], yTitle: "bytes/s" },
    { id: "gpu-chart", traces: [{ key: "gpu_util", name: "GPU engine utilization %", color: PALETTE.gpu }], yTitle: "%" }
  ];

  function traceY(data, key) {
    var h = (data && data.histories) || {};
    var arr = h[key];
    return Array.isArray(arr) ? arr : [];
  }

  function renderChart(spec, data) {
    var el = document.getElementById(spec.id);
    if (!el) return;
    if (document.hidden) return; // skip chart repaints in background tabs
    if (!window.Plotly) {
      el.innerHTML = '<div class="note">chart library unavailable</div>';
      return;
    }
    var t0 = traceY(data, "t");
    var hasAnyData = spec.traces.some(function (tr) {
      return traceY(data, tr.key).some(function (v) { return v !== null; });
    });
    var xs = t0.map(function (sec) { return new Date(sec * 1000); });
    var traces, layout;
    if (hasAnyData) {
      traces = spec.traces.map(function (tr) {
        return {
          x: xs,
          y: traceY(data, tr.key),
          name: tr.name,
          mode: "lines",
          line: { color: tr.color, width: 2, shape: "spline", smoothing: 0.6 },
          connectgaps: false,
          hovertemplate: "%{x|%H:%M:%S} · %{y:.1f}<extra>" + tr.name + "</extra>"
        };
      });
      layout = JSON.parse(JSON.stringify(CHART_BASE_LAYOUT));
      layout.xaxis = Object.assign({}, CHART_BASE_LAYOUT.xaxis,
        { range: null, autorange: true });
      layout.yaxis = Object.assign({}, CHART_BASE_LAYOUT.yaxis,
        { title: { text: spec.yTitle, font: { size: 10 } } });
    } else {
      // Empty state drawn INSIDE Plotly: chart divs are never wiped with
      // innerHTML (that pollutes Plotly's internal div state and breaks
      // the next Plotly.react diff).
      traces = [];
      layout = JSON.parse(JSON.stringify(CHART_BASE_LAYOUT));
      layout.xaxis = Object.assign({}, CHART_BASE_LAYOUT.xaxis,
        { visible: false });
      layout.yaxis = Object.assign({}, CHART_BASE_LAYOUT.yaxis,
        { visible: false });
      layout.showlegend = false;
      layout.annotations = [{
        text: "waiting for samples…",
        xref: "paper", yref: "paper", x: 0.5, y: 0.5,
        showarrow: false,
        font: { color: "#8fa0bf", size: 12 }
      }];
    }
    try {
      Plotly.react(el, traces, layout, PLOTLY_CONFIG);
    } catch (e) {
      // Self-heal: a full re-init discards any polluted internal state.
      state.lastChartError = spec.id + ": " +
        (e && e.message ? e.message : String(e));
      try {
        Plotly.newPlot(el, traces, layout, PLOTLY_CONFIG);
        state.lastChartError = null;
      } catch (e2) {
        state.lastChartError = spec.id + ": newPlot also failed: " +
          (e2 && e2.message ? e2.message : String(e2));
      }
    }
  }

  /* ---------------- renderers ---------------- */
  function renderHeader(d) {
    var m = d.meta || {};
    setText("host-name", m.hostname || "unknown host");
    setText("os-name", m.os_name || m.os_system || "—");
    var ver = m.os_build ? "build " + m.os_build : "";
    var verChip = document.getElementById("os-version");
    if (verChip) {
      verChip.textContent = ver;
      verChip.style.display = ver ? "" : "none";
    }
    setText("os-arch", m.architecture || "—");
    setText("python-version", "Python " + (m.python_version || "—"));
    setText("clock", new Date().toLocaleTimeString());
  }

  function renderCpu(d) {
    var c = d.cpu || {};
    var pctEl = document.getElementById("cpu-percent");
    if (pctEl) {
      pctEl.textContent = fmtPct(c.percent);
      pctEl.className = "big-value " + pctColorClass(c.percent);
    }
    setText("cpu-clock", fmtClock(c.clock_mhz) +
      (c.clock_source === "pdh_effective" ? "" : ""));
    setText("cpu-logical", c.count_logical !== null && c.count_logical !== undefined
      ? c.count_logical + " logical cores" : "—");
    setText("cpu-physical", c.count_physical !== null && c.count_physical !== undefined
      ? c.count_physical + " physical" : "");
    var note = document.getElementById("cpu-clock-note");
    if (note) {
      note.textContent = c.clock_note ||
        (c.clock_source ? "clock source: " + c.clock_source : "");
    }
    var grid = document.getElementById("cpu-cores");
    if (grid) {
      var cores = c.per_core || [];
      if (!cores.length) {
        grid.innerHTML = '<div class="note">per-core load unavailable</div>';
      } else {
        var html = "";
        for (var i = 0; i < cores.length; i++) {
          var v = cores[i] === null ? 0 : cores[i];
          var hue = 210 - Math.round(v) * 2.1; /* blue -> red */
          html += '<div class="core" style="border-color:hsl(' + hue + ',70%,45%)">'
            + '<span class="pct">' + (cores[i] === null ? "—" : Math.round(v)) + '</span>'
            + '<span class="lbl">core ' + (i + 1) + '</span></div>';
        }
        grid.innerHTML = html;
      }
    }
  }

  function renderMemory(d) {
    var m = d.memory || {};
    var pctEl = document.getElementById("mem-percent");
    if (pctEl) {
      pctEl.textContent = fmtPct(m.percent);
      pctEl.className = "big-value " + pctColorClass(m.percent);
    }
    var bar = document.getElementById("mem-bar");
    if (bar) bar.style.width = (m.percent === null || m.percent === undefined ? 0 : m.percent) + "%";
    setText("mem-total", fmtBytes(m.total));
    setText("mem-used", fmtBytes(m.used));
    setText("mem-available", fmtBytes(m.available));
    setText("swap-used", fmtBytes(m.swap_used));
    setText("swap-percent", m.swap_total ? "(" + fmtPct(m.swap_percent) + " of swap)" : "");
  }

  function renderGpu(d) {
    var g = d.gpu || { status: "unavailable", reason: "no data" };
    var chip = document.getElementById("gpu-status");
    var util = document.getElementById("gpu-util");
    var note = document.getElementById("gpu-util-note");
    var adapters = document.getElementById("gpu-adapters");
    var engines = document.getElementById("gpu-engines");
    var extra = document.getElementById("gpu-extra");

    var statusText, statusClass;
    if (g.status === "ok") { statusText = "active"; statusClass = "ok"; }
    else if (g.status === "initializing") { statusText = "starting…"; statusClass = "warn"; }
    else if (g.status === "unavailable") { statusText = "unavailable"; statusClass = "warn"; }
    else { statusText = "error"; statusClass = "err"; }
    if (g.updating) statusText += " · updating…";
    else if (typeof g.age_seconds === "number" && g.age_seconds > 6) {
      statusText += " · last read " + Math.round(g.age_seconds) + "s ago";
    }
    if (chip) { chip.textContent = statusText; chip.className = "status-chip " + statusClass; }

    var u = g.utilization || {};
    if (util) {
      util.textContent = u.available ? fmtPct(u.percent) : "—";
      util.className = "big-value " + (u.available ? pctColorClass(u.percent) : "");
    }
    if (note) {
      if (u.available) {
        note.textContent = (u.scope || "") + " · " + (u.source || "");
      } else {
        note.textContent = "GPU utilization unavailable — " + (g.reason || u.reason || "no counters") +
          (g.status === "initializing" ? "" : "");
      }
    }

    if (adapters) {
      var list = (g.adapters || []);
      if (!list.length) {
        adapters.innerHTML = '<div class="note">' +
          esc(g.reason || "Adapter identity unavailable on this system.") + '</div>';
      } else {
        adapters.innerHTML = list.map(function (a) {
          var sub = [];
          if (a.driver_version) sub.push("driver " + a.driver_version);
          if (a.driver_date) sub.push(a.driver_date);
          if (a.status && a.status !== "OK") sub.push("status: " + a.status);
          return '<div class="adapter"><span class="name">' + esc(a.name) + '</span>'
            + '<div class="sub">' + esc(sub.join(" · ")) + '</div></div>';
        }).join("");
      }
    }

    if (engines) {
      var types = u.engines_by_type || {};
      var keys = Object.keys(types);
      if (!keys.length) {
        engines.innerHTML = "";
      } else {
        engines.innerHTML = keys.sort().map(function (k) {
          return '<span class="engine-chip">' + esc(k) + ' <b>' + types[k] + '%</b></span>';
        }).join("");
      }
    }

    setText("gpu-driver", list && list.length && list[0].driver_version
      ? list[0].driver_version : (u.available ? "—" : "unavailable"));
    var mem = g.memory || {};
    var vramText;
    if (mem.available && mem.dedicated_bytes !== null && mem.dedicated_bytes !== undefined) {
      vramText = fmtBytes(mem.dedicated_bytes) + " in use";
      if (mem.total_adapter_bytes) vramText += " of " + fmtBytes(mem.total_adapter_bytes);
      vramText += " · " + (mem.source || "");
    } else if (list && list.length && list[0].adapter_ram_bytes) {
      vramText = "≈ " + fmtBytes(list[0].adapter_ram_bytes) +
        (list[0].adapter_ram_note ? " (" + list[0].adapter_ram_note + ")" : "");
    } else {
      vramText = "unavailable (not exposed by this system for this adapter)";
    }
    setText("gpu-vram", vramText);

    var temp = g.temperature || {};
    setText("gpu-temp", temp.available
      ? temp.celsius + " °C · " + temp.source
      : "not exposed" + (temp.reason ? " — " + temp.reason : ""));

    if (extra) {
      var nvidia = g.nvidia || {};
      var bits = [];
      if (nvidia.available) {
        bits.push("NVIDIA backend active (" + nvidia.gpus.length + " GPU"
          + (nvidia.gpus.length > 1 ? "s" : "") + ")");
      } else if (nvidia.reason) {
        bits.push(nvidia.reason);
      }
      if (u.note) bits.push(u.note);
      extra.textContent = bits.join(" · ");
    }
  }

  function volumeRow(v, withKind) {
    var label = esc(v.mountpoint || v.device);
    if (v.error) {
      return '<tr><td class="ellip">' + label + '</td>'
        + (withKind ? '<td>' + esc(v.kind || "—") + '</td>' : "")
        + '<td>' + esc(v.fstype) + '</td>'
        + '<td colspan="' + (withKind ? 4 : 4) + '" class="unavailable">unavailable — '
        + esc(v.error) + '</td></tr>';
    }
    var pct = v.percent === null || v.percent === undefined
      ? "—" : Math.round(v.percent) + "%";
    return '<tr><td class="ellip">' + label + '</td>'
      + (withKind ? '<td>' + esc(v.kind || "—") + '</td>' : "")
      + '<td>' + esc(v.fstype) + '</td>'
      + '<td>' + fmtBytes(v.total) + '</td>'
      + '<td>' + fmtBytes(v.used) + '</td>'
      + '<td>' + fmtBytes(v.free) + '</td>'
      + '<td>' + pct + '</td></tr>';
  }

  function renderStorage(d) {
    var st = d.storage || {};
    var io = st.io || {};
    var readEl = document.getElementById("disk-read");
    var writeEl = document.getElementById("disk-write");
    if (readEl) {
      readEl.textContent = "R " + fmtRate(io.read_bps);
      readEl.className = "rate read";
    }
    if (writeEl) {
      writeEl.textContent = "W " + fmtRate(io.write_bps);
      writeEl.className = "rate write";
    }
    setText("disk-io-note", io.available
      ? ("IOPS " + Math.round(io.read_iops || 0) + " r / " + Math.round(io.write_iops || 0) + " w")
      : ("I/O rates unavailable" + (io.reason ? " — " + io.reason : " (measuring…)")));

    var allVols = st.volumes || [];
    var primary = st.primary_volumes;
    var other = st.other_volumes;
    if (!primary) {
      primary = allVols.filter(function (v) { return !v.kind || v.kind === "physical"; });
      other = allVols.filter(function (v) { return v.kind && v.kind !== "physical"; });
    }
    var tbody = document.getElementById("volumes-tbody");
    if (tbody) {
      if (!primary.length && !allVols.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="note">No mounted volumes reported' +
          (st.volumes_error ? " — " + esc(st.volumes_error) : "") + '</td></tr>';
      } else if (!primary.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="note">No physical volumes reported — see Snap / virtual mounts below</td></tr>';
      } else {
        tbody.innerHTML = primary.map(function (v) { return volumeRow(v, false); }).join("");
      }
    }
    var otherDetails = document.getElementById("volumes-other-details");
    var otherBody = document.getElementById("volumes-other-tbody");
    var otherSummary = document.getElementById("volumes-other-summary");
    if (otherDetails && otherBody) {
      if (other && other.length) {
        otherDetails.hidden = false;
        if (otherSummary) {
          otherSummary.textContent = "Show Snap / virtual mounts (" + other.length + ")";
        }
        otherBody.innerHTML = other.map(function (v) { return volumeRow(v, true); }).join("");
      } else {
        otherDetails.hidden = true;
        otherBody.innerHTML = "";
      }
    }
    setText("volumes-note", "Default view shows physical drives and user volumes. Snap/virtual mounts are expandable. I/O rates are measured on physical disks.");
    setText("disk-totals", io.total_read_bytes !== null && io.total_read_bytes !== undefined
      ? ("since boot: " + fmtBytes(io.total_read_bytes) + " read · " + fmtBytes(io.total_write_bytes) + " written")
      : "");
  }

  function netRate(v) {
    return v === null || v === undefined ? "measuring…" : fmtBytes(v) + "/s";
  }

  function netRow(a, withKind) {
    var kind = a.kind || (a.is_loopback ? "loopback" : "physical");
    var nm = esc(a.name);
    if (!withKind && kind === "overlay") {
      nm += ' <span class="mini">(overlay)</span>';
    }
    return '<tr><td class="ellip">' + nm + '</td>'
      + (withKind ? '<td>' + esc(kind) + '</td>' : "")
      + '<td>' + (kind === "loopback" ? "—" : netRate(a.recv_bps)) + '</td>'
      + '<td>' + (kind === "loopback" ? "—" : netRate(a.sent_bps)) + '</td>'
      + '<td>' + fmtBytes(a.bytes_recv) + '</td>'
      + '<td>' + fmtBytes(a.bytes_sent) + '</td></tr>';
  }

  function renderNetwork(d) {
    var n = d.network || {};
    var totals = n.physical_totals || n.totals || {};
    var downEl = document.getElementById("net-down");
    var upEl = document.getElementById("net-up");
    if (downEl) downEl.textContent = "↓ " + fmtRate(totals.recv_bps);
    if (upEl) upEl.textContent = "↑ " + fmtRate(totals.sent_bps);

    var ads = n.adapters || [];
    var primary = ads.filter(function (a) {
      var kind = a.kind || (a.is_loopback ? "loopback" : "physical");
      return kind === "physical" || kind === "overlay";
    });
    var other = ads.filter(function (a) {
      var kind = a.kind || (a.is_loopback ? "loopback" : "physical");
      return kind === "virtual" || kind === "loopback";
    });
    var tbody = document.getElementById("net-tbody");
    if (tbody) {
      if (!primary.length && !ads.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="note">No network adapters reported</td></tr>';
      } else if (!primary.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="note">No physical adapters reported — see virtual / loopback below</td></tr>';
      } else {
        tbody.innerHTML = primary.map(function (a) { return netRow(a, false); }).join("");
      }
    }
    var otherDetails = document.getElementById("net-other-details");
    var otherBody = document.getElementById("net-other-tbody");
    var otherSummary = document.getElementById("net-other-summary");
    if (otherDetails && otherBody) {
      if (other.length) {
        otherDetails.hidden = false;
        if (otherSummary) {
          otherSummary.textContent = "Show virtual / loopback adapters (" + other.length + ")";
        }
        otherBody.innerHTML = other.map(function (a) { return netRow(a, true); }).join("");
      } else {
        otherDetails.hidden = true;
        otherBody.innerHTML = "";
      }
    }
    setText("net-note", "Headline rates prioritize physical LAN adapters. Docker bridges, veth, and loopback are listed separately. Totals are cumulative since boot — do not read totals as speed.");
  }

  function renderProcesses(d) {
    var p = d.processes || {};
    setText("proc-count", p.total !== null && p.total !== undefined
      ? p.total + " processes running" : "—");
    setText("proc-note", p.error ? "process list unavailable — " + p.error : (p.note || ""));
    var tbody = document.getElementById("proc-tbody");
    if (!tbody) return;
    var list = (p.list || []).slice();
    list.sort(function (a, b) {
      var ka = a[state.sortBy === "cpu" ? "cpu_percent" : "memory_percent"] || 0;
      var kb = b[state.sortBy === "cpu" ? "cpu_percent" : "memory_percent"] || 0;
      return kb - ka;
    });
    if (!list.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="note">No accessible processes</td></tr>';
      return;
    }
    tbody.innerHTML = list.map(function (r) {
      function pct(v) { return v === null || v === undefined ? "—" : (Math.round(v * 10) / 10).toFixed(1) + "%"; }
      return '<tr><td>' + esc(r.pid) + '</td>'
        + '<td class="ellip">' + esc(r.name) + '</td>'
        + '<td>' + pct(r.cpu_percent) + '</td>'
        + '<td>' + pct(r.memory_percent) + '</td>'
        + '<td>' + fmtBytes(r.rss) + '</td></tr>';
    }).join("");
  }

  function renderAll(d) {
    renderHeader(d);
    renderCpu(d);
    renderMemory(d);
    renderGpu(d);
    renderStorage(d);
    renderNetwork(d);
    renderProcesses(d);
    renderAI(d.ai_servers);
    renderFleet(d.fleet);
    CHARTS.forEach(function (spec) { renderChart(spec, d); });
    setText("refresh-note", "refresh: " + Math.round(REFRESH_MS / 1000) + " s · history: last "
      + Math.round(HISTORY_LEN * REFRESH_MS / 1000) + " s · " + HISTORY_LEN + " samples");
    setText("sampler-note", d.sampler && d.sampler.last_error
      ? "sampler warning: " + d.sampler.last_error : "sampler: " + (d.sampler ? d.sampler.samples : 0) + " samples");
    setText("uptime", fmtUptime(d.meta ? d.meta.uptime_seconds : null));
    var fleetCard = document.getElementById("card-fleet");
    if (fleetCard) fleetCard.hidden = state.view.mode === "peer";
  }

  /* ---------------- uptime + clock tickers ---------------- */
  setInterval(function () {
    setText("clock", new Date().toLocaleTimeString());
    var d = state.data;
    if (d && d.meta && d.meta.boot_time) {
      setText("uptime", fmtUptime(Math.max(0, Date.now() / 1000 - d.meta.boot_time)));
    }
  }, 1000);

  /* ---------------- fetch loop ---------------- */
  function showError(msg) {
    var banner = document.getElementById("error-banner");
    if (banner) banner.hidden = false;
    setText("error-text", msg);
    document.body.classList.add("dashboard-stale");
    state.connected = false;
  }
  function clearError() {
    var banner = document.getElementById("error-banner");
    if (banner) banner.hidden = true;
    document.body.classList.remove("dashboard-stale");
    state.connected = true;
  }

  function fetchOnce() {
    if (state.inFlight) return; // never overlap polls
    state.inFlight = true;
    var ctrl = new AbortController();
    // generous abort: a busy tab may delay promise resolution; the abort is
    // only meant to catch a dead server, never to punish slow rendering
    var abortMs = Math.max(3000, REFRESH_MS * 3);
    var timer = setTimeout(function () { ctrl.abort(); }, abortMs);
    fetch("/metrics", { signal: ctrl.signal, cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        clearTimeout(timer);
        state.failures = 0;
        state.lastError = null;
        state.data = data;
        clearError();
        var overlay = document.getElementById("loading-overlay");
        if (overlay && !overlay.classList.contains("hidden")) {
          overlay.classList.add("hidden");
        }
        tickRender(data);
      })
      .catch(function (err) {
        clearTimeout(timer);
        state.failures = (state.failures || 0) + 1;
        state.lastError = (err && err.message) ? err.message : String(err);
        showError(state.data
          ? "Lost connection to the monitor — retrying every second (showing last data)"
          : "Could not reach the monitor (" + (err && err.message ? err.message : "network error") + ") — retrying…");
      })
      .then(function () {
        state.inFlight = false;
        state.lastTickAt = Date.now();
        // retry quickly after failures so a transient stall self-heals
        var delay = state.failures ? 1000 : REFRESH_MS;
        state.fetchTimer = setTimeout(fetchOnce, delay);
      });
  }

  // Watchdog: if the refresh timer was throttled/stopped by the browser
  // (hidden tab, system sleep/resume), recover as soon as the page can run.
  setInterval(function () {
    if (!state.inFlight && state.lastTickAt &&
        Date.now() - state.lastTickAt > REFRESH_MS * 3) {
      fetchOnce();
    }
  }, 1000);

  /* ---------------- sort tabs ---------------- */
  function bindSortTabs() {
    var cpu = document.getElementById("sort-cpu");
    var mem = document.getElementById("sort-mem");
    if (cpu) cpu.addEventListener("click", function () {
      state.sortBy = "cpu";
      cpu.classList.add("active"); if (mem) mem.classList.remove("active");
      if (state.data) renderProcesses(state.data);
    });
    if (mem) mem.addEventListener("click", function () {
      state.sortBy = "mem";
      mem.classList.add("active"); if (cpu) cpu.classList.remove("active");
      if (state.data) renderProcesses(state.data);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    bindSortTabs();
    bindAIControls();
    fetchOnce();
  });

  /* ---------------- v2: AI Model Servers ---------------- */
  function aiDot(status) {
    var map = { online: "online", busy: "busy", loading: "loading",
                listening: "listening", offline: "offline", error: "error" };
    return '<span class="dot ' + (map[status] || "offline") + '"></span>';
  }

  function postJSON(url, body, method) {
    return fetch(url, {
      method: method || "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().then(function (j) { return { status: r.status, body: j }; })
        .catch(function () { return { status: r.status,
          body: { ok: false, message: "unparsable response" } }; });
    }).catch(function (e) {
      return { status: 0, body: { ok: false, message: "network error: " + e } };
    });
  }

  function mCell(label, val) {
    return "<span>" + label + " <b>" + val + "</b></span>";
  }

  function aiServerCard(s, ai) {
    var ops = (ai.ops || {})[s.id];
    var caps = s.capabilities || {};
    var status = s.status || "offline";
    var loadedList = s.loaded_models || [];
    var loadedName = loadedList.length
      ? loadedList.map(function (m) { return m.name; }).join(", ") : null;
    var modelName = loadedName || s.model_name || null;
    var inst = s.installed_models || [];
    var sid = s.id || s.key;

    var details = [];
    if (s.pid) details.push("PID " + s.pid);
    details.push(s.host + ":" + s.port);
    if (s.api_type) details.push("API: " + s.api_type);
    if (typeof s.uptime_seconds === "number") details.push("up " + fmtUptime(s.uptime_seconds));
    if (s.status_detail) details.push(s.status_detail);
    if (s.api_error && status !== "online") details.push("API: " + s.api_error);

    var metrics = [];
    metrics.push(mCell("CPU", s.cpu_percent === null || s.cpu_percent === undefined
      ? "—" : (Math.round(s.cpu_percent * 10) / 10) + "%"));
    metrics.push(mCell("RAM", s.ram_bytes ? fmtBytes(s.ram_bytes) : "—"));
    metrics.push(mCell("VRAM", s.vram_bytes ? fmtBytes(s.vram_bytes) : "—"));
    metrics.push(mCell("tokens/s", (s.tokens_per_sec === null || s.tokens_per_sec === undefined)
      ? "—" : s.tokens_per_sec));
    metrics.push(mCell("active req", (s.active_requests === null || s.active_requests === undefined)
      ? "—" : s.active_requests));
    metrics.push(mCell("ctx used", (s.context_utilization === null || s.context_utilization === undefined)
      ? "—" : Math.round(s.context_utilization * 100) + "%"));
    if (s.context_tokens) metrics.push(mCell("ctx cap", s.context_tokens));
    if (s.latency_ms !== null && s.latency_ms !== undefined) {
      metrics.push(mCell("latency", s.latency_ms + " ms"));
    }

    var controls = [];
    var remotePeer = state.view.mode === "peer";
    var allowCtl = !remotePeer || state.peerAllowControl === true;
    if (s.configured) {
      var dis = remotePeer && !allowCtl
        ? " disabled title='remote control is disabled for this machine — enable it in its fleet configuration'"
        : "";
      controls.push('<button class="btn small" data-act="start" data-sid="' + esc(sid) + '"'
        + (caps.can_start && allowCtl ? "" : dis
            || " disabled title='configure a local executable first'")
        + ">Start</button>");
      controls.push('<button class="btn small danger" data-act="stop" data-sid="' + esc(sid) + '"'
        + (caps.can_stop && allowCtl ? "" : dis
            || " disabled title='configure a local executable first'")
        + ">Stop</button>");
      controls.push('<button class="btn small" data-act="restart" data-sid="' + esc(sid) + '"'
        + ((caps.can_restart && allowCtl) ? "" : (dis || " disabled")) + ">Restart</button>");
      if (inst.length || caps.can_load || caps.can_unload) {
        // Prefer installed list; also offer currently loaded names for unload targets.
        var opts = {};
        inst.forEach(function (m) { if (m && m.name) opts[m.name] = m.name; });
        (loadedList || []).forEach(function (m) {
          var n = m && (m.name || m.id); if (n) opts[n] = n + " (loaded)";
        });
        var optHtml = Object.keys(opts).map(function (k) {
          return '<option value="' + esc(k) + '">' + esc(opts[k]) + '</option>';
        }).join("");
        controls.push('<select class="ai-model-select" data-sid="' + esc(sid) + '"'
          + ((remotePeer && !allowCtl)
            ? " disabled title='enable Allow remote control for this machine first'"
            : "")
          + '><option value="">— model —</option>' + optHtml + '</select>');
        controls.push('<button class="btn small" data-act="load" data-sid="' + esc(sid) + '"'
          + ((caps.can_load && allowCtl) ? ""
             : " disabled title='" + (caps.can_load
                ? "enable Allow remote control for this machine first"
                : "runtime model loading not supported here — use Restart with a selected model") + "'")
          + ">Load</button>");
        controls.push('<button class="btn small" data-act="unload" data-sid="' + esc(sid) + '"'
          + ((caps.can_unload && allowCtl) ? ""
             : " disabled title='" + (caps.can_unload
                ? "enable Allow remote control for this machine first"
                : "Unload is not supported for this engine — use Stop / Restart") + "'")
          + ">Unload</button>");
      }
      controls.push('<button class="btn small" data-act="logs" data-sid="' + esc(sid) + '"'
        + (remotePeer ? " disabled title='logs are available on the machine itself'" : "")
        + ">Logs</button>");
      controls.push('<button class="btn small" data-act="configure" data-sid="' + esc(sid) + '"'
        + (remotePeer ? " disabled title='configuration is done on the machine itself'" : "")
        + ">Configure</button>");
      if (remotePeer) {
        controls.push('<span class="mini">' + (allowCtl
          ? "remote control enabled — actions apply on the peer machine"
          : "monitor only — remote control disabled for this machine") + '</span>');
      }
    } else {
      controls.push('<button class="btn small" data-act="adopt" data-key="' + esc(s.key || "") + '"'
        + (remotePeer ? " disabled title='adopting is done on the machine itself'" : "")
        + '>Adopt to manage</button>');
      controls.push('<span class="mini">discovered — start/stop appear only after adopting (unconfigured processes are never touched)</span>');
    }
    if (ops) {
      controls.push('<span class="op-note">' + esc(ops.kind + ": " + ops.status
        + (ops.message ? " — " + ops.message : "")) + '</span>');
    }

    var modelHtml = modelName
      ? '<div class="model-line">model: <b>' + esc(modelName) + '</b>'
        + (s.model_path ? ' <span class="mini">' + esc(s.model_path) + '</span>' : "")
        + (loadedList[0] && loadedList[0].quantization
          ? ' · ' + esc(loadedList[0].quantization) : "")
        + (loadedList[0] && loadedList[0].parameter_size
          ? ' · ' + esc(loadedList[0].parameter_size) : "")
        + (loadedList[0] && loadedList[0].size_bytes
          ? ' · ' + fmtBytes(loadedList[0].size_bytes) : "")
        + (loadedList[0] && loadedList[0].vram_bytes
          ? ' · VRAM ' + fmtBytes(loadedList[0].vram_bytes) : "")
        + '</div>'
      : '<div class="model-line mini">'
        + (status === "online" ? "no model loaded" : "no model information available")
        + '</div>';

    var installedNote = inst.length
      ? '<div class="ai-sub">' + inst.length + " installed model(s)</div>" : "";

    return '<div class="ai-server-card">'
      + '<div class="ai-server-top">' + aiDot(status)
      + '<span class="name">' + esc(s.name || "AI server") + '</span>'
      + '<span class="type-badge">' + esc(s.type || "?") + '</span>'
      + (s.ready === true ? '<span class="status-chip ok">ready</span>'
        : s.ready === false ? '<span class="status-chip warn">not ready</span>' : "")
      + '</div>'
      + '<div class="ai-sub">' + esc(details.join(" · ")) + '</div>'
      + modelHtml + installedNote
      + '<div class="ai-metrics">' + metrics.join("") + '</div>'
      + '<div class="ai-controls">' + controls.join("") + '</div>'
      + '</div>';
  }

  function renderAI(ai) {
    var wrap = document.getElementById("ai-servers");
    if (!wrap) return;
    state.lastAI = ai || {};
    ai = ai || {};
    var disc = ai.discovery || {};
    var chip = document.getElementById("ai-discovery-status");
    if (chip) {
      chip.textContent = disc.probing ? "scanning…"
        : (disc.last_scan_ts ? "last scan "
          + new Date(disc.last_scan_ts * 1000).toLocaleTimeString()
          : "waiting for first scan");
      chip.className = "status-chip " + (disc.probing ? "warn" : "ok");
    }
    var note = document.getElementById("ai-note");
    if (note) {
      var bits = [];
      if (disc.last_error) bits.push("discovery error: " + disc.last_error);
      if (disc.interval_seconds) bits.push("auto-scan every " + disc.interval_seconds + " s");
      bits.push("a listening port alone is not health — API probes decide");
      note.textContent = bits.join(" · ");
    }
    var servers = ai.servers || [];
    if (!servers.length) {
      wrap.innerHTML = '<div class="note">No AI inference servers detected yet. '
        + "Running servers (Ollama, llama-server, LM Studio, Lemonade, "
        + "OpenAI-compatible endpoints) are discovered automatically — or add "
        + "one manually with + Add server.</div>";
      return;
    }
    wrap.innerHTML = servers.map(function (s) { return aiServerCard(s, ai); }).join("");
  }

  /* ---------------- v2: Fleet Servers ---------------- */
  function renderFleet(fleet) {
    var wrap = document.getElementById("fleet-machines");
    if (!wrap) return;
    fleet = fleet || {};
    var machines = fleet.machines || [];
    state.lastFleetMachines = machines;
    var chip = document.getElementById("fleet-status");
    if (chip) {
      chip.textContent = machines.length
        ? ("polling every " + fleet.poll_interval_seconds + " s") : "no machines configured";
      chip.className = "status-chip " + (machines.length ? "ok" : "");
    }
    if (!machines.length) {
      wrap.innerHTML = '<div class="note">No fleet machines yet. Add a computer '
        + "running this same monitor to see its identity, load and AI readiness "
        + "here. Local monitoring works without any fleet entry.</div>";
      return;
    }
    wrap.innerHTML = machines.map(function (m) {
      var meta = m.summary || {};
      var ais = m.ai;
      var bits = [];
      if (meta.hostname) bits.push(meta.hostname);
      if (meta.os_name) bits.push(meta.os_name);
      if (meta.architecture) bits.push(meta.architecture);
      if (meta.cpu_percent !== null && meta.cpu_percent !== undefined) {
        bits.push("CPU " + Math.round(meta.cpu_percent) + "%");
      }
      if (meta.memory_percent !== null && meta.memory_percent !== undefined) {
        bits.push("RAM " + Math.round(meta.memory_percent) + "%");
      }
      if (meta.uptime_seconds) bits.push("up " + fmtUptime(meta.uptime_seconds));
      var aiText = ais ? ((ais.ready_count || 0) + " of "
        + ((ais.servers || []).length) + " AI server(s) ready")
        : "AI readiness unknown";
      var seen = m.last_seen ? new Date(m.last_seen * 1000).toLocaleTimeString() : "never";
      var serverBits = (ais && ais.servers ? ais.servers : []).map(function (s) {
        return "<span>" + esc(s.name || "?") + ": <b>" + esc(s.status || "?") + "</b></span>";
      }).join("");
      return '<div class="ai-server-card">'
        + '<div class="ai-server-top">' + aiDot(m.online ? "online" : "offline")
        + '<span class="name">' + esc(m.name) + '</span>'
        + '<span class="type-badge">' + esc(m.host + ":" + m.port) + '</span>'
        + (m.online ? '<span class="status-chip ok">reachable</span>'
          : '<span class="status-chip err">offline</span>') + '</div>'
        + '<div class="ai-sub">'
        + (m.online ? esc(bits.join(" · ")) : "offline — " + esc(m.last_error || "unreachable"))
        + (m.last_seen ? " · last seen " + esc(seen) : "") + '</div>'
        + '<div class="fleet-grid"><span>AI: <b>' + esc(aiText) + '</b></span>'
        + serverBits + '</div>'
        + '<div class="ai-controls">'
        + '<button class="btn small" data-act="goto-machine" data-mid="' + esc(m.id) + '">Details</button>'
        + '<button class="btn small" data-act="fleet-configure" data-fid="' + esc(m.id) + '">Configure</button>'
        + (m.allow_control ? '<span class="status-chip ok">remote control</span>'
          : '<span class="mini">monitor only</span>') + '</div>'
        + '</div>';
    }).join("");
  }

  /* ---------------- v2/v3: AI / fleet control actions ---------------- */
  function selectedModel(sid) {
    var sel = document.querySelector('.ai-model-select[data-sid="' + sid + '"]');
    return sel ? sel.value : "";
  }

  function controlUrl(sid, action) {
    if (state.view.mode === "peer") {
      // proxied through the fleet API — the peer must have remote control
      // enabled, otherwise the backend refuses before any HTTP is sent
      return "/api/fleet/" + state.view.machineId + "/ai/servers/"
        + encodeURIComponent(sid) + "/" + action;
    }
    return "/api/ai/servers/" + encodeURIComponent(sid) + "/" + action;
  }

  function handleAIAction(act, sid, key, fid) {
    var ai = state.lastAI || {};
    function cfgById(id) {
      return (ai.config || []).filter(function (c) { return c.id === id; })[0];
    }
    if (act === "goto-machine") {
      if (fid === "__ALL__") setView("all", null);
      else if (fid === "__MCC__") setView("mcc", null);
      else setView(fid === "self" ? "self" : "peer", fid === "self" ? null : fid);
      return;
    } else if (act === "add-peer") {
      postJSON("/api/fleet/discovered/" + encodeURIComponent(key) + "/add", {})
        .then(showResult);
      return;
    }
    if (act === "start") {
      postJSON(controlUrl(sid, "start"), { model: selectedModel(sid) })
        .then(showResult);
    } else if (act === "stop") {
      if (!window.confirm("Stop this AI server? Active inference will be interrupted.")) return;
      postJSON(controlUrl(sid, "stop"), { force: false }).then(function (res) {
        if (res.status === 409 && window.confirm(
          res.body.message + "\n\nForce stop anyway?")) {
          postJSON(controlUrl(sid, "stop"), { force: true }).then(showResult);
        } else showResult(res);
      });
    } else if (act === "restart") {
      if (!window.confirm("Restart this AI server?")) return;
      postJSON(controlUrl(sid, "restart"), { force: false }).then(function (res) {
        if (res.status === 409 && window.confirm(
          res.body.message + "\n\nForce restart anyway?")) {
          postJSON(controlUrl(sid, "restart"), { force: true }).then(showResult);
        } else showResult(res);
      });
    } else if (act === "load") {
      var lm = selectedModel(sid);
      if (!lm) { window.alert("Choose a model from the dropdown first."); return; }
      postJSON(controlUrl(sid, "load"), { model: lm }).then(showResult);
    } else if (act === "unload") {
      var um = selectedModel(sid);
      if (!um) { window.alert("Choose a model from the dropdown first."); return; }
      if (!window.confirm("Unload model '" + um + "'?")) return;
      postJSON(controlUrl(sid, "unload"), { model: um }).then(showResult);
    } else if (act === "logs") {
      fetch("/api/ai/servers/" + sid + "/logs").then(function (r) { return r.json(); })
        .then(function (j) {
          setText("logs-title", "Server output");
          var el = document.getElementById("logs-content");
          if (el) el.textContent = (j.data && j.data.length) ? j.data.join("\n")
            : "(no output captured yet — output is captured for servers started by this dashboard)";
          openModal("logs-modal");
        });
    } else if (act === "configure") {
      openAIConfig(cfgById(sid));
    } else if (act === "adopt") {
      if (!window.confirm("Adopt this discovered server for management?")) return;
      postJSON("/api/ai/servers/" + encodeURIComponent(key) + "/adopt", {})
        .then(showResult);
    } else if (act === "fleet-configure") {
      var m = (state.lastFleetMachines || []).filter(function (x) { return x.id === fid; })[0];
      openFleetModal(m);
    }
  }
  function showResult(res) {
    var b = res.body || {};
    window.alert((b.ok ? "OK: " : "FAILED: ") + (b.message || ("HTTP " + res.status))
      + (b.data && b.data.warnings && b.data.warnings.length
        ? "\nWarnings:\n- " + b.data.warnings.join("\n- ") : ""));
  }

  /* ---------------- v2: modals ---------------- */
  function openModal(id) {
    var el = document.getElementById(id);
    if (el) el.hidden = false;
  }
  function closeModal(id) {
    var el = document.getElementById(id);
    if (el) el.hidden = true;
  }
  function openAIConfig(entry) {
    entry = entry || {};
    setText("ai-config-title", entry.id ? "Configure AI server" : "Add AI server");
    function set(id, v) { var el = document.getElementById(id); if (el) el.value = v === null || v === undefined ? "" : v; }
    set("ai-cfg-id", entry.id || "");
    set("ai-cfg-name", entry.name || "");
    set("ai-cfg-type", entry.type || "llama-server");
    set("ai-cfg-host", entry.host || "127.0.0.1");
    set("ai-cfg-port", entry.port || "");
    set("ai-cfg-exe", entry.exe || "");
    set("ai-cfg-args", entry.args || "");
    set("ai-cfg-modeldir", entry.model_dir || "");
    setText("ai-cfg-warnings", "");
    var del = document.getElementById("ai-cfg-delete");
    if (del) del.hidden = !entry.id;
    openModal("ai-config-modal");
  }
  function openFleetModal(entry) {
    entry = entry || {};
    setText("fleet-modal-title", entry.id ? "Configure fleet machine" : "Add fleet machine");
    function set(id, v) { var el = document.getElementById(id); if (el) el.value = v === null || v === undefined ? "" : v; }
    set("fleet-cfg-id", entry.id || "");
    set("fleet-cfg-name", entry.name || "");
    set("fleet-cfg-host", entry.host || "");
    set("fleet-cfg-port", entry.port || "");
    set("fleet-cfg-note", entry.note || "");
    var ctl = document.getElementById("fleet-cfg-control");
    if (ctl) ctl.checked = entry.allow_control === true;
    var del = document.getElementById("fleet-cfg-delete");
    if (del) del.hidden = !entry.id;
    openModal("fleet-modal");
  }

  function bindAIControls() {
    var refresh = document.getElementById("ai-refresh");
    if (refresh) refresh.addEventListener("click", function () {
      postJSON("/api/ai/discovery/refresh", {}).then(showResult);
    });
    var add = document.getElementById("ai-add");
    if (add) add.addEventListener("click", function () { openAIConfig(null); });
    var cfgClose = document.getElementById("ai-config-close");
    if (cfgClose) cfgClose.addEventListener("click", function () { closeModal("ai-config-modal"); });
    var cfgSave = document.getElementById("ai-cfg-save");
    if (cfgSave) cfgSave.addEventListener("click", function () {
      function val(id) { var el = document.getElementById(id); return el ? el.value : ""; }
      postJSON("/api/ai/servers", {
        id: val("ai-cfg-id") || undefined,
        name: val("ai-cfg-name"),
        type: val("ai-cfg-type"),
        host: val("ai-cfg-host"),
        port: parseInt(val("ai-cfg-port"), 10) || 0,
        exe: val("ai-cfg-exe"),
        args: val("ai-cfg-args"),
        model_dir: val("ai-cfg-modeldir")
      }).then(function (res) {
        showResult(res);
        if (res.body && res.body.ok) closeModal("ai-config-modal");
      });
    });
    var cfgDel = document.getElementById("ai-cfg-delete");
    if (cfgDel) cfgDel.addEventListener("click", function () {
      var idEl = document.getElementById("ai-cfg-id");
      var id = idEl ? idEl.value : "";
      if (!id) return;
      if (!window.confirm("Delete this server configuration?")) return;
      fetch("/api/ai/servers/" + encodeURIComponent(id), { method: "DELETE" })
        .then(function () { closeModal("ai-config-modal"); });
    });

    var fleetAdd = document.getElementById("fleet-add");
    if (fleetAdd) fleetAdd.addEventListener("click", function () { openFleetModal(null); });
    var fleetClose = document.getElementById("fleet-close");
    if (fleetClose) fleetClose.addEventListener("click", function () { closeModal("fleet-modal"); });
    var fleetSave = document.getElementById("fleet-cfg-save");
    if (fleetSave) fleetSave.addEventListener("click", function () {
      function val(id) { var el = document.getElementById(id); return el ? el.value : ""; }
      postJSON("/api/fleet", {
        id: val("fleet-cfg-id") || undefined,
        name: val("fleet-cfg-name"),
        host: val("fleet-cfg-host"),
        port: parseInt(val("fleet-cfg-port"), 10) || 0,
        note: val("fleet-cfg-note"),
        allow_control: !!(document.getElementById("fleet-cfg-control")
          && document.getElementById("fleet-cfg-control").checked)
      }).then(function (res) {
        showResult(res);
        if (res.body && res.body.ok) closeModal("fleet-modal");
      });
    });
    var fleetDel = document.getElementById("fleet-cfg-delete");
    if (fleetDel) fleetDel.addEventListener("click", function () {
      var idEl = document.getElementById("fleet-cfg-id");
      var id = idEl ? idEl.value : "";
      if (!id) return;
      if (!window.confirm("Remove this fleet machine? (nothing is changed on the remote machine)")) return;
      fetch("/api/fleet/" + encodeURIComponent(id), { method: "DELETE" })
        .then(function () { closeModal("fleet-modal"); });
    });
    var logsClose = document.getElementById("logs-close");
    if (logsClose) logsClose.addEventListener("click", function () { closeModal("logs-modal"); });

    // delegated actions on server cards (survives innerHTML re-renders)
    document.addEventListener("click", function (ev) {
      var el = ev.target.closest("[data-act]");
      if (!el || el.disabled) return;
      handleAIAction(el.getAttribute("data-act"),
        el.getAttribute("data-sid"),
        el.getAttribute("data-key"),
        el.getAttribute("data-fid") || el.getAttribute("data-mid"));
    });

    bindFleetViewControls();
  }

  /* ================= v3: machine views (All | self | peer) ================= */
  function showViewContainers(mode) {
    var all = document.getElementById("view-all");
    var mach = document.getElementById("view-machine");
    var mcc = document.getElementById("view-mcc");
    var head = document.getElementById("machine-view-head");
    if (all) all.hidden = mode !== "all";
    if (mach) mach.hidden = mode !== "machine";
    if (mcc) mcc.hidden = mode !== "mcc";
    if (head) head.hidden = mode !== "machine" || state.view.mode === "self";
  }

  function setView(mode, machineId) {
    state.view = { mode: mode, machineId: machineId || null };
    if (mode === "peer") {
      var m = (state.lastFleetMachines || []).filter(function (x) {
        return x.id === machineId;
      })[0];
      state.peerAllowControl = m ? m.allow_control === true : false;
      state.peerDetail = null;
    }
    if (mode === "mcc") {
      showViewContainers("mcc");
      refreshMCC();
      return;
    }
    // immediate re-render from cached data
    if (state.lastData) tickRender(state.lastData, true);
  }

  function tickRender(d, force) {
    state.lastData = d;
    renderMachineTabs(d);
    if (state.view.mode === "mcc") {
      showViewContainers("mcc");
      return;
    }
    if (state.view.mode === "all") {
      showViewContainers("all");      renderOverview(d);
      renderInventory(d.fleet);
      renderDiscoveryMaybe(d);
      return;
    }
    showViewContainers("machine");
    if (state.view.mode === "self") {
      setMachineHead(null);
      renderAll(d);
    } else {
      fetchFleetDetail(state.view.machineId, force);
    }
  }

  function renderMachineTabs(d) {
    var bar = document.getElementById("machine-tabs");
    if (!bar) return;
    var machines = (d.fleet && d.fleet.machines) || [];
    var label = (d.instance && d.instance.label) || "This machine";
    var sig = machines.map(function (m) {
      return m.id + "|" + m.name + "|" + (m.online ? "1" : "0");
    }).join(";") + "||" + label;
    if (sig === state._tabsSig) {
      highlightActiveTab(bar);   // re-apply in case the view changed
      return;
    }
    state._tabsSig = sig;
    var html = '<button class="tab machine-tab" data-act="goto-machine" data-mid="__ALL__" type="button">All Machines</button>'
      + '<button class="tab machine-tab" data-act="goto-machine" data-mid="__MCC__" type="button">Model Control</button>'
      + '<button class="tab machine-tab" data-act="goto-machine" data-mid="self" type="button">'
      + esc(label) + '</button>';
    html += machines.map(function (m) {
      return '<button class="tab machine-tab" data-act="goto-machine" data-mid="'
        + esc(m.id) + '" type="button">' + aiDot(m.online ? "online" : "offline")
        + esc(m.name) + '</button>';
    }).join("");
    bar.innerHTML = html;
    highlightActiveTab(bar);
  }

  function highlightActiveTab(bar) {
    var tabs = bar.querySelectorAll(".machine-tab");
    for (var i = 0; i < tabs.length; i++) {
      var mid = tabs[i].getAttribute("data-mid");
      var active = (state.view.mode === "all" && mid === "__ALL__")
        || (state.view.mode === "mcc" && mid === "__MCC__")
        || (state.view.mode === "self" && mid === "self")
        || (state.view.mode === "peer" && mid === state.view.machineId);
      tabs[i].className = "tab machine-tab" + (active ? " active" : "");
    }
  }

  function setMachineHead(info) {
    var head = document.getElementById("machine-view-head");
    if (!head) return;
    head.hidden = !info;
    if (info) {
      setText("machine-view-title", info.name);
      setText("machine-view-note", info.note);
    }
  }

  function fetchFleetDetail(machineId, force) {
    fetch("/api/fleet/" + encodeURIComponent(machineId) + "/detail",
          { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (!j || !j.ok || !j.data || !j.data.detail) {
          var m = (state.lastFleetMachines || []).filter(function (x) {
            return x.id === machineId;
          })[0];
          setMachineHead({
            name: m ? m.name : "machine",
            note: (m && m.last_error ? "offline — " + m.last_error
                   : "offline — no cached data yet")
              + (m && m.last_seen
                 ? " · last seen " + new Date(m.last_seen * 1000).toLocaleTimeString()
                 : "")
          });
          return;
        }
        var d = j.data.detail;
        state.peerAllowControl = ((state.lastFleetMachines || [])
          .filter(function (x) { return x.id === machineId; })[0] || {}
        ).allow_control === true;
        setMachineHead({
          name: j.data.name,
          note: (j.data.online ? "live (cached server-side, refreshed every "
                + "10 s)" : "OFFLINE — showing last known data")
            + (j.data.last_seen
               ? " · last seen " + new Date(j.data.last_seen * 1000).toLocaleTimeString()
               : "")
            + (state.peerAllowControl
               ? " · remote control enabled"
               : " · monitor only")
        });
        renderAll(d);
      })
      .catch(function () { /* keep last view; next tick retries */ });
  }

  function selfSummary(d) {
    var g = (d.gpu || {}).utilization || {};
    return {
      hostname: (d.meta || {}).hostname,
      os_name: (d.meta || {}).os_name,
      architecture: (d.meta || {}).architecture,
      uptime_seconds: (d.meta || {}).uptime_seconds,
      cpu_percent: (d.cpu || {}).percent,
      cpu_clock_mhz: (d.cpu || {}).clock_mhz,
      memory_percent: (d.memory || {}).percent,
      gpu_status: (d.gpu || {}).status,
      gpu_percent: g.percent,
      gpu_available: g.available,
      gpu_vram_used: ((d.gpu || {}).memory || {}).dedicated_bytes,
      gpu_vram_total: ((d.gpu || {}).memory || {}).total_adapter_bytes,
      net_recv_bps: ((d.network || {}).totals || {}).recv_bps,
      net_sent_bps: ((d.network || {}).totals || {}).sent_bps,
      disk_read_bps: ((d.storage || {}).io || {}).read_bps,
      disk_write_bps: ((d.storage || {}).io || {}).write_bps
    };
  }

  function selfAiSummary(d) {
    var servers = (d.ai_servers && d.ai_servers.servers) || [];
    var ready = 0;
    var online = 0;
    var rows = servers.map(function (s) {
      var loaded = s.loaded_models || [];
      if (s.ready) ready++;
      if (s.status === "online" || s.status === "busy") online++;
      return {
        id: s.id || s.key, name: s.name, type: s.type, status: s.status,
        ready: s.ready,
        model: loaded.length ? loaded[0].name : s.model_name,
        quantization: loaded.length ? loaded[0].quantization : null,
        cpu_percent: s.cpu_percent, ram_bytes: s.ram_bytes,
        vram_bytes: s.vram_bytes, tokens_per_sec: s.tokens_per_sec,
        active_requests: s.active_requests
      };
    });
    return { online_count: online, ready_count: ready, servers: rows };
  }

  function ovBar(label, pctv, cls) {
    var v = (pctv === null || pctv === undefined) ? 0 : Math.max(0, Math.min(100, pctv));
    return '<div class="ov-bar"><span class="label">' + label + '</span>'
      + '<div class="bar-wrap"><div class="bar-fill ' + cls + '" style="width:'
      + v.toFixed(0) + '%"></div></div>'
      + '<span>' + (pctv === null || pctv === undefined ? "—" : Math.round(pctv) + "%") + '</span></div>';
  }

  function ovCard(opts) {
    var s = opts.summary || {};
    var chips = [];
    if (s.hostname) chips.push(esc(s.hostname));
    if (s.os_name) chips.push(esc(s.os_name));
    if (s.architecture) chips.push(esc(s.architecture));
    if (s.uptime_seconds) chips.push("up " + fmtUptime(s.uptime_seconds));
    if (s.cpu_clock_mhz) chips.push(Math.round(s.cpu_clock_mhz) + " MHz");
    var ai = opts.ai;
    var aiText = ai ? ((ai.ready_count || 0) + " of " + (ai.servers || []).length
      + " AI server(s) ready") : "AI readiness unknown";
    var gpuNote = s.gpu_available === false
      ? "GPU: " + esc(s.gpu_status || "unavailable")
      : null;
    var vram = (s.gpu_vram_used && s.gpu_vram_total)
      ? fmtBytes(s.gpu_vram_used) + " / " + fmtBytes(s.gpu_vram_total) : null;
    return '<div class="overview-card">'
      + '<div class="row1">' + aiDot(opts.online ? "online" : "offline")
      + '<span class="name">' + esc(opts.name) + '</span>'
      + '<span class="type-badge">' + esc(opts.tag) + '</span>'
      + (opts.self ? '<span class="status-chip ok">this machine</span>'
        : (opts.online ? '<span class="status-chip ok">reachable</span>'
          : '<span class="status-chip err">offline</span>'))
      + (opts.allowControl ? '<span class="status-chip">remote ctl</span>' : "")
      + '</div>'
      + '<div class="ov-chips">' + chips.join(" · ") + (gpuNote
        ? (chips.length ? " · " : "") + gpuNote : "") + '</div>'
      + '<div class="ov-bars">'
      + ovBar("CPU", s.cpu_percent, "cpu")
      + ovBar("RAM", s.memory_percent, "mem")
      + (s.gpu_available ? ovBar("GPU", s.gpu_percent, "gpu") : "")
      + '</div>'
      + '<div class="ov-chips">'
      + '<span>AI: <b>' + esc(aiText) + '</b></span>'
      + (vram ? '<span>VRAM: <b>' + vram + '</b></span>' : "")
      + (s.net_recv_bps !== null && s.net_recv_bps !== undefined
        ? '<span>net ↓ <b>' + fmtRate(s.net_recv_bps) + '</b> ↑ <b>'
          + fmtRate(s.net_sent_bps) + '</b></span>' : "")
      + '</div>'
      + (opts.lastError ? '<div class="ai-sub">' + esc(opts.lastError) + '</div>' : "")
      + '<div class="ov-actions">'
      + '<button class="btn small" data-act="goto-machine" data-mid="'
      + esc(opts.mid) + '">Details</button>'
      + (opts.configureAttrs || "")
      + '</div>'
      + '</div>';
  }

  function renderOverview(d) {
    var grid = document.getElementById("overview-grid");
    if (!grid) return;
    var cards = [ovCard({
      mid: "self", name: (d.instance && d.instance.label) || "This machine",
      tag: "localhost", self: true, online: true,
      summary: selfSummary(d), ai: selfAiSummary(d)
    })];
    var machines = (d.fleet && d.fleet.machines) || [];
    for (var i = 0; i < machines.length; i++) {
      var m = machines[i];
      var cfgBtn = '<button class="btn small" data-act="fleet-configure" data-fid="'
        + esc(m.id) + '">Configure</button>';
      cards.push(ovCard({
        mid: m.id, name: m.name, tag: m.host + ":" + m.port,
        online: m.online, summary: m.summary || {}, ai: m.ai,
        allowControl: m.allow_control, lastError: m.last_error,
        configureAttrs: cfgBtn
      }));
    }
    grid.innerHTML = cards.join("");
    var chipEl = document.getElementById("fleet-poll-status");
    if (chipEl) {
      chipEl.textContent = machines.length
        ? ("fleet: " + machines.length + " machine(s) · polling every "
           + (d.fleet.poll_interval_seconds || 10) + " s")
        : "fleet: no machines configured yet";
      chipEl.className = "status-chip "
        + (d.fleet && d.fleet.poll_error ? "err" : "ok");
    }
    var noteEl = document.getElementById("fleet-note");
    if (noteEl) {
      noteEl.textContent = (d.fleet && d.fleet.poll_error)
        ? ("fleet poll error: " + d.fleet.poll_error
           + " — affected machines show their last known data")
        : "Each machine runs this same monitor. Peer failures never affect local monitoring.";
    }
  }

  function renderInventory(fleet) {
    var tbody = document.getElementById("inventory-tbody");
    if (!tbody) return;
    var q = (state.inventoryQuery || "").toLowerCase();
    var filter = state.inventoryFilter || "all";
    var rows = (fleet && fleet.ai_inventory) || [];
    // prepend THIS machine's AI servers so the inventory is truly fleet-wide
    var d = state.lastData || {};
    var selfLabel = (d.instance && d.instance.label) || "This machine";
    var selfAi = selfAiSummary(d);
    (selfAi.servers || []).forEach(function (srv) {
      rows = [Object.assign({ machine_id: "self", machine_name: selfLabel,
                machine_host: "localhost", allow_control: true }, srv)
              ].concat(rows);
    });
    if (filter !== "all") {
      rows = rows.filter(function (r) { return r.status === filter; });
    }
    if (q) {
      rows = rows.filter(function (r) {
        return ((r.model || "") + " " + (r.name || "") + " "
          + (r.machine_name || "") + " " + (r.type || ""))
          .toLowerCase().indexOf(q) >= 0;
      });
    }
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="11" class="note">'
        + (q || filter !== "all"
          ? "No AI servers match the current search/filter."
          : "No AI servers reported by the fleet yet — they appear here as "
            + "machines come online and AI discovery runs.")
        + '</td></tr>';
      return;
    }
    function pct(v) {
      return (v === null || v === undefined) ? "—"
        : (Math.round(v * 10) / 10) + "%";
    }
    tbody.innerHTML = rows.map(function (r) {
      var ctl = (r.machine_id === "self" || r.allow_control)
        ? '<button class="btn small" data-act="goto-machine" data-mid="'
          + esc(r.machine_id) + '">Manage</button>'
        : '<span class="mini">monitor only</span>';
      return '<tr>'
        + '<td>' + esc(r.machine_name)
        + (r.machine_id === "self" ? ' <span class="mini">(local)</span>' : "")
        + '</td>'
        + '<td>' + esc(r.name || "?") + '</td>'
        + '<td>' + esc(r.type || "?") + '</td>'
        + '<td>' + (r.model ? esc(r.model)
          + (r.quantization ? ' <span class="mini">' + esc(r.quantization) + '</span>' : "")
          : '<span class="mini">—</span>') + '</td>'
        + '<td>' + aiDot(r.status) + esc(r.status || "?")
        + (r.ready === true ? ' <span class="status-chip ok">ready</span>' : "")
        + '</td>'
        + '<td>' + pct(r.cpu_percent) + '</td>'
        + '<td>' + (r.ram_bytes ? fmtBytes(r.ram_bytes) : "—") + '</td>'
        + '<td>' + (r.vram_bytes ? fmtBytes(r.vram_bytes) : "—") + '</td>'
        + '<td>' + (r.tokens_per_sec === null || r.tokens_per_sec === undefined
          ? "—" : r.tokens_per_sec) + '</td>'
        + '<td>' + (r.active_requests === null || r.active_requests === undefined
          ? "—" : r.active_requests) + '</td>'
        + '<td>' + ctl + '</td></tr>';
    }).join("");
  }

  function renderDiscoveryMaybe(d) {
    // throttled: fetch discovered peers every ~10 s, not every 2 s tick
    state._discoTick = (state._discoTick || 0) + 1;
    if (state._discoTick % 5 !== 1) return;
    var panel = document.getElementById("discovered-panel");
    fetch("/api/fleet/discovered", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!panel) return;
        var peers = j.data || [];
        var disco = j.discovery || {};
        var html = '<h3 class="mini" style="margin:10px 0 4px">Discovered on LAN (UDP broadcast, port '
          + esc(disco.port || 51015) + ')</h3>';
        if (disco.error) {
          html += '<div class="note">' + esc(disco.error) + '</div>';
        } else if (!peers.length) {
          html += '<div class="note">No peers heard yet — announcements go out '
            + 'every 15 s. Firewalled broadcasts? Add machines manually with host/IP.</div>';
        } else {
          html += '<div class="discovered-list">' + peers.map(function (p) {
            return '<div class="discovered-row">'
              + aiDot("listening") + '<b>' + esc(p.name) + '</b>'
              + '<span class="ip">' + esc(p.ip + ":" + p.port) + '</span>'
              + '<span class="seen">hostname ' + esc(p.hostname || "—")
              + ' · seen ' + new Date(p.last_seen * 1000).toLocaleTimeString()
              + '</span>'
              + '<button class="btn small" data-act="add-peer" data-key="'
              + esc(p.ip) + '">Add</button></div>';
          }).join("") + '</div>';
        }
        panel.innerHTML = html;
      }).catch(function () { /* discovery panel is optional */ });
  }

  function bindFleetViewControls() {
    var search = document.getElementById("inventory-search");
    if (search) search.addEventListener("input", function () {
      state.inventoryQuery = search.value || "";
      if (state.lastData) renderInventory(state.lastData.fleet);
    });
    var filter = document.getElementById("inventory-filter");
    if (filter) filter.addEventListener("change", function () {
      state.inventoryFilter = filter.value || "all";
      if (state.lastData) renderInventory(state.lastData.fleet);
    });
    var scan = document.getElementById("fleet-discover-btn");
    if (scan) scan.addEventListener("click", function () {
      state._discoTick = 0;  // force the throttled fetch on the next tick
      if (state.lastData) renderDiscoveryMaybe(state.lastData);
    });
  }

  /* ================= v3.2: Model Control Center ================= */
  state.mcc = {
    mode: "simple",
    machine: "all",
    query: "",
    filter: "all",
    cards: [],
    profiles: [],
    selectedCard: null,
    pending: {},
  };

  function mccControlUrl(card, action) {
    if (card.is_self || !card.machine_id) {
      return "/api/ai/servers/" + encodeURIComponent(card.server_id) + "/" + action;
    }
    return "/api/fleet/" + encodeURIComponent(card.machine_id)
      + "/ai/servers/" + encodeURIComponent(card.server_id) + "/" + action;
  }

  function refreshMCC() {
    var chip = document.getElementById("mcc-status");
    if (chip) { chip.textContent = "refreshing…"; chip.className = "status-chip warn"; }
    Promise.all([
      fetch("/api/mcc/inventory", { cache: "no-store" }).then(function (r) { return r.json(); }),
      fetch("/api/mcc/profiles", { cache: "no-store" }).then(function (r) { return r.json(); }),
    ]).then(function (pair) {
      var inv = pair[0]; var prof = pair[1];
      state.mcc.cards = (inv.data && inv.data.cards) || [];
      state.mcc.profiles = (prof.data && prof.data.profiles) || [];
      state.mcc.templates = (prof.data && prof.data.templates) || [];
      renderMCCFilters();
      renderMCCProfiles();
      renderMCCCards();
      if (chip) {
        chip.textContent = state.mcc.cards.length + " model card(s)";
        chip.className = "status-chip ok";
      }
    }).catch(function (err) {
      if (chip) {
        chip.textContent = "error: " + (err && err.message ? err.message : err);
        chip.className = "status-chip err";
      }
    });
  }

  function renderMCCFilters() {
    var host = document.getElementById("mcc-machine-filters");
    if (!host) return;
    var machines = {};
    state.mcc.cards.forEach(function (c) { machines[c.machine] = true; });
    var names = Object.keys(machines).sort();
    var html = '<button type="button" class="tab' + (state.mcc.machine === "all" ? " active" : "")
      + '" data-mcc-machine="all">All machines</button>';
    names.forEach(function (n) {
      html += '<button type="button" class="tab' + (state.mcc.machine === n ? " active" : "")
        + '" data-mcc-machine="' + esc(n) + '">' + esc(n) + '</button>';
    });
    host.innerHTML = html;
  }

  function renderMCCProfiles() {
    var sel = document.getElementById("mcc-profile-select");
    if (!sel) return;
    var opts = ['<option value="">— profile —</option>'];
    (state.mcc.profiles || []).forEach(function (p) {
      opts.push('<option value="' + esc(p.id) + '">' + esc(p.name) + '</option>');
    });
    sel.innerHTML = opts.join("");
  }

  function filteredMCCCards() {
    var q = (state.mcc.query || "").toLowerCase();
    var filter = state.mcc.filter || "all";
    var machine = state.mcc.machine || "all";
    return (state.mcc.cards || []).filter(function (c) {
      if (machine !== "all" && c.machine !== machine) return false;
      if (filter === "loaded" && c.model_status !== "loaded") return false;
      if (filter === "unloaded" && c.model_status !== "unloaded") return false;
      if (filter === "manageable" && !(c.actions && (c.actions.load || c.actions.unload || c.actions.restart))) return false;
      if (!q) return true;
      var hay = [c.model, c.machine, c.engine, c.server_name, c.endpoint].join(" ").toLowerCase();
      return hay.indexOf(q) >= 0;
    });
  }

  function renderMCCCards() {
    var host = document.getElementById("mcc-cards");
    if (!host) return;
    var cards = filteredMCCCards();
    if (!cards.length) {
      host.innerHTML = '<div class="note">No model cards match this filter.</div>';
      return;
    }
    var mode = state.mcc.mode || "simple";
    host.innerHTML = cards.map(function (c) {
      var status = c.model_status === "loaded" ? "Loaded" : (c.model ? "Unloaded" : (c.status || "unknown"));
      var ctx = c.context_tokens != null ? (Number(c.context_tokens).toLocaleString() + " ctx") : "Context: Unknown — not reported by backend";
      var mem = [];
      if (c.ram_bytes != null) mem.push("RAM " + fmtBytes(c.ram_bytes));
      if (c.vram_bytes != null) mem.push("VRAM " + fmtBytes(c.vram_bytes));
      if (c.size_bytes != null) mem.push("size " + fmtBytes(c.size_bytes));
      var res = c.resources || {};
      if (mode !== "simple") {
        if (res.ram_available_bytes != null) mem.push("avail RAM " + fmtBytes(res.ram_available_bytes));
        if (res.vram_total_bytes != null) mem.push("GPU mem " + fmtBytes(res.vram_used_bytes || 0) + "/" + fmtBytes(res.vram_total_bytes));
      }
      var btns = [];
      if (c.actions.load) btns.push('<button class="btn" data-mcc-act="load" data-cid="' + esc(c.id) + '">Load</button>');
      if (c.actions.unload) btns.push('<button class="btn danger" data-mcc-act="unload" data-cid="' + esc(c.id) + '">Unload</button>');
      if (c.actions.switch_via_restart) btns.push('<button class="btn" data-mcc-act="switch" data-cid="' + esc(c.id) + '" title="Restart server with this model">Load via Restart</button>');
      if (c.actions.stop) btns.push('<button class="btn danger" data-mcc-act="stop" data-cid="' + esc(c.id) + '">Stop server</button>');
      if (c.actions.restart) btns.push('<button class="btn" data-mcc-act="restart" data-cid="' + esc(c.id) + '">Restart server</button>');
      btns.push('<button class="btn" data-mcc-act="settings" data-cid="' + esc(c.id) + '">Settings</button>');
      btns.push('<button class="btn" data-mcc-act="details" data-cid="' + esc(c.id) + '">Details</button>');
      if (!c.actions.load && c.disabled_reasons && c.disabled_reasons.load && c.model && c.model_status !== "loaded") {
        btns.push('<span class="mini">' + esc(c.disabled_reasons.load) + '</span>');
      }
      if (!c.actions.unload && c.disabled_reasons && c.disabled_reasons.unload && c.model_status === "loaded") {
        btns.push('<span class="mini">' + esc(c.disabled_reasons.unload) + '</span>');
      }
      var advanced = "";
      if (mode === "advanced" || mode === "expert") {
        advanced = '<div class="mcc-meta">'
          + '<div><span>Active requests</span><b>' + esc(c.active_requests != null ? c.active_requests : "—") + '</b></div>'
          + '<div><span>Tok/s</span><b>' + esc(c.tokens_per_sec != null ? c.tokens_per_sec : "—") + '</b></div>'
          + '<div><span>Endpoint</span><b class="ellip">' + esc(c.endpoint || "—") + '</b></div>'
          + '<div><span>Remote control</span><b>' + esc(c.is_self ? "local" : (c.allow_control ? "enabled" : "disabled")) + '</b></div>'
          + '</div>';
      }
      if (mode === "expert") {
        advanced += '<div class="mini">configured args: ' + esc(c.configured_args || "(none / unknown)") + '</div>';
        advanced += '<div class="mini">engine note: ' + esc((c.capabilities && c.capabilities.notes) || "—") + '</div>';
      }
      return '<article class="mcc-card">'
        + '<div class="mcc-card-top">'
        + '<h3>' + esc(c.model || "(no model)") + ' — ' + esc(c.machine) + '</h3>'
        + '<span class="status-chip ' + (c.model_status === "loaded" ? "ok" : "warn") + '">' + esc(status) + '</span>'
        + '</div>'
        + '<div class="mcc-sub">Engine: <b>' + esc(c.engine_label || c.engine) + '</b> · '
        + esc(ctx) + (mem.length ? " · " + mem.join(" · ") : "") + '</div>'
        + '<div class="mcc-sub">Server: ' + esc(c.server_name || c.server_id || "—")
        + (c.quantization ? " · " + esc(c.quantization) : "")
        + (c.parameter_size ? " · " + esc(c.parameter_size) : "") + '</div>'
        + advanced
        + '<div class="mcc-actions">' + btns.join("") + '</div>'
        + '</article>';
    }).join("");
  }

  function findMCCCard(cid) {
    return (state.mcc.cards || []).filter(function (c) { return c.id === cid; })[0];
  }

  function openMCCSettings(card) {
    state.mcc.selectedCard = card;
    state.mcc.pending = {};
    var title = document.getElementById("mcc-modal-title");
    if (title) title.textContent = (card.model || "Server") + " · Settings (" + (state.mcc.mode || "simple") + ")";
    var body = document.getElementById("mcc-modal-body");
    if (!body) return;
    var caps = card.capabilities || {};
    var params = caps.parameters || {};
    var mode = state.mcc.mode || "simple";
    var simpleKeys = ["context", "thinking", "temperature", "max_output", "keep_alive"];
    var html = '<p class="note">Active = verified from the running server. Configured = stored launch settings. Pending = your edits (not applied yet).</p>';
    html += '<div class="mcc-kv"><div><span>Active model</span><b>' + esc(card.model || "—") + '</b></div>';
    html += '<div><span>Active context</span><b>' + esc(card.context_tokens != null ? card.context_tokens : "Unknown — not reported by backend") + '</b></div></div>';
    var keys = Object.keys(params);
    if (mode === "simple") keys = keys.filter(function (k) { return simpleKeys.indexOf(k) >= 0; });
    if (mode !== "expert") keys = keys.filter(function (k) { return k !== "launch_args"; });
    keys.forEach(function (k) {
      var meta = params[k] || {};
      if (meta.ui === "none") return;
      var support = meta.support || "unknown";
      var apply = meta.apply || "unknown";
      html += '<label class="mcc-field">' + esc(meta.label || k)
        + '<span class="mini"> · ' + esc(support) + " · apply: " + esc(apply) + '</span>'
        + (meta.help ? '<div class="mini">' + esc(meta.help) + '</div>' : "")
        + (meta.note ? '<div class="mini">' + esc(meta.note) + '</div>' : "");
      if (support === "unsupported") {
        html += '<div class="mini">Unsupported on this engine.</div></label>';
        return;
      }
      if (meta.ui === "toggle") {
        html += '<select data-mcc-param="' + esc(k) + '"><option value="">— leave unchanged —</option>'
          + '<option value="true">Enabled</option><option value="false">Disabled</option></select>';
      } else {
        html += '<input data-mcc-param="' + esc(k) + '" type="text" placeholder="leave blank to keep current">';
      }
      html += '</label>';
    });
    if (mode === "simple") {
      html += '<label class="mcc-field">Quick profile template'
        + '<select id="mcc-quick-template"><option value="">— optional —</option>';
      (state.mcc.templates || []).forEach(function (t, i) {
        html += '<option value="' + i + '">' + esc(t.name) + '</option>';
      });
      html += '</select><div class="mini">Templates never auto-apply — they only fill pending fields.</div></label>';
    }
    body.innerHTML = html;
    openModal("mcc-modal");
  }

  function collectMCCPending() {
    var pending = {};
    var body = document.getElementById("mcc-modal-body");
    if (!body) return pending;
    body.querySelectorAll("[data-mcc-param]").forEach(function (el) {
      var v = (el.value || "").trim();
      if (!v) return;
      if (v === "true") pending[el.getAttribute("data-mcc-param")] = true;
      else if (v === "false") pending[el.getAttribute("data-mcc-param")] = false;
      else if (/^-?\d+$/.test(v)) pending[el.getAttribute("data-mcc-param")] = parseInt(v, 10);
      else if (/^-?\d+\.\d+$/.test(v)) pending[el.getAttribute("data-mcc-param")] = parseFloat(v);
      else pending[el.getAttribute("data-mcc-param")] = v;
    });
    var qt = document.getElementById("mcc-quick-template");
    if (qt && qt.value !== "") {
      var t = (state.mcc.templates || [])[parseInt(qt.value, 10)];
      if (t && t.settings) {
        Object.keys(t.settings).forEach(function (k) {
          if (pending[k] === undefined) pending[k] = t.settings[k];
        });
      }
    }
    return pending;
  }

  function mccPreviewAndMaybeApply(apply) {
    var card = state.mcc.selectedCard;
    if (!card) return;
    var pending = collectMCCPending();
    postJSON("/api/mcc/settings/preview", {
      engine: card.engine,
      configured_args: card.configured_args,
      live_context: card.context_tokens,
      live_model: card.model,
      pending: pending,
    }).then(function (res) {
      var view = (res.body && res.body.data) || {};
      var lines = (view.changes || []).map(function (ch) {
        return ch.label + ":\n  Current: " + ch.current + "\n  Requested: " + ch.requested
          + "\n  (" + ch.apply + ")";
      });
      if (!lines.length) {
        window.alert("No pending changes.");
        return;
      }
      var msg = lines.join("\n\n")
        + (view.restart_required ? "\n\nRestart required to apply startup settings." : "")
        + "\n\n" + (apply ? "Apply these changes now?" : "Preview only — nothing applied.");
      if (!apply) { window.alert(msg); return; }
      if (!window.confirm(msg)) return;
      if (view.restart_required) {
        if (!card.actions.restart) {
          window.alert("This change needs a restart, but restart is not available for this server (configure/adopt it and enable remote control if needed). Previous configuration was not modified.");
          return;
        }
        if (!window.confirm("Confirm restart of " + (card.server_name || card.server_id)
          + " on " + card.machine + "?\n\nPrevious launch args are preserved in the server config until you save new args in Configure / Expert Mode.\nThis UI does not silently rewrite launch arguments.")) return;
        // Safety: do not rewrite args automatically. Expert must edit Configure.
        window.alert("Restart-required settings must be written into Launch arguments via Configure / Expert Mode, then Restart. Pending request-level prefs were not forced into the server.\n\nNothing was changed automatically.");
        return;
      }
      window.alert("Pending values are request-level preferences. They are not written into Modelfiles or launch args automatically. Use them from AI Lab / clients when sending prompts.\n\nNothing destructive was applied.");
    });
  }

  function mccAct(act, cid) {
    var card = findMCCCard(cid);
    if (!card) return;
    if (act === "details") {
      window.alert(JSON.stringify({
        machine: card.machine, engine: card.engine, model: card.model,
        status: card.status, model_status: card.model_status,
        endpoint: card.endpoint, context_tokens: card.context_tokens,
        actions: card.actions, disabled_reasons: card.disabled_reasons,
        resources: card.resources,
      }, null, 2));
      return;
    }
    if (act === "settings") { openMCCSettings(card); return; }
    if (!card.server_id) {
      window.alert("This server is not configured for management. Adopt it on its own machine first.");
      return;
    }
    if (act === "load" || act === "switch") {
      if (!card.model) { window.alert("No model selected."); return; }
      // Memory warning (estimate)
      postJSON("/api/mcc/memory-warning", {
        model_size_bytes: card.size_bytes,
        available_ram_bytes: (card.resources || {}).ram_available_bytes,
        available_vram_bytes: (card.resources || {}).vram_total_bytes,
        context_tokens: card.context_tokens,
      }).then(function (res) {
        var w = (res.body && res.body.data) || {};
        var proceed = true;
        if (w.warn) {
          proceed = window.confirm(
            "Memory Warning\n\n" + (w.message || "Potential memory pressure")
            + "\nCurrent available RAM: " + (w.available_ram_bytes != null ? fmtBytes(w.available_ram_bytes) : "Unknown")
            + "\nEstimated model requirement: " + (w.estimate_bytes != null ? fmtBytes(w.estimate_bytes) : "Unknown")
            + " (" + (w.estimate_label || "estimate") + ")"
            + "\nEstimated remaining memory: " + (w.estimated_remaining_bytes != null ? fmtBytes(w.estimated_remaining_bytes) : "Unknown")
            + "\n\n[Cancel] / OK to Continue Anyway");
        }
        if (!proceed) return;
        if (act === "switch") {
          if (!window.confirm("Load '" + card.model + "' by restarting "
            + (card.server_name || "server") + " on " + card.machine
            + "?\n\nThis stops the current resident model.")) return;
          postJSON(mccControlUrl(card, "restart"), { force: false, model: card.model }).then(function (r) {
            if (r.status === 409 && window.confirm((r.body && r.body.message) + "\n\nForce restart?")) {
              postJSON(mccControlUrl(card, "restart"), { force: true, model: card.model }).then(function (r2) {
                showResult(r2); setTimeout(refreshMCC, 1500);
              });
            } else { showResult(r); setTimeout(refreshMCC, 1500); }
          });
          return;
        }
        if (!window.confirm("Load model '" + card.model + "' on " + card.machine + "?")) return;
        postJSON(mccControlUrl(card, "load"), { model: card.model }).then(function (r) {
          showResult(r); setTimeout(refreshMCC, 1500);
        });
      });
      return;
    }
    if (act === "unload") {
      if (!card.model) return;
      if (!window.confirm("Unload model '" + card.model + "' on " + card.machine + "?")) return;
      postJSON(mccControlUrl(card, "unload"), { model: card.model }).then(function (r) {
        showResult(r); setTimeout(refreshMCC, 1500);
      });
      return;
    }
    if (act === "stop") {
      if (!window.confirm("Stop model server on " + card.machine + "? This is Stop server, not Unload model.")) return;
      postJSON(mccControlUrl(card, "stop"), { force: false }).then(function (r) {
        if (r.status === 409 && window.confirm((r.body && r.body.message) + "\n\nForce stop?")) {
          postJSON(mccControlUrl(card, "stop"), { force: true }).then(function (r2) {
            showResult(r2); setTimeout(refreshMCC, 1500);
          });
        } else { showResult(r); setTimeout(refreshMCC, 1500); }
      });
      return;
    }
    if (act === "restart") {
      if (!window.confirm("Restart model server on " + card.machine + "?")) return;
      postJSON(mccControlUrl(card, "restart"), { force: false }).then(function (r) {
        if (r.status === 409 && window.confirm((r.body && r.body.message) + "\n\nForce restart?")) {
          postJSON(mccControlUrl(card, "restart"), { force: true }).then(function (r2) {
            showResult(r2); setTimeout(refreshMCC, 1500);
          });
        } else { showResult(r); setTimeout(refreshMCC, 1500); }
      });
    }
  }

  function bindMCC() {
    document.querySelectorAll(".mcc-mode").forEach(function (btn) {
      btn.addEventListener("click", function () {
        state.mcc.mode = btn.getAttribute("data-mcc-mode") || "simple";
        document.querySelectorAll(".mcc-mode").forEach(function (b) {
          b.classList.toggle("active", b === btn);
        });
        renderMCCCards();
      });
    });
    var refresh = document.getElementById("mcc-refresh");
    if (refresh) refresh.addEventListener("click", refreshMCC);
    var search = document.getElementById("mcc-search");
    if (search) search.addEventListener("input", function () {
      state.mcc.query = search.value || "";
      renderMCCCards();
    });
    var filter = document.getElementById("mcc-filter");
    if (filter) filter.addEventListener("change", function () {
      state.mcc.filter = filter.value || "all";
      renderMCCCards();
    });
    var filters = document.getElementById("mcc-machine-filters");
    if (filters) filters.addEventListener("click", function (ev) {
      var btn = ev.target.closest("[data-mcc-machine]");
      if (!btn) return;
      state.mcc.machine = btn.getAttribute("data-mcc-machine") || "all";
      renderMCCFilters();
      renderMCCCards();
    });
    var cards = document.getElementById("mcc-cards");
    if (cards) cards.addEventListener("click", function (ev) {
      var btn = ev.target.closest("[data-mcc-act]");
      if (!btn) return;
      mccAct(btn.getAttribute("data-mcc-act"), btn.getAttribute("data-cid"));
    });
    ["mcc-modal-close", "mcc-modal-cancel"].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.addEventListener("click", function () { closeModal("mcc-modal"); });
    });
    var apply = document.getElementById("mcc-modal-apply");
    if (apply) apply.addEventListener("click", function () { mccPreviewAndMaybeApply(true); });
    var saveProf = document.getElementById("mcc-modal-save-profile");
    if (saveProf) saveProf.addEventListener("click", function () {
      var card = state.mcc.selectedCard || {};
      var pending = collectMCCPending();
      var name = window.prompt("Profile name", (card.model || "model") + " custom");
      if (!name) return;
      postJSON("/api/mcc/profiles", {
        name: name,
        engine: card.engine,
        model: card.model,
        machine: card.machine,
        server_id: card.server_id,
        settings: pending,
        startup_settings: {},
        request_settings: pending,
      }).then(function (r) {
        showResult(r);
        refreshMCC();
      });
    });
    var preview = document.getElementById("mcc-profile-preview");
    if (preview) preview.addEventListener("click", function () {
      var sel = document.getElementById("mcc-profile-select");
      var pid = sel && sel.value;
      if (!pid) { window.alert("Choose a saved profile first."); return; }
      var card = state.mcc.selectedCard || filteredMCCCards()[0] || {};
      postJSON("/api/mcc/profiles/" + encodeURIComponent(pid) + "/preview", {
        engine: card.engine,
        configured_args: card.configured_args,
        live_context: card.context_tokens,
        live_model: card.model,
      }).then(function (r) {
        var view = (r.body && r.body.data) || {};
        var panel = document.getElementById("mcc-profile-panel");
        if (!panel) return;
        panel.hidden = false;
        var lines = (view.changes || []).map(function (ch) {
          return esc(ch.label) + ": " + esc(String(ch.current)) + " → " + esc(String(ch.requested))
            + " (" + esc(ch.apply) + ")";
        });
        panel.innerHTML = "<b>Profile preview (not applied):</b> "
          + esc((view.profile && view.profile.name) || "")
          + "<br>" + (lines.length ? lines.join("<br>") : "No differences from current known settings.")
          + (view.restart_required ? "<br><b>Restart required</b> to apply startup settings." : "")
          + "<br><span class='mini'>" + esc(view.note || "") + "</span>";
      });
    });
    var neu = document.getElementById("mcc-profile-new");
    if (neu) neu.addEventListener("click", function () {
      var name = window.prompt("New profile name");
      if (!name) return;
      postJSON("/api/mcc/profiles", { name: name, settings: {} }).then(function (r) {
        showResult(r); refreshMCC();
      });
    });
  }

  // debug handle (harmless in production, invaluable for diagnosis)
  window.__MONITOR_STATE = state;
  // Bind MCC after DOM hooks exist
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bindMCC);
  } else {
    bindMCC();
  }
})();
