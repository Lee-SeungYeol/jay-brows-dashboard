"""Fetch Instagram + Facebook Page stats/comments via the Meta Graph API.

Writes site/data.json. Runs in GitHub Actions; token comes from the
META_TOKEN secret (a Page access token, or a user token with page access).
"""
import bisect
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

API = "https://graph.facebook.com/" + os.environ.get("GRAPH_VERSION", "v23.0")
TOKEN = os.environ.get("META_TOKEN", "").strip()
POST_LIMIT = int(os.environ.get("POST_LIMIT", "25"))
COMMENT_LIMIT = int(os.environ.get("COMMENT_LIMIT", "300"))
LEVEL_STRONG, LEVEL_MID = 100, 50  # 조회수 급상승 level by views gained per hour (강함 ≥ 100, 보통 ≥ 50)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site", "data.json")

errors = []


def get(path, token, **params):
    params["access_token"] = token
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(body)["error"]["message"]
        except Exception:
            msg = body[:300]
        raise RuntimeError(f"{path}: {msg}") from None


def paged(path, token, cap, **params):
    """Follow paging.next until cap items are collected."""
    first = get(path, token, **params)
    items, nxt = first.get("data", []), first.get("paging", {}).get("next")
    while nxt and len(items) < cap:
        try:
            with urllib.request.urlopen(nxt, timeout=30) as r:
                page = json.load(r)
        except urllib.error.HTTPError:
            break
        items += page.get("data", [])
        nxt = page.get("paging", {}).get("next")
    return items[:cap]


def soft(fn, default=None):
    """Run fn, record the error instead of failing the whole run."""
    try:
        return fn()
    except Exception as e:
        errors.append(str(e))
        return default


def resolve_page():
    """Return (page_id, page_token) for either a user token or a page token."""
    accounts = soft(lambda: get("me/accounts", TOKEN, fields="id,name,access_token"))
    if accounts and accounts.get("data"):
        wanted = os.environ.get("PAGE_ID")
        pages = accounts["data"]
        page = next((p for p in pages if p["id"] == wanted), pages[0])
        return page["id"], page["access_token"]
    wanted = os.environ.get("PAGE_ID")
    if wanted:
        # Business-managed pages don't show up in me/accounts; ask for the page directly.
        page = soft(lambda: get(wanted, TOKEN, fields="id,access_token"))
        if page and page.get("access_token"):
            errors.clear()
            return page["id"], page["access_token"]
    me = get("me", TOKEN, fields="id")
    errors.clear()  # me/accounts fails for page tokens; that's expected
    return me["id"], TOKEN


def fb_comments(post_id, tok):
    try:
        # filter=stream returns replies too, flattened in time order.
        rows = paged(
            f"{post_id}/comments", tok, COMMENT_LIMIT, limit=100, filter="stream",
            order="reverse_chronological", fields="id,from,message,created_time,like_count,parent{id}",
        )
    except RuntimeError as e:
        # Some posts (e.g. shared/collab) need App Review to read comments; skip quietly.
        if "(#10)" not in str(e):
            errors.append(str(e))
        return []
    return [{
        "id": c.get("id"),
        "user": (c.get("from") or {}).get("name", "익명"),
        "text": c.get("message", ""),
        "time": c.get("created_time"),
        "likes": c.get("like_count", 0),
        "reply": bool(c.get("parent")),
        "parent": (c.get("parent") or {}).get("id"),
    } for c in rows]


FB_REACTIONS = ("LIKE", "LOVE", "WOW", "HAHA", "SAD", "ANGRY", "CARE")
FB_BASE_FIELDS = ("id,message,created_time,permalink_url,full_picture,status_type,shares,"
                  "reactions.summary(total_count).limit(0),comments.summary(total_count).limit(0)")
FB_BREAKDOWN = "".join(f",reactions.type({r}).limit(0).summary(total_count).as(r_{r.lower()})"
                       for r in FB_REACTIONS)
# Candidate metrics; ones the API rejects are dropped (supported set cached per day in store).
FB_POST_METRICS = {
    "post": "post_impressions_unique,post_impressions,post_clicks,post_reactions_by_type_total,"
            "post_media_view,post_total_media_view_unique",
    "video": "post_impressions_unique,post_impressions,post_clicks,post_reactions_by_type_total,"
             "post_media_view,post_total_media_view_unique,post_video_views,post_video_views_unique,"
             "post_video_avg_time_watched,post_video_view_time",
}
FB_PAGE_METRICS = ("page_impressions_unique,page_post_engagements,page_views_total,page_fan_adds,"
                   "page_follows,page_daily_follows_unique,page_media_view,page_total_media_view_unique")
FB_COMMENT_TTL = 3600  # latest posts: re-read comments when the count changes, else hourly


def fb_parse(rows):
    """{metric: value} from the last value of each insights row (+ full series for multi-day rows)."""
    out, series = {}, {}
    for r in rows:
        vals = r.get("values") or []
        if vals:
            out[r["name"]] = vals[-1].get("value")
            series[r["name"]] = vals
    return out, series


def fb_insights(obj_id, key, candidates, tok, cache, state, **params):
    """Insights for obj_id; unsupported metrics are removed (probed one by one) and remembered for a day."""
    today = datetime.now(timezone.utc).date().isoformat()
    bad = cache.setdefault("bad", {})  # objects that returned no insights at all today
    if bad.get(obj_id) == today:
        return {}, {}
    ent = cache.get(key)
    fresh = bool(ent) and ent.get("t", "")[:10] == today
    if fresh:
        try:
            return fb_parse(get(f"{obj_id}/insights", tok, metric=",".join(ent["ok"]), **params).get("data", []))
        except RuntimeError:
            pass  # object-specific failure (e.g. shared post), or a metric just got retired
    # Probe a bounded number of times per run, so a permission problem can't explode the call count.
    if state["probes"].get(key, 0) >= (1 if fresh else 3):
        bad[obj_id] = today
        return {}, {}
    state["probes"][key] = state["probes"].get(key, 0) + 1
    metrics = candidates.split(",")
    try:
        res = fb_parse(get(f"{obj_id}/insights", tok, metric=",".join(metrics), **params).get("data", []))
        cache[key] = {"t": today, "ok": metrics}
        state["probes"][key] = 99  # supported set known; no more probing this run
        return res
    except RuntimeError:
        pass
    ok, out, series = [], {}, {}
    for m in metrics:
        try:
            o, s = fb_parse(get(f"{obj_id}/insights", tok, metric=m, **params).get("data", []))
        except RuntimeError:
            continue
        ok.append(m)
        out.update(o)
        series.update(s)
    if ok:
        cache[key] = {"t": today, "ok": ok}
        state["probes"][key] = 99
    else:  # nothing worked => probably this object, not the metrics
        bad[obj_id] = today
    return out, series


def fb_post_row(p):
    st = p.get("status_type") or ""
    kind = "VIDEO" if "video" in st else "PHOTO" if "photo" in st else "SHARE" if "shared" in st else "STATUS"
    by_type = {r: p[f"r_{r.lower()}"].get("summary", {}).get("total_count", 0)
               for r in FB_REACTIONS if f"r_{r.lower()}" in p}
    return {
        "id": p["id"],
        "kind": kind,
        "status_type": st or None,
        "text": p.get("message", ""),
        "time": p.get("created_time"),
        "url": p.get("permalink_url"),
        "image": p.get("full_picture"),
        "likes": p.get("reactions", {}).get("summary", {}).get("total_count", 0),
        "reactions_by_type": by_type or None,
        "comments_count": p.get("comments", {}).get("summary", {}).get("total_count", 0),
        "shares": p.get("shares", {}).get("count", 0),
    }


def fb_all_posts(page_id, tok):
    """Every page post; falls back to smaller pages / no reaction breakdown if the query is too heavy."""
    last = None
    for fields, limit in ((FB_BASE_FIELDS + FB_BREAKDOWN, 100), (FB_BASE_FIELDS + FB_BREAKDOWN, 25),
                          (FB_BASE_FIELDS, 100)):
        try:
            return paged(f"{page_id}/posts", tok, 10000, limit=limit, fields=fields)
        except RuntimeError as e:
            last = e
    raise last


def facebook(page_id, tok):
    page = get(page_id, tok, fields="id,name,fan_count,followers_count,link,picture{url}")
    posts = [fb_post_row(p) for p in soft(lambda: fb_all_posts(page_id, tok), [])]
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    mcache = load("fb_metrics.json", {})
    state = {"probes": {}}

    # Insights + comments: latest POST_LIMIT every run, plus a rotating slice of older posts.
    detail = load("fb_detail.json", {})
    cursor = load("fb_cursor.json", 0)
    older = posts[POST_LIMIT:]
    if older:
        cursor %= len(older)
    rotating = older[cursor:cursor + ROTATE]
    save("fb_cursor.json", cursor + ROTATE)
    for i, p in enumerate(posts[:POST_LIMIT] + rotating):
        d = detail.get(p["id"], {})
        kind = "video" if p["kind"] == "VIDEO" else "post"
        ins, _ = fb_insights(p["id"], kind, FB_POST_METRICS[kind], tok, mcache, state)
        if ins or "insights" not in d:
            d["insights"] = ins
        d["checked"] = now
        stale = True
        if i < POST_LIMIT and d.get("comments_seen") == p["comments_count"] and d.get("comments_checked"):
            age = (now_dt - datetime.fromisoformat(d["comments_checked"])).total_seconds()
            stale = age > FB_COMMENT_TTL
        if stale:
            d["comments"] = fb_comments(p["id"], tok) if p["comments_count"] else []
            d["comments_seen"] = p["comments_count"]
            d["comments_checked"] = now
        detail[p["id"]] = d
    save("fb_detail.json", detail)
    for p in posts:
        d = detail.get(p["id"], {})
        p["insights"] = d.get("insights") or None
        p["checked"] = d.get("checked")
        p["comments"] = d.get("comments", [])

    # Page-level daily insights for the last 90 days (buckets are Meta's day, labelled by day covered).
    _, series = fb_insights(page_id, "page", FB_PAGE_METRICS, tok, mcache, state, period="day",
                            since=int((now_dt - timedelta(days=90)).timestamp()), until=int(now_dt.timestamp()))
    today_utc = now_dt.date().isoformat()
    mcache["bad"] = {k: v for k, v in mcache.get("bad", {}).items() if v == today_utc}
    save("fb_metrics.json", mcache)
    daily = {}
    for m, vals in series.items():
        for v in vals:
            if "end_time" not in v:
                continue
            end = datetime.strptime(v["end_time"][:19], "%Y-%m-%dT%H:%M:%S")
            daily.setdefault((end - timedelta(days=1)).date().isoformat(), {})[m] = v.get("value")
    daily_list = [{"date": k, **daily[k]} for k in sorted(daily)]
    kst = timezone(timedelta(hours=9))
    today_kst = datetime.now(kst).date()
    today = daily.get(today_kst.isoformat(), {})
    yesterday = daily.get((today_kst - timedelta(days=1)).isoformat(), {})
    if not yesterday and daily_list:  # Meta's day lags KST; fall back to the latest finished day
        yesterday = {k: v for k, v in daily_list[-1].items() if k != "date"}
    errors[:] = [e for e in errors if "/insights" not in e]  # unsupported metrics are expected

    # All-time totals across every post (reaction/comment counts are always fresh; insights cover checked posts).
    with_ins = [p for p in posts if p.get("insights")]
    by_type = {r: sum((p["reactions_by_type"] or {}).get(r, 0) for p in posts) for r in FB_REACTIONS}
    totals = {
        "posts": len(posts),
        "reactions": sum(p["likes"] or 0 for p in posts),
        "comments": sum(p["comments_count"] or 0 for p in posts),
        "shares": sum(p["shares"] or 0 for p in posts),
        "reactions_by_type": by_type if any(p["reactions_by_type"] for p in posts) else None,
        "insights_coverage": len(with_ins),
    }
    for m in ("post_impressions_unique", "post_impressions", "post_clicks", "post_media_view",
              "post_total_media_view_unique", "post_video_views"):
        vals = [p["insights"][m] for p in with_ins if isinstance(p["insights"].get(m), (int, float))]
        if vals:
            totals[m] = sum(vals)

    # Hourly snapshot of the headline numbers.
    snap = {"t": now, "followers": page.get("followers_count"), "fans": page.get("fan_count"),
            "posts": totals["posts"], "reactions": totals["reactions"], "comments": totals["comments"],
            "shares": totals["shares"]}
    hist = load("fb_history.json", [])
    if not hist or hist[-1]["t"][:13] != now[:13]:
        hist.append(snap)
    save("fb_history.json", hist)

    return {
        "name": page.get("name"),
        "link": page.get("link"),
        "picture": page.get("picture", {}).get("data", {}).get("url"),
        "followers": page.get("followers_count"),
        "fans": page.get("fan_count"),
        "today": today,
        "yesterday": yesterday,
        "daily": daily_list,
        "totals": totals,
        "follower_history": hist,
        "posts": posts,
    }


STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "store")
ROTATE = int(os.environ.get("ROTATE", "25"))  # older posts refreshed in full per run

POST_METRICS = {
    "REELS": "reach,saved,shares,views,total_interactions,likes,comments,ig_reels_avg_watch_time,ig_reels_video_view_total_time",
    "FEED": "reach,saved,shares,views,total_interactions,likes,comments,profile_visits,follows",
}


def load(name, default):
    try:
        with open(os.path.join(STORE, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(name, obj):
    os.makedirs(STORE, exist_ok=True)
    with open(os.path.join(STORE, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


IG_OK_METRICS = {}  # kind -> metric list that worked last time (persisted in store)


def insights(media_id, kind, tok):
    """Per-media insights; metrics a media type doesn't support are dropped and retried."""
    metrics = list(IG_OK_METRICS.get(kind) or POST_METRICS[kind].split(","))
    for _ in range(len(metrics)):
        try:
            rows = get(f"{media_id}/insights", tok, metric=",".join(metrics)).get("data", [])
            out = {}
            for r in rows:
                v = r.get("total_value", {}).get("value") if "total_value" in r else (r.get("values") or [{}])[0].get("value")
                out[r["name"]] = v
            IG_OK_METRICS.setdefault(kind, metrics)
            return out
        except RuntimeError as e:
            bad = next((m for m in metrics if m in str(e)), None)
            if not bad:
                return {}
            metrics.remove(bad)
    return {}


def ig_views_all(ids, tok):
    """Current `views` for every media id via the Graph batch API (50 per request, in parallel).
    Per-post failures are skipped; a failed batch falls back to one call per post."""
    def one(mid):
        try:
            rows = get(f"{mid}/insights", tok, metric="views").get("data", [])
        except RuntimeError:
            return mid, None
        return mid, _views_value(rows)

    def chunk(ids50):
        body = urllib.parse.urlencode({
            "access_token": tok, "include_headers": "false",
            "batch": json.dumps([{"method": "GET", "relative_url": f"{i}/insights?metric=views"} for i in ids50]),
        }).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(API + "/", data=body), timeout=60) as r:
                res = json.load(r)
        except Exception:
            return [one(i) for i in ids50]
        out = []
        for mid, item in zip(ids50, res):
            v = None
            if item and item.get("code") == 200:
                try:
                    v = _views_value(json.loads(item.get("body") or "{}").get("data", []))
                except ValueError:
                    pass
            out.append((mid, v))
        return out

    chunks = [ids[i:i + 50] for i in range(0, len(ids), 50)]
    views = {}
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(chunks)))) as pool:
        for rows in pool.map(chunk, chunks):
            views.update({mid: v for mid, v in rows if isinstance(v, (int, float))})
    return views


def _views_value(rows):
    for r in rows:
        if r.get("name") == "views":
            return r.get("total_value", {}).get("value") if "total_value" in r else (r.get("values") or [{}])[0].get("value")
    return None


WINDOW_MIN, KEEP_MIN = 60, 75


def ig_growth(posts, views, last, now):
    """Top 10 posts by views gained over the last 60 minutes (rolling window).

    store/ig_views_window.json = [{t, views:{id: n}}, ...], one snapshot per run,
    pruned to the last ~75 minutes. Baseline = newest snapshot at least 60 min old;
    until one exists, the oldest snapshot available (window_min says how long).
    """
    tnow = datetime.fromisoformat(now)
    snaps = load("ig_views_window.json", [])
    if not snaps and last and last.get("views") and last.get("t"):
        snaps = [{"t": last["t"], "views": last["views"]}]  # seed from the previous run
    snaps = [s for s in snaps if (tnow - datetime.fromisoformat(s["t"])).total_seconds() <= KEEP_MIN * 60
             and s["t"] < now]
    snaps.sort(key=lambda s: datetime.fromisoformat(s["t"]))
    old = [s for s in snaps if (tnow - datetime.fromisoformat(s["t"])).total_seconds() >= WINDOW_MIN * 60]
    base = old[-1] if old else (snaps[0] if snaps else None)
    save("ig_views_window.json", snaps + [{"t": now, "views": {k: v for k, v in views.items() if v is not None}}])
    prev = (base or {}).get("views") or {}
    since = (base or {}).get("t")
    mins = round((tnow - datetime.fromisoformat(since)).total_seconds() / 60, 1) if since else None
    rows = []
    for p in posts:
        v, pv = views.get(p["id"]), prev.get(p["id"])
        if v is None or pv is None:
            continue
        dv = v - pv
        rows.append({"p": p, "views": v, "views_prev": pv, "dv": dv,
                     "rate": round(dv / pv * 100, 2) if pv else None})
    grow = sorted(r["dv"] for r in rows if r["dv"] > 0)
    n = len(grow)

    def per_hour(dv):
        # window shorter than ~1h (still collecting): scale the pace so far to one hour
        return dv if not mins or mins >= 55 else round(dv * 60 / mins, 1)

    def level(n):
        return "강함" if n >= LEVEL_STRONG else "보통" if n >= LEVEL_MID else "약함"

    top = sorted((r for r in rows if r["dv"] > 0), key=lambda r: (r["dv"], r["rate"] or 0), reverse=True)[:10]
    out = [{
        "id": r["p"]["id"], "kind": r["p"]["kind"], "type": r["p"]["type"],
        "text": (r["p"].get("text") or "")[:80], "url": r["p"]["url"], "image": r["p"]["image"],
        "time": r["p"]["time"], "views": r["views"], "views_prev": r["views_prev"], "dv": r["dv"],
        "rate": r["rate"], "per_hour": per_hour(r["dv"]), "level": level(per_hour(r["dv"])),
    } for r in top]
    median = None
    if n:
        median = grow[n // 2] if n % 2 else (grow[n // 2 - 1] + grow[n // 2]) / 2
    meta = {"since": since, "window_min": mins, "posts_with_growth": n, "median_dv": median,
            "posts_compared": len(rows)}
    return out, meta


DAILY_KEYS = ("reach", "views", "profile_views", "accounts_engaged")
DELTA_KEYS = ("followers", "likes", "comments", "shares", "saves", "avg_reaction", "avg_likes",
              "avg_views", "views_total", "posts") + DAILY_KEYS
DELTA_WINDOW_MIN = 60


def ig_hour_delta(runs, row):
    """Badge changes over a rolling hour: value now minus the newest run at least 60 min old
    (or the oldest run available while history is shorter). Per key, only runs that have it count."""
    kst = timezone(timedelta(hours=9))
    tnow = datetime.fromisoformat(row["t"])
    cutoff = tnow - timedelta(minutes=DELTA_WINDOW_MIN)
    past = sorted((r for r in runs[:-1] if r.get("t")), key=lambda r: datetime.fromisoformat(r["t"]))
    if not past:
        return None
    day = tnow.astimezone(kst).date().isoformat()

    def base_for(k):
        have = [r for r in past if r.get(k) is not None]
        if not have:
            return None
        old = [r for r in have if datetime.fromisoformat(r["t"]) <= cutoff]
        return old[-1] if old else have[0]

    delta, since = {}, None
    for k in DELTA_KEYS:
        if row.get(k) is None:
            continue
        b = base_for(k)
        if b is None:
            continue
        if k in DAILY_KEYS and datetime.fromisoformat(b["t"]).astimezone(kst).date().isoformat() != day:
            continue  # daily metrics reset at midnight KST
        if k in ("shares", "saves", "avg_reaction") and b.get("coverage") != row.get("coverage"):
            continue  # insight coverage changed in the window: totals moved by themselves
        delta[k] = round(row[k] - b[k], 2)
        if since is None or b["t"] < since:
            since = b["t"]
    if since is None:
        return None
    delta["since"] = since
    delta["window_min"] = round((tnow - datetime.fromisoformat(since)).total_seconds() / 60, 1)
    return delta


HV_TOL = timedelta(minutes=10)


def _hv_at(pts, b):
    """views_total at KST hour boundary b: first run at/after b within 10 min, else the closest earlier run within 10 min."""
    i = bisect.bisect_left(pts, (b,))
    if i < len(pts) and pts[i][0] - b <= HV_TOL:
        return pts[i]
    if i > 0 and b - pts[i - 1][0] <= HV_TOL:
        return pts[i - 1]
    return None


def ig_hourly_views(runs):
    """Store ig_hourly_views.json = {"YYYY-MM-DD": {"HH": gain}} (KST): views_total gain per completed clock hour."""
    kst = timezone(timedelta(hours=9))
    store = load("ig_hourly_views.json", {})
    pts = sorted((datetime.fromisoformat(r["t"]), r["views_total"]) for r in runs
                 if r.get("t") and r.get("views_total") is not None)
    now = datetime.now(timezone.utc)
    cur_h = now.astimezone(kst).replace(minute=0, second=0, microsecond=0)
    so_far = None
    if pts:
        h = pts[0][0].astimezone(kst).replace(minute=0, second=0, microsecond=0)
        while h + timedelta(hours=1) <= cur_h:
            day, hh = h.date().isoformat(), f"{h.hour:02d}"
            e = h + timedelta(hours=1)
            if hh not in store.get(day, {}) and now >= e + HV_TOL:
                a, b = _hv_at(pts, h), _hv_at(pts, e)
                if a and b and (b[0] - a[0]) >= timedelta(minutes=50):
                    store.setdefault(day, {})[hh] = max(0, b[1] - a[1])
            h = e
        a = _hv_at(pts, cur_h)
        if a and pts[-1][0] >= a[0]:
            so_far = max(0, pts[-1][1] - a[1])
    store = {d: dict(sorted(v.items())) for d, v in sorted(store.items())}
    save("ig_hourly_views.json", store)
    avg, n = [], []
    for i in range(24):
        vals = [v[f"{i:02d}"] for v in store.values() if f"{i:02d}" in v]
        n.append(len(vals))
        avg.append(round(sum(vals) / len(vals)) if vals else None)
    return {"days": store, "avg": avg, "n": n, "current_hour": cur_h.hour, "current_hour_so_far": so_far}


def ig_media_row(m):
    kind = "REELS" if m.get("media_product_type") == "REELS" else "FEED"
    return {
        "id": m["id"],
        "kind": kind,
        "type": m.get("media_type"),
        "text": m.get("caption", ""),
        "time": m.get("timestamp"),
        "url": m.get("permalink"),
        "image": m.get("thumbnail_url") or m.get("media_url"),
        "likes": m.get("like_count", 0),
        "comments_count": m.get("comments_count", 0),
        "collab": bool(m.get("collaborators")) if "collaborators" in m else None,
    }


def instagram(page_id, tok):
    ig_ref = get(page_id, tok, fields="instagram_business_account").get("instagram_business_account")
    if not ig_ref:
        errors.append("페이스북 페이지에 연결된 인스타그램 프로페셔널 계정이 없습니다.")
        return None
    ig_id = ig_ref["id"]
    prof = get(ig_id, tok, fields="username,name,followers_count,follows_count,media_count,profile_picture_url")

    # Every post we own (feed, carousel, reels); counts refresh every run.
    fields = ("id,caption,media_type,media_product_type,media_url,thumbnail_url,permalink,"
              "timestamp,like_count,comments_count")
    media = soft(lambda: paged(f"{ig_id}/media", tok, 10000, limit=100, fields=fields), [])
    posts = [ig_media_row(m) for m in media]

    # Comments + insights: latest POST_LIMIT every run, plus a rotating slice of older posts.
    detail = load("ig_detail.json", {})
    cursor = load("ig_cursor.json", 0)
    older = posts[POST_LIMIT:]
    if older:
        cursor %= len(older)
    targets = posts[:POST_LIMIT] + older[cursor:cursor + ROTATE]
    save("ig_cursor.json", cursor + ROTATE)
    now = datetime.now(timezone.utc).isoformat()
    IG_OK_METRICS.update(load("ig_metrics.json", {}))

    def refresh(p):
        old = detail.get(p["id"], {})
        d = {"insights": insights(p["id"], p["kind"], tok) or old.get("insights"), "checked": now}
        # Comments: re-read when the count changed, otherwise at most hourly.
        # Cached lists from before comment ids were collected count as stale (needed for reply deep links).
        fresh = (old.get("comments_seen") == p["comments_count"] and old.get("comments_checked", "")[:13] == now[:13]
                 and all(c.get("id") for c in old.get("comments", [])))
        if fresh:
            d.update({k: old[k] for k in ("comments", "comments_seen", "comments_checked") if k in old})
        else:
            d["comments"] = soft(lambda: ig_comments(p["id"], tok), []) if p["comments_count"] else []
            d["comments_seen"], d["comments_checked"] = p["comments_count"], now
        return p["id"], d

    # Calls are independent, so run them in parallel (one at a time took ~7 min per update).
    with ThreadPoolExecutor(max_workers=16) as pool:
        for pid, d in pool.map(refresh, targets):
            detail[pid] = d
    save("ig_metrics.json", IG_OK_METRICS)
    for p in posts:
        p.update(detail.get(p["id"], {}))
    save("ig_detail.json", detail)

    # Current views for every post+reel, every run (one batch call per 50 posts).
    t_views = datetime.now(timezone.utc)
    views = soft(lambda: ig_views_all([p["id"] for p in posts], tok), {}) or {}
    views_sec = round((datetime.now(timezone.utc) - t_views).total_seconds(), 1)
    for p in posts:
        v = views.get(p["id"])
        if v is None:
            continue
        p["views"] = v
        if p.get("insights"):
            p["insights"] = {**p["insights"], "views": v}

    # Account-level insights.
    reach = soft(lambda: get(f"{ig_id}/insights", tok, metric="reach", period="day").get("data", []), [])
    daily_reach = [{"date": v["end_time"][:10], "value": v["value"]}
                   for d in reach for v in d.get("values", [])]
    def acct(metric, **extra):
        r = soft(lambda: get(f"{ig_id}/insights", tok, metric=metric, period="day",
                             metric_type="total_value", **extra).get("data", []), [])
        return metric, (r[0].get("total_value", {}).get("value") if r else None)

    with ThreadPoolExecutor(max_workers=13) as pool:
        totals = {m: v for m, v in pool.map(acct, (
            "reach", "views", "profile_views", "accounts_engaged", "total_interactions",
            "likes", "comments", "shares", "saves", "replies", "follows_and_unfollows",
            "website_clicks", "profile_links_taps")) if v is not None}
    errors[:] = [e for e in errors if "/insights" not in e]  # unsupported metrics are expected

    # Yesterday's full-day totals (KST), shown until our own history covers 24h.
    kst = timezone(timedelta(hours=9))
    midnight = datetime.now(kst).replace(hour=0, minute=0, second=0, microsecond=0)
    span = {"since": int((midnight - timedelta(days=1)).timestamp()), "until": int(midnight.timestamp())}
    with ThreadPoolExecutor(max_workers=4) as pool:
        yesterday = {m: v for m, v in pool.map(lambda m: acct(m, **span),
                                               ("reach", "views", "profile_views", "accounts_engaged")) if v is not None}
    errors[:] = [e for e in errors if "/insights" not in e]

    # All-time totals across every post and reel (new posts are included automatically).
    # Shares/saves come from per-post insights, so they cover posts checked so far.
    with_ins = [p for p in posts if p.get("insights")]
    totals_all = {
        "posts": len(posts),
        "likes": sum(p["likes"] or 0 for p in posts),
        "comments": sum(p["comments_count"] or 0 for p in posts),
        "shares": sum(p["insights"].get("shares") or 0 for p in with_ins),
        "saves": sum(p["insights"].get("saved") or 0 for p in with_ins),
        "insights_coverage": len(with_ins),
    }
    # 게시물당 평균 반응 = (likes + comments + shares + saves) ÷ posts, over every post and reel we own.
    # Shares/saves are scaled up from the posts whose insights we have until every post is covered.
    n, cov = totals_all["posts"], totals_all["insights_coverage"]
    own = totals_all["likes"] + totals_all["comments"] + (
        (totals_all["shares"] + totals_all["saves"]) * n / cov if cov else 0)
    totals_all["avg_reaction"] = round(own / n, 2) if n else None
    # 게시물당 평균 좋아요 = likes ÷ posts, over every feed post and reel.
    totals_all["avg_likes"] = round(totals_all["likes"] / totals_all["posts"], 2) if totals_all["posts"] else None

    # Hourly snapshot of the headline numbers; the page diffs against ~24h ago.
    snap = {"t": now, "followers": prof.get("followers_count"), "follows": prof.get("follows_count"),
            "media": prof.get("media_count"), "likes": totals_all["likes"], "comments": totals_all["comments"],
            "shares": totals_all["shares"], "saves": totals_all["saves"]}
    snap.update({k: totals.get(k) for k in ("reach", "views", "profile_views", "accounts_engaged")})
    # Change since the previous run (the page shows it as +N next to each number).
    # Shares/saves only compare posts that had insights both times, so filling in
    # older posts' insights doesn't look like new activity.
    day = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    cur_posts = {p["id"]: [p["insights"].get("shares") or 0, p["insights"].get("saved") or 0]
                 for p in with_ins}
    cur = {"followers": prof.get("followers_count"), "likes": totals_all["likes"],
           "comments": totals_all["comments"], "avg_reaction": totals_all["avg_reaction"],
           "posts": totals_all["posts"], "avg_likes": totals_all["avg_likes"]}
    cur.update({k: totals.get(k) for k in ("reach", "views", "profile_views", "accounts_engaged")})
    last = load("ig_last.json", None)
    prev_views = (last or {}).get("views") or {}
    views_store = {p["id"]: views.get(p["id"], prev_views.get(p["id"])) for p in posts}
    views_store = {k: v for k, v in views_store.items() if v is not None}
    # Posts missing this run keep their previous value, so a failed call never looks like a drop.
    # A few media never return views (API doesn't support them); they're simply not counted.
    views_total = sum(views_store.values()) if views_store else None
    totals_all["views_total"] = views_total
    cur["views_total"] = views_total
    # 게시물당 평균 조회수 = views_total ÷ number of feed posts + reels counted in it.
    avg_views = round(views_total / len(views_store), 2) if views_store else None
    totals_all["avg_views"] = avg_views
    cur["avg_views"] = avg_views
    top_growth, top_meta = ig_growth(posts, views, last, now)
    top_meta.update({"views_fetched": len(views), "views_seconds": views_sec})
    save("ig_last.json", {"t": now, "day": day, "values": cur, "posts": cur_posts, "views": views_store})

    # Per-update series for the charts (one point per run, ~every 3 min; kept forever).
    runs = load("ig_runs.json", [])
    row = {"t": now, "followers": cur["followers"], "likes": cur["likes"], "comments": cur["comments"],
           "shares": totals_all["shares"], "saves": totals_all["saves"], "posts": totals_all["posts"],
           "avg_reaction": totals_all["avg_reaction"], "coverage": totals_all["insights_coverage"],
           "views_total": views_total, "avg_views": avg_views, "avg_likes": totals_all["avg_likes"]}
    row.update({k: totals.get(k) for k in DAILY_KEYS})
    runs.append(row)
    delta = ig_hour_delta(runs, row)
    save("ig_runs.json", runs)
    hourly = soft(lambda: ig_hourly_views(runs))

    hist = load("ig_followers.json", [])
    if not hist or hist[-1]["t"][:13] != now[:13]:
        hist.append(snap)
    save("ig_followers.json", hist)

    return {
        "username": prof.get("username"),
        "name": prof.get("name"),
        "picture": prof.get("profile_picture_url"),
        "followers": prof.get("followers_count"),
        "follows": prof.get("follows_count"),
        "media_count": prof.get("media_count"),
        "daily_reach": daily_reach,
        "today": totals,
        "yesterday": yesterday,
        "totals": totals_all,
        "delta": delta,
        "top_growth": top_growth,
        "top_growth_meta": top_meta,
        "runs": runs,
        "hourly_views": hourly,
        "follower_history": hist,
        "posts": posts,
    }


def ig_comments(media_id, tok):
    """Top-level comments plus their replies, flattened."""
    base = "id,username,text,timestamp,like_count"
    rows = None
    # Busy posts can trip "Please reduce the amount of data": shrink the page, then drop the nested replies.
    for lim in (50, 25, 10):
        try:
            rows = paged(f"{media_id}/comments", tok, COMMENT_LIMIT, limit=lim, fields=f"{base},replies{{{base}}}")
            break
        except RuntimeError as e:
            if "reduce the amount" not in str(e):
                raise
    if rows is None:
        rows = paged(f"{media_id}/comments", tok, COMMENT_LIMIT, limit=50, fields=base)

        def replies_of(c):
            try:
                return paged(f"{c['id']}/replies", tok, COMMENT_LIMIT, limit=50, fields=base)
            except RuntimeError:
                return []
        with ThreadPoolExecutor(max_workers=8) as pool:
            for c, rs in zip(rows, pool.map(replies_of, rows)):
                c["replies"] = {"data": rs}
    out = []
    for c in rows:
        out.append({"user": c.get("username", ""), "text": c.get("text", ""),
                    "time": c.get("timestamp"), "likes": c.get("like_count", 0), "reply": False,
                    "id": c.get("id")})
        for r in c.get("replies", {}).get("data", []):
            out.append({"user": r.get("username", ""), "text": r.get("text", ""),
                        "time": r.get("timestamp"), "likes": r.get("like_count", 0), "reply": True,
                        "id": r.get("id"), "parent": c.get("id")})
    return out[:COMMENT_LIMIT]


def main():
    data = {"updated": datetime.now(timezone.utc).isoformat(), "facebook": None, "instagram": None}
    if not TOKEN:
        errors.append("META_TOKEN 시크릿이 아직 설정되지 않았습니다.")
    else:
        try:
            page_id, page_tok = resolve_page()
            with ThreadPoolExecutor(max_workers=2) as pool:
                fb = pool.submit(soft, lambda: facebook(page_id, page_tok))
                ig = pool.submit(soft, lambda: instagram(page_id, page_tok))
                data["facebook"], data["instagram"] = fb.result(), ig.result()
        except Exception as e:
            errors.append(str(e))
    if TOKEN:
        info = soft(lambda: get("debug_token", TOKEN, input_token=TOKEN).get("data", {}), {})
        data["token"] = {"type": info.get("type"), "expires_at": info.get("expires_at"),
                         "data_access_expires_at": info.get("data_access_expires_at"),
                         "valid": info.get("is_valid")}
    data["errors"] = errors
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"wrote {OUT}; errors: {errors}", file=sys.stderr)


if __name__ == "__main__":
    main()
