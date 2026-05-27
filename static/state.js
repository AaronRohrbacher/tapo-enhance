// Single source of truth for UI state. Mutations go through `set()` so
// listeners always see consistent snapshots. The render layer subscribes
// to slices it cares about.

(function (global) {
  "use strict";

  const state = {
    camera: { status: "connecting", host: "", alias: "" },
    live: { status: "idle", url: null, error: null },
    archive: {
      date: "",
      clips: [],
      selected: new Set(),
      thumbs: {}, // key -> "ready" | "queued" | "running" | "failed"
      thumbErrors: {}, // key -> message
      thumbCounts: { total: 0, ready: 0, queued: 0, running: 0, failed: 0 },
    },
    player: { clip: null, status: "idle" },  // idle | loading | playing | error
    downloads: {}, // key -> "running" | "done" | "failed"
    bulk: null, // { id, total, done, failed, current, status }
    local: [],
    cache: { recordings: 0, thumbs: 0, previews: 0, stream: 0, total: 0 },
    gateway: { busy: null, queued: 0, liveAttached: false, liveRunning: false },
  };

  const listeners = new Set();

  function emit() {
    for (const l of listeners) l(state);
  }

  function set(updater) {
    if (typeof updater === "function") {
      updater(state);
    } else {
      Object.assign(state, updater);
    }
    emit();
  }

  function subscribe(fn) {
    listeners.add(fn);
    fn(state);
    return () => listeners.delete(fn);
  }

  function clipKey(c) {
    return `${c.date}/${c.startTime}/${c.endTime}`;
  }

  global.S = { state, set, subscribe, clipKey };
})(window);
