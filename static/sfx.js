// 80s/90s hacker-terminal sounds. Pure Web Audio, no assets. Audio context
// is suspended until the user clicks/types, so nothing plays before then.
// Vibe: WarGames terminal · Hackers neon · Tron lightcycle · TNG LCARS.

(function (global) {
  "use strict";

  let ctx = null;
  let master = null;
  let bus = null;       // pre-master with a touch of distortion for grit
  let conv = null;      // small convolution for a "rack" reverb tail
  let enabled = (() => {
    try { return localStorage.getItem("tapo.sfx") !== "off"; } catch { return true; }
  })();
  let volume = (() => {
    try { return parseFloat(localStorage.getItem("tapo.sfx.vol") || "0.35"); } catch { return 0.35; }
  })();

  function ensure() {
    if (!ctx) {
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      master = ctx.createGain();
      master.gain.value = volume;
      master.connect(ctx.destination);

      // tiny IR for a single-rack-unit reverb-ish tail
      const sr = ctx.sampleRate;
      const irLen = Math.floor(sr * 0.12);
      const ir = ctx.createBuffer(2, irLen, sr);
      for (let c = 0; c < 2; c++) {
        const d = ir.getChannelData(c);
        for (let i = 0; i < irLen; i++) {
          const t = i / irLen;
          d[i] = (Math.random() * 2 - 1) * Math.pow(1 - t, 2.2) * 0.5;
        }
      }
      conv = ctx.createConvolver();
      conv.buffer = ir;
      const wet = ctx.createGain(); wet.gain.value = 0.12;
      conv.connect(wet).connect(master);

      bus = ctx.createGain(); bus.gain.value = 1.0;
      bus.connect(master);
      bus.connect(conv);
    }
    if (ctx.state === "suspended") ctx.resume();
  }

  function _out() { return bus; }

  // ── primitive: a single oscillator note with envelope and optional pitch slide
  function tone({
    freq = 800, dur = 0.08, type = "square", vol = 1,
    attack = 0.004, release = 0.05, slide = null, slideTime = null,
    detune = 0,
  } = {}) {
    if (!enabled) return;
    ensure();
    const t0 = ctx.currentTime;
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.type = type;
    o.frequency.setValueAtTime(freq, t0);
    o.detune.setValueAtTime(detune, t0);
    if (slide !== null) {
      o.frequency.exponentialRampToValueAtTime(Math.max(40, slide), t0 + (slideTime ?? dur));
    }
    g.gain.setValueAtTime(0, t0);
    g.gain.linearRampToValueAtTime(vol, t0 + attack);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur + release);
    o.connect(g).connect(_out());
    o.start(t0);
    o.stop(t0 + dur + release + 0.05);
  }

  // ── primitive: filtered noise burst (used for shutter/glitch/data)
  function noise({ dur = 0.1, vol = 0.4, hp = 0, lp = 4000, env = "decay" } = {}) {
    if (!enabled) return;
    ensure();
    const t0 = ctx.currentTime;
    const len = Math.max(1, Math.floor(ctx.sampleRate * dur));
    const buf = ctx.createBuffer(1, len, ctx.sampleRate);
    const data = buf.getChannelData(0);
    for (let i = 0; i < len; i++) data[i] = Math.random() * 2 - 1;
    const src = ctx.createBufferSource();
    src.buffer = buf;
    const g = ctx.createGain();
    if (env === "decay") {
      g.gain.setValueAtTime(vol, t0);
      g.gain.exponentialRampToValueAtTime(0.001, t0 + dur);
    } else if (env === "attack") {
      g.gain.setValueAtTime(0, t0);
      g.gain.linearRampToValueAtTime(vol, t0 + dur * 0.6);
      g.gain.exponentialRampToValueAtTime(0.001, t0 + dur);
    } else {
      g.gain.value = vol;
    }
    let node = src;
    if (hp) {
      const f = ctx.createBiquadFilter(); f.type = "highpass"; f.frequency.value = hp;
      node.connect(f); node = f;
    }
    if (lp) {
      const f = ctx.createBiquadFilter(); f.type = "lowpass"; f.frequency.value = lp;
      node.connect(f); node = f;
    }
    node.connect(g).connect(_out());
    src.start(t0);
    src.stop(t0 + dur + 0.05);
  }

  // ── primitive: dual-osc stab with slight detune (richer than `tone`)
  function stab({ freq = 800, dur = 0.08, type = "square", vol = 0.7, attack = 0.003, release = 0.04 } = {}) {
    tone({ freq, dur, type, vol: vol * 0.7, attack, release });
    tone({ freq, dur, type, vol: vol * 0.5, attack, release, detune: -7 });
    tone({ freq: freq * 2, dur: dur * 0.6, type: "triangle", vol: vol * 0.18, attack, release });
  }

  // ── primitive: schedule a sequence of tones; returns the total ms
  function seq(notes) {
    let off = 0;
    for (const n of notes) {
      const delay = n.at != null ? n.at : off;
      setTimeout(() => tone(n), delay);
      off = delay + (n.gap ?? 60);
    }
    return off;
  }

  // ──────────────────────────────────────────────────────────────────────
  // PUBLIC SOUND PALETTE
  // Each method is one "event" the UI fires. Names match intent, not waveform.
  // ──────────────────────────────────────────────────────────────────────

  const SFX = {
    set: (on) => {
      enabled = !!on;
      try { localStorage.setItem("tapo.sfx", on ? "on" : "off"); } catch {}
    },
    enabled: () => enabled,
    volume: (v) => {
      if (v == null) return volume;
      volume = Math.max(0, Math.min(1, v));
      try { localStorage.setItem("tapo.sfx.vol", String(volume)); } catch {}
      if (master) master.gain.setTargetAtTime(volume, ctx.currentTime, 0.02);
    },

    // ── micro-UI ──
    // Subtle keystroke / heard on every button press. Dry, dark, fast.
    click() {
      tone({ freq: 180, dur: 0.012, type: "triangle", vol: 0.55 });
      tone({ freq: 1400, dur: 0.008, type: "square", vol: 0.18 });
    },
    // Higher chirp — tab switch, dropdown open, etc.
    blip() {
      tone({ freq: 1600, dur: 0.04, type: "square", vol: 0.55, slide: 2400, slideTime: 0.04 });
    },
    // Tiny tick — for hovers, counters, ambient pulses.
    tick() { tone({ freq: 2800, dur: 0.008, type: "square", vol: 0.32 }); },

    // ── status / toast ──
    // Pleasant two-note rising bell — success.
    ok() {
      tone({ freq: 988,  dur: 0.06, type: "sine", vol: 0.5 });
      setTimeout(() => tone({ freq: 1480, dur: 0.10, type: "sine", vol: 0.55 }), 70);
    },
    // Three-note falling motif — error, but not aggressive.
    err() {
      tone({ freq: 740, dur: 0.07, type: "sawtooth", vol: 0.55 });
      setTimeout(() => tone({ freq: 520, dur: 0.08, type: "sawtooth", vol: 0.6 }), 90);
      setTimeout(() => tone({ freq: 360, dur: 0.14, type: "sawtooth", vol: 0.7, slide: 220, slideTime: 0.14 }), 200);
    },
    // Klaxon — three pulse-pairs. Alert: camera offline.
    alert() {
      const pair = () => {
        stab({ freq: 880, dur: 0.09, type: "square", vol: 0.85 });
        setTimeout(() => stab({ freq: 660, dur: 0.09, type: "square", vol: 0.85 }), 100);
      };
      for (let i = 0; i < 3; i++) setTimeout(pair, i * 230);
    },

    // ── access / connection ──
    // WarGames-style modem handshake — short, recognisable, ~700ms total.
    // Used on live-start, reconnect-success.
    access() {
      // low carrier + filtered hash + answer-tone bell
      tone({ freq: 140, dur: 0.20, type: "sine",     vol: 0.65,
             attack: 0.005, release: 0.10, slide: 70, slideTime: 0.18 });
      setTimeout(() => noise({ dur: 0.22, vol: 0.12, hp: 700, lp: 2400 }), 60);
      // chirps walking up — the "negotiating" feel
      [1500, 1800, 2100, 2400, 2700].forEach((f, i) =>
        setTimeout(() => tone({ freq: f, dur: 0.022, type: "square", vol: 0.35 }), 220 + i * 60));
      // final bell — "carrier locked"
      setTimeout(() => {
        tone({ freq: 1380, dur: 0.20, type: "sine", vol: 0.55, release: 0.12 });
        tone({ freq: 2070, dur: 0.20, type: "sine", vol: 0.30, release: 0.12 });
      }, 560);
    },
    // Access denied — descending sawtooth triplet with sub-tone.
    denied() {
      [{ f: 220, d: 0.18, w: 0 }, { f: 180, d: 0.18, w: 200 }, { f: 140, d: 0.30, w: 400 }]
        .forEach(({ f, d, w }) => setTimeout(() => {
          tone({ freq: f, dur: d, type: "sawtooth", vol: 0.9 });
          tone({ freq: f * 0.5, dur: d, type: "square", vol: 0.3 });
        }, w));
    },

    // ── data motion ──
    // "Receiving data" — a brrrk loop, ~200ms. Use for download start.
    transfer() {
      const t0 = ctx ? ctx.currentTime : 0;
      for (let i = 0; i < 8; i++) {
        setTimeout(() => {
          tone({ freq: 1800 + (i % 2 ? 200 : 0), dur: 0.015, type: "square", vol: 0.4 });
        }, i * 22);
      }
      setTimeout(() => noise({ dur: 0.08, vol: 0.08, hp: 1500, lp: 3500 }), 0);
    },
    // Single decisive chime — operation finished cleanly.
    done() {
      tone({ freq: 740,  dur: 0.05, type: "triangle", vol: 0.45 });
      setTimeout(() => tone({ freq: 1480, dur: 0.06, type: "triangle", vol: 0.5 }), 50);
      setTimeout(() => tone({ freq: 2220, dur: 0.10, type: "sine",     vol: 0.55 }), 110);
    },

    // ── snapshot ──
    // Classic film-camera shutter — punchy, mechanical.
    shutter() {
      // mirror slap
      noise({ dur: 0.04, vol: 0.55, hp: 900, lp: 6000 });
      // shutter close
      setTimeout(() => noise({ dur: 0.05, vol: 0.45, hp: 600, lp: 4500 }), 55);
      // small spring-return click
      setTimeout(() => tone({ freq: 220, dur: 0.018, type: "triangle", vol: 0.4 }), 70);
    },

    // ── live / live-stop ──
    // Power-on whoosh: rising filter-swept noise + low rumble. ~450ms.
    powerOn() {
      // rumble sub
      tone({ freq: 60, dur: 0.45, type: "sine", vol: 0.5, attack: 0.05, release: 0.20, slide: 120, slideTime: 0.45 });
      // whoosh: noise gated, gets brighter as it grows
      noise({ dur: 0.45, vol: 0.18, hp: 200, lp: 4000, env: "attack" });
      // final lock-on blip
      setTimeout(() => stab({ freq: 1320, dur: 0.06, type: "square", vol: 0.6 }), 420);
    },
    // Power-off: falling pitch fade.
    powerOff() {
      tone({ freq: 880, dur: 0.30, type: "sawtooth", vol: 0.45, slide: 80, slideTime: 0.30 });
      tone({ freq: 440, dur: 0.30, type: "sine",     vol: 0.25, slide: 60, slideTime: 0.30 });
    },

    // ── recover scan ──
    // A sweeping ping — used during the recover/reconnect SSE phases.
    // Called per-event so multiple sweeps stack into a "scanning" feel.
    scan() {
      tone({ freq: 600, dur: 0.18, type: "sine", vol: 0.32,
             attack: 0.01, release: 0.06, slide: 1800, slideTime: 0.18 });
    },

    // ── booting / boot-up jingle (for page load) ──
    boot() {
      // arpeggio C-E-G-C up
      [261.6, 329.6, 392.0, 523.3].forEach((f, i) =>
        setTimeout(() => stab({ freq: f, dur: 0.08, type: "square", vol: 0.45 }), i * 70));
      // closing high tick
      setTimeout(() => tone({ freq: 2200, dur: 0.04, type: "sine", vol: 0.5 }), 340);
    },
  };

  // ── auto-wire global UI events so callers don't have to remember.
  // Plain buttons get a click; tabs get a blip. Keyboard "Enter" beeps too.
  // Page authors can still call SFX.X() directly for context-specific cues.
  document.addEventListener("click", (ev) => {
    const t = ev.target;
    if (!t) return;
    if (t.matches && t.matches("button, .btn, .tab, .date-chip, .toast-close")) {
      if (t.classList && t.classList.contains("tab")) SFX.blip();
      else SFX.click();
    }
  }, true);

  global.SFX = SFX;
})(window);
