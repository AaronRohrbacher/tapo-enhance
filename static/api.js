// Tiny API wrapper. Every JSON response goes through `unpack()` which
// produces a uniform { ok, data, code, error, retryable } shape. The UI
// never has to substring-match error strings.

(function (global) {
  "use strict";

  async function unpack(res) {
    let body = null;
    try {
      body = await res.json();
    } catch (e) {
      return { ok: false, code: "INTERNAL", error: `bad json from server (${res.status})`, retryable: false };
    }
    if (body.ok === true) {
      return { ok: true, data: body, code: null, error: null, retryable: false };
    }
    return {
      ok: false,
      code: body.code || "INTERNAL",
      error: body.error || `HTTP ${res.status}`,
      retryable: !!body.retryable,
      data: body,
    };
  }

  async function get(url) {
    try {
      const res = await fetch(url);
      return await unpack(res);
    } catch (e) {
      return { ok: false, code: "NETWORK", error: e.message, retryable: true };
    }
  }

  async function post(url, body) {
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
      return await unpack(res);
    } catch (e) {
      return { ok: false, code: "NETWORK", error: e.message, retryable: true };
    }
  }

  global.API = { get, post };
})(window);
