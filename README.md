# Instagram random-sample time-series collector

This project repeatedly collects public Instagram Professional-account data
through Meta's official Graph API and stores every observation as CSV. It does
not automate the Instagram website or bypass private-account access.

## Important target limitation

Meta's API cannot return a globally random Instagram account. Random sampling
therefore works from a candidate pool that you register or import. Candidates
must be visible Business/Creator accounts; personal and private accounts cannot
be collected with Business Discovery.

## 1. Configure Meta credentials

Copy `.env.example` to `.env` and enter the long-lived access token plus the ID
of your authorized Instagram Professional account:

```powershell
cd C:\instagram_data_set
Copy-Item .\.env.example .\.env
notepad .\.env
```

```text
INSTAGRAM_ACCESS_TOKEN=YOUR_LONG_LIVED_ACCESS_TOKEN
INSTAGRAM_IG_USER_ID=YOUR_AUTHORIZED_PROFESSIONAL_ACCOUNT_ID
INSTAGRAM_API_VERSION=v26.0
```

Never share or commit `.env`.

## 2. Prepare the random candidate pool

Create a CSV containing a `username` column. See `target_pool.example.csv`.

```csv
username
nasa
natgeo
instagram
```

Import it and check the registered pool:

```powershell
python .\instagram_collector.py target import .\target_pool.csv
python .\instagram_collector.py target list
```

You can also add candidates one at a time:

```powershell
python .\instagram_collector.py target add nasa
```

## 3. Create a random test experiment

This randomly selects 10 enabled candidates and creates collection jobs at
baseline, 1 hour, 4 hours, 12 hours, and 24 hours after experiment creation:

```powershell
python .\instagram_collector.py experiment start --sample-size 10 --schedule test
```

The exact random seed and selected usernames are saved in
`data\experiments.csv`, so the sample is reproducible and auditable.

The future production preset is already available:

```powershell
python .\instagram_collector.py experiment start --sample-size 100 --schedule production
```

Its milestones are baseline, 6 hours, 12 hours, 1 day, 3 days, 1 week, and 2
weeks. A custom schedule is also supported:

```powershell
python .\instagram_collector.py experiment start --sample-size 10 --offsets-hours 0,2,8,24
```

## 4. Run collection in the background

Start a hidden background collector. It checks once per minute and collects
only milestones that are due:

```powershell
.\start_background.ps1
```

Check status or stop it:

```powershell
.\status_background.ps1
python .\instagram_collector.py experiment status
.\stop_background.ps1
```

Background logs are stored in:

- `data\background_collector.log`
- `data\background_collector.error.log`
- `data\background_collector.pid`

The hidden process survives closing the PowerShell window but stops when
Windows restarts or the user signs out. Run `start_background.ps1` again after
that; all pending jobs are stored in CSV and resume automatically.

To run only one experiment in the background:

```powershell
.\start_background.ps1 -ExperimentId exp_YYYYMMDDTHHMMSSZ_12345678
```

## Manual collection commands

Run all milestones currently due once, without a background process:

```powershell
python .\instagram_collector.py experiment run-due
```

Collect every enabled target immediately without an experiment:

```powershell
python .\instagram_collector.py collect
```

## Foreground Reels web collection

This optional mode opens a visible Edge/Chrome window. Sign in manually, open
the Reels feed, and return to the PowerShell window. The collector records each
real Reel URL as the visible feed advances; it does not guess shortcodes or
store an Instagram password.

Collect 50 unique URLs. `IntervalSeconds` is the fallback delay when required
Reel data is missing; a complete Reel advances after 0.5 seconds:

```powershell
.\start_reels_web.ps1 -MaxItems 50 -IntervalSeconds 5
```

Run the same automatic collection without showing a browser window. Sign in
once with the visible command first so the saved browser profile can be reused:

```powershell
.\start_reels_web.ps1 -MaxItems 50 -IntervalSeconds 2 -Background
```

Collect only Reels whose caption hashtags partially match either side of an
OR query:

```powershell
.\start_reels_web.ps1 -HashtagQuery '"맛집" OR "서울맛집"' -MaxItems 50 -IntervalSeconds 2 -Background
```

The collector gathers Reel links from both hashtag pages, interleaves the two
candidate lists, and stores a Reel only when an actual hashtag token contains
at least one query term. For example, this query accepts `#맛집`, `#강남맛집`,
`#서울맛집`, and `#서울맛집추천`, but it does not match ordinary caption text
that is not part of a hashtag.

To scroll in the browser yourself and confirm each capture with Enter:

```powershell
.\start_reels_web.ps1 -MaxItems 50 -Manual
```

Refresh every Reel already listed in the workbook without showing a browser:

```powershell
.\refresh_reels_xlsx.ps1 -IntervalSeconds 2 -Background
```

Output is saved to `data_web\reels_web.csv` and synchronized to
`data_web\instagram_data.xlsx`. The collector stores URL, the Instagram web
`user_id`, username, Reel title
(the visible caption), hashtags, audio name, upload time, likes, comments, and
reposts. When Instagram exposes them, it also stores the location name,
latitude, and longitude. The `ad` field is `true` when the page shows an exact
`광고`/`후원됨`/`Sponsored`/`Paid partnership` label or the Reel response contains
an explicit ad, sponsor, paid-partnership, or ad-ID signal; otherwise it is
`false`. The same Reel row also contains `follower_count`, its collection time,
and the follower lookup status. Before reading a caption it clicks the visible
`더 보기`/`More` control.
Hashtags are extracted from the full expanded caption, while the stored title
excludes hashtag tokens, is limited to 300 characters, and receives `...` when
truncated. It also keeps
the collection time. Likes, comments, and reposts are stored only as parsed
numbers; the abbreviated UI text is not retained. Instagram UI changes can
make some best-effort fields blank; the URL is the stable primary field.

For upload time, the collector first uses a visible HTML `time` element. When
the Reels fullscreen UI omits it, the collector reuses the matching `taken_at`
value from the Reel JSON response that the page already downloaded. This does
not make an extra API request and does not require an API token.

The same matching Reel response supplies the account username, full caption,
audio metadata, and exact integer engagement counts. Music is stored as
`artist · title`; original audio falls back to the original-audio title and
creator username. DOM count labels are used only when response metadata is
unavailable.

The `user_id` is taken from the same Reel JSON response, so it does not add a
profile visit or another request. Each newly seen user is also upserted into
`data_web\users.csv`. Follower counts do not use a Meta access token or the
Graph API. A separate headless browser in the same Node process copies the
saved login session and visits newly seen public profiles one at a time while
Reel scrolling continues. When Reel collection ends, the visible Reel browser
closes but the headless follower queue stays alive. The terminal prints each
result as `[Follower completed/queued] @username -> follower_count`. After a
successful follower count, the next lookup starts after 0.5 seconds. Failed or
incomplete lookups keep the configured follower interval (eight seconds by
default), and results cached within one hour are reused. Login, challenge, and
temporary-access-limit pages stop the
follower queue instead of attempting to bypass them. Lookup history is appended
to `data_web\follower_lookups.csv` with source `instagram_web`.

To enrich pending users again after Reel collection has finished:

```powershell
.\update_followers.ps1 -IntervalSeconds 8 -CacheHours 1
```

This command reuses the saved Instagram login and runs the profile browser in
the background. Use `-Force` only when every saved user must be retried
regardless of the one-hour cache. Successful follower results are merged into
the matching rows of `reels_web.csv`; `users.csv` and `follower_lookups.csv`
remain available as supporting history worksheets in `instagram_data.xlsx`.
Each initial and repeated Reel snapshot stores `reaction_rate` as
`like_count / follower_count`. It remains blank until a positive follower count
is available and is displayed as a percentage in XLSX.
If Instagram requests login or an account check, the remaining users are left
deferred for a later retry. After the follower queue finishes, the command
automatically synchronizes the CSV files to XLSX. If `instagram_data.xlsx` is
open in Excel, the latest Reel and follower data is written to
`instagram_data_updated.xlsx` in the same folder instead of being skipped.

To revisit every Reel already listed in the XLSX workbook and append a fresh
engagement snapshot:

```powershell
.\refresh_reels_xlsx.ps1 -IntervalSeconds 2
```

Close `instagram_data.xlsx` before running this command. It reads URLs from the
`reels_web` sheet in row order and visits each unique URL once in the visible
browser. A Reel keeps one row. Each repeat collection adds a right-side column
group such as `2nd collect_collected_at`, `2nd collect_like_count`, and
`2nd collect_comment_count`. The collection-time cell includes elapsed time
from the initial collection, for example
`2026-08-04 01:00:00 (+2Hour)`. In XLSX, `days_since_upload` displays elapsed
time below one day as whole hours such as `+20hours`, and truncates longer
values to whole days such as `+12day`. The CSV keeps the original decimal value
for calculations. Repeat groups contain only collection time,
upload age, likes, comments, reposts, and followers. Metric values identical
to the previous snapshot remain blank. Changed metrics display the current
value and the change from the most recent actual value, such as `21(+4)` and
then `29(+8)`. Raw CSV values remain numeric for later comparisons. The final
step synchronizes all CSV data back into the same `instagram_data.xlsx`
workbook.

CSV timestamps remain ISO UTC so they are unambiguous. During XLSX
synchronization, every date/time column is displayed in Korea Standard Time
(UTC+9), including `follower_lookups.collected_at`.

If an older compatible `reels_web.csv` schema exists, elapsed-time snapshot
columns such as `+2Hour_*` are migrated in order to `2nd collect_*`,
`3rd collect_*`, and so on without losing their engagement values. An
incompatible unknown schema is preserved as a timestamped
`reels_web_legacy_*.csv` file before the new schema is created.
When the same Reel appears again, it updates that Reel's horizontal time-series
columns instead of adding another row.

## CSV outputs

All output is appended or safely updated under `data`:

- `experiments.csv`: random seed, selected targets, preset, experiment status.
- `experiment_jobs.csv`: every scheduled milestone and its completion/delay.
- `account_timeseries.csv`: follower/following/media counts per milestone.
- `media_timeseries.csv`: post like/comment counts per milestone.
- `posts.csv`: one current metadata row per post.
- `api_usage.csv`: rate-limit header observations.
- `runs.csv`: every API collection run and result.

The time-series files include `experiment_id`, `milestone`, and
`scheduled_for`, allowing baseline/1h/4h/12h/24h comparisons even when a run is
slightly delayed.

## API safety

The collector saves each completed response and stops before the next request
when `X-Business-Use-Case-Usage` or `X-App-Usage` reaches 90 percent. HTTP 429
and Instagram rate-limit errors also stop collection without deleting saved
history. To change the threshold for the background process:

```powershell
.\start_background.ps1 -UsageThreshold 80
```
