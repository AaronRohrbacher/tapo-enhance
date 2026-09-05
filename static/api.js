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

  async function request(url, options, timeoutMs) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const res = await fetch(url, { ...options, signal: controller.signal });
      return await unpack(res);
    } catch (e) {
      const timedOut = e && e.name === "AbortError";
      return {
        ok: false,
        code: timedOut ? "TIMEOUT" : "NETWORK",
        error: timedOut ? "request timed out" : (e.message || "network request failed"),
        retryable: true,
      };
    } finally {
      clearTimeout(timer);
    }
  }

  async function get(url) {
    return request(url, {}, 45_000);
  }

  async function post(url, body) {
    return request(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      }, 10 * 60_000);
  }

  global.API = { get, post };
})(window);
