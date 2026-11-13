# hnrss

An RSS feed of Hacker News stories that:

- crossed a **score threshold** (default `125`),
- have aged at least **N hours** (default `8`, so top comments have settled), and
- include a **small tree of top comments** inline (see [Comment tree](#comment-tree)), skipping commenters whose HN accounts are under 3 years old, and
- thread in any comments by a short list of **notable commenters** (tptacek, patio11, simonw, tosh, plus a few OpenAI/Anthropic people who've disclosed their affiliation on HN — see `fetch.py`) that the top tree missed.

Feed URL: <https://elialbert.com/hnrss/feed.xml> (also `https://elialbert.github.io/hnrss/feed.xml`).

RSS `<link>` points to the article; the HN comments link is the first thing inside the body.

## How it works

```
GitHub Actions (cron, hourly at :07)
  → python fetch.py
      → Algolia HN search:   stories with points >= THRESHOLD, aged in [MIN_AGE, LOOKBACK]
      → Algolia HN search:   one query for comments by NOTABLE_AUTHORS on the new stories
      → HN Firebase API:     for each new story, walk kids[] in HN display order to build
                             the comment tree; look up commenter account ages;
                             walk parent links to thread notable comments in
      → write feed.xml + state.json
  → commit & push if changed
GitHub Pages
  → serves feed.xml from the repo root
```

Two APIs because they each do one thing better:

- **Algolia** (`hn.algolia.com/api/v1/search`) — easy filters (created_at_i, author, story). Great for discovery. `points` is no longer filterable there, so the score threshold is applied client-side.
- **HN Firebase** (`hacker-news.firebaseio.com/v0/item/{id}.json`) — `kids` is in **HN display order** (HN's ranking). Algolia's `children` array is creation-time order, which surfaces wrong "top" comments.

## Comment tree

`TREE_SHAPE` in `fetch.py` sets the shape. Each entry is one top-level comment:

```python
TREE_SHAPE = [
    {"direct": 3, "spine_extra": 2},  # Top #1: 3 direct replies; first reply followed 2 more levels
    {"direct": 2, "spine_extra": 2},  # Top #2
    {"direct": 2, "spine_extra": 2},  # Top #3
]
```

"Top" means HN's display order. Dead/deleted comments are skipped, as are
authors whose accounts are younger than `MIN_ACCOUNT_AGE_DAYS` (3 years). The
walk keeps going down `kids[]` until it finds enough that qualify, up to
`MAX_KIDS_TO_SCAN` (30) per parent. Account creation dates are cached in
`state.json` under `authors`.

## Notable commenters

`NOTABLE_AUTHORS` is a list of HN usernames worth reading wherever they show up.
Each run makes **one** Algolia request for their comments on that run's new
stories (`tags=comment,(author_…),(story_…)`). For each hit that isn't already
in the tree, up to `MAX_NOTABLE_PER_STORY` (6) per story:

1. Walk `parent` links up to the story to get the chain leading to the comment.
   Comments nested deeper than `MAX_NOTABLE_DEPTH` (5, top-level = 1) are
   skipped. Past that, the context chain swamps the item.
2. Merge that chain into the tree. If an ancestor is already shown, the comment
   is threaded in under it. Otherwise the chain is appended as an **Also**
   top-level entry. Ancestors are shown even if they fail the account-age
   filter, because the comment makes no sense without them.
3. Add the notable comment's top reply (account-age filter applies).

Direct replies to the story are taken first, then the oldest. Notable authors are
shown as **★ name** everywhere in the tree, including when they were
already in the top tree. If the Algolia query fails, the feed builds without
them.

## Files

| File | Purpose |
|---|---|
| `fetch.py` | The whole generator. Stdlib only — no `requirements.txt`. |
| `feed.xml` | The published RSS feed. Regenerated each run. |
| `state.json` | Story IDs we've already added, with their cached rendered HTML. Acts as the dedup set and lets us regenerate the feed without re-fetching. |
| `.github/workflows/update.yml` | Hourly cron + commit + push. |

## Tuning

Constants at the top of `fetch.py`:

```python
SCORE_THRESHOLD = 125            # min HN points
MIN_AGE_HOURS   = 8              # stories must be at least this old (lets top comments settle)
LOOKBACK_HOURS  = 72             # how far back to scan for candidates
MAX_ITEMS       = 100            # how many to retain in feed
GUID_PREFIX     = "hn3"          # bump to make readers re-ingest every item
MIN_ACCOUNT_AGE_DAYS = 3 * 365   # skip commenters with younger accounts
MAX_KIDS_TO_SCAN = 30            # per parent, how far down kids[] to look
TREE_SHAPE = [...]               # see "Comment tree"
NOTABLE_AUTHORS = ["tptacek", "patio11", "simonw", "tosh", "tedsanders", ...]
MAX_NOTABLE_PER_STORY = 6
MAX_NOTABLE_DEPTH = 5
```

Rendered HTML is cached per item in `state.json`, so changes affect **new items
only**. Bumping `GUID_PREFIX` doesn't re-render cached items either. It only
makes readers treat every item as new, which means Miniflux shows duplicates.

## Running locally

```bash
python3 fetch.py    # writes feed.xml + state.json in cwd
```

Stdlib only. First run on an empty `state.json` will hit the HN Firebase API once per candidate-story top-level kid plus account lookups (a few hundred requests for a backfill, a few dozen per new story on incremental runs, fewer as the author cache warms). No auth, no rate-limit issues in practice.

To force a clean rebuild: delete `state.json` and re-run. (Items older than `LOOKBACK_HOURS` won't be re-discovered though.)

## Manual cron trigger

GitHub repo → **Actions** → "Update feed" → **Run workflow**. Useful if you've just changed `SCORE_THRESHOLD` and want the feed to reflect it without waiting for the next hour.

## Pages config

Repo **Settings → Pages**: source = "Deploy from a branch", branch = `main`, folder = `/ (root)`. The whole repo is the site; `feed.xml` lives at the root.
