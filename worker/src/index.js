// Jay Brows 대시보드 → 인스타그램 댓글 답글 프록시 (의존성 없음)
// Secrets: META_TOKEN, REPLY_PASSWORD   Var: ALLOWED_ORIGIN
const GRAPH = "https://graph.facebook.com/v23.0";
const enc = new TextEncoder();

function cors(env, origin) {
  const h = {"Vary": "Origin"};
  if (origin && origin === env.ALLOWED_ORIGIN) {
    h["Access-Control-Allow-Origin"] = origin;
    h["Access-Control-Allow-Methods"] = "POST, OPTIONS";
    h["Access-Control-Allow-Headers"] = "Authorization, Content-Type";
    h["Access-Control-Max-Age"] = "86400";
  }
  return h;
}
const json = (obj, status, h) =>
  new Response(JSON.stringify(obj), {status, headers: {...h, "Content-Type": "application/json; charset=utf-8"}});

// 상수 시간 비교: 두 값을 SHA-256으로 같은 길이로 만든 뒤 XOR 누적
async function safeEqual(a, b) {
  const [x, y] = await Promise.all([a, b].map(s => crypto.subtle.digest("SHA-256", enc.encode(String(s)))));
  const u = new Uint8Array(x), v = new Uint8Array(y);
  let d = 0;
  for (let i = 0; i < u.length; i++) d |= u[i] ^ v[i];
  return d === 0;
}
async function authed(req, env) {
  if (!env.REPLY_PASSWORD) return false;
  const m = (req.headers.get("Authorization") || "").match(/^Bearer (.+)$/);
  return safeEqual(m ? m[1] : "", env.REPLY_PASSWORD).then(ok => ok && !!m);
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

export default {
  async fetch(req, env) {
    const origin = req.headers.get("Origin");
    const h = cors(env, origin);
    if (origin && origin !== env.ALLOWED_ORIGIN) return json({ok: false, error: "origin not allowed"}, 403, h);
    if (req.method === "OPTIONS") return new Response(null, {status: 204, headers: h});
    const path = new URL(req.url).pathname;
    if (req.method !== "POST" || (path !== "/reply" && path !== "/check")) return json({ok: false, error: "not found"}, 404, h);

    if (!(await authed(req, env))) { await sleep(1000); return json({ok: false, error: "unauthorized"}, 401, h); }
    if (path === "/check") return json({ok: true}, 200, h);

    let body;
    try { body = await req.json(); } catch (e) { return json({ok: false, error: "invalid JSON"}, 400, h); }
    const cid = String(body?.comment_id ?? ""), msg = typeof body?.message === "string" ? body.message : "";
    if (!/^\d{1,30}$/.test(cid)) return json({ok: false, error: "invalid comment_id"}, 400, h);
    if (!msg.trim() || msg.length > 2200) return json({ok: false, error: "message must be 1-2200 chars"}, 400, h);
    if (!env.META_TOKEN) return json({ok: false, error: "META_TOKEN not configured"}, 500, h);

    let r, j;
    try {
      r = await fetch(`${GRAPH}/${cid}/replies`, {
        method: "POST",
        headers: {"Content-Type": "application/x-www-form-urlencoded"},
        body: new URLSearchParams({message: msg, access_token: env.META_TOKEN}),
      });
      j = await r.json().catch(() => ({}));
    } catch (e) { return json({ok: false, error: "Meta API 연결 실패"}, 502, h); }
    if (!r.ok || !j.id) {
      const e = j.error || {};
      return json({ok: false, error: e.error_user_msg || e.message || `Meta API error (HTTP ${r.status})`}, r.ok ? 502 : r.status, h);
    }
    return json({ok: true, id: j.id}, 200, h);
  },
};
