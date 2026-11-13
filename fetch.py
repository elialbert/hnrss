#!/usr/bin/env python3
"""Generate an RSS feed of aged, high-score HN stories with a comment tree inline.

For each story above SCORE_THRESHOLD and at least MIN_AGE_HOURS old, we fetch
a small tree of top comments shaped by TREE_SHAPE: a few top-level comments,
each with its direct replies and a deeper spine off the first reply.

Commenters whose HN accounts are younger than MIN_ACCOUNT_AGE_DAYS are skipped
— we keep walking kids[] until enough qualifying authors are found, capped by
MAX_KIDS_TO_SCAN so a story full of new accounts doesn't blow up the request
budget. Account-age lookups are cached in state.json across runs.

Notable commenters (NOTABLE_AUTHORS): one Algolia query per run finds any of
their comments on the new stories. Each one not already shown is threaded into
the tree along with its ancestor chain (shown regardless of the age filter, as
context) plus its top reply. Notable authors are starred wherever they appear.

Story discovery uses Algolia's search API. Comment selection and account-age
lookups use HN's Firebase API. Algolia's children[] is creation-time order;
Firebase's kids[] is HN display order (HN's ranking), which is what we want
for "top".
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
LOOKBACK_HOURS = 72
MAX_ITEMS = 100
GUID_PREFIX = "hn3"  # bump when item rendering changes to force readers to re-ingest

MIN_ACCOUNT_AGE_DAYS = 3 * 365  # filter out commenters with accounts younger than this
MAX_KIDS_TO_SCAN = 30  # per parent, cap how deep we look for qualifying kids

# Per top-level slot: how many direct replies + how many extra spine levels off
# the first reply. spine_extra=2 means first_reply → its_top_reply → its_top_reply.
TREE_SHAPE = [
    {"direct": 3, "spine_extra": 2},
    {"direct": 2, "spine_extra": 2},
    {"direct": 2, "spine_extra": 2},
]

# Commenters worth surfacing even when they're not in the top-ranked tree.
NOTABLE_AUTHORS = [
    "tptacek", "patio11", "simonw", "tosh",
    # AI lab people with self-disclosed affiliations (checked 2026-11-13)
    "tedsanders",       # OpenAI
    "karpathy",         # ex-OpenAI, independent
    "hauntsaninja",     # OpenAI
    "neonbjb",          # OpenAI
    "changoplatanero",  # OpenAI
    "eschluntz",        # Anthropic
    "bcherny",          # Anthropic, Claude Code
]
MAX_NOTABLE_PER_STORY = 3  # cap on threaded-in notable comments per story
MAX_NOTABLE_DEPTH = 5  # skip notable comments nested deeper (top-level = 1)

STATE_FILE = Path("state.json")
FEED_FILE = Path("feed.xml")

ALGOLIA_SEARCH = "https://hn.algolia.com/api/v1/search"
HN_FIREBASE_ITEM = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
HN_FIREBASE_USER = "https://hacker-news.firebaseio.com/v0/user/{name}.json"
HN_ITEM_URL = "https://news.ycombinator.com/item?id={id}"

_item_cache = {}  # per-run Firebase item cache; ancestor walks revisit tree items


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "hnrss-generator/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def find_candidate_stories():
    # Algolia removed `points` from its filterable attributes — filter age server-side
    # and points client-side. /search is popularity-sorted, so top 1000 covers our window.
    now = int(time.time())
    older_than = now - MIN_AGE_HOURS * 3600
    newer_than = now - LOOKBACK_HOURS * 3600
    params = {
        "tags": "story",
        "numericFilters": ",".join([
            f"created_at_i<={older_than}",
            f"created_at_i>={newer_than}",
        ]),
        "hitsPerPage": 1000,
    }
    hits = fetch_json(f"{ALGOLIA_SEARCH}?{urllib.parse.urlencode(params)}").get("hits", [])
    return [h for h in hits if (h.get("points") or 0) >= SCORE_THRESHOLD]


def fetch_hn_item(item_id):
    item_id = int(item_id)
    if item_id not in _item_cache:
        _item_cache[item_id] = fetch_json(HN_FIREBASE_ITEM.format(id=item_id))
    return _item_cache[item_id]


def fetch_user_created(username, cache):
    """Return account creation Unix timestamp (or None on miss/error).
    Mutates `cache` in place — misses cache as None too, so we don't refetch."""
    if username in cache:
        return cache[username]
    try:
        user = fetch_json(HN_FIREBASE_USER.format(name=urllib.parse.quote(username, safe="")))
    except Exception:
        cache[username] = None
        return None
    created = user.get("created") if user else None
    cache[username] = created
    return created


def is_live_comment(item):
    return bool(item and not item.get("deleted") and not item.get("dead") and item.get("text"))


def passes_age_filter(comment, cache, threshold_ts):
    by = comment.get("by")
    if not by:
        return False
    created = fetch_user_created(by, cache)
    return created is not None and created <= threshold_ts


def collect_filtered_kids(parent, target, cache, threshold_ts):
    """Walk parent.kids in HN display order, return up to `target` comments that
    are live and whose authors pass the age filter. Bounded by MAX_KIDS_TO_SCAN."""
    out = []
    scanned = 0
    for kid_id in parent.get("kids") or []:
        if len(out) >= target or scanned >= MAX_KIDS_TO_SCAN:
            break
        scanned += 1
        kid = fetch_hn_item(kid_id)
        if not is_live_comment(kid):
            continue
        if not passes_age_filter(kid, cache, threshold_ts):
            continue
        out.append(kid)
    return out


def build_branch(parent, direct, spine_extra, cache, threshold_ts):
    """Return [{comment, replies}, ...] for `parent`'s direct replies. The first
    reply gets a recursive spine of length `spine_extra` (siblings stay shallow)."""
    if direct <= 0 or parent is None:
        return []
    replies = collect_filtered_kids(parent, direct, cache, threshold_ts)
    out = []
    for i, reply in enumerate(replies):
        children = (
            build_branch(reply, 1, spine_extra - 1, cache, threshold_ts)
            if i == 0 and spine_extra > 0
            else []
        )
        out.append({"comment": reply, "replies": children})
    return out


def build_tree(story_id, cache, threshold_ts):
    """Build the per-story comment tree per TREE_SHAPE."""
    story = fetch_hn_item(story_id)
    if not story:
        return []
    top_levels = collect_filtered_kids(story, len(TREE_SHAPE), cache, threshold_ts)
    tree = []
    for spec, top in zip(TREE_SHAPE, top_levels):
        replies = build_branch(top, spec["direct"], spec["spine_extra"], cache, threshold_ts)
        tree.append({"comment": top, "replies": replies})
    return tree


def find_notable_comments(story_ids):
    """One Algolia query for comments by NOTABLE_AUTHORS on any of `story_ids`.
    Returns {story_id: [hit, ...]}. Best-effort: on error returns {}."""
    if not NOTABLE_AUTHORS or not story_ids:
        return {}
    tags = ",".join([
        "comment",
        "(" + ",".join(f"author_{a}" for a in NOTABLE_AUTHORS) + ")",
        "(" + ",".join(f"story_{sid}" for sid in story_ids) + ")",
    ])
    params = {"tags": tags, "hitsPerPage": 1000}
    try:
        hits = fetch_json(
            f"{ALGOLIA_SEARCH}_by_date?{urllib.parse.urlencode(params)}"
        ).get("hits", [])
    except Exception as e:
        print(f"notable-author search failed: {e}")
        return {}
    by_story = {}
    for h in hits:
        by_story.setdefault(str(h.get("story_id")), []).append(h)
    return by_story


def tree_ids(nodes):
    ids = set()
    for node in nodes:
        ids.add(node["comment"]["id"])
        ids |= tree_ids(node["replies"])
    return ids


def ancestor_chain(comment, story_id):
    """[top-level, ..., comment] following parent links up to the story.
    None if the chain is broken (deleted/missing item, or wrong story)."""
    chain = [comment]
    item = comment
    for _ in range(50):  # HN threads don't nest this deep; guards against loops
        parent_id = item.get("parent")
        if parent_id is None:
            return None
        if str(parent_id) == str(story_id):
            return list(reversed(chain))
        item = fetch_hn_item(parent_id)
        if not item:
            return None
        chain.append(item)
    return None


def thread_in(tree, chain):
    """Merge chain (top-level → target) into tree, reusing nodes already present."""
    nodes = tree
    for comment in chain:
        node = next((n for n in nodes if n["comment"]["id"] == comment["id"]), None)
        if node is None:
            node = {"comment": comment, "replies": []}
            nodes.append(node)
        nodes = node["replies"]
    return node


def add_notable_comments(tree, story_id, hits, cache, threshold_ts):
    """Thread up to MAX_NOTABLE_PER_STORY notable comments (not already shown)
    into `tree`, each with its ancestors and its top reply."""
    # Shallowest first (direct replies to the story), then oldest — earlier
    # comments are the ones the thread formed around.
    hits = sorted(hits, key=lambda h: (str(h.get("parent_id")) != str(story_id),
                                       h.get("created_at_i") or 0))
    added = 0
    for h in hits:
        if added >= MAX_NOTABLE_PER_STORY:
            break
        cid = int(h["objectID"])
        if cid in tree_ids(tree):
            continue
        comment = fetch_hn_item(cid)
        if not is_live_comment(comment):
            continue
        chain = ancestor_chain(comment, story_id)
        if not chain or len(chain) > MAX_NOTABLE_DEPTH:
            continue
        if not all(is_live_comment(c) for c in chain):
            continue
        node = thread_in(tree, chain)
        if not node["replies"]:
            node["replies"] = [
                {"comment": r, "replies": []}
                for r in collect_filtered_kids(comment, 1, cache, threshold_ts)
            ]
        added += 1
    return added


def article_host(url):
    """e.g. 'https://www.foo.com/x' → 'foo.com'; empty string for unparseable / non-http."""
    if not url:
        return ""
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return ""
    return host.removeprefix("www.")


def render_comment_html(comment, label):
    by = comment.get("by") or "?"
    author = html.escape(by)
    if by in NOTABLE_AUTHORS:
        author = f"★ <strong>{author}</strong>"
    body = comment.get("text") or ""  # already HTML from Firebase
    return (
        f'<p>{label} — <em>{author}</em>:</p>\n'
        f"<blockquote>{body}</blockquote>"
    )


def render_replies(replies, depth):
    arrow = "↳" * depth
    parts = []
    multiple = len(replies) > 1
    for j, node in enumerate(replies, start=1):
        label = f"{arrow} reply {j}" if multiple else f"{arrow} reply"
        parts.append(render_comment_html(node["comment"], label))
        if node["replies"]:
            parts.append("<blockquote>")
            parts.append(render_replies(node["replies"], depth + 1))
            parts.append("</blockquote>")
    return "\n".join(parts)


def render_tree(tree, num_top):
    parts = []
    for i, node in enumerate(tree, start=1):
        # Nodes past the TREE_SHAPE slots were threaded in for a notable author.
        label = f"<strong>Top #{i}</strong>" if i <= num_top else "<strong>Also</strong>"
        parts.append(render_comment_html(node["comment"], label))
        if node["replies"]:
            parts.append("<blockquote>")
            parts.append(render_replies(node["replies"], 1))
            parts.append("</blockquote>")
    return "\n".join(parts)


def build_html(story, tree, num_top):
    sid = story["objectID"]
    article_url = story.get("url") or HN_ITEM_URL.format(id=sid)
    hn_url = HN_ITEM_URL.format(id=sid)
    points = story.get("points", 0)
    num_comments = story.get("num_comments", 0)
    host = article_host(story.get("url"))
    host_tag = f' <small>({html.escape(host)})</small>' if host else ""
    parts = [
        f'<p><a href="{html.escape(article_url, quote=True)}"><strong>→ Article</strong></a>{host_tag}</p>',
        f'<p><a href="{html.escape(hn_url, quote=True)}">→ HN comments</a> '
        f"&nbsp;·&nbsp; {points} points &nbsp;·&nbsp; {num_comments} comments</p>",
        "<hr>",
        render_tree(tree, num_top),
    ]
    return "\n".join(parts)


def short_summary(story):
    return (
        f'{story.get("points", 0)} points, {story.get("num_comments", 0)} comments. '
        f"See description for top comments."
    )


def load_state():
    if STATE_FILE.exists():
        s = json.loads(STATE_FILE.read_text())
        s.setdefault("authors", {})
        return s
    return {"items": [], "authors": {}}


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
    cache = state["authors"]
    threshold_ts = int(time.time()) - MIN_ACCOUNT_AGE_DAYS * 86400

    candidates = [s for s in find_candidate_stories() if s["objectID"] not in seen]
    notable = find_notable_comments([s["objectID"] for s in candidates])
    added = notable_added = 0
    for story in candidates:
        sid = story["objectID"]
        tree = build_tree(sid, cache, threshold_ts)
        num_top = len(tree)
        notable_added += add_notable_comments(
            tree, sid, notable.get(sid, []), cache, threshold_ts
        )
        state["items"].append({
            "id": sid,
            "title": story.get("title") or "(no title)",
            "created_at_i": story["created_at_i"],
            "article_url": story.get("url") or HN_ITEM_URL.format(id=sid),
            "summary": short_summary(story),
            "html": build_html(story, tree, num_top),
        })
        added += 1

    state["items"].sort(key=lambda x: x["created_at_i"], reverse=True)
    state["items"] = state["items"][:MAX_ITEMS]

    save_state(state)
    write_feed(state["items"])
    print(
        f"Added {added} new items ({notable_added} notable comments threaded in); feed has {len(state['items'])} total; "
        f"author cache: {len(cache)} entries"
    )


if __name__ == "__main__":
    main()
