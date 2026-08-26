# Anonymous Refresh Specification

## Goal

Refresh already-collected public Reel URLs without loading the saved Instagram
login profile, while preserving the original non-changing metadata.

## Requirements

- Apply only to `refresh --no-login`; normal logged-in collection keeps its
  exact-metric gate and existing behavior.
- For a URL with an existing history row, preserve `user_id`, `username`,
  `title`, `hashtags`, `audio_name`, `location_name`, `ad`, and `uploaded_at`.
- Store the new `collected_at` and recompute `days_since_upload`.
- Store only raw non-negative integer Network values for view, like, comment,
  and repost counts; do not reuse a prior metric or parse a compact UI label.
  If a public metric cannot be confirmed, append the refresh with that metric
  empty so the unknown value is explicit and the remaining URL queue continues.
- Try the follower API up to three times only when the result is a transient
  `web_error`.  For login-required, rate-limited, unavailable, or exhausted
  results, store an empty `follower_count` and continue to the next Reel.
- The follower result must never stop the no-login refresh job.
- Existing wide exports must show a subsequent collection, elapsed days, and
  the built-in metric deltas.  The new follower snapshot stays blank when the
  fresh value is unavailable.
- Do not print `[METRIC]` lines.

## Non-goals

- Do not infer missing digits from `1.6만`, `36K`, or any other rendered text.
- Do not refresh BGM/audio or overwrite any preserved static metadata.
- Do not alter the logged-in collector's exact source-field requirements.
