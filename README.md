# hnrss

An RSS feed of Hacker News stories that:

- crossed a **score threshold** (default `125`),
- have aged at least **N hours** (default `8`, so top comments have settled), and
- include the **top 4 comments** inline (top-level #1, its top reply, top-level #2, its top reply).

Feed URL: <https://elialbert.com/hnrss/feed.xml> (also `https://elialbert.github.io/hnrss/feed.xml`).

RSS `<link>` points to the article; the HN comments link is the first thing inside the body.

## How it works

```
GitHub Actions (cron, hourly at :07)
  → python fetch.py
      → Algolia HN search:   stories with points >= THRESHOLD, aged in [MIN_AGE, LOOKBACK]
      → HN Firebase API:     for each new story, walk kids[] in HN display order
                             to pick top comment + top reply + second comment + its top reply
      → write feed.xml + state.json
  → commit & push if changed
GitHub Pages
  → serves feed.xml from the repo root
```

Two APIs because they each do one thing better:

- **Algolia** (`hn.algolia.com/api/v1/search`) — easy numeric filters (points, created_at_i). Great for discovery.
- **HN Firebase** (`hacker-news.firebaseio.com/v0/item/{id}.json`) — `kids` is in **HN display order** (HN's ranking). Algolia's `children` array is creation-time order, which surfaces wrong "top" comments.

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
SCORE_THRESHOLD = 125    # min HN points
MIN_AGE_HOURS   = 8      # stories must be at least this old (lets top comments settle)
LOOKBACK_HOURS  = 72     # how far back to scan for candidates
MAX_ITEMS       = 100    # how many to retain in feed
```

To change what comments get pulled, see `pick_comments()` and `build_html()`.

## Running locally

```bash
python3 fetch.py    # writes feed.xml + state.json in cwd
```

Stdlib only. First run on an empty `state.json` will hit the HN Firebase API once per candidate-story top-level kid (a few hundred requests for a backfill, ~6 per new story on incremental runs). No auth, no rate-limit issues in practice.

To force a clean rebuild: delete `state.json` and re-run. (Items older than `LOOKBACK_HOURS` won't be re-discovered though.)

## Manual cron trigger

GitHub repo → **Actions** → "Update feed" → **Run workflow**. Useful if you've just changed `SCORE_THRESHOLD` and want the feed to reflect it without waiting for the next hour.

## Pages config

Repo **Settings → Pages**: source = "Deploy from a branch", branch = `main`, folder = `/ (root)`. The whole repo is the site; `feed.xml` lives at the root.
