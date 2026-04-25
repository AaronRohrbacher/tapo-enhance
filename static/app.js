// ── Helpers ─────────────────────────────────────────────────────────────────

async function api(url, opts = {}) {
  try {
    const res = await fetch(url, opts);
    return await res.json();
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

function post(url, body = {}) {
  return api(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

let toastTimer;
function toast(msg, type = "info") {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = `toast ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 4000);
}

function formatTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function formatDuration(seconds) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

// ── Tabs ────────────────────────────────────────────────────────────────────

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-content").forEach((c) => c.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");

    if (btn.dataset.tab === "recordings") loadLocalRecordings();
    if (btn.dataset.tab === "settings") loadCameraInfo();
  });
});

// ── Camera Status ───────────────────────────────────────────────────────────

async function checkStatus() {
  const statusEl = document.getElementById("camera-status");
  const textEl = document.getElementById("status-text");
  statusEl.className = "status connecting";
  textEl.textContent = "Connecting...";

  const data = await api("/api/camera/status");
  if (data.ok) {
    statusEl.className = "status connected";
    textEl.textContent = `${data.device_alias} (${data.device_model})`;
  } else {
    statusEl.className = "status disconnected";
    textEl.textContent = data.error?.substring(0, 50) || "Disconnected";
  }
}

// ── Live Stream (RTSP via ffmpeg HLS) ───────────────────────────────────────

let hls = null;

const btnStart = document.getElementById("btn-start-stream");
const btnStartSD = document.getElementById("btn-start-stream-sd");
const btnStop = document.getElementById("btn-stop-stream");
const btnRefresh = document.getElementById("btn-refresh-stream");
const btnSnapshot = document.getElementById("btn-snapshot");
const overlay = document.getElementById("stream-overlay");
const video = document.getElementById("live-video");

function attachHls(url) {
  if (hls) {
    hls.destroy();
    hls = null;
  }

  if (Hls.isSupported()) {
    hls = new Hls({
      liveSyncDurationCount: 2,
      liveMaxLatencyDurationCount: 5,
      enableWorker: true,
      lowLatencyMode: true,
    });
    hls.loadSource(url);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      video.play().catch(() => {});
    });
    hls.on(Hls.Events.ERROR, (_, data) => {
      if (data.fatal) {
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
          toast("Stream buffering, retrying...", "error");
          setTimeout(() => hls && hls.startLoad(), 2000);
        } else {
          toast("Stream error: " + data.details, "error");
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

async function startStream(quality) {
  btnStart.disabled = true;
  if (btnStartSD) btnStartSD.disabled = true;
  const label = quality === "hd" ? "HD" : "SD";
  btnStart.innerHTML = `<span class="spinner"></span>Starting ${label}...`;

  const data = await post("/api/stream/start", { quality });
  if (data.ok) {
    overlay.classList.add("hidden");
    btnStop.disabled = false;
    btnRefresh.disabled = false;

    // Wait for ffmpeg to generate initial HLS segments
    setTimeout(() => attachHls(data.url + "?t=" + Date.now()), 3000);
    toast(`${label} stream started via RTSP`, "success");
  } else {
    toast("Failed: " + data.error, "error");
  }

  btnStart.disabled = false;
  if (btnStartSD) btnStartSD.disabled = false;
  btnStart.textContent = "HD Stream";
}

btnStart.addEventListener("click", () => startStream("hd"));

const btnWake = document.getElementById("btn-wake");
if (btnWake) {
  btnWake.addEventListener("click", async () => {
    btnWake.disabled = true;
    btnWake.innerHTML = '<span class="spinner"></span>Waking...';
    const hint = document.getElementById("wake-hint");
    hint.textContent = "Sending Wake-on-LAN...";

    const data = await post("/api/camera/wake");
    if (data.ok) {
      toast(`Camera awake in ${data.seconds}s`, "success");
      hint.textContent = "Camera is awake! Start the stream.";
      checkStatus();
    } else {
      toast("Wake failed: " + data.error, "error");
      hint.textContent = "Wake failed. Try ringing the doorbell.";
    }
    btnWake.disabled = false;
    btnWake.textContent = "Wake Camera";
  });
}

btnStop.addEventListener("click", async () => {
  if (hls) { hls.destroy(); hls = null; }
  video.src = "";
  await post("/api/stream/stop");
  overlay.classList.remove("hidden");
  btnStop.disabled = true;
  btnRefresh.disabled = true;
  toast("Stream stopped");
});

btnRefresh.addEventListener("click", () => {
  attachHls("/stream/output.m3u8?t=" + Date.now());
});

if (btnSnapshot) {
  btnSnapshot.addEventListener("click", async () => {
    btnSnapshot.disabled = true;
    btnSnapshot.innerHTML = '<span class="spinner"></span>';
    const img = document.getElementById("snapshot-img");
    try {
      const res = await fetch("/api/snapshot?t=" + Date.now());
      if (res.ok) {
        const blob = await res.blob();
        img.src = URL.createObjectURL(blob);
        img.classList.remove("hidden");
        toast("Snapshot captured", "success");
      } else {
        toast("Snapshot failed", "error");
      }
    } catch (e) {
      toast("Snapshot error: " + e.message, "error");
    }
    btnSnapshot.disabled = false;
    btnSnapshot.textContent = "Snapshot";
  });
}

// Check if stream was already running
async function checkStream() {
  const data = await api("/api/stream/status");
  if (data.running) {
    overlay.classList.add("hidden");
    btnStop.disabled = false;
    btnRefresh.disabled = false;
    attachHls("/stream/output.m3u8?t=" + Date.now());
  }
}

// ── Recordings ──────────────────────────────────────────────────────────────

const recDate = document.getElementById("rec-date");
recDate.value = new Date().toISOString().split("T")[0];

document.getElementById("btn-load-recordings").addEventListener("click", () => {
  const date = recDate.value.replace(/-/g, "");
  loadRecordings(date);
});

document.getElementById("btn-load-dates").addEventListener("click", loadAvailableDates);

async function loadAvailableDates() {
  const panel = document.getElementById("available-dates");
  panel.classList.remove("hidden");
  panel.innerHTML = '<span class="spinner"></span> Loading dates...';

  const data = await api("/api/recordings/dates");
  if (!data.ok) {
    panel.innerHTML = `<p>Error: ${data.error}</p>`;
    return;
  }

  const dates = data.dates;
  if (!dates || dates.length === 0) {
    panel.innerHTML = "<p>No recordings found in the last 30 days.</p>";
    return;
  }

  let dateList = [];
  if (Array.isArray(dates)) {
    for (const entry of dates) {
      if (entry.date) dateList.push(entry.date);
      else if (typeof entry === "string") dateList.push(entry);
    }
  }

  if (dateList.length === 0) {
    panel.innerHTML = `<pre style="font-size:0.75rem;color:var(--text-dim)">${JSON.stringify(dates, null, 2)}</pre>`;
    return;
  }

  panel.innerHTML = dateList
    .map((d) => {
      const formatted = `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`;
      return `<span class="date-chip" data-date="${d}">${formatted}</span>`;
    })
    .join("");

  panel.querySelectorAll(".date-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const d = chip.dataset.date;
      recDate.value = `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`;
      loadRecordings(d);
    });
  });
}

async function loadRecordings(date) {
  const grid = document.getElementById("recordings-list");
  grid.innerHTML = '<p class="placeholder"><span class="spinner"></span> Loading recordings...</p>';

  const data = await api(`/api/recordings/${date}`);
  if (!data.ok) {
    grid.innerHTML = `<p class="placeholder">Error: ${data.error}</p>`;
    return;
  }

  const recs = data.recordings;
  if (!recs || recs.length === 0) {
    grid.innerHTML = '<p class="placeholder">No recordings found for this date.</p>';
    return;
  }

  let clips = [];
  const items = Array.isArray(recs) ? recs : [recs];
  for (const item of items) {
    if (item.video_results) {
      for (const vr of item.video_results) {
        clips.push({ startTime: parseInt(vr.startTime), endTime: parseInt(vr.endTime) });
      }
    } else if (item.startTime) {
      clips.push({ startTime: parseInt(item.startTime), endTime: parseInt(item.endTime) });
    } else {
      // Format: {"search_video_results_N": {startTime, endTime, vedio_type}}
      for (const [key, val] of Object.entries(item)) {
        if (key.startsWith("search_video_results") && val && val.startTime) {
          clips.push({ startTime: parseInt(val.startTime), endTime: parseInt(val.endTime) });
        }
      }
    }
  }

  if (clips.length === 0) {
    grid.innerHTML = `<p class="placeholder">Found data but couldn't parse clips.<br><pre style="font-size:0.7rem;text-align:left">${JSON.stringify(recs, null, 2).slice(0, 500)}</pre></p>`;
    return;
  }

  clips.sort((a, b) => a.startTime - b.startTime);

  grid.innerHTML = clips
    .map(
      (c) => `
    <div class="recording-card">
      <div class="time">${formatTime(c.startTime)} - ${formatTime(c.endTime)}</div>
      <div class="duration">${formatDuration(c.endTime - c.startTime)}</div>
      <div class="actions">
        <button class="btn btn-primary btn-download" data-start="${c.startTime}" data-end="${c.endTime}" data-date="${date}">
          Download & Play
        </button>
      </div>
    </div>
  `
    )
    .join("");

  grid.querySelectorAll(".btn-download").forEach((btn) => {
    btn.addEventListener("click", () => downloadAndPlay(btn));
  });
}

async function downloadAndPlay(btn) {
  const startTime = btn.dataset.start;
  const endTime = btn.dataset.end;
  const date = btn.dataset.date;
  const duration = endTime - startTime;

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Downloading...';

  const data = await post("/api/recordings/download", { startTime, endTime, date });

  if (data.ok) {
    const player = document.getElementById("recording-player");
    const vid = document.getElementById("recording-video");
    const title = document.getElementById("recording-title");

    player.classList.remove("hidden");
    title.textContent = `${formatTime(startTime)} - ${formatTime(endTime)}`;
    vid.src = data.file + "?t=" + Date.now();
    vid.play().catch(() => {});

    player.scrollIntoView({ behavior: "smooth" });
    let msg = data.cached ? "Loaded from cache" : "Download complete";
    if (data.method) msg += ` (via ${data.method})`;
    toast(msg, "success");
  } else {
    toast("Download failed: " + data.error, "error");
  }

  btn.disabled = false;
  btn.textContent = "Download & Play";
}

document.getElementById("btn-close-player").addEventListener("click", () => {
  document.getElementById("recording-player").classList.add("hidden");
  document.getElementById("recording-video").src = "";
});

async function loadLocalRecordings() {
  const data = await api("/api/recordings/local");
  const list = document.getElementById("local-list");

  if (!data.ok || data.files.length === 0) {
    list.innerHTML = '<p style="color:var(--text-dim);font-size:0.85rem">No downloaded recordings yet.</p>';
    return;
  }

  list.innerHTML = data.files
    .map(
      (f) => `
    <div class="local-card" data-path="${f.path}">
      <div>
        <div>${f.date}: ${f.file}</div>
        <div class="meta">${f.size_mb} MB</div>
      </div>
      <span style="color:var(--accent)">Play</span>
    </div>
  `
    )
    .join("");

  list.querySelectorAll(".local-card").forEach((card) => {
    card.addEventListener("click", () => {
      const player = document.getElementById("recording-player");
      const vid = document.getElementById("recording-video");
      player.classList.remove("hidden");
      vid.src = card.dataset.path;
      vid.play().catch(() => {});
      player.scrollIntoView({ behavior: "smooth" });
    });
  });
}

// ── Settings ────────────────────────────────────────────────────────────────

async function loadCameraInfo() {
  const el = document.getElementById("camera-info");
  el.innerHTML = '<span class="spinner"></span> Loading...';

  const data = await api("/api/camera/status");
  if (!data.ok) {
    el.innerHTML = `<p style="color:var(--danger)">Error: ${data.error}</p>`;
    return;
  }

  el.innerHTML = `
    <div class="info-row"><span class="info-label">Name</span><span>${data.device_alias}</span></div>
    <div class="info-row"><span class="info-label">Model</span><span>${data.device_model}</span></div>
    <div class="info-row"><span class="info-label">Firmware</span><span>${data.sw_version}</span></div>
    <div class="info-row"><span class="info-label">Hardware</span><span>${data.hw_version}</span></div>
    <div class="info-row"><span class="info-label">MAC</span><span>${data.mac}</span></div>
    <div class="info-row"><span class="info-label">Cloud Password</span><span>${data.has_cloud_password ? "Configured" : "Not set (recording download may use RTSP fallback)"}</span></div>
    <div class="info-row"><span class="info-label">RTSP HD</span><span>${data.rtsp_url_hd}</span></div>
    <div class="info-row"><span class="info-label">RTSP SD</span><span>${data.rtsp_url_sd}</span></div>
  `;
}

document.getElementById("btn-reconnect").addEventListener("click", async () => {
  toast("Reconnecting...");
  const data = await post("/api/camera/reconnect");
  if (data.ok) {
    toast("Reconnected", "success");
    checkStatus();
    loadCameraInfo();
  } else {
    toast("Failed: " + data.error, "error");
  }
});

document.getElementById("btn-privacy-on").addEventListener("click", async () => {
  const data = await post("/api/camera/privacy", { enabled: true });
  toast(data.ok ? "Privacy mode ON" : "Error: " + data.error, data.ok ? "success" : "error");
});

document.getElementById("btn-privacy-off").addEventListener("click", async () => {
  const data = await post("/api/camera/privacy", { enabled: false });
  toast(data.ok ? "Privacy mode OFF" : "Error: " + data.error, data.ok ? "success" : "error");
});

// ── Init ────────────────────────────────────────────────────────────────────

checkStatus();
checkStream();
