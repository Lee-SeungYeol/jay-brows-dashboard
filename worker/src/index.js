// Jay Brows 대시보드 → 인스타그램 댓글 답글 프록시 (의존성 없음)
// Secrets: META_TOKEN, REPLY_PASSWORD   Var: ALLOWED_ORIGIN, (선택) IG_USER_ID
const GRAPH = "https://graph.facebook.com/v23.0";
// 좋아요 API(IG User /likes, instagram_manage_engagement)는 2026-04 신설 — Meta 문서 예시 버전 사용
const GRAPH_LIKES = "https://graph.facebook.com/v26.0";
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
const metaErr = (j, r) => { const e = (j && j.error) || {}; return e.error_user_msg || e.message || `Meta API error (HTTP ${r.status})`; };

// 좋아요를 누를 인스타 비즈니스 계정 id: IG_USER_ID 변수 우선, 없으면 토큰으로 한 번 찾아서 캐시
let igIdCache = "";
async function igUserId(env) {
  if (env.IG_USER_ID) return String(env.IG_USER_ID);
  if (igIdCache) return igIdCache;
  const auth = {headers: {"Authorization": "Bearer " + env.META_TOKEN}};
  const tries = [
    [`${GRAPH}/me?fields=instagram_business_account`, j => j.instagram_business_account?.id],
    [`${GRAPH}/me/accounts?fields=instagram_business_account`, j => (j.data || []).map(p => p.instagram_business_account?.id).find(Boolean)],
  ];
  for (const [u, pick] of tries) {
    try { const r = await fetch(u, auth); const id = pick(await r.json().catch(() => ({}))); if (id) return (igIdCache = String(id)); } catch (e) {}
  }
  return "";
}

export default {
  async fetch(req, env) {
    const origin = req.headers.get("Origin");
    const h = cors(env, origin);
    if (origin && origin !== env.ALLOWED_ORIGIN) return json({ok: false, error: "origin not allowed"}, 403, h);
    if (req.method === "OPTIONS") return new Response(null, {status: 204, headers: h});
    const path = new URL(req.url).pathname;
    if (req.method !== "POST" || !["/reply", "/check", "/like"].includes(path)) return json({ok: false, error: "not found"}, 404, h);

    if (!(await authed(req, env))) { await sleep(1000); return json({ok: false, error: "unauthorized"}, 401, h); }
    if (path === "/check") return json({ok: true}, 200, h);

    let body;
    try { body = await req.json(); } catch (e) { return json({ok: false, error: "invalid JSON"}, 400, h); }
    if (path === "/like") return like(body, env, h);
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
    if (!r.ok || !j.id) return json({ok: false, error: metaErr(j, r)}, r.ok ? 502 : r.status, h);
    return json({ok: true, id: j.id}, 200, h);
  },
};

// POST /like {comment_id, unlike?} → POST/DELETE /{IG_USER_ID}/likes {comment_id} (토큰은 헤더로만)
async function like(body, env, h) {
  const cid = String(body?.comment_id ?? ""), unlike = body?.unlike === true;
  if (!/^\d{1,30}$/.test(cid)) return json({ok: false, error: "invalid comment_id"}, 400, h);
  if (body?.unlike != null && typeof body.unlike !== "boolean") return json({ok: false, error: "unlike must be boolean"}, 400, h);
  if (!env.META_TOKEN) return json({ok: false, error: "META_TOKEN not configured"}, 500, h);
  const ig = await igUserId(env);
  if (!/^\d{1,30}$/.test(ig)) return json({ok: false, error: "IG_USER_ID not configured"}, 500, h);
  let r, j;
  try {
    r = await fetch(`${GRAPH_LIKES}/${ig}/likes`, {
      method: unlike ? "DELETE" : "POST",
      headers: {"Authorization": "Bearer " + env.META_TOKEN, "Content-Type": "application/json"},
      body: JSON.stringify({comment_id: cid}),
    });
    j = await r.json().catch(() => ({}));
  } catch (e) { return json({ok: false, error: "Meta API 연결 실패"}, 502, h); }
  if (!r.ok || j.success !== true) return json({ok: false, error: metaErr(j, r)}, r.ok ? 502 : r.status, h);
  return json({ok: true}, 200, h);
}
