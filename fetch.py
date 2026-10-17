"""Fetch Instagram + Facebook Page stats/comments via the Meta Graph API.

Writes site/data.json. Runs in GitHub Actions; token comes from the
META_TOKEN secret (a Page access token, or a user token with page access).
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://graph.facebook.com/" + os.environ.get("GRAPH_VERSION", "v23.0")
TOKEN = os.environ.get("META_TOKEN", "").strip()
POST_LIMIT = int(os.environ.get("POST_LIMIT", "25"))
COMMENT_LIMIT = int(os.environ.get("COMMENT_LIMIT", "300"))
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
        return paged(
            f"{post_id}/comments", tok, COMMENT_LIMIT, limit=100, filter="stream",
            order="reverse_chronological", fields="from,message,created_time,like_count,parent{id}",
        )
    except RuntimeError as e:
        # Some posts (e.g. shared/collab) need App Review to read comments; skip quietly.
        if "(#10)" not in str(e):
            errors.append(str(e))
        return []


def facebook(page_id, tok):
    page = get(page_id, tok, fields="id,name,fan_count,followers_count,link,picture{url}")
    posts = soft(lambda: get(
        f"{page_id}/posts", tok, limit=POST_LIMIT,
        fields="id,message,created_time,permalink_url,full_picture,shares,"
               "reactions.summary(total_count).limit(0),comments.summary(total_count).limit(0)",
    ).get("data", []), [])
    out = []
    for p in posts:
        has = p.get("comments", {}).get("summary", {}).get("total_count", 0)
        comments = [] if not has else fb_comments(p["id"], tok)
        out.append({
            "id": p["id"],
            "text": p.get("message", ""),
            "time": p.get("created_time"),
            "url": p.get("permalink_url"),
            "image": p.get("full_picture"),
            "likes": p.get("reactions", {}).get("summary", {}).get("total_count", 0),
            "comments_count": p.get("comments", {}).get("summary", {}).get("total_count", 0),
            "shares": p.get("shares", {}).get("count", 0),
            "comments": [{
                "user": (c.get("from") or {}).get("name", "익명"),
                "text": c.get("message", ""),
                "time": c.get("created_time"),
                "likes": c.get("like_count", 0),
                "reply": bool(c.get("parent")),
            } for c in comments],
        })
    return {
        "name": page.get("name"),
        "link": page.get("link"),
        "picture": page.get("picture", {}).get("data", {}).get("url"),
        "followers": page.get("followers_count"),
        "fans": page.get("fan_count"),
        "posts": out,
    }


STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "store")
ROTATE = int(os.environ.get("ROTATE", "25"))  # older posts refreshed in full per run

POST_METRICS = {
    "REELS": "reach,saved,shares,views,total_interactions,likes,comments,ig_reels_avg_watch_time,ig_reels_video_view_total_time",
    "FEED": "reach,saved,shares,views,total_interactions,likes,comments,profile_visits,follows",
    "STORY": "reach,replies,shares,views,total_interactions,follows,profile_visits,navigation",
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


def insights(media_id, kind, tok):
    """Per-media insights; metrics a media type doesn't support are dropped and retried."""
    metrics = POST_METRICS[kind].split(",")
    for _ in range(len(metrics)):
        try:
            rows = get(f"{media_id}/insights", tok, metric=",".join(metrics)).get("data", [])
            out = {}
            for r in rows:
                v = r.get("total_value", {}).get("value") if "total_value" in r else (r.get("values") or [{}])[0].get("value")
                out[r["name"]] = v
            return out
        except RuntimeError as e:
            bad = next((m for m in metrics if m in str(e)), None)
            if not bad:
                return {}
            metrics.remove(bad)
    return {}


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
    for p in targets:
        d = {"insights": insights(p["id"], p["kind"], tok), "checked": now}
        d["comments"] = soft(lambda: ig_comments(p["id"], tok), []) if p["comments_count"] else []
        detail[p["id"]] = d
    for p in posts:
        p.update(detail.get(p["id"], {}))
    save("ig_detail.json", detail)

    # Stories vanish after 24h, so keep every one we have seen.
    stories = load("ig_stories.json", {})
    live = soft(lambda: paged(f"{ig_id}/stories", tok, 200, limit=50,
                              fields="id,media_type,media_url,thumbnail_url,permalink,timestamp,caption"), [])
    live_ids = set()
    for st in live:
        live_ids.add(st["id"])
        row = stories.get(st["id"], {})
        row.update({
            "id": st["id"], "type": st.get("media_type"), "time": st.get("timestamp"),
            "url": st.get("permalink"), "image": st.get("thumbnail_url") or st.get("media_url"),
            "text": st.get("caption", ""),
        })
        ins = insights(st["id"], "STORY", tok)
        if ins:
            row["insights"] = ins  # last values seen before it expired
        row["checked"] = now
        stories[st["id"]] = row
    save("ig_stories.json", stories)
    story_list = sorted(stories.values(), key=lambda r: r.get("time") or "", reverse=True)
    for r in story_list:
        r["live"] = r["id"] in live_ids

    # Posts other accounts tagged us in (most collab posts we don't own show up here).
    # The full list is slow (API only allows small pages), so refresh it hourly.
    tag_cache = load("ig_tagged.json", {"t": "", "rows": []})
    if tag_cache["t"][:13] != now[:13] or not tag_cache["rows"]:
        rows = soft(lambda: paged(
            f"{ig_id}/tags", tok, 2000, limit=10,
            fields="id,username,caption,media_type,permalink,timestamp,like_count,comments_count",
        ), None)
        if rows is not None:
            tag_cache = {"t": now, "rows": rows}
            save("ig_tagged.json", tag_cache)
    tagged = tag_cache["rows"]

    # Account-level insights.
    reach = soft(lambda: get(f"{ig_id}/insights", tok, metric="reach", period="day").get("data", []), [])
    daily_reach = [{"date": v["end_time"][:10], "value": v["value"]}
                   for d in reach for v in d.get("values", [])]
    totals = {}
    for metric in ("reach", "views", "profile_views", "accounts_engaged", "total_interactions",
                   "likes", "comments", "shares", "saves", "replies", "follows_and_unfollows",
                   "website_clicks", "profile_links_taps"):
        r = soft(lambda: get(f"{ig_id}/insights", tok, metric=metric, period="day",
                             metric_type="total_value").get("data", []), [])
        if r:
            totals[metric] = r[0].get("total_value", {}).get("value")
    errors[:] = [e for e in errors if "/insights" not in e]  # unsupported metrics are expected

    # Yesterday's full-day totals (KST), shown until our own history covers 24h.
    kst = timezone(timedelta(hours=9))
    midnight = datetime.now(kst).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = {}
    for metric in ("reach", "views", "profile_views", "accounts_engaged"):
        r = soft(lambda: get(f"{ig_id}/insights", tok, metric=metric, period="day", metric_type="total_value",
                             since=int((midnight - timedelta(days=1)).timestamp()),
                             until=int(midnight.timestamp())).get("data", []), [])
        if r:
            yesterday[metric] = r[0].get("total_value", {}).get("value")
    errors[:] = [e for e in errors if "/insights" not in e]

    # Hourly snapshot of the headline numbers; the page diffs against ~24h ago.
    feed = [p for p in posts if p["kind"] == "FEED"][:POST_LIMIT]
    snap = {"t": now, "followers": prof.get("followers_count"), "follows": prof.get("follows_count"),
            "media": prof.get("media_count"),
            "r_likes": sum(p["likes"] or 0 for p in feed), "r_comments": sum(p["comments_count"] or 0 for p in feed)}
    snap.update({k: totals.get(k) for k in ("reach", "views", "profile_views", "accounts_engaged")})
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
        "follower_history": hist,
        "posts": posts,
        "stories": story_list,
        "tagged": [{
            "id": m["id"], "owner": m.get("username", ""), "text": m.get("caption", ""),
            "time": m.get("timestamp"), "url": m.get("permalink"), "type": m.get("media_type"),
            "likes": m.get("like_count", 0), "comments_count": m.get("comments_count", 0),
        } for m in tagged],
    }


def ig_comments(media_id, tok):
    """Top-level comments plus their replies, flattened."""
    out = []
    rows = paged(
        f"{media_id}/comments", tok, COMMENT_LIMIT, limit=50,
        fields="username,text,timestamp,like_count,replies{username,text,timestamp,like_count}",
    )
    for c in rows:
        out.append({"user": c.get("username", ""), "text": c.get("text", ""),
                    "time": c.get("timestamp"), "likes": c.get("like_count", 0), "reply": False})
        for r in c.get("replies", {}).get("data", []):
            out.append({"user": r.get("username", ""), "text": r.get("text", ""),
                        "time": r.get("timestamp"), "likes": r.get("like_count", 0), "reply": True})
    return out[:COMMENT_LIMIT]


def main():
    data = {"updated": datetime.now(timezone.utc).isoformat(), "facebook": None, "instagram": None}
    if not TOKEN:
        errors.append("META_TOKEN 시크릿이 아직 설정되지 않았습니다.")
    else:
        try:
            page_id, page_tok = resolve_page()
            data["facebook"] = soft(lambda: facebook(page_id, page_tok))
            data["instagram"] = soft(lambda: instagram(page_id, page_tok))
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
