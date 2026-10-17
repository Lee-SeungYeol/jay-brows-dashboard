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
from datetime import datetime, timezone

API = "https://graph.facebook.com/" + os.environ.get("GRAPH_VERSION", "v23.0")
TOKEN = os.environ.get("META_TOKEN", "").strip()
POST_LIMIT = int(os.environ.get("POST_LIMIT", "25"))
COMMENT_LIMIT = int(os.environ.get("COMMENT_LIMIT", "20"))
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
        comments = [] if not has else soft(lambda: get(
            f"{p['id']}/comments", tok, limit=COMMENT_LIMIT, order="reverse_chronological",
            fields="from,message,created_time,like_count",
        ).get("data", []), [])
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


def instagram(page_id, tok):
    ig_ref = get(page_id, tok, fields="instagram_business_account").get("instagram_business_account")
    if not ig_ref:
        errors.append("페이스북 페이지에 연결된 인스타그램 프로페셔널 계정이 없습니다.")
        return None
    ig_id = ig_ref["id"]
    prof = get(ig_id, tok, fields="username,name,followers_count,follows_count,media_count,profile_picture_url")
    media = soft(lambda: get(
        f"{ig_id}/media", tok, limit=POST_LIMIT,
        fields="id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,like_count,comments_count",
    ).get("data", []), [])
    out = []
    for m in media:
        comments = []
        if m.get("comments_count"):
            comments = soft(lambda: get(
                f"{m['id']}/comments", tok, limit=COMMENT_LIMIT,
                fields="username,text,timestamp,like_count",
            ).get("data", []), [])
        out.append({
            "id": m["id"],
            "text": m.get("caption", ""),
            "time": m.get("timestamp"),
            "url": m.get("permalink"),
            "image": m.get("thumbnail_url") or m.get("media_url"),
            "type": m.get("media_type"),
            "likes": m.get("like_count", 0),
            "comments_count": m.get("comments_count", 0),
            "comments": [{
                "user": c.get("username", ""),
                "text": c.get("text", ""),
                "time": c.get("timestamp"),
                "likes": c.get("like_count", 0),
            } for c in comments],
        })
    reach = soft(lambda: get(f"{ig_id}/insights", tok, metric="reach", period="day").get("data", []), [])
    daily_reach = [{"date": v["end_time"][:10], "value": v["value"]}
                   for d in reach for v in d.get("values", [])]
    return {
        "username": prof.get("username"),
        "name": prof.get("name"),
        "picture": prof.get("profile_picture_url"),
        "followers": prof.get("followers_count"),
        "follows": prof.get("follows_count"),
        "media_count": prof.get("media_count"),
        "daily_reach": daily_reach,
        "posts": out,
    }


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
    data["errors"] = errors
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"wrote {OUT}; errors: {errors}", file=sys.stderr)


if __name__ == "__main__":
    main()
