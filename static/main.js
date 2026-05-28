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
    UI.renderLocal(s);
    UI.renderCache(s);
  });
  UI.renderTabs();

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
      });
      UI.renderSettings(r.data);
      if (!wasConnected) SFX.access();
      return true;
    }
    set((s) => { s.camera.status = "disconnected"; });
    SFX.alert();
    if (r.retryable) toast("camera offline — try [reconnect]", "error");
    else if (r.code === "CAMERA_AUTH") toast("auth failed — check TAPO_PASSWORD in .env", "error");
    else if (r.code === "CAMERA_NOT_CONFIGURED") toast("set TAPO_HOST + TAPO_PASSWORD in .env", "error");
    return false;
  }

  // poll gateway status (busy/queued) every 2s while live or downloads active
  async function pollGateway() {
    const r = await API.get("/api/gateway/status");
    if (r.ok) {
      set((s) => {
        s.gateway = {
          busy: r.data.busy,
          queued: r.data.queued,
          liveAttached: r.data.live_attached,
          liveRunning: r.data.live_running,
        };
      });
    }
  }
  setInterval(pollGateway, 2000);

  // ── live ─────────────────────────────────────────────────────────────
  let hls = null;
  const video = $("#live-video");
  $("#btn-mute").addEventListener("click", () => {
    video.muted = !video.muted;
    $("#btn-mute").textContent = video.muted ? "unmute" : "mute";
  });

  function attachHls(url) {
    if (hls) { hls.destroy(); hls = null; }
    if (window.Hls && Hls.isSupported()) {
      hls = new Hls({
        liveSyncDurationCount: 2,
        liveMaxLatencyDurationCount: 5,
        enableWorker: true,
        lowLatencyMode: true,
      });
      hls.loadSource(url);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
      hls.on(Hls.Events.ERROR, (_e, data) => {
        if (data.fatal) {
          if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
            setTimeout(() => hls && hls.startLoad(), 1500);
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
    set((s) => { s.live.status = "starting"; });
    SFX.powerOn();
    const r = await API.post("/api/stream/start", {});
    if (!r.ok) {
      set((s) => { s.live.status = "error"; s.live.error = r.error; });
      SFX.err();
      toast("live failed: " + r.error, "error");
      if (r.retryable) recover();
      return;
    }
    set((s) => { s.live.status = "running"; s.live.url = r.data.url; });
    setTimeout(() => attachHls(r.data.url + "?t=" + Date.now()), 1500);
  });

  $("#btn-live-stop").addEventListener("click", async () => {
    if (hls) { hls.destroy(); hls = null; }
    video.src = "";
    SFX.powerOff();
    await API.post("/api/stream/stop", {});
    set((s) => { s.live.status = "idle"; s.live.url = null; });
  });

  $("#btn-live-refresh").addEventListener("click", () => {
    if (state.live.url) attachHls(state.live.url + "?t=" + Date.now());
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
      toast("archive load failed: " + r.error, "error");
      return;
    }
    set((s) => {
      s.archive.date = date;
      s.archive.clips = r.data.clips || [];
      s.archive.selected = new Set();
      s.archive.thumbs = {};
      s.archive.thumbErrors = {};
      s.archive.thumbCounts = { total: 0, ready: 0, queued: 0, running: 0, failed: 0 };
    });
    pollThumbStatus();
    primeThumbs();
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
    primeThumbs();
  }

  // Prime the thumb queue for every clip on screen so the queue panel
  // counts reflect the full set, not just the ones lazy-loaded.
  function primeThumbs() {
    state.archive.clips.forEach((c) => {
      fetch(`/api/thumb/${c.date}/${c.startTime}/${c.endTime}`, { method: "HEAD" }).catch(() => {});
    });
  }

  // Rolling thumb-status poll. Stops only when the FRONTEND view of every
  // clip is settled (ready or failed). Don't trust backend counts to gate
  // stopping — those only include clips that called `backfill.request`,
  // and clips served from local cache never go through it.
  let thumbPollTimer = null;
  async function pollThumbStatus() {
    const date = state.archive.date;
    if (!date || date === "*") return;
    const r = await API.get(`/api/thumb-status/${date}`);
    if (r.ok) {
      const c = r.data.counts;
      set((s) => {
        s.archive.thumbCounts = {
          total: c.total, ready: c.ready, queued: c.queued, running: c.running, failed: c.failed,
        };
      });
      await refreshClipThumbs();
    }
    const settled = state.archive.clips.every((cl) => {
      const st = state.archive.thumbs[clipKey(cl)];
      return st === "ready" || st === "failed";
    });
    clearTimeout(thumbPollTimer);
    if (!settled) thumbPollTimer = setTimeout(pollThumbStatus, 1500);
  }
  async function refreshClipThumbs() {
    const clips = state.archive.clips;
    await Promise.all(clips.map(async (c) => {
      const k = clipKey(c);
      if (state.archive.thumbs[k] === "ready") return;
      try {
        const res = await fetch(`/api/thumb/${c.date}/${c.startTime}/${c.endTime}`, { method: "HEAD" });
        const status = res.headers.get("X-Thumb-Status") || "queued";
        const err = res.headers.get("X-Thumb-Error") || "";
        // renderArchive embeds the status in the img URL (`?s=${ts}`), so
        // a state change here naturally swaps the img to a fresh fetch.
        set((s) => {
          s.archive.thumbs[k] = status;
          if (err) s.archive.thumbErrors[k] = err;
        });
      } catch {}
    }));
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
      loadLocal();
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

  async function watchClip(clip) {
    const mySeq = ++watchSeq;
    const card = $("#player-card");
    const vid = $("#player-video");
    const status = $("#player-status");
    card.classList.remove("hidden");
    status.textContent = "loading…";
    set((s) => { s.player.clip = clip; s.player.status = "loading"; });
    $("#player-title").textContent = `${clip.date} • ${formatHMS(clip.startTime)} – ${formatHMS(clip.endTime)}`;
    const url = `/api/recordings/stream/${clip.date}/${clip.startTime}/${clip.endTime}?t=${Date.now()}`;

    let done = false;
    const onReady = () => {
      if (mySeq !== watchSeq || done) return;
      done = true;
      status.textContent = "";
      set((s) => { s.player.status = "playing"; });
      // Removing all candidate listeners: which one fires first depends on
      // browser + whether the file is already cached locally.
      vid.removeEventListener("loadeddata", onReady);
      vid.removeEventListener("loadedmetadata", onReady);
      vid.removeEventListener("canplay", onReady);
      vid.removeEventListener("playing", onReady);
    };
    vid.addEventListener("loadeddata", onReady);
    vid.addEventListener("loadedmetadata", onReady);
    vid.addEventListener("canplay", onReady);
    vid.addEventListener("playing", onReady);

    // Reset element state before assigning new src so a half-loaded prior
    // source doesn't leave the element in a stuck state.
    try { vid.pause(); } catch {}
    vid.removeAttribute("src");
    vid.load();
    vid.src = url;
    vid.muted = false;
    vid.play().catch(() => {});

    // If the file was already buffered locally, readyState may already be
    // >= HAVE_CURRENT_DATA by the time we attach. Cover that race.
    if (vid.readyState >= 2) onReady();

    // Cold watches (camera download) can take 30-90s. Switch the status
    // text after a few seconds so the user knows we're not stuck.
    setTimeout(() => {
      if (done || mySeq !== watchSeq || vid.readyState >= 2) return;
      status.textContent = "downloading from camera (this can take a while)…";
    }, 4_000);

    setTimeout(async () => {
      // Re-check guards before AND after every await — the video may have
      // started playing or the user may have moved on by the time HEAD
      // returns (HEAD waits for any in-flight download to finish).
      if (done || mySeq !== watchSeq || vid.readyState >= 2) return;
      let head;
      try { head = await fetch(url, { method: "HEAD" }); }
      catch (e) {
        if (done || mySeq !== watchSeq || vid.readyState >= 2) return;
        status.textContent = e.message;
        set((s) => { s.player.status = "error"; });
        return;
      }
      if (done || mySeq !== watchSeq || vid.readyState >= 2) return;
      if (!head.ok) {
        const r = await fetch(url);
        if (done || mySeq !== watchSeq || vid.readyState >= 2) return;
        let err = "playback timed out";
        try { const j = await r.json(); err = j.error || err; } catch {}
        status.textContent = err;
        set((s) => { s.player.status = "error"; });
        toast("playback failed: " + err, "error");
      }
    }, 30_000);
    card.scrollIntoView({ behavior: "smooth" });
  }

  $("#btn-player-close").addEventListener("click", () => {
    $("#player-card").classList.add("hidden");
    $("#player-video").src = "";
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
      loadLocal();
    }
  }

  // ── local list ───────────────────────────────────────────────────────
  async function loadLocal() {
    const r = await API.get("/api/recordings/local");
    if (r.ok) set((s) => { s.local = r.data.files; });
    loadCache();
  }
  $("#local-list").addEventListener("click", (ev) => {
    const row = ev.target.closest(".local-row");
    if (!row) return;
    const card = $("#player-card");
    const vid = $("#player-video");
    card.classList.remove("hidden");
    $("#player-title").textContent = row.querySelector("div").textContent;
    vid.src = row.dataset.path;
    vid.play().catch(() => {});
    $("#tab-archive").classList.add("active");
    $$(".tab-content").forEach((c) => c.classList.toggle("active", c.id === "tab-archive"));
    $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === "archive"));
    card.scrollIntoView({ behavior: "smooth" });
  });

  // ── cache ────────────────────────────────────────────────────────────
  async function loadCache() {
    const r = await API.get("/api/cache/usage");
    if (!r.ok) return;
    const u = r.data.usage;
    set((s) => {
      s.cache = {
        recordings: u.recordings.bytes, recordings_files: u.recordings.files,
        thumbs: u.thumbs.bytes, thumbs_files: u.thumbs.files,
        previews: u.previews.bytes, previews_files: u.previews.files,
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
      loadCache();
      if (target === "recordings" || target === "all") loadLocal();
    });
  });

  // ── settings ─────────────────────────────────────────────────────────
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
    await loadStatus();
    loadLocal();
  })();
})();
