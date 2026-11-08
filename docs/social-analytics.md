# Social media analytics — reports for marketing departments

HeyCatch sells social listening and analytics. The ecosystem's
version is `core/social_analytics.py` plus the program
`ecosystem/social_analytics.py`: it turns a platform export into a
marketing report with graphs — engagement by channel, best posting
time, follower growth, top posts, keyword sentiment, and
recommendations.

```bash
python ecosystem/social_analytics.py report --data export.csv
python ecosystem/social_analytics.py report --data export.json --channel instagram
python ecosystem/social_analytics.py report --data export.csv --title "October social report"
```

Output, in `reports/`:

- `social_<date>_<slug>.md` — the report, linking to the SVG charts
- `social_<date>_<slug>.html` — the same report with the SVG charts
  **embedded inline**, so the graphs are visible when the file is
  opened in a browser (Markdown cannot embed SVG on GitHub preview)
- `social_<date>_<slug>_engagement.svg` — engagement rate by channel
- `social_<date>_<slug>_posting_time.svg` — average interactions by hour
- `social_<date>_<slug>_followers.svg` — follower growth over time

**Every report includes the graphs.** A chart is only written when
the data supports it (no reach and no followers means no engagement
chart; no time data means no posting-time chart; fewer than two
follower observations means no growth chart) — a missing chart is
reported in the "Unavailable" list, never drawn empty.

## Input formats

The input is always a file the owner exported from their own
platform accounts. There is no live API: X and Threads are
login-walled with no credentials in the repo, and the other
platforms need the owner's own account tokens. Reddit live data
is a follow-up, not this build.

### CSV — one row per post, plus optional follower-snapshot rows

Column names are case-insensitive; these aliases are accepted:
`post id`/`id` → `post_id`, `text`/`title` → `caption`,
`type` → `post_type`, `posted at` → `posted_at`,
`follower count` → `followers`, `row type` → `row_type`.

| Column | Meaning |
| --- | --- |
| `date` | Post or snapshot date (YYYY-MM-DD, also YYYY/MM/DD or ISO datetime) |
| `time` | Posting time HH:MM (or `posted_at`, an ISO datetime) |
| `channel` | instagram, x, tiktok, youtube, facebook, linkedin, threads — anything else is kept as-is |
| `post_id` | Optional post identifier |
| `caption` | Caption / text / title — used for sentiment and top-post labels |
| `post_type` | Optional: reel, photo, video, text, link, … |
| `likes`, `comments`, `shares`, `saves` | Counts (`comments` is always a count, never text) |
| `reach`, `impressions`, `views` | Counts |
| `followers` | Follower count on that date |
| `row_type` | Optional: `post` or `snapshot`. Without it, a row with a follower count and no engagement columns is a snapshot |

Numbers may be plain (`1234`), comma-grouped (`"1,234"`) or
suffixed (`1.2k` = 1200, `2.5m`). A row that cannot be parsed (bad
date, no channel, no numbers at all) is skipped and counted, never
silently treated as zero. An export with zero usable rows is
rejected with an error.

### JSON

Either a list of objects with the same field names, or an object
with `"posts"` and/or `"snapshots"` lists.

### Example CSV (illustrative numbers — format only, not real data)

```csv
date,time,channel,post_id,caption,post_type,likes,comments,shares,saves,reach,impressions,views,followers,row_type
2026-11-02,09:00,instagram,ig1,Amazing new drop love it!,photo,100,10,5,0,1000,1200,,,post
2026-11-09,09:00,instagram,ig2,Great photos thank you all,photo,120,12,8,0,1100,1300,,,post
2026-11-03,18:00,instagram,ig3,New stock arrived,reel,20,2,0,0,500,600,,,post
2026-11-10,18:00,instagram,ig4,Behind the scenes,photo,25,3,1,0,550,700,,,post
2026-11-02,12:00,tiktok,tt1,Watch this amazing trick,video,300,20,40,0,,5000,5000,,post
2026-11-09,12:00,tiktok,tt2,Terrible delay awful service sorry,video,280,18,35,0,,4800,4800,,post
2026-11-03,12:00,tiktok,tt3,Quick tip of the day,video,50,5,5,0,,900,900,,post
2026-11-10,12:00,tiktok,tt4,Part two of the series,video,45,4,4,0,,850,850,,post
2026-11-01,,instagram,,,,,,,,,,,5000,snapshot
2026-11-10,,instagram,,,,,,,,,,,5200,snapshot
2026-11-01,,tiktok,,,,,,,,,,,800,snapshot
2026-11-10,,tiktok,,,,,,,,,,,750,snapshot
```

Save it as `export.csv` and run `report --data export.csv` to see
the full report. The numbers above are invented to show the format;
they are not any business's real data.

## What the report computes

- **Per channel and overall:** posts, likes, comments, shares,
  saves, interactions, reach, impressions, latest follower count,
  engagement rate, top posts by engagement.
- **Engagement rate** is interactions / reach when reach is
  present, else interactions / followers, else unavailable. The
  report always prints which denominator was used — a rate without
  its denominator is a lie.
- **Best posting hour and weekday** by average interactions. A
  bucket needs at least 2 posts to win, so one viral post cannot
  crown an hour. No time data → the section is omitted and the
  report says so.
- **Follower growth** from dated follower observations (snapshots
  or posts that carry a follower count): first → last, absolute
  and percentage change, per-day rate. One observation is not
  growth; it is reported as unavailable.
- **Keyword sentiment, not AI.** A fixed positive/negative word
  list is counted in captions and titles (word boundaries,
  case-insensitive). The report labels it "keyword sentiment, not
  AI" every time, and omits it entirely when there is no text.
- **Recommendations** are deterministic rules, max 5: the weakest
  channel, the best posting time, flat or falling followers,
  missing reach data, negative-skewing sentiment.

## The charts — vector only

All charts are hand-built **SVG strings** (no matplotlib, no new
dependency, no raster images anywhere in this feature):

- **Engagement by channel** — vertical bars, rate in percent.
- **Best posting time** — bars of average interactions per hour,
  the best hour highlighted.
- **Follower growth** — lines, one per channel, with dated points.

SVG is vector: the charts stay sharp at any size and can be
dropped straight into a deck or a web page.

## What it cannot do

- **No live API.** X and Threads are login-walled, no credentials
  exist; the other platforms need the owner's own tokens. Live
  platform data comes from the owner's own exports. Reddit live
  data is the follow-up, not this build.
- **No competitor data, ad spend, or why a post worked** — only
  what the export recorded.
- **No auto-posting and no scheduling.** This is analysis and
  reporting; nothing is published.
- **Sentiment is keyword counting**, not an AI reading comments.
- **Missing metrics are "unavailable", never zero-filled.** A
  report that prints a 0% engagement rate for a channel with no
  reach data would be wrong; this one prints "unavailable" and
  says what to export next time.
