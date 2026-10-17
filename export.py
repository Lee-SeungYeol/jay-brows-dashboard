"""Export site/data.json to statistics-friendly CSV files (UTF-8 with BOM for Excel).

Usage: python export.py [input_json] [output_dir]
  defaults: site/data.json -> site/export/
  (or env EXPORT_INPUT / EXPORT_OUTPUT)
Tolerant of missing / extra keys: every dict is flattened, columns are the union across rows.
"""
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def kst(s):
    """ISO time string (any offset) -> 'YYYY-MM-DD HH:MM:SS' in KST; '' if missing/unparseable."""
    if not s:
        return ""
    try:
        t = str(s).replace("Z", "+00:00")
        if len(t) >= 5 and t[-5] in "+-" and t[-3] != ":":  # +0000 -> +00:00
            t = t[:-2] + ":" + t[-2:]
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(s)


def clean(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def flat(prefix_src, base_keys, prefix):
    """Flatten a nested dict; key collides with base column -> prefixed."""
    out = {}
    if not isinstance(prefix_src, dict):
        return out
    for k, v in prefix_src.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                out[f"{k}_{k2}"] = v2
        else:
            out[prefix + k if k in base_keys else k] = v
    return out


def write_csv(path, rows, first_cols):
    cols = list(first_cols)
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    if "insights_checked_at" in cols:
        cols.remove("insights_checked_at")
        cols.append("insights_checked_at")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([clean(r.get(c)) for c in cols])
    return len(rows)


def lst(x):
    return x if isinstance(x, list) else []


def dct(x):
    return x if isinstance(x, dict) else {}


SKIP_POST = {"text", "time", "image", "comments", "insights", "checked", "reactions_by_type"}


def post_rows(posts, base):
    rows = []
    for p in lst(posts):
        if not isinstance(p, dict):
            continue
        r = {c: p.get(c) for c in base if c not in ("posted_at_kst", "caption", "insights_checked_at")}
        r["posted_at_kst"] = kst(p.get("time"))
        r["caption"] = p.get("text") or p.get("message") or ""
        # extra scalar keys added later by fetch.py
        for k, v in p.items():
            if k not in SKIP_POST and k not in r and not isinstance(v, (list, dict)):
                r[k] = v
        r.update(flat(p.get("insights"), set(r) | {"likes", "comments"}, "insights_"))
        rb = dct(p.get("reactions_by_type"))
        for k, v in rb.items():
            r[f"reaction_{k}"] = v
        if "checked" in p:
            r["insights_checked_at"] = kst(p.get("checked"))
        rows.append(r)
    return rows


def comment_rows(posts):
    rows = []
    for p in lst(posts):
        if not isinstance(p, dict):
            continue
        for c in lst(p.get("comments")):
            if not isinstance(c, dict):
                continue
            r = {
                "post_id": p.get("id"),
                "post_url": p.get("url"),
                "time_kst": kst(c.get("time")),
                "user": c.get("user"),
                "text": c.get("text"),
                "likes": c.get("likes"),
                "is_reply": c.get("reply"),
            }
            for k, v in c.items():
                if k not in ("time", "user", "text", "likes", "reply") and not isinstance(v, (list, dict)):
                    r[k] = v
            rows.append(r)
    return rows


def hourly_rows(hist):
    rows = []
    for h in lst(hist):
        if not isinstance(h, dict):
            continue
        r = {"time_kst": kst(h.get("t") or h.get("time"))}
        for k, v in h.items():
            if k in ("t", "time"):
                continue
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    r[f"{k}_{k2}"] = v2
            elif not isinstance(v, list):
                r[k] = v
        rows.append(r)
    return rows


def main():
    src = (sys.argv[1] if len(sys.argv) > 1 else None) or os.environ.get("EXPORT_INPUT") or "site/data.json"
    out = (sys.argv[2] if len(sys.argv) > 2 else None) or os.environ.get("EXPORT_OUTPUT") or "site/export"
    with open(src, encoding="utf-8") as f:
        d = json.load(f)
    os.makedirs(out, exist_ok=True)
    ig = dct(d.get("instagram"))
    fb = dct(d.get("facebook"))
    files = {}

    def emit(name, rows, cols, desc):
        n = write_csv(os.path.join(out, name), rows, cols)
        files[name] = {"rows": n, "desc": desc}

    emit("ig_posts.csv",
         post_rows(ig.get("posts"), ["id", "kind", "type", "posted_at_kst", "url", "caption", "likes", "comments_count"]),
         ["id", "kind", "type", "posted_at_kst", "url", "caption", "likes", "comments_count"],
         "인스타 게시물/릴스별 좋아요·댓글·도달·조회·저장·공유 등 인사이트")
    emit("ig_comments.csv", comment_rows(ig.get("posts")),
         ["post_id", "post_url", "time_kst", "user", "text", "likes", "is_reply"],
         "인스타 댓글 전체 (작성 시각, 작성자, 내용, 좋아요, 답글 여부)")

    emit("ig_tagged.csv",
         post_rows(ig.get("tagged"), ["id", "owner", "type", "posted_at_kst", "url", "caption", "likes", "comments_count"]),
         ["id", "owner", "type", "posted_at_kst", "url", "caption", "likes", "comments_count"],
         "다른 계정이 태그/협업한 게시물")
    emit("ig_hourly.csv", hourly_rows(ig.get("follower_history")), ["time_kst"],
         "인스타 팔로워·팔로잉·게시물 수 시간별 기록 (추이 분석용)")

    # account metrics, long format
    upd = d.get("updated")
    today = kst(upd)[:10] if upd else datetime.now(KST).strftime("%Y-%m-%d")
    try:
        yday = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        yday = ""
    acc = []
    for k, v in dct(ig.get("today")).items():
        acc.append({"date": today, "period": "today", "metric": k, "value": v})
    for k, v in dct(ig.get("yesterday")).items():
        acc.append({"date": yday, "period": "yesterday", "metric": k, "value": v})
    for x in lst(ig.get("daily_reach")):
        if isinstance(x, dict):
            acc.append({"date": x.get("date"), "period": "daily", "metric": "reach", "value": x.get("value")})
    for k in ("followers", "follows", "media_count"):
        if k in ig:
            acc.append({"date": today, "period": "snapshot", "metric": k, "value": ig.get(k)})
    for k, v in dct(ig.get("totals")).items():
        acc.append({"date": today, "period": "all_time", "metric": f"totals_{k}", "value": v})
    for k in ("followers", "fans"):
        if k in fb:
            acc.append({"date": today, "period": "snapshot", "metric": f"fb_{k}", "value": fb.get(k)})
    for per in ("today", "yesterday"):
        for k, v in dct(fb.get(per)).items():
            if not isinstance(v, (dict, list)):
                acc.append({"date": today if per == "today" else yday, "period": per, "metric": f"fb_{k}", "value": v})
    emit("account_today.csv", acc, ["date", "period", "metric", "value"],
         "계정 지표 오늘/어제/일별 도달/누적 합계 (long 형식: date, metric, value)")

    emit("fb_posts.csv",
         post_rows(fb.get("posts"), ["id", "type", "posted_at_kst", "url", "caption", "likes", "comments_count", "shares"]),
         ["id", "type", "posted_at_kst", "url", "caption", "likes", "comments_count", "shares"],
         "페이스북 게시물별 좋아요·댓글·공유 (+ 반응 종류/인사이트)")
    emit("fb_comments.csv", comment_rows(fb.get("posts")),
         ["post_id", "post_url", "time_kst", "user", "text", "likes", "is_reply"],
         "페이스북 댓글 전체")
    emit("fb_hourly.csv", hourly_rows(fb.get("follower_history") or fb.get("history")), ["time_kst"],
         "페이스북 팔로워 시간별 기록")

    index = {
        "generated_at_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
        "data_updated_kst": kst(upd),
        "files": [{"name": k, **v} for k, v in files.items()],
    }
    with open(os.path.join(out, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    print("export:", ", ".join(f"{k}={v['rows']}" for k, v in files.items()))


if __name__ == "__main__":
    main()
