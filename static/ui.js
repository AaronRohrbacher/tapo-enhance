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
    const signature = JSON.stringify(clips.map((c) => `${c.date}/${c.startTime}/${c.endTime}`));
    if (grid.dataset.signature === signature && clips.length) {
      // Keep image nodes intact while selection and download state change.
      // Recreating the grid refetched every thumbnail on unrelated updates.
      $("#btn-arch-bulk").disabled = s.archive.selected.size === 0;
      grid.querySelectorAll(".clip-card").forEach((card, index) => {
        const clip = clips[index];
        const key = card.dataset.key;
        const status = s.downloads[key] || (s.localKeys.has(key) ? "done" : "idle");
        const thumb = s.archive.thumbs[key] || "idle";
        card.classList.toggle("selected", s.archive.selected.has(key));
        card.classList.toggle("downloading", status === "running");
        card.classList.toggle("downloaded", status === "done");
        card.classList.toggle("queued", status !== "running" && status !== "done" && ["queued", "running"].includes(thumb));
        const badge = card.querySelector(".badge");
        badge.className = `badge ${thumb}`;
        badge.textContent = thumb === "idle" ? "waiting" : thumb;
        const existing = card.querySelector(".local-badge");
        if (status === "done" && !existing) {
          const local = document.createElement("span");
          local.className = "local-badge";
          local.textContent = "DOWNLOADED ✓";
          card.querySelector(".thumb-box").append(local);
        } else if (status !== "done" && existing) existing.remove();
        const button = card.querySelector("[data-action='download']");
        button.disabled = status === "running" || status === "done";
        button.textContent = status === "done" ? "downloaded" : "download";
        const operation = s.operations[`download:${key}`];
        card.querySelector(".meta-dur").textContent = status === "running" && operation
          ? `${operation.message || operation.phase}${Number.isFinite(operation.progress) ? ` ${Math.round(operation.progress * 100)}%` : ""}`
          : `${fmtDur(clip.endTime - clip.startTime)}${status === "running" ? " • downloading…" : status === "done" ? " • downloaded" : status === "failed" ? " • download failed" : ""}`;
      });
      return;
    }
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
      const thumbLabel = ts === "idle" ? "waiting" : ts;
      const dlState = s.downloads[k] || (s.localKeys.has(k) ? "done" : undefined);
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
            ${dlState === "done" ? '<span class="local-badge">DOWNLOADED ✓</span>' : ""}
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
            <button class="btn small" data-action="download" ${dlState === "running" || dlState === "done" ? "disabled" : ""}>${dlState === "done" ? "downloaded" : "download"}</button>
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
      (c.idle ? ` • ${c.idle} waiting` : "") +
      (c.queued ? ` • ${c.queued} queued` : "") +
      (c.running ? ` • ${c.running} running` : "") +
      (c.failed ? ` • ${c.failed} failed` : "");
  }

  function renderBulk(s) {
    const el = $("#arch-status");
    if (!el) return;
    const singles = Object.values(s.operations).filter((e) => e.operation === "download").map((e) => ({
      id: e.key, total: 1, done: e.phase === "complete" ? 1 : 0,
      failed: e.phase === "error" ? 1 : 0,
      status: e.phase === "complete" ? "done" : e.phase === "error" ? "error" : e.phase === "cancelled" ? "cancelled" : "running",
      message: `${e.message || e.phase} • ${e.key}`, progress: e.progress || 0,
      errors: e.error ? [e.error] : [], finished: e.updated,
    }));
    const candidates = [...Object.values(s.jobs), ...singles];
    const active = candidates.filter((j) => ["queued", "running", "cancelling"].includes(j.status));
    $("#manual-sync-card").classList.toggle("hidden", !!s.dvrEnabled && !active.length);
    const jobs = active.length ? active : candidates.sort((a, b) =>
      (a.finished || a.started || 0) - (b.finished || b.started || 0)).slice(-1);
    if (!jobs.length) {
      el.textContent = "No sync running. Select recordings below to download.";
      return;
    }
    el.replaceChildren(...jobs.map((j) => {
      const row = document.createElement("div");
      const label = document.createElement("div");
      const pct = Math.round(Math.max(0, Math.min(1, j.progress || 0)) * 100);
      const active = ["queued", "running", "cancelling"].includes(j.status);
      label.textContent = `${j.message || j.status} • ${j.done}/${j.total} ready` +
        (j.failed ? ` • ${j.failed} failed` : "") + ` • ${pct}%`;
      row.append(label);
      const detail = document.createElement("div");
      detail.className = "muted";
      detail.textContent = [
        j.download_current ? `Camera: ${j.download_current}` : "",
        j.conversion_current ? `Converting ${j.conversion_current}` : "",
        active && s.events.status !== "connected" ? "Progress connection lost; reconnecting…" : "",
      ].filter(Boolean).join(" • ");
      row.append(detail);
      const progress = document.createElement("div");
      progress.className = "progress";
      progress.setAttribute("role", "progressbar");
      progress.setAttribute("aria-label", "Manual sync progress");
      progress.setAttribute("aria-valuemin", "0");
      progress.setAttribute("aria-valuemax", "100");
      progress.setAttribute("aria-valuenow", String(pct));
      const fill = document.createElement("div");
      fill.className = "fill";
      fill.style.width = `${pct}%`;
      progress.append(fill);
      row.append(progress);
      if (j.errors && j.errors.length) {
        const errors = document.createElement("div");
        errors.textContent = j.errors.join(" • ");
        row.append(errors);
      }
      return row;
    }));
  }

  function renderCache(s) {
    const el = $("#cache-rows");
    if (!el) return;
    const u = s.cache;
    const buckets = {
      recordings: ["downloaded recordings", "Local DVR MP4s; playback uses these files directly."],
      thumbs: ["thumbnails", "Cached still images for recording cards."],
      previews: ["temporary recording MP4s", "Browser-playable MP4 copies fetched from the camera without adding them to the downloaded DVR archive."],
      playback: ["undownloaded-recording HLS files", "HLS playlists and two-second video segments generated while watching a camera recording that has not been downloaded."],
      stream: ["live-view buffer", "Temporary HLS segments used by the Live tab."],
    };
    el.innerHTML = Object.entries(buckets).map(([key, label]) =>
      `<div class="cache-row"><span class="cache-label"><span>${label[0]}</span><span class="muted">${label[1]}</span></span><span class="muted">${u[key + "_files"] || 0} files</span><span>${fmtMb(u[key] || 0)}</span></div>`
    ).join("") + `<div class="cache-row total"><span>total cache</span><span></span><span>${fmtMb(u.total || 0)}</span></div>`;
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
      <div class="info-row"><span class="label">alias</span><span>${escapeHtml(info.alias)}</span></div>
      <div class="info-row"><span class="label">model</span><span>${escapeHtml(info.model)}</span></div>
      <div class="info-row"><span class="label">firmware</span><span>${escapeHtml(info.sw)}</span></div>
      <div class="info-row"><span class="label">hardware</span><span>${escapeHtml(info.hw)}</span></div>
      <div class="info-row"><span class="label">mac</span><span>${escapeHtml(info.mac)}</span></div>
      <div class="info-row"><span class="label">host</span><span>${escapeHtml(info.host)}</span></div>
    `;
  }

  function escapeHtml(value) {
    return String(value ?? "unknown")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  let renderedFeatures = null;
  function renderFeatures(s) {
    const status = $("#feature-status");
    const controls = $("#feature-controls");
    if (!status || !controls || !s.features) return;
    const f = s.features;
    if (f === renderedFeatures) return;
    renderedFeatures = f;
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
    renderCache, renderSettings, renderFeatures,
    toast,
  };
})(window);
