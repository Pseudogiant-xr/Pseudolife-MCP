// api.js — fetch client for the console REST surface (/api/*).

let _token = localStorage.getItem("pl_token") || "";
const _listeners = new Set();

export function getToken() { return _token; }
export function setToken(t) {
  _token = t || "";
  if (_token) localStorage.setItem("pl_token", _token);
  else localStorage.removeItem("pl_token");
}
export function onUnauthorized(fn) { _listeners.add(fn); return () => _listeners.delete(fn); }

async function request(method, path, { params, body } = {}) {
  const url = new URL(path, location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v == null || v === "" ) continue;
      url.searchParams.set(k, Array.isArray(v) ? v.join(",") : String(v));
    }
  }
  const headers = {};
  if (_token) headers["Authorization"] = `Bearer ${_token}`;
  if (body) headers["Content-Type"] = "application/json";

  let res;
  try {
    res = await fetch(url, { method, headers, body: body ? JSON.stringify(body) : undefined });
  } catch (netErr) {
    const e = new Error("Network error — is the daemon running?");
    e.code = 0; e.cause = netErr; throw e;
  }

  if (res.status === 401) {
    for (const fn of _listeners) { try { fn(); } catch {} }
    const e = new Error("unauthorized"); e.code = 401; throw e;
  }
  let data = null;
  const text = await res.text();
  if (text) { try { data = JSON.parse(text); } catch { data = { raw: text }; } }
  if (!res.ok) {
    const e = new Error((data && data.error) || res.statusText || `HTTP ${res.status}`);
    e.code = res.status; e.data = data; throw e;
  }
  return data ?? {};
}

export const api = {
  get: (path, params) => request("GET", path, { params }),
  post: (path, body) => request("POST", path, { body }),
};
