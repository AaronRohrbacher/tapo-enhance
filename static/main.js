// Wires DOM events ↔ state ↔ API. Single ownership of each interaction.

(function () {
  "use strict";
  const { $, $$, toast } = UI;
  const { state, set, subscribe, clipKey } = S;

  // ── SFX mute toggle ──────────────────────────────────────────────────
  const sfxBtn = $("#btn-sfx");
  function paintSfxBtn() {
    if (!sfxBtn) return;
    const on = SFX.enabled();
    sfxBtn.textContent = on ? "◍ sfx" : "◌ sfx";
    sfxBtn.classList.toggle("primary", on);
  }
  paintSfxBtn();
  if (sfxBtn) sfxBtn.addEventListener("click", () => {
    SFX.set(!SFX.enabled());
    paintSfxBtn();
    if (SFX.enabled()) SFX.blip();
  });

  // ── render subscription ──────────────────────────────────────────────
  subscribe((s) => {
    UI.renderCamera(s);
    UI.renderGateway(s);
    UI.renderLive(s);
    UI.renderArchive(s);
    UI.renderArchiveStatus(s);
    UI.renderBulk(s);
    UI.renderCache(s);
    UI.renderFeatures(s);
  });
  UI.renderTabs((tab) => {
    if (tab !== "live" && (state.live.status !== "idle" || state.gateway.liveAttached)) {
      stopLive({ sound: false });
    }
    if (tab === "settings") {
      if (!state.features) loadFeatures();
      loadDvr();
    } else if (tab === "archive") {
      loadLocalRecordings();
      loadDvr();
    }
  });

  async function loadThemes() {
    const select = $("#theme-select");
    const r = await API.get("/api/themes");
    if (!r.ok) { toast("themes unavailable: " + r.error, "error"); return; }
    const themes = r.data.themes.filter((t) => !t.error && Object.keys(t.colors).length);
    const saved = localStorage.getItem("tapo.theme") || "phosphor";
    select.replaceChildren(...themes.map((theme) => {
      const option = document.createElement("option");
      option.value = theme.id;
      option.textContent = theme.name;
      option.selected = theme.id === saved;
      return option;
    }));
    const apply = (id) => {
      const theme = themes.find((t) => t.id === id) || themes.find((t) => t.id === "phosphor");
      if (!theme) return;
      Object.entries(theme.colors).forEach(([key, value]) => document.documentElement.style.setProperty(key, value));
      localStorage.setItem("tapo.theme", theme.id);
    };
    apply(select.value || saved);
    select.addEventListener("change", () => apply(select.value));
  }

  function showSetup(show) {
    $("#setup-panel").classList.toggle("hidden", !show);
    $$(".tab-content").forEach((section) => section.classList.toggle("hidden", show));
  }

  let initialDiscoveryStarted = false;
  async function loadConfig(show = false) {
    const r = await API.get("/api/config");
    if (!r.ok) { toast("configuration unavailable: " + r.error, "error"); return false; }
    const form = $("#camera-config-form");
    form.elements.host.value = r.data.host || "";
    form.elements.user.value = r.data.user || "admin";
    form.elements.subnet.value = r.data.configured ? (r.data.subnet || "") : "";
    form.elements.password.value = "";
    form.dataset.initialSetup = String(!r.data.configured);
    form.elements.mac.value = "";
    $("#setup-camera-step").classList.remove("hidden");
    $("#setup-dvr-step").classList.add("hidden");
    form.querySelector('[type="submit"]').textContent = "connect camera";
    $("#btn-config-cancel").classList.toggle("hidden", !r.data.configured);
    showSetup(show || !r.data.configured);
    if (!r.data.configured && !initialDiscoveryStarted) {
      initialDiscoveryStarted = true;
      setTimeout(() => $("#btn-discover").click(), 0);
    }
    return r.data.configured;
  }

  function hydrateDvrForm(data) {
    const days = data.retention_days || 1;
    const form = $("#dvr-settings-form");
    form.elements.enabled.checked = !!data.enabled;
    form.elements.retention_days.value = days;
    form.elements.retention.value = data.keep_forever ? "forever" : "delete";
    form.elements.interval_minutes.value = String(data.interval_minutes || 1440);
    form.elements.daily_time.value = data.daily_time || "00:10";
    form.dataset.originalDays = String(days);
    form.dataset.originalKeep = String(!!data.keep_forever);
    toggleDvrFields(form);
    toggleDailyTime(form);
    renderDvrWindow(days, !!data.keep_forever);
  }

  function renderDvrWindow(days, keepForever) {
    const count = Math.max(1, Number(days) || 1);
    $("#dvr-window").textContent = `Sync window: ${count} day${count === 1 ? "" : "s"}${keepForever ? " • local copies kept permanently" : ""}`;
  }

  function applyDvr(data) {
    renderDvrWindow(data.retention_days, !!data.keep_forever);
    const local = data.available_days
      ? `Downloaded recordings: ${data.local_files} clip${data.local_files === 1 ? "" : "s"} across ${data.available_days} day${data.available_days === 1 ? "" : "s"}.`
      : "Downloaded recordings: none yet.";
    $("#dvr-availability").textContent = local;
    $("#dvr-schedule").textContent = data.enabled
      ? `Automatic sync: ${data.schedule}.`
      : "Automatic sync is disabled. Enable DVR Mode in Settings.";
    const syncButton = $("#btn-dvr-sync");
    const progressWrap = $("#dvr-progress-wrap");
    const progress = Math.max(0, Math.min(1, Number(data.download_progress) || 0));
    const percent = Math.round(progress * 100);
    progressWrap.classList.toggle("hidden", !data.syncing || progress >= 1);
    progressWrap.setAttribute("aria-valuenow", String(percent));
    $("#dvr-progress-fill").style.width = `${percent}%`;
    if (data.syncing) {
      const finished = Number(data.download_complete) || 0;
      const failed = Number(data.download_failed) || 0;
      const total = Number(data.sync_total) || 0;
      const eta = formatEta(data.download_eta_seconds);
      $("#dvr-progress-detail").textContent = total
        ? `${finished} of ${total} downloaded${failed ? ` • ${failed} failed` : ""} • ${percent}%${eta ? ` • ETA ${eta}` : ""}`
        : "Checking camera history…";
    }
    const conversionProgress = Math.max(0, Math.min(1, Number(data.conversion_current_progress) || 0));
    const conversionPercent = Math.round(conversionProgress * 100);
    const conversionKey = data.conversion_current_key || "";
    const conversionActive = !!data.syncing && data.sync_total > 0;
    $("#dvr-conversion-card").classList.toggle("hidden", !conversionActive);
    $("#dvr-conversion-progress").setAttribute("aria-valuenow", String(conversionPercent));
    $("#dvr-conversion-fill").style.width = `${conversionPercent}%`;
    $("#dvr-conversion-status").textContent = conversionKey
      ? `CONVERTING ${conversionKey}`
      : "WAITING FOR DOWNLOADED VIDEOS";
    const conversionEta = formatEta(data.conversion_current_eta_seconds);
    $("#dvr-conversion-detail").textContent = conversionKey
      ? `${conversionPercent}%${conversionEta ? ` • ETA ${conversionEta}` : ""}`
      : "Conversion will begin when a download is staged.";
    if (syncButton) {
      const wasSyncing = syncButton.dataset.syncing === "true";
      syncButton.disabled = !!data.syncing || !data.enabled;
      syncButton.dataset.syncing = String(!!data.syncing);
      if (data.syncing && progress < 1) $("#dvr-status").textContent = "DOWNLOADING CAMERA VIDEOS";
      else if (data.syncing) $("#dvr-status").textContent = "CAMERA DOWNLOADS COMPLETE";
      else if (!data.enabled) $("#dvr-status").textContent = "DVR sync is disabled";
      else if (data.last_error) $("#dvr-status").textContent = `LAST SYNC FAILED — ${data.last_error}`;
      else if (wasSyncing) $("#dvr-status").textContent = "SYNC COMPLETE";
      else if (data.last_sync) $("#dvr-status").textContent = `Last sync completed ${new Date(data.last_sync * 1000).toLocaleString()}`;
      else $("#dvr-status").textContent = "Waiting for first sync";
    }
  }

  function formatEta(value) {
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds < 0) return "";
    if (seconds < 60) return `${Math.max(1, Math.round(seconds))}s`;
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    return `${hours}h ${minutes % 60}m`;
  }

  async function loadDvr() {
    const r = await API.get("/api/dvr");
    if (r.ok) {
      hydrateDvrForm(r.data);
      applyDvr(r.data);
    }
    else $("#dvr-availability").textContent = "DVR status unavailable: " + r.error;
    return r;
  }

  function toggleDvrFields(form) {
    form.querySelector(".dvr-dependent").disabled = !form.elements.enabled.checked;
  }

  function toggleDailyTime(form) {
    form.querySelector(".daily-time").classList.toggle(
      "hidden", form.elements.interval_minutes.value !== "1440"
    );
  }

  function formatCameraDate(value) {
    if (!value || value.length !== 8) return "unknown";
    return `${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6, 8)}`;
  }

  [$("#setup-dvr-form"), $("#dvr-settings-form")].forEach((form) => {
    form.elements.enabled.addEventListener("change", () => toggleDvrFields(form));
    form.elements.interval_minutes.addEventListener("change", () => toggleDailyTime(form));
  });

  // clock
  const clock = $("#clock");
  setInterval(() => {
    clock.textContent = new Date().toLocaleTimeString([], { hour12: false });
  }, 1000);

  // initial date input value: today, local
  (function () {
    const d = new Date();
    const v = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    $("#arch-date").value = v;
  })();

  // ── camera status polling ────────────────────────────────────────────
  async function loadStatus() {
    const wasConnected = state.camera.status === "connected";
    set((s) => { s.camera.status = "connecting"; });
    const r = await API.get("/api/camera/status");
    if (r.ok) {
      set((s) => {
        s.camera.status = "connected";
        s.camera.alias = r.data.alias;
        s.camera.host = r.data.host;
        const battery = r.data.battery_percent;
        s.camera.battery = battery !== null && battery !== undefined && Number.isFinite(Number(battery))
          ? Number(battery)
          : null;
        s.camera.charging = r.data.is_charging;
      });
      UI.renderSettings(r.data);
      if (!wasConnected) SFX.access();
      return true;
    }
    set((s) => { s.camera.status = "disconnected"; });
    SFX.alert();
    if (r.retryable) toast("camera offline — try [reconnect]", "error");
    else if (r.code === "CAMERA_AUTH") toast("camera authentication failed — reconfigure the encrypted vault", "error");
    else if (r.code === "CAMERA_NOT_CONFIGURED") showSetup(true);
    return false;
  }

  // ── server-pushed state (replaces gateway-status + thumb HEAD polling) ──
  // One SSE stream carries every gateway/thumb state change. No timers, no
  // per-thumbnail HEADs — idle means zero traffic.
  function openEvents() {
    const es = new EventSource("/api/events");
    es.onopen = () => { state.events = { status: "connected", error: null }; };
    es.onmessage = (e) => {
      let evt;
      try { evt = JSON.parse(e.data); }
      catch {
        state.events = { status: "error", error: "Server sent an invalid progress event" };
        toast(state.events.error, "error");
        return;
      }
      if (evt.type === "gateway") applyGateway(evt);
      else if (evt.type === "thumb") applyThumb(evt);
      else if (evt.type === "operation") applyOperation(evt);
      else if (evt.type === "dvr") applyDvr(evt);
      else if (evt.type === "cache") applyCache(evt.usage);
    };
    es.onerror = () => {
      state.events = { status: "reconnecting", error: "Progress connection lost; reconnecting" };
      UI.renderGateway(state);
    };
  }

  function applyOperation(evt) {
    const id = `${evt.operation}:${evt.key}`;
    state.operations[id] = evt;
    const pct = Number.isFinite(evt.progress) ? ` ${Math.round(evt.progress * 100)}%` : "";
    const message = (evt.message || evt.phase || "working") + pct;

    if (evt.operation === "playback" && state.player.clip && clipKey(state.player.clip) === evt.key) {
      $("#player-status").textContent = message;
      state.player.status = evt.phase === "error" ? "error" : evt.phase === "complete" ? "ready" : "loading";
      if (evt.phase === "error") toast("playback failed: " + (evt.error || message), "error");
    } else if (evt.operation === "download") {
      state.downloads[evt.key] = evt.phase === "complete" ? "done" : evt.phase === "error" ? "failed" : "running";
      const card = $(`.clip-card[data-key="${CSS.escape(evt.key)}"]`);
      const meta = card && card.querySelector(".meta-dur");
      if (meta) meta.textContent = message;
    } else if (evt.operation === "dvr") {
      if (evt.phase === "complete") {
        set((s) => { s.localKeys.add(evt.key); });
      }
      if (evt.phase === "error") toast("DVR sync: " + (evt.error || message), "error");
    } else if (evt.operation === "live") {
      if (evt.phase === "error") {
        state.live.status = "error";
        state.live.error = evt.error || evt.message;
      } else if (evt.phase === "paused" || evt.phase === "reconnecting") {
        state.live.status = "paused";
      } else if (evt.phase === "playing") {
        state.live.status = "running";
        state.live.error = null;
        if (state.live.url !== evt.url) {
          state.live.url = evt.url;
          attachHls(evt.url);
        }
      }
      UI.renderLive(state);
    }
  }

  function applyGateway(evt) {
    // Mutate directly + render only the gateway indicator. Going through
    // set() would re-render the whole archive grid (and refetch every thumb)
    // on each gateway tick — the exact waste we're removing.
    state.gateway = {
      busy: evt.busy,
      queued: evt.queued,
      liveAttached: evt.liveAttached,
      liveRunning: evt.liveRunning,
    };
    UI.renderGateway(state);
  }

  function applyThumb(evt) {
    if (!evt.status) return;
    const k = evt.key;
    if (state.archive.thumbs[k] === evt.status) return;        // no change → ignore
    if (!state.archive.clips.some((c) => clipKey(c) === k)) return;  // not on screen
    state.archive.thumbs[k] = evt.status;
    if (evt.error) state.archive.thumbErrors[k] = evt.error;
    patchThumbCard(k, evt.status);
    refreshThumbCounts();
  }

  // Surgically update one clip's card — no full grid rebuild (which would
  // recreate and refetch every <img>).
  function patchThumbCard(key, status) {
    const card = $(`.clip-card[data-key="${CSS.escape(key)}"]`);
    if (!card) return;
    const [date, st, en] = key.split("/");
    const img = card.querySelector("img.thumb");
    if (img) {
      // The initial visible-image request already queued the work. Refetch
      // exactly once when the real JPEG is ready, not on every queue phase.
      if (status === "ready") img.src = `/api/thumb/${date}/${st}/${en}?s=ready`;
      img.classList.toggle("failed", status === "failed");
    }
    const badge = card.querySelector(".badge");
    if (badge) { badge.className = "badge " + status; badge.textContent = status; }
    if (!card.classList.contains("downloading") && !card.classList.contains("downloaded")) {
      card.classList.toggle("queued", status === "queued" || status === "running");
    }
  }

  function refreshThumbCounts() {
    const clips = state.archive.clips;
    const counts = { total: clips.length, idle: 0, ready: 0, queued: 0, running: 0, failed: 0 };
    clips.forEach((c) => {
      const st = state.archive.thumbs[clipKey(c)] || "idle";
      if (counts[st] != null) counts[st]++;
    });
    state.archive.thumbCounts = counts;
    UI.renderArchiveStatus(state);
  }

  // ── live ─────────────────────────────────────────────────────────────
  let hls = null;
  let liveRequestGeneration = 0;
  const video = $("#live-video");
  $("#btn-mute").addEventListener("click", () => {
    video.muted = !video.muted;
    $("#btn-mute").textContent = video.muted ? "unmute" : "mute";
  });

  function attachHls(url) {
    if (hls) { hls.destroy(); hls = null; }
    if (window.Hls && Hls.isSupported()) {
      hls = new Hls({
        startPosition: -1,
        liveSyncDurationCount: 2,
        liveMaxLatencyDurationCount: 5,
        maxBufferLength: 12,
        backBufferLength: 0,
        enableWorker: true,
        lowLatencyMode: false,
        manifestLoadingMaxRetry: 8,
        levelLoadingMaxRetry: 8,
        fragLoadingMaxRetry: 8,
      });
      hls.loadSource(url);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        if (Number.isFinite(hls.liveSyncPosition)) video.currentTime = hls.liveSyncPosition;
        video.play().catch(() => {});
      });
      hls.on(Hls.Events.LEVEL_UPDATED, () => {
        const edge = hls.liveSyncPosition;
        if (Number.isFinite(edge) && edge - video.currentTime > 10) video.currentTime = edge;
      });
      hls.on(Hls.Events.ERROR, (_e, data) => {
        if (data.fatal) {
          if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
            setTimeout(() => hls && hls.startLoad(), 1500);
          } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
            hls.recoverMediaError();
          } else {
            set((s) => { s.live.status = "error"; s.live.error = data.details; });
          }
        }
      });
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = url;
      video.play().catch(() => {});
    } else {
      toast("HLS not supported in this browser", "error");
    }
  }

  $("#btn-live-start").addEventListener("click", async () => {
    const requestGeneration = ++liveRequestGeneration;
    set((s) => { s.live.status = "starting"; });
    SFX.powerOn();
    const r = await API.post("/api/stream/start", {});
    if (requestGeneration !== liveRequestGeneration) {
      if (r.ok) await API.post("/api/stream/stop", {});
      return;
    }
    if (!r.ok) {
      set((s) => { s.live.status = "error"; s.live.error = r.error; });
      SFX.err();
      toast("live failed: " + r.error, "error");
      if (r.retryable) recover();
      return;
    }
    const url = r.data.url;
    const alreadyAttached = state.live.url === url && hls;
    set((s) => { s.live.status = "running"; s.live.url = url; s.live.error = null; });
    if (!alreadyAttached) setTimeout(() => attachHls(url), 250);
  });

  async function stopLive({ sound = true } = {}) {
    ++liveRequestGeneration;
    if (hls) { hls.destroy(); hls = null; }
    video.pause();
    video.removeAttribute("src");
    video.load();
    if (sound) SFX.powerOff();
    set((s) => { s.live.status = "stopping"; s.live.url = null; });
    const r = await API.post("/api/stream/stop", {});
    if (!r.ok) {
      set((s) => { s.live.status = "error"; s.live.error = r.error; });
      toast("could not stop live: " + r.error, "error");
      return false;
    }
    set((s) => { s.live.status = "idle"; s.live.url = null; s.live.error = null; });
    return true;
  }

  $("#btn-live-stop").addEventListener("click", () => stopLive());

  $("#btn-live-refresh").addEventListener("click", () => {
    if (state.live.url) {
      const url = new URL(state.live.url, window.location.href);
      url.searchParams.set("t", Date.now());
      attachHls(url.pathname + url.search);
    }
  });

  $("#btn-snapshot").addEventListener("click", async () => {
    const img = $("#snapshot-img");
    img.classList.add("hidden");
    SFX.shutter();
    try {
      const res = await fetch("/api/snapshot?t=" + Date.now());
      if (res.ok) {
        const blob = await res.blob();
        img.src = URL.createObjectURL(blob);
        img.classList.remove("hidden");
        toast("snapshot captured", "success");
      } else {
        const j = await res.json();
        SFX.err();
        toast("snapshot: " + (j.error || res.status), "error");
      }
    } catch (e) {
      SFX.err();
      toast("snapshot error: " + e.message, "error");
    }
  });

  $("#btn-recover").addEventListener("click", recover);

  function recover() {
    set((s) => { s.camera.status = "connecting"; });
    SFX.scan();
    const es = new EventSource("/api/camera/recover");
    es.onmessage = (e) => {
      try {
        const evt = JSON.parse(e.data);
        if (evt.phase === "done") {
          es.close();
          if (evt.ok) { SFX.access(); loadStatus(); }
          else SFX.denied();
        } else {
          SFX.scan();
        }
        toast(evt.message, evt.phase === "done" ? (evt.ok ? "success" : "error") : "info");
      } catch {}
    };
    es.onerror = () => { es.close(); };
  }

  // ── archive ──────────────────────────────────────────────────────────
  $("#btn-arch-load").addEventListener("click", () => {
    const date = $("#arch-date").value.replace(/-/g, "");
    if (date) loadDate(date);
  });
  $("#btn-arch-dates").addEventListener("click", loadDates);
  $("#btn-arch-all").addEventListener("click", loadAll);

  async function loadDates() {
    const panel = $("#arch-dates");
    panel.classList.remove("hidden");
    panel.innerHTML = '<span class="muted">loading…</span>';
    const r = await API.get("/api/recordings/dates");
    if (!r.ok) {
      panel.innerHTML = `<span class="muted">${r.error}</span>`;
      if (r.retryable) toast(r.error, "error");
      return;
    }
    if (!r.data.dates.length) {
      panel.innerHTML = '<span class="muted">no recordings in last 30 days.</span>';
      return;
    }
    panel.innerHTML = r.data.dates.map((d) =>
      `<span class="date-chip" data-date="${d}">${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}</span>`
    ).join("");
    panel.querySelectorAll(".date-chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        const d = chip.dataset.date;
        $("#arch-date").value = `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`;
        loadDate(d);
      });
    });
  }

  async function loadDate(date) {
    const r = await API.get(`/api/recordings/${date}`);
    if (!r.ok) {
      toast("could not show camera recordings: " + r.error, "error");
      return;
    }
    set((s) => {
      s.archive.date = date;
      s.archive.clips = r.data.clips || [];
      s.archive.selected = new Set();
      s.archive.thumbs = {};
      s.archive.thumbErrors = {};
      s.archive.thumbCounts = { total: 0, idle: 0, ready: 0, queued: 0, running: 0, failed: 0 };
    });
    // Rendering the grid issues one <img> GET per clip, which enqueues the
    // thumb server-side; readiness then arrives over SSE (see applyThumb).
    refreshThumbCounts();
  }

  async function loadAll() {
    const r = await API.get("/api/recordings/all?days=30");
    if (!r.ok) { toast(r.error, "error"); return; }
    set((s) => {
      s.archive.date = "*";
      s.archive.clips = (r.data.clips || []).sort((a, b) => b.startTime - a.startTime);
      s.archive.selected = new Set();
      s.archive.thumbs = {};
    });
    refreshThumbCounts();
  }

  $("#btn-arch-select-all").addEventListener("click", () => {
    set((s) => {
      if (s.archive.selected.size === s.archive.clips.length) {
        s.archive.selected.clear();
      } else {
        s.archive.clips.forEach((c) => s.archive.selected.add(clipKey(c)));
      }
    });
  });

  $("#btn-arch-bulk").addEventListener("click", async () => {
    const clips = state.archive.clips.filter((c) => state.archive.selected.has(clipKey(c)));
    if (!clips.length) return;
    const r = await API.post("/api/recordings/bulk", { clips });
    if (!r.ok) { toast(r.error, "error"); return; }
    pollBulk(r.data.job_id, r.data.total);
  });

  $("#arch-grid").addEventListener("click", (ev) => {
    const card = ev.target.closest(".clip-card");
    if (!card) return;
    const action = ev.target.dataset.action;
    const k = card.dataset.key;
    const clip = state.archive.clips.find((c) => clipKey(c) === k);
    if (!clip) return;
    if (action === "toggle") {
      set((s) => {
        if (s.archive.selected.has(k)) s.archive.selected.delete(k);
        else s.archive.selected.add(k);
      });
    } else if (action === "download") {
      downloadClip(clip);
    } else {
      watchClip(clip);
    }
  });

  async function downloadClip(clip) {
    const k = clipKey(clip);
    set((s) => { s.downloads[k] = "running"; });
    SFX.transfer();
    const r = await API.post("/api/recordings/download", clip);
    if (r.ok) {
      set((s) => { s.downloads[k] = "done"; });
      SFX.done();
      toast(`saved ${clip.date} • ${formatHMS(clip.startTime)}`, "success");
      loadLocalRecordings();
    } else {
      set((s) => { s.downloads[k] = "failed"; });
      SFX.denied();
      toast("download failed: " + r.error, "error");
      if (r.retryable) recover();
    }
  }

  // Track the currently-watched clip so stale listeners (from a previous
  // watch click) don't fire and stomp on the new player state.
  let watchSeq = 0;
  let playerHls = null;

  async function watchClip(clip) {
    const mySeq = ++watchSeq;
    const card = $("#player-card");
    const vid = $("#player-video");
    const status = $("#player-status");
    card.classList.remove("hidden");
    status.textContent = "loading…";
    set((s) => { s.player.clip = clip; s.player.status = "loading"; });
    $("#player-title").textContent = `${clip.date} • ${formatHMS(clip.startTime)} – ${formatHMS(clip.endTime)}`;
    if (playerHls) { playerHls.destroy(); playerHls = null; }
    try { vid.pause(); } catch {}
    vid.removeAttribute("src");
    vid.load();
    vid.muted = false;
    card.scrollIntoView({ behavior: "smooth" });

    const started = await API.post("/api/recordings/play", clip);
    if (mySeq !== watchSeq) return;
    if (!started.ok) {
      status.textContent = started.error;
      set((s) => { s.player.status = "error"; });
      toast("playback failed: " + started.error, "error");
      return;
    }

    const url = `${started.data.url}?t=${Date.now()}`;
    const markPlaying = () => {
      if (mySeq !== watchSeq) return;
      status.textContent = "";
      set((s) => { s.player.status = "playing"; });
      vid.play().catch(() => {});
    };
    const fail = (message) => {
      if (mySeq !== watchSeq) return;
      status.textContent = message;
      set((s) => { s.player.status = "error"; });
      toast("playback failed: " + message, "error");
    };

    if (started.data.source === "local") {
      vid.src = url;
      vid.addEventListener("loadedmetadata", markPlaying, { once: true });
      vid.addEventListener("error", () => fail("local recording could not be played"), { once: true });
    } else if (window.Hls && Hls.isSupported()) {
      playerHls = new Hls({
        enableWorker: true,
        lowLatencyMode: false,
        manifestLoadingMaxRetry: 8,
        levelLoadingMaxRetry: 8,
        fragLoadingMaxRetry: 8,
      });
      playerHls.loadSource(url);
      playerHls.attachMedia(vid);
      playerHls.on(Hls.Events.MANIFEST_PARSED, markPlaying);
      playerHls.on(Hls.Events.ERROR, (_event, data) => {
        if (!data.fatal || mySeq !== watchSeq) return;
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR) playerHls.startLoad();
        else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) playerHls.recoverMediaError();
        else fail(data.details || "unrecoverable media error");
      });
    } else if (vid.canPlayType("application/vnd.apple.mpegurl")) {
      vid.src = url;
      vid.addEventListener("loadedmetadata", markPlaying, { once: true });
      vid.addEventListener("error", () => fail("browser rejected playback stream"), { once: true });
    } else {
      fail("HLS is not supported in this browser");
    }
  }

  $("#btn-player-close").addEventListener("click", async () => {
    ++watchSeq;
    if (playerHls) { playerHls.destroy(); playerHls = null; }
    await API.post("/api/recordings/play/stop", {});
    $("#player-card").classList.add("hidden");
    const vid = $("#player-video");
    try { vid.pause(); } catch {}
    vid.removeAttribute("src");
    vid.load();
    set((s) => { s.player.clip = null; s.player.status = "idle"; });
  });

  // ── bulk poll ────────────────────────────────────────────────────────
  let bulkTimer = null;
  async function pollBulk(jobId, total) {
    clearTimeout(bulkTimer);
    const r = await API.get(`/api/jobs/${jobId}`);
    if (!r.ok) {
      toast("bulk job lost: " + r.error, "error");
      set((s) => { s.bulk = null; });
      return;
    }
    set((s) => { s.bulk = r.data.job; });
    if (r.data.job.status === "running" || r.data.job.status === "cancelling") {
      bulkTimer = setTimeout(() => pollBulk(jobId, total), 1500);
    } else {
      toast(`bulk ${r.data.job.status}: ${r.data.job.done} ok / ${r.data.job.failed} failed`, r.data.job.failed ? "error" : "success");
      setTimeout(() => set((s) => { s.bulk = null; }), 5000);
      loadLocalRecordings();
    }
  }

  // Correlate the on-disk archive with camera clips. This survives reloads
  // and includes files downloaded by unattended DVR syncs.
  async function loadLocalRecordings() {
    const r = await API.get("/api/recordings/local");
    if (r.ok) set((s) => { s.localKeys = new Set(r.data.files.map((file) => file.key)); });
    return r;
  }

  // ── cache ────────────────────────────────────────────────────────────
  function applyCache(u) {
    if (!u) return;
    set((s) => {
      s.cache = {
        recordings: u.recordings.bytes, recordings_files: u.recordings.files,
        thumbs: u.thumbs.bytes, thumbs_files: u.thumbs.files,
        previews: u.previews.bytes, previews_files: u.previews.files,
        playback: u.playback.bytes, playback_files: u.playback.files,
        stream: u.stream.bytes, stream_files: u.stream.files,
        total: u.total_bytes,
      };
    });
  }
  $$("[data-cache]").forEach((b) => {
    b.addEventListener("click", async () => {
      const target = b.dataset.cache;
      if (!confirm(`purge ${target}? this cannot be undone.`)) return;
      const r = await API.post("/api/cache/clear", { target });
      if (!r.ok) { toast(r.error, "error"); return; }
      toast(`purged ${target}: ${r.data.removed_files} files`, "success");
      if (target === "recordings" || target === "all") loadLocalRecordings();
    });
  });

  // ── settings ─────────────────────────────────────────────────────────
  async function loadFeatures() {
    set((s) => { s.features = { loading: true }; });
    const r = await API.get("/api/camera/features");
    if (!r.ok) {
      set((s) => { s.features = { error: r.error }; });
      toast("could not read camera features: " + r.error, "error");
      return;
    }
    set((s) => { s.features = r.data; });
  }
  $("#btn-features-refresh").addEventListener("click", loadFeatures);
  $("#feature-controls").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-feature]");
    if (!button) return;
    button.disabled = true;
    const value = button.dataset.value === "true";
    const r = await API.post(`/api/camera/feature/${button.dataset.feature}`, { value });
    toast(r.ok ? `${button.dataset.feature.replace(/_/g, " ")} ${value ? "on" : "off"}` : r.error, r.ok ? "success" : "error");
    await loadFeatures();
  });
  $("#feature-controls").addEventListener("change", async (event) => {
    if (event.target.id !== "day-night-mode") return;
    event.target.disabled = true;
    const r = await API.post("/api/camera/feature/day_night", { value: event.target.value });
    toast(r.ok ? `day/night mode: ${event.target.value}` : r.error, r.ok ? "success" : "error");
    await loadFeatures();
  });
  $("#btn-configure").addEventListener("click", () => {
    showSetup(true);
    loadConfig(true);
  });
  $("#btn-config-cancel").addEventListener("click", () => {
    if (state.camera.status === "connected") showSetup(false);
  });
  $("#btn-discover").addEventListener("click", async () => {
    const button = $("#btn-discover");
    const progress = $("#discovery-progress");
    const results = $("#discovery-results");
    const form = $("#camera-config-form");
    button.disabled = true;
    progress.textContent = "scanning LAN — this can take up to a minute…";
    results.classList.add("hidden");
    results.replaceChildren();
    const subnet = form.elements.subnet.value.trim();
    const r = await API.get(`/api/discovery${subnet ? `?subnet=${encodeURIComponent(subnet)}` : ""}`);
    button.disabled = false;
    if (!r.ok) {
      progress.textContent = "scan failed: " + r.error;
      toast("camera discovery failed: " + r.error, "error");
      return;
    }
    if (!subnet && r.data.subnets.length) form.elements.subnet.value = r.data.subnets[0];
    if (!r.data.cameras.length) {
      progress.textContent = "no compatible cameras found — check the subnet or enter the address manually";
      return;
    }
    const known = r.data.cameras.filter((camera) => camera.known_camera);
    const automatic = known.length === 1 ? known[0] : (r.data.cameras.length === 1 ? r.data.cameras[0] : null);
    if (automatic) {
      form.elements.host.value = automatic.ip;
      form.elements.mac.value = automatic.mac || "";
      progress.textContent = `${automatic.known_camera ? "known camera" : "camera candidate"} found at ${automatic.ip}`;
      return;
    }
    progress.textContent = `${r.data.cameras.length} camera candidate${r.data.cameras.length === 1 ? "" : "s"} found`;
    results.replaceChildren(...r.data.cameras.map((camera) => {
      const choice = document.createElement("button");
      choice.type = "button";
      choice.className = "btn discovery-choice";
      choice.textContent = `${camera.known_camera ? "known camera · " : ""}${camera.ip}${camera.mac ? ` · ${camera.mac}` : ""} · ${camera.source}`;
      choice.addEventListener("click", () => {
        form.elements.host.value = camera.ip;
        form.elements.mac.value = camera.mac || "";
        progress.textContent = `selected ${camera.ip}`;
        results.classList.add("hidden");
      });
      return choice;
    }));
    results.classList.remove("hidden");
  });
  $("#camera-config-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const progress = $("#config-progress");
    const submit = form.querySelector('[type="submit"]');
    submit.disabled = true;
    progress.textContent = "waking camera and reading available history…";
    const r = await API.post("/api/config", {
      host: form.elements.host.value.trim(), user: form.elements.user.value.trim(),
      password: form.elements.password.value, subnet: form.elements.subnet.value.trim(),
      mac: form.elements.mac.value,
    });
    form.elements.password.value = "";
    submit.disabled = false;
    if (!r.ok) {
      progress.textContent = "failed: " + r.error;
      toast(r.error, "error");
      return;
    }
    if (form.dataset.initialSetup !== "true") {
      progress.textContent = `connected as ${r.data.user} — credentials encrypted`;
      showSetup(false);
      await loadStatus();
      state.features = null;
      return;
    }
    const dvrForm = $("#setup-dvr-form");
    const defaultDays = Math.max(1, Number(r.data.dvr_retention_days) || 7);
    dvrForm.elements.enabled.checked = true;
    dvrForm.elements.retention_days.value = defaultDays;
    dvrForm.elements.retention.value = "delete";
    dvrForm.elements.interval_minutes.value = "1440";
    dvrForm.elements.daily_time.value = "00:10";
    toggleDvrFields(dvrForm);
    toggleDailyTime(dvrForm);
    $("#setup-camera-history").textContent = r.data.history_error
      ? "Camera connected, but its recording history could not be read. The one-day value can be changed, or reconnect to retry."
      : r.data.camera_available_days
        ? "Camera recording history is available within the configured sync window."
        : "The camera currently reports no recorded videos. DVR will retain the selected window as new videos arrive.";
    $("#setup-camera-step").classList.add("hidden");
    $("#setup-dvr-step").classList.remove("hidden");
    $("#setup-dvr-step").focus();
  });

  $("#setup-dvr-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = form.querySelector('[type="submit"]');
    const progress = $("#setup-dvr-progress");
    submit.disabled = true;
    progress.textContent = "saving…";
    const r = await API.post("/api/dvr", {
      enabled: form.elements.enabled.checked,
      retention_days: form.elements.retention_days.value || 1,
      keep_forever: form.elements.retention.value === "forever",
      interval_minutes: form.elements.interval_minutes.value,
      daily_time: form.elements.daily_time.value || "00:10",
      sync_now: form.elements.initial_sync.value === "now",
    });
    submit.disabled = false;
    if (!r.ok) { progress.textContent = "failed: " + r.error; toast(r.error, "error"); return; }
    progress.textContent = "saved";
    showSetup(false);
    await loadStatus();
    loadLocalRecordings();
  });

  $("#dvr-settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const status = $("#dvr-settings-status");
    const keepForever = form.elements.retention.value === "forever";
    const days = Number(form.elements.retention_days.value);
    const reducingRetention = !keepForever && (
      form.dataset.originalKeep === "true" || days < Number(form.dataset.originalDays || days)
    );
    if (reducingRetention && !confirm("This can delete local video copies outside the new history window. Videos on the camera will not be deleted. Continue?")) return;
    status.textContent = "saving…";
    const r = await API.post("/api/dvr", {
      enabled: form.elements.enabled.checked,
      retention_days: form.elements.retention_days.value || 1,
      keep_forever: keepForever,
      interval_minutes: form.elements.interval_minutes.value,
      daily_time: form.elements.daily_time.value || "00:10",
      sync_now: form.elements.initial_sync.value === "now",
    });
    status.textContent = r.ok ? "saved" : "failed: " + r.error;
    if (r.ok) {
      hydrateDvrForm(r.data);
      applyDvr(r.data);
      toast("DVR settings saved", "success");
    }
    else toast(r.error, "error");
  });
  $("#btn-dvr-sync").addEventListener("click", async () => {
    const button = $("#btn-dvr-sync");
    const status = $("#dvr-status");
    button.disabled = true;
    status.textContent = "requesting sync…";
    const r = await API.post("/api/dvr/sync", {});
    if (!r.ok) {
      button.disabled = false;
      status.textContent = "sync failed: " + r.error;
      toast(r.error, "error");
      return;
    }
    status.textContent = r.data.message;
    toast(r.data.message, "success");
  });
  $("#btn-reconnect").addEventListener("click", async () => {
    const r = await API.post("/api/camera/reconnect");
    toast(r.ok ? "reconnected" : "failed: " + r.error, r.ok ? "success" : "error");
    if (r.ok) loadStatus();
  });
  $("#btn-privacy-on").addEventListener("click", async () => {
    const r = await API.post("/api/camera/privacy", { enabled: true });
    toast(r.ok ? "privacy on" : r.error, r.ok ? "success" : "error");
  });
  $("#btn-privacy-off").addEventListener("click", async () => {
    const r = await API.post("/api/camera/privacy", { enabled: false });
    toast(r.ok ? "privacy off" : r.error, r.ok ? "success" : "error");
  });
  $("#btn-reboot").addEventListener("click", async () => {
    if (!confirm("reboot the camera?")) return;
    const r = await API.post("/api/camera/reboot");
    toast(r.ok ? "reboot sent" : r.error, r.ok ? "success" : "error");
  });
  $("#btn-events").addEventListener("click", async () => {
    const log = $("#events-log");
    log.textContent = "loading…";
    const r = await API.get("/api/camera/events?hours=24");
    log.textContent = r.ok ? JSON.stringify(r.data.events, null, 2) : ("error: " + r.error);
  });

  // ── helpers ──────────────────────────────────────────────────────────
  function formatHMS(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour12: false });
  }

  // ── boot ─────────────────────────────────────────────────────────────
  // Boot jingle plays on first user interaction (audio context can't start
  // before that anyway). Wire to the first click so it's tied to volition.
  document.addEventListener("click", () => SFX.boot(), { once: true });
  (async function init() {
    openEvents();
    await loadThemes();
    const configured = await loadConfig();
    if (configured) await loadStatus();
    loadLocalRecordings();
    loadDvr();
  })();
})();
