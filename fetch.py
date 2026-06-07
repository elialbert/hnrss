#!/usr/bin/env python3
"""Generate an RSS feed of aged, high-score HN stories with top comments inline.

For each story above SCORE_THRESHOLD and at least MIN_AGE_HOURS old, we capture
the title, the HN comments URL (as the item link), the article URL, and four
comments: the top-level #1, its top reply, the top-level #2, and its top reply.
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

STATE_FILE = Path("state.json")
FEED_FILE = Path("feed.xml")

ALGOLIA_SEARCH = "https://hn.algolia.com/api/v1/search"
ALGOLIA_ITEM = "https://hn.algolia.com/api/v1/items/{id}"
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


def pick_comments(story_id):
    """Return [top1, top1-reply, top2, top2-reply], any of which may be None.

    Algolia's `children` array preserves HN's display order, which is HN's
    ranking — so children[0] is the top comment.
    """
    data = fetch_json(ALGOLIA_ITEM.format(id=story_id))
    top_level = [c for c in (data.get("children") or []) if c.get("text") and c.get("author")]

    def top_reply(comment):
        replies = [r for r in (comment.get("children") or []) if r.get("text") and r.get("author")]
        return replies[0] if replies else None

    out = [None, None, None, None]
    if len(top_level) >= 1:
        out[0] = top_level[0]
        out[1] = top_reply(top_level[0])
    if len(top_level) >= 2:
        out[2] = top_level[1]
        out[3] = top_reply(top_level[1])
    return out


def render_comment(comment, label):
    if comment is None:
        return f"<p><em>{label}: (none)</em></p>"
    author = html.escape(comment.get("author") or "?")
    # Algolia returns comment .text as HTML already
    body = comment.get("text") or ""
    return (
        f'<p><strong>{label}</strong> — <em>{author}</em>:</p>\n'
        f"<blockquote>{body}</blockquote>"
    )


def build_html(story, comments):
    article_url = story.get("url") or HN_ITEM_URL.format(id=story["objectID"])
    points = story.get("points", 0)
    num_comments = story.get("num_comments", 0)
    parts = [
        f'<p><a href="{html.escape(article_url, quote=True)}"><strong>→ Read the article</strong></a> '
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
        hn_url = HN_ITEM_URL.format(id=it["id"])
        rendered_items.append(
            "    <item>\n"
            f"      <title>{xml_escape(it['title'])}</title>\n"
            f"      <link>{xml_escape(hn_url)}</link>\n"
            f'      <guid isPermaLink="false">hn-{it["id"]}</guid>\n'
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
