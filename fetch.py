"""Fetch Instagram + Facebook Page stats/comments via the Meta Graph API.

Writes site/data.json. Runs in GitHub Actions; token comes from the
META_TOKEN secret (a Page access token, or a user token with page access).
"""
import bisect
import json
import os
import sys
import threading
import time
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

# ---- API-wide failure detection (circuit breaker) ----
# These mean "the whole API is unusable right now": stop calling immediately (no retries, no
# per-post fallbacks), keep the last good dashboard, and let the workflow back off.
RATE_CODES = {4, 17, 32, 613}   # app / user / page / custom rate limits
TOKEN_CODES = {190}             # invalid / expired access token
fatal = {}                      # first API-wide error: {"kind", "code", "message"}
CALLS = {"http": 0, "batch_items": 0}
USAGE = {"max": 0}              # highest X-App-Usage / X-Business-Use-Case-Usage percentage seen
_lock = threading.Lock()


class ApiError(RuntimeError):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def _classify(code, msg):
    """Record (once) and return the fatal kind for an error, or None if it is object-specific."""
    m = (msg or "").lower()
    kind = ("blocked" if "api access blocked" in m or "access blocked" in m else
            "rate" if code in RATE_CODES or "request limit" in m or "rate limit" in m else
            "token" if code in TOKEN_CODES else None)
    if kind:
        with _lock:
            if not fatal:
                fatal.update(kind=kind, code=code, message=(msg or "")[:300])
    return kind


def _parse_err(body):
    try:
        e = json.loads(body)["error"]
        return e.get("message") or body[:300], e.get("code")
    except Exception:
        return body[:300], None


def _usage(headers):
    """Track Meta's usage headers (percent of the rate-limit budget used)."""
    for h in ("x-app-usage", "x-business-use-case-usage", "x-page-usage"):
        raw = headers.get(h)
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        rows = [obj] if h != "x-business-use-case-usage" else [r for v in obj.values() for r in v]
        for r in rows:
            for k in ("call_count", "total_time", "total_cputime"):
                v = r.get(k)
                if isinstance(v, (int, float)) and v > USAGE["max"]:
                    USAGE["max"] = v


def _open(url, data=None, timeout=30):
    if fatal:
        raise ApiError(f"skipped after API-wide error: {fatal.get('message')}", fatal.get("code"))
    with _lock:
        CALLS["http"] += 1
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout) as r:
            _usage(r.headers)
            return json.load(r)
    except urllib.error.HTTPError as e:
        _usage(e.headers or {})
        msg, code = _parse_err(e.read().decode("utf-8", "replace"))
        _classify(code, msg)
        raise ApiError(msg, code) from None


def get(path, token, **params):
    params["access_token"] = token
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    try:
        return _open(url)
    except ApiError as e:
        raise ApiError(f"{path}: {e}", e.code) from None


def paged(path, token, cap, **params):
    """Follow paging.next until cap items are collected."""
    first = get(path, token, **params)
    items, nxt = first.get("data", []), first.get("paging", {}).get("next")
    while nxt and len(items) < cap:
        try:
            page = _open(nxt)
        except ApiError:
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
FB_COMMENT_CAP = int(os.environ.get("COMMENT_CAP", "20"))  # comment-thread reads per run, at most
COMMENT_TTL = 3600  # latest posts: re-read comments when the count changes, else hourly


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


# Store writes are buffered and only written by flush(): a run that hits an API-wide error
# must not leave half-updated history (e.g. a runs row full of nulls) behind.
_pending = {}


def load(name, default):
    if name in _pending:
        return _pending[name]
    try:
        with open(os.path.join(STORE, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(name, obj):
    _pending[name] = obj


def flush(only=None):
    os.makedirs(STORE, exist_ok=True)
    for name, obj in list(_pending.items()):
        if only is not None and name not in only:
            continue
        with open(os.path.join(STORE, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


# ---- Cadence: parts that don't need refreshing every 3 minutes ----
SLOW_SEC = int(os.environ.get("SLOW_SEC", "900"))  # FB page, IG account insights, rotation, token info
_sched = None


def due(key, secs=SLOW_SEC):
    """True if `key` hasn't run within `secs` (marks it as run now; persisted in store/schedule.json)."""
    global _sched
    if _sched is None:
        _sched = load("schedule.json", {})
    if time.time() - _sched.get(key, 0) < secs - 30:  # 30 s slack for run-to-run jitter
        return False
    mark(key)
    return True


def mark(key):
    global _sched
    if _sched is None:
        _sched = load("schedule.json", {})
    _sched[key] = time.time()
    save("schedule.json", _sched)


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
            res = _open(API + "/", data=body, timeout=60)
        except ApiError:
            if fatal:
                return []  # API-wide error: never fan out into one call per post
            return [one(i) for i in ids50]
        except Exception:
            return [one(i) for i in ids50]
        with _lock:
            CALLS["batch_items"] += len(ids50)
        out = []
        for mid, item in zip(ids50, res or []):
            v = None
            if item and item.get("code") == 200:
                try:
                    v = _views_value(json.loads(item.get("body") or "{}").get("data", []))
                except ValueError:
                    pass
            elif item:
                msg, code = _parse_err(item.get("body") or "")
                _classify(code, msg)
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


def ig_views_hourly(views, now):
    """store/ig_views_hourly.json = {"t": [KST hour ISO...], "v": {id: [views|null aligned to t]}}:
    one snapshot per KST clock hour (first run of the hour), kept forever."""
    kst = timezone(timedelta(hours=9))
    hour = datetime.fromisoformat(now).astimezone(kst).replace(minute=0, second=0, microsecond=0).isoformat()
    st = load("ig_views_hourly.json", {"t": [], "v": {}})
    ts, vs = st.get("t") or [], st.get("v") or {}
    if ts and ts[-1] >= hour:
        return st
    n = len(ts)
    for k in vs:
        vs[k].append(views.get(k))
    for k, v in views.items():
        if k not in vs and v is not None:
            vs[k] = [None] * n + [v]
    ts.append(hour)
    st = {"t": ts, "v": vs}
    save("ig_views_hourly.json", st)
    return st


TOP3_MAX_PTS = 600


def _thin(pts, tnow):
    """Downsample: keep all points from the last 24h; thin older ones evenly (first one always kept)."""
    if len(pts) > TOP3_MAX_PTS:
        recent = [p for p in pts if (tnow - p[0]).total_seconds() <= 86400]
        old = pts[:len(pts) - len(recent)]
        room = max(2, TOP3_MAX_PTS - len(recent))
        if len(old) > room:
            step = (len(old) - 1) / (room - 1)
            old = [old[round(i * step)] for i in range(room)]
        pts = old + recent
    return [[dt.isoformat(), v] for dt, v in pts]


def ig_top3_series(top_growth, hourly, views, now):
    """Cumulative views over time for the top 3 of top_growth: ALL hourly history (kept forever)
    + every 3-min snapshot in ig_views_window.json + the current value.
    To keep data.json small, a series longer than TOP3_MAX_PTS keeps every point from the last 24h
    and evenly thins the older (hourly) points so the total fits."""
    tnow = datetime.fromisoformat(now)
    snaps = load("ig_views_window.json", [])
    w0 = min((datetime.fromisoformat(s["t"]) for s in snaps), default=tnow)
    out = []
    for r in top_growth[:3]:
        pid, pts = r["id"], {}
        for t, v in zip(hourly.get("t") or [], (hourly.get("v") or {}).get(pid) or []):
            dt = datetime.fromisoformat(t)
            if v is not None and dt < w0:
                pts[dt] = v
        for s in snaps:
            v = (s.get("views") or {}).get(pid)
            if v is not None:
                pts[datetime.fromisoformat(s["t"])] = v
        if views.get(pid) is not None:
            pts[tnow] = views[pid]
        out.append({"id": pid, "text": (r.get("text") or "")[:40], "url": r.get("url"), "image": r.get("image"),
                    "kind": r.get("kind"),
                    "points": _thin([[dt, v] for dt, v in sorted(pts.items(), key=lambda x: x[0])], tnow)})
    return out


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


INSIGHT_CAP = int(os.environ.get("INSIGHT_CAP", "30"))  # per-post insight calls per run, at most
COMMENT_CAP = int(os.environ.get("COMMENT_CAP", "20"))  # comment-thread reads per run, at most
COMMENT_TTL = 3600  # latest POST_LIMIT posts: comments re-read hourly even if the count didn't change


def instagram(page_id, tok):
    ids = load("ids.json", {})
    if ids.get("page") == page_id and ids.get("ig"):
        ig_id = ids["ig"]  # the linked IG account never changes; skip that call every run
    else:
        ig_ref = get(page_id, tok, fields="instagram_business_account").get("instagram_business_account")
        if not ig_ref:
            errors.append("페이스북 페이지에 연결된 인스타그램 프로페셔널 계정이 없습니다.")
            return None
        ig_id = ig_ref["id"]
        save("ids.json", {"page": page_id, "ig": ig_id})
    prof = get(ig_id, tok, fields="username,name,followers_count,follows_count,media_count,profile_picture_url")

    # Every post we own (feed, carousel, reels); counts refresh every run.
    fields = ("id,caption,media_type,media_product_type,media_url,thumbnail_url,permalink,"
              "timestamp,like_count,comments_count")
    media = soft(lambda: paged(f"{ig_id}/media", tok, 10000, limit=100, fields=fields), [])
    posts = [ig_media_row(m) for m in media]

    # Per-post insights + comments. Only what changed is re-read every run:
    #  - insights: posts whose like/comment counts moved since their last check, the latest
    #    POST_LIMIT posts every SLOW_SEC, and a rotating ROTATE-post slice of older posts every
    #    SLOW_SEC; capped at INSIGHT_CAP calls per run (newest first).
    #  - comments: when the comment count changed, or hourly for the latest POST_LIMIT posts.
    detail = load("ig_detail.json", {})
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    IG_OK_METRICS.update(load("ig_metrics.json", {}))

    def age(t):
        try:
            return (now_dt - datetime.fromisoformat(t)).total_seconds()
        except (TypeError, ValueError):
            return float("inf")

    counts_of = {p["id"]: p["comments_count"] for p in posts}
    ins_ids = []
    for i, p in enumerate(posts):
        d = detail.get(p["id"]) or {}
        counts = [p["likes"], p["comments_count"]]
        if "seen" not in d:
            d["seen"] = counts  # first run with this logic: don't treat every post as "changed"
            detail[p["id"]] = d
        if d.get("seen") != counts or not d.get("checked") or (i < POST_LIMIT and age(d.get("checked")) >= SLOW_SEC - 30):
            ins_ids.append(p["id"])
    older = posts[POST_LIMIT:]
    if older and due("ig_rotate"):
        cursor = load("ig_cursor.json", 0) % len(older)
        ins_ids += [p["id"] for p in older[cursor:cursor + ROTATE]]
        save("ig_cursor.json", cursor + ROTATE)
    ins_ids = set(list(dict.fromkeys(ins_ids))[:INSIGHT_CAP])

    def comments_stale(i, p):
        old = detail.get(p["id"]) or {}
        if old.get("comments_seen") != p["comments_count"]:
            return True
        # Cached lists from before comment ids were collected count as stale (needed for reply deep links).
        if not all(c.get("id") for c in old.get("comments", [])):
            return True
        return i < POST_LIMIT and age(old.get("comments_checked")) >= COMMENT_TTL

    # Posts with comments need a call; capped per run (newest first) so a backlog after an outage
    # is spread over several runs instead of one burst. Posts with 0 comments need no call.
    cm_list = [p["id"] for i, p in enumerate(posts) if comments_stale(i, p)]
    cm_calls = [pid for pid in cm_list if counts_of[pid]][:COMMENT_CAP]
    cm_ids = set(cm_calls) | {pid for pid in cm_list if not counts_of[pid]}
    targets = [p for p in posts if p["id"] in ins_ids or p["id"] in cm_ids]

    def refresh(p):
        d = dict(detail.get(p["id"]) or {})
        if p["id"] in ins_ids:
            d["insights"] = insights(p["id"], p["kind"], tok) or d.get("insights")
            d["checked"] = now
            d["seen"] = [p["likes"], p["comments_count"]]
        if p["id"] in cm_ids:
            d["comments"] = soft(lambda: ig_comments(p["id"], tok), []) if p["comments_count"] else []
            d["comments_seen"], d["comments_checked"] = p["comments_count"], now
        return p["id"], d

    # Calls are independent, so run them in parallel.
    with ThreadPoolExecutor(max_workers=16) as pool:
        for pid, d in pool.map(refresh, targets):
            detail[pid] = d
    STATS.update(ig_insight_calls=len(ins_ids), ig_comment_reads=len(cm_calls))
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

    # Account-level insights + yesterday's totals: at most every SLOW_SEC (cached in store/ig_acct.json).
    kst = timezone(timedelta(hours=9))
    acct_cache = load("ig_acct.json", None)
    day_kst = datetime.now(kst).date().isoformat()
    refresh_acct = due("ig_acct")
    if acct_cache and acct_cache.get("day") == day_kst and not refresh_acct:
        daily_reach, totals, yesterday = acct_cache["daily_reach"], acct_cache["totals"], acct_cache["yesterday"]
    else:
        mark("ig_acct")
        daily_reach, totals, yesterday = ig_account(ig_id, tok, kst)
        save("ig_acct.json", {"day": day_kst, "t": now, "daily_reach": daily_reach,
                              "totals": totals, "yesterday": yesterday})

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
    top3 = soft(lambda: ig_top3_series(top_growth, ig_views_hourly(views_store, now), views_store, now), [])
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
        "top3_series": top3,
        "runs": runs,
        "hourly_views": hourly,
        "follower_history": hist,
        "posts": posts,
    }


ACCT_METRICS = ("reach", "views", "profile_views", "accounts_engaged", "total_interactions",
                "likes", "comments", "shares", "saves", "replies", "follows_and_unfollows",
                "website_clicks", "profile_links_taps")
ACCT_DAY_METRICS = ("reach", "views", "profile_views", "accounts_engaged")


def ig_account(ig_id, tok, kst):
    """(daily_reach, today's totals, yesterday's KST totals) for the IG account.
    All metrics go in one call; only if Meta rejects the combination do we fall back to one call each."""
    reach = soft(lambda: get(f"{ig_id}/insights", tok, metric="reach", period="day").get("data", []), [])
    daily_reach = [{"date": v["end_time"][:10], "value": v["value"]}
                   for d in reach for v in d.get("values", [])]

    def tv(rows):
        return {r["name"]: r.get("total_value", {}).get("value") for r in rows if r.get("name")}

    def many(metrics, **extra):
        try:
            out = tv(get(f"{ig_id}/insights", tok, metric=",".join(metrics), period="day",
                         metric_type="total_value", **extra).get("data", []))
        except RuntimeError:
            if fatal:
                return {}

            def one(m):
                try:
                    return tv(get(f"{ig_id}/insights", tok, metric=m, period="day",
                                  metric_type="total_value", **extra).get("data", []))
                except RuntimeError:
                    return {}
            out = {}
            with ThreadPoolExecutor(max_workers=8) as pool:
                for o in pool.map(one, metrics):
                    out.update(o)
        return {m: out[m] for m in metrics if out.get(m) is not None}

    totals = many(ACCT_METRICS)
    # Yesterday's full-day totals (KST), shown until our own history covers 24h.
    midnight = datetime.now(kst).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = many(ACCT_DAY_METRICS, since=int((midnight - timedelta(days=1)).timestamp()),
                     until=int(midnight.timestamp()))
    errors[:] = [e for e in errors if "/insights" not in e]  # unsupported metrics are expected
    return daily_reach, totals, yesterday


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


BASE_DELAY, MAX_DELAY = 180, 3600  # chain cadence: 3 min, backing off up to 60 min on API-wide errors
SITE_DATA_URL = os.environ.get("SITE_DATA_URL", "https://lee-seungyeol.github.io/jay-brows-dashboard/data.json")
STATS = {}
REASON_KO = {
    "blocked": "Meta API 접근 차단 (Meta 개발자 계정 확인 필요)",
    "rate": "Meta API 호출 한도 초과 (자동으로 잠시 후 다시 시도해요)",
    "token": "Meta 액세스 토큰 만료/무효 (토큰 재발급 필요)",
    "token_missing": "META_TOKEN 시크릿이 설정되지 않음",
    "error": "Meta API에서 데이터를 받지 못함",
}


def gh_output(**kv):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")


def last_good():
    """The last dashboard built from a successful run: the live site first (it only ever serves
    good data, plus a banner while stale), else the store copy."""
    try:
        with urllib.request.urlopen(SITE_DATA_URL + "?t=" + str(int(time.time())), timeout=30) as r:
            live = json.load(r)
        if live.get("instagram") or live.get("facebook"):
            return live
    except Exception:
        pass
    return load("last_good_data.json", None)


def main():
    t0 = time.time()
    now = datetime.now(timezone.utc).isoformat()
    health = load("health.json", {})
    # Backed off after an API-wide error: runs that start before the retry time (cron backups,
    # stray dispatches) don't touch the API at all. FORCE=1 (manual dispatch) overrides.
    retry_at = health.get("next_run_at", 0) if not health.get("ok", True) else 0
    if retry_at and t0 < retry_at - 60 and os.environ.get("FORCE") not in ("1", "true"):
        print(f"backing off after {health.get('consecutive_failures')} failure(s); "
              f"next API attempt in {int(retry_at - t0)} s - skipping", file=sys.stderr)
        gh_output(skipped="true")
        return

    data = {"updated": now, "facebook": None, "instagram": None}
    attempted = []
    if not TOKEN:
        errors.append("META_TOKEN 시크릿이 아직 설정되지 않았습니다.")
        fatal.update(kind="token_missing", code=None, message="META_TOKEN missing")
    else:
        try:
            page_id, page_tok = resolve_page()
            do_fb = due("fb") or not load("fb_last.json", None)
            with ThreadPoolExecutor(max_workers=2) as pool:
                fb = pool.submit(soft, lambda: facebook(page_id, page_tok)) if do_fb else None
                ig = pool.submit(soft, lambda: instagram(page_id, page_tok))
                data["instagram"] = ig.result()
                attempted.append(data["instagram"])
                if fb:
                    data["facebook"] = fb.result()
                    attempted.append(data["facebook"])
                    if data["facebook"]:
                        save("fb_last.json", data["facebook"])
                else:
                    data["facebook"] = load("fb_last.json", None)  # refreshed every SLOW_SEC
        except Exception as e:
            errors.append(str(e))
        if not fatal:
            if due("token") or not load("token_info.json", None):
                info = soft(lambda: get("debug_token", TOKEN, input_token=TOKEN).get("data", {}), {})
                save("token_info.json", {"type": info.get("type"), "expires_at": info.get("expires_at"),
                                         "data_access_expires_at": info.get("data_access_expires_at"),
                                         "valid": info.get("is_valid")})
            data["token"] = load("token_info.json", None)

    failed = bool(fatal) or not attempted or not any(attempted)
    n_fail = 0 if not failed else int(health.get("consecutive_failures") or 0) + 1
    if failed:
        kind = fatal.get("kind") or "error"
        msg = fatal.get("message") or (errors[0] if errors else "no data returned")
        delay = min(MAX_DELAY, BASE_DELAY * 2 ** n_fail)
    else:
        kind, msg = None, None
        delay = BASE_DELAY if USAGE["max"] < 80 else BASE_DELAY * 4  # near Meta's budget: slow down early
    calls = {**CALLS, **STATS, "usage_max_pct": USAGE["max"]}
    prev_good = None
    if failed:
        prev_good = last_good()
        stale_since = health.get("stale_since") or (prev_good or {}).get("updated") or now
    health = {
        "ok": not failed, "consecutive_failures": n_fail,
        "last_error": msg, "last_error_kind": kind, "reason": REASON_KO.get(kind) if kind else None,
        "next_delay_s": delay, "next_run_at": int(t0) + delay, "last_run": now,
        "last_ok": now if not failed else health.get("last_ok"),
        "stale_since": stale_since if failed else None, "calls": calls,
    }
    save("health.json", health)
    public_health = {k: v for k, v in health.items() if k != "calls"}

    if failed:
        # Keep the last good dashboard; only add the stale note. History in store is left untouched.
        if prev_good:
            save("last_good_data.json", {k: v for k, v in prev_good.items()
                                         if k not in ("stale_since", "health")})
            data = dict(prev_good)
        data["stale_since"] = stale_since
        data["health"] = public_health
        data["errors"] = [f"{health['reason']}: {msg}"] + [e for e in errors if e != msg][:5]
        flush(only={"health.json", "last_good_data.json"})
    else:
        data["errors"] = errors
        data["health"] = public_health
        # The store copy (fallback if the live site can't be read) is ~2 MB: refresh it at most every
        # SLOW_SEC, and always on the first good run after an outage.
        if due("last_good") or n_fail == 0 and load("last_good_data.json", None) is None:
            save("last_good_data.json", data)
        flush()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    gh_output(skipped="false", ok=str(not failed).lower(), next_delay_s=delay)
    print(f"wrote {OUT}; ok={not failed} calls={calls} next_delay_s={delay} errors: {errors}", file=sys.stderr)


if __name__ == "__main__":
    main()
