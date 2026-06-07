#!/usr/bin/env python3
"""Generate an RSS feed of aged, high-score HN stories with top comments inline.

For each story above SCORE_THRESHOLD and at least MIN_AGE_HOURS old, we capture
the title, the article URL (as the item link), the HN comments URL, and four
comments: the top-level #1, its top reply, the top-level #2, and its top reply.

Story discovery uses Algolia's search API. Comment selection uses HN's official
Firebase API, whose `kids` array is in HN display order (HN's ranking) —
Algolia's children array is in creation-time order, which gives wrong "top"
comments.
"""

import html
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

SCORE_THRESHOLD = 125
MIN_AGE_HOURS = 8
LOOKBACK_HOURS = 72  # how far back to scan for candidate stories
MAX_ITEMS = 100  # retained in feed
GUID_PREFIX = "hn2"  # bump when item rendering changes to force readers to re-ingest

STATE_FILE = Path("state.json")
FEED_FILE = Path("feed.xml")

ALGOLIA_SEARCH = "https://hn.algolia.com/api/v1/search"
HN_FIREBASE_ITEM = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
HN_ITEM_URL = "https://news.ycombinator.com/item?id={id}"


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "hnrss-generator/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def find_candidate_stories():
    now = int(time.time())
    older_than = now - MIN_AGE_HOURS * 3600
    newer_than = now - LOOKBACK_HOURS * 3600
    params = {
        "tags": "story",
        "numericFilters": ",".join([
            f"points>={SCORE_THRESHOLD}",
            f"created_at_i<={older_than}",
            f"created_at_i>={newer_than}",
        ]),
        "hitsPerPage": 100,
    }
    return fetch_json(f"{ALGOLIA_SEARCH}?{urllib.parse.urlencode(params)}").get("hits", [])


def fetch_hn_item(item_id):
    return fetch_json(HN_FIREBASE_ITEM.format(id=item_id))


def is_live_comment(item):
    return bool(item and not item.get("deleted") and not item.get("dead") and item.get("text"))


def first_live_kid(parent):
    """Walk parent's kids in HN display order, return the first live comment."""
    for kid_id in parent.get("kids") or []:
        kid = fetch_hn_item(kid_id)
        if is_live_comment(kid):
            return kid
    return None


def pick_comments(story_id):
    """Return [top1, top1-reply, top2, top2-reply], any slot may be None.

    HN's Firebase API returns `kids` in HN's display order, so kids[0] is the
    actual top comment as it appears on the story page.
    """
    story = fetch_hn_item(story_id)
    kid_ids = (story.get("kids") if story else None) or []

    live_top = []
    for kid_id in kid_ids:
        if len(live_top) >= 2:
            break
        kid = fetch_hn_item(kid_id)
        if is_live_comment(kid):
            live_top.append(kid)

    out = [None, None, None, None]
    if len(live_top) >= 1:
        out[0] = live_top[0]
        out[1] = first_live_kid(live_top[0])
    if len(live_top) >= 2:
        out[2] = live_top[1]
        out[3] = first_live_kid(live_top[1])
    return out


def render_comment(comment, label):
    if comment is None:
        return f"<p><em>{label}: (none)</em></p>"
    author = html.escape(comment.get("by") or "?")
    # HN Firebase API returns .text as HTML already
    body = comment.get("text") or ""
    return (
        f'<p><strong>{label}</strong> — <em>{author}</em>:</p>\n'
        f"<blockquote>{body}</blockquote>"
    )


def build_html(story, comments):
    sid = story["objectID"]
    article_url = story.get("url") or HN_ITEM_URL.format(id=sid)
    hn_url = HN_ITEM_URL.format(id=sid)
    points = story.get("points", 0)
    num_comments = story.get("num_comments", 0)
    parts = [
        f'<p><a href="{html.escape(article_url, quote=True)}"><strong>→ Article</strong></a></p>',
        f'<p><a href="{html.escape(hn_url, quote=True)}">→ HN comments</a> '
        f"&nbsp;·&nbsp; {points} points &nbsp;·&nbsp; {num_comments} comments</p>",
        "<hr>",
    ]
    labels = ["Top comment", "Reply to top comment", "Second comment", "Reply to second comment"]
    for label, c in zip(labels, comments):
        parts.append(render_comment(c, label))
    return "\n".join(parts)


def short_summary(story):
    return (
        f'{story.get("points", 0)} points, {story.get("num_comments", 0)} comments. '
        f"See description for top comments."
    )


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"items": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def cdata(s):
    # Defensive: split any literal "]]>" so it can't close the CDATA section.
    return "<![CDATA[" + s.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def write_feed(items):
    now_rfc = format_datetime(datetime.now(timezone.utc))
    rendered_items = []
    for it in items:
        pub = format_datetime(datetime.fromtimestamp(it["created_at_i"], tz=timezone.utc))
        link = it.get("article_url") or HN_ITEM_URL.format(id=it["id"])
        rendered_items.append(
            "    <item>\n"
            f"      <title>{xml_escape(it['title'])}</title>\n"
            f"      <link>{xml_escape(link)}</link>\n"
            f'      <guid isPermaLink="false">{GUID_PREFIX}-{it["id"]}</guid>\n'
            f"      <pubDate>{pub}</pubDate>\n"
            f"      <description>{xml_escape(it['summary'])}</description>\n"
            f"      <content:encoded>{cdata(it['html'])}</content:encoded>\n"
            "    </item>"
        )
    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">\n'
        "  <channel>\n"
        f"    <title>HN ≥{SCORE_THRESHOLD}, aged {MIN_AGE_HOURS}h+</title>\n"
        "    <link>https://news.ycombinator.com/</link>\n"
        f"    <description>HN stories above {SCORE_THRESHOLD} points, at least "
        f"{MIN_AGE_HOURS}h old, with top comments inline.</description>\n"
        f"    <lastBuildDate>{now_rfc}</lastBuildDate>\n"
        + "\n".join(rendered_items) + "\n"
        "  </channel>\n"
        "</rss>\n"
    )
    FEED_FILE.write_text(feed)


def main():
    state = load_state()
    seen = {it["id"] for it in state["items"]}

    candidates = find_candidate_stories()
    added = 0
    for story in candidates:
        sid = story["objectID"]
        if sid in seen:
            continue
        comments = pick_comments(sid)
        state["items"].append({
            "id": sid,
            "title": story.get("title") or "(no title)",
            "created_at_i": story["created_at_i"],
            "article_url": story.get("url") or HN_ITEM_URL.format(id=sid),
            "summary": short_summary(story),
            "html": build_html(story, comments),
        })
        added += 1

    state["items"].sort(key=lambda x: x["created_at_i"], reverse=True)
    state["items"] = state["items"][:MAX_ITEMS]

    save_state(state)
    write_feed(state["items"])
    print(f"Added {added} new items; feed has {len(state['items'])} total")


if __name__ == "__main__":
    main()
