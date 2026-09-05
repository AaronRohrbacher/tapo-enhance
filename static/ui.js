// Pure rendering: subscribes to state, mutates the DOM. No fetches here.

(function (global) {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  // ── tabs ─────────────────────────────────────────────────────────────
  function renderTabs(onChange) {
    const buttons = $$(".tab");
    buttons.forEach((b) => {
      b.addEventListener("click", () => {
        buttons.forEach((x) => x.classList.toggle("active", x === b));
        $$(".tab-content").forEach((c) =>
          c.classList.toggle("active", c.id === `tab-${b.dataset.tab}`)
        );
        if (onChange) onChange(b.dataset.tab);
      });
    });
  }

  // ── camera status pill ────────────────────────────────────────────────
  function renderCamera(s) {
    $("#status-dot").className = "dot " + s.camera.status;
    $("#status-text").textContent =
      s.camera.status === "connected"
        ? `${s.camera.alias || "camera"} • ${s.camera.host || ""}`
        : s.camera.status;
    const battery = $("#battery-status");
    if (battery) {
      battery.textContent = Number.isFinite(s.camera.battery)
        ? `${s.camera.battery}%${s.camera.charging ? " ⚡" : ""}`
        : "battery unavailable";
      battery.title = s.camera.charging ? "camera battery (charging)" : "camera battery";
    }
  }

  // ── gateway busy indicator ────────────────────────────────────────────
  function renderGateway(s) {
    const el = $("#gateway-state");
    if (!el) return;
    const parts = [];
    if (s.gateway.busy) parts.push(`busy: ${s.gateway.busy}`);
    if (s.gateway.queued) parts.push(`queued: ${s.gateway.queued}`);
    if (s.gateway.liveRunning) parts.push("live");
    el.textContent = parts.join(" • ") || "camera ready";
  }

  // ── live overlay ──────────────────────────────────────────────────────
  function renderLive(s) {
    const overlay = $("#live-overlay");
    const msg = $("#live-msg");
    const stop = $("#btn-live-stop");
    const refresh = $("#btn-live-refresh");
    const start = $("#btn-live-start");

    if (s.live.status === "running") {
      overlay.classList.add("hidden");
      stop.disabled = false;
      refresh.disabled = false;
      start.disabled = true;
    } else if (s.live.status === "starting" || s.live.status === "stopping") {
      overlay.classList.remove("hidden");
      msg.textContent = s.live.status === "stopping" ? "stopping…" : "starting…";
      start.disabled = true;
    } else if (s.live.status === "paused") {
      overlay.classList.remove("hidden");
      msg.textContent = "paused — yielding camera";
      start.disabled = true;
    } else if (s.live.status === "error") {
      overlay.classList.remove("hidden");
      msg.textContent = "error: " + (s.live.error || "unknown");
      start.disabled = false;
      stop.disabled = true;
      refresh.disabled = true;
    } else {
      overlay.classList.remove("hidden");
      msg.textContent = "no signal";
      start.disabled = false;
      stop.disabled = true;
      refresh.disabled = true;
    }
  }

  // ── archive grid ──────────────────────────────────────────────────────
  function fmtTime(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  }
  function fmtDur(s) {
    const m = Math.floor(s / 60); const r = s % 60;
    return m ? `${m}m ${String(r).padStart(2, "0")}s` : `${r}s`;
  }

  const thumbObserver = "IntersectionObserver" in window
    ? new IntersectionObserver((entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          const img = entry.target;
          if (img.dataset.src && !img.src) img.src = img.dataset.src;
          thumbObserver.unobserve(img);
        });
      }, { rootMargin: "240px 0px" })
    : null;

  function activateVisibleThumbs(grid) {
    grid.querySelectorAll("img.thumb[data-src]").forEach((img) => {
      if (img.src) return;
      if (thumbObserver) thumbObserver.observe(img);
      else img.src = img.dataset.src;
    });
  }

  function renderArchive(s) {
    const grid = $("#arch-grid");
    if (!grid) return;
    const clips = s.archive.clips;
    const signature = JSON.stringify({
      clips: clips.map((c) => `${c.date}/${c.startTime}/${c.endTime}`),
      selected: Array.from(s.archive.selected).sort(),
      thumbs: s.archive.thumbs,
      downloads: s.downloads,
    });
    if (grid.dataset.signature === signature) return;
    grid.dataset.signature = signature;
    if (!clips.length) {
      grid.innerHTML = '<p class="placeholder">no clips for this date.</p>';
      $("#btn-arch-bulk").disabled = true;
      $("#btn-arch-select-all").disabled = true;
      return;
    }
    $("#btn-arch-select-all").disabled = false;
    $("#btn-arch-bulk").disabled = s.archive.selected.size === 0;
    grid.innerHTML = clips.map((c) => {
      const k = `${c.date}/${c.startTime}/${c.endTime}`;
      const sel = s.archive.selected.has(k);
      const ts = s.archive.thumbs[k] || "idle";
      const thumbLabel = ts === "idle" ? "not loaded" : ts;
      const dlState = s.downloads[k];
      let cls = "clip-card";
      if (sel) cls += " selected";
      if (dlState === "running") cls += " downloading";
      else if (dlState === "done") cls += " downloaded";
      else if (ts === "queued" || ts === "running") cls += " queued";
      return `
        <div class="${cls}" data-key="${k}">
          <div class="thumb-box">
            <img class="thumb${ts === "failed" ? " failed" : ""}"
                 loading="lazy" decoding="async"
                 data-src="/api/thumb/${c.date}/${c.startTime}/${c.endTime}?s=${ts}" />
            <span class="badge ${ts}">${thumbLabel}</span>
            <span class="check" data-action="toggle">✓</span>
          </div>
          <div class="meta">
            <span class="meta-time">${fmtTime(c.startTime)}</span>
            <span class="meta-dur">${fmtDur(c.endTime - c.startTime)}${
              dlState === "running" ? " • downloading…" :
              dlState === "done" ? " • downloaded" :
              dlState === "failed" ? " • download failed" : ""
            }</span>
          </div>
          <div class="actions">
            <button class="btn small" data-action="watch">watch</button>
            <button class="btn small" data-action="download" ${dlState === "running" ? "disabled" : ""}>download</button>
          </div>
        </div>`;
    }).join("");
    activateVisibleThumbs(grid);
  }

  function renderArchiveStatus(s) {
    const c = s.archive.thumbCounts;
    const el = $("#arch-thumbs");
    if (!el || !s.archive.clips.length) {
      if (el) el.classList.add("hidden");
      return;
    }
    el.classList.remove("hidden");
    el.textContent =
      `thumbnails: ${c.ready}/${c.total || s.archive.clips.length} ready` +
      (c.idle ? ` • ${c.idle} waiting to load` : "") +
      (c.queued ? ` • ${c.queued} queued` : "") +
      (c.running ? ` • ${c.running} running` : "") +
      (c.failed ? ` • ${c.failed} failed` : "");
  }

  function renderBulk(s) {
    const el = $("#arch-status");
    if (!el) return;
    if (!s.bulk) {
      el.classList.add("hidden");
      return;
    }
    el.classList.remove("hidden");
    const j = s.bulk;
    const pct = j.total ? Math.round(((j.done + j.failed) / j.total) * 100) : 0;
    el.innerHTML = `
      <div>bulk: ${j.done}/${j.total} done${j.failed ? ` • ${j.failed} failed` : ""}${j.current ? ` • ${j.current}` : ""}</div>
      <div class="progress"><div class="fill" style="width: ${pct}%"></div></div>
    `;
  }

  // ── local list ────────────────────────────────────────────────────────
  function fmtBytes(mb) {
    return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`;
  }
  function renderLocal(s) {
    const el = $("#local-list");
    if (!el) return;
    if (!s.local.length) {
      el.innerHTML = '<p class="placeholder">no local clips yet — download something from archive.</p>';
      return;
    }
    el.innerHTML = s.local.map((f) =>
      `<div class="local-row" data-path="${f.path}">
        <div>
          <div>${f.date} • ${f.file}</div>
          <div class="meta">${fmtBytes(f.size_mb)}</div>
        </div>
        <div class="muted">play ▶</div>
      </div>`
    ).join("");
  }

  function renderCache(s) {
    const el = $("#cache-rows");
    if (!el) return;
    const u = s.cache;
    el.innerHTML = ["recordings", "thumbs", "previews", "playback", "stream"].map((k) =>
      `<div class="cache-row"><span>${k}</span><span class="muted">${u[k + "_files"] || 0} files</span><span>${fmtMb(u[k] || 0)}</span></div>`
    ).join("") + `<div class="cache-row total"><span>total</span><span></span><span>${fmtMb(u.total || 0)}</span></div>`;
  }
  function fmtMb(b) {
    if (!b) return "0 B";
    const u = ["B", "KB", "MB", "GB"];
    let i = 0; let n = b;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(n >= 10 ? 0 : 1)} ${u[i]}`;
  }

  // ── settings info ─────────────────────────────────────────────────────
  function renderSettings(info) {
    const el = $("#cam-info");
    if (!el) return;
    if (!info) { el.innerHTML = '<p class="muted">load camera info first.</p>'; return; }
    el.innerHTML = `
      <div class="info-row"><span class="label">alias</span><span>${info.alias}</span></div>
      <div class="info-row"><span class="label">model</span><span>${info.model}</span></div>
      <div class="info-row"><span class="label">firmware</span><span>${info.sw}</span></div>
      <div class="info-row"><span class="label">hardware</span><span>${info.hw}</span></div>
      <div class="info-row"><span class="label">mac</span><span>${info.mac}</span></div>
      <div class="info-row"><span class="label">host</span><span>${info.host}</span></div>
    `;
  }

  function escapeHtml(value) {
    return String(value ?? "unknown")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderFeatures(s) {
    const status = $("#feature-status");
    const controls = $("#feature-controls");
    if (!status || !controls || !s.features) return;
    const f = s.features;
    if (f.loading) {
      status.innerHTML = '<p class="muted">reading supported features from camera…</p>';
      controls.innerHTML = "";
      return;
    }
    if (f.error) {
      status.innerHTML = `<p class="error-text">${escapeHtml(f.error)}</p>`;
      controls.innerHTML = "";
      return;
    }
    const rows = [
      ["SD card", `${f.storage.status || "unknown"} • ${f.storage.free || "?"} free of ${f.storage.total || "?"}`],
      ["recording", `${f.storage.recording_enabled ? "enabled" : "disabled"} • loop ${f.storage.loop_recording ? "on" : "off"}`],
      ["video", `${f.video.resolution || "?"} • ${f.video.codec || "?"} • ${f.video.bitrate_kbps || "?"} kbps`],
      ["battery profile", f.power.mode || "unknown"],
      ["firmware", `${f.firmware.state || "unknown"}${f.firmware.last_upgrade_succeeded === false ? " • last upgrade failed" : ""}`],
    ];
    status.innerHTML = rows.map(([label, value]) =>
      `<div class="info-row"><span class="label">${escapeHtml(label)}</span><span>${escapeHtml(value)}</span></div>`
    ).join("") + (Object.keys(f.errors || {}).length
      ? `<div class="muted">${Object.keys(f.errors).length} unsupported read${Object.keys(f.errors).length === 1 ? "" : "s"} omitted</div>` : "");
    const labels = {
      led: "status LED", privacy: "privacy mode", motion: "motion detection",
      person: "person detection", package: "package detection",
      record_audio: "record audio", ring: "doorbell ring",
    };
    controls.innerHTML = Object.entries(labels).map(([key, label]) => {
      const value = f.controls[key];
      if (value === null || value === undefined) return "";
      return `<div class="feature-row"><span>${escapeHtml(label)}</span><span>${value ? "on" : "off"}</span>` +
        (key === "privacy" ? "" : `<button class="btn small" data-feature="${key}" data-value="${!value}">turn ${value ? "off" : "on"}</button>`) +
        `</div>`;
    }).join("") + `
      <div class="feature-row"><label for="day-night-mode">day / night</label><span>${escapeHtml(f.controls.day_night)}</span>
        <select id="day-night-mode" class="btn small">
          ${["auto", "day", "night"].map((v) => `<option value="${v}"${f.controls.day_night === v ? " selected" : ""}>${v}</option>`).join("")}
        </select>
      </div>`;
  }

  // ── toast ─────────────────────────────────────────────────────────────
  let toastTimer = null;
  function toast(msg, type = "info") {
    const el = $("#toast");
    clearTimeout(toastTimer);
    el.className = "toast " + type;
    el.classList.remove("hidden");
    const safe = String(msg).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    el.innerHTML =
      `<span class="toast-msg">${safe}</span>` +
      `<button class="toast-close">×</button>`;
    el.querySelector(".toast-close").addEventListener("click", () => el.classList.add("hidden"));
    if (type !== "error") {
      toastTimer = setTimeout(() => el.classList.add("hidden"), 4000);
    }
  }

  global.UI = {
    $, $$,
    renderTabs, renderCamera, renderGateway, renderLive,
    renderArchive, renderArchiveStatus, renderBulk,
    renderLocal, renderCache, renderSettings, renderFeatures,
    toast,
  };
})(window);
