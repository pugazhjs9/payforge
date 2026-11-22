// Tiny helpers shared by the dashboard pages. The session cookie is httpOnly, so
// JavaScript never sees it; the browser sends it automatically. Writes also send the
// per-session CSRF token from /auth/me.
let csrfToken = null;

async function api(method, path, body) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (csrfToken && method !== "GET") headers["X-CSRF-Token"] = csrfToken;
  const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) {
    const message = data?.error?.message ?? `Request failed (${res.status})`;
    throw Object.assign(new Error(message), { status: res.status });
  }
  return data;
}

async function loadMe() {
  const me = await api("GET", "/auth/me");
  csrfToken = me.csrf_token;
  return me;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  Object.entries(attrs).forEach(([k, v]) => (k === "onclick" ? (node.onclick = v) : node.setAttribute(k, v)));
  children.forEach((c) => node.append(c));
  return node;
}
