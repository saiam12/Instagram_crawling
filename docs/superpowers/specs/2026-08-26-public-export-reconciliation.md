# Public Export Reconciliation Design

## Goal

Keep all user-facing CSV, JSON, and XLSX exports synchronized while preserving separate raw collection histories for future collection and refresh operations.

## Public formats

- `users.csv` and `users.xlsx` use the same row-history fields: `collection_number`, identity fields, `follower_count`, `follower_count_change`, and `collected_at`.
- `reels.csv`, `reels.json`, `reels.xlsx`, and any temporary `reels_updated.xlsx` use the same display projection as `reels.xlsx`, including refresh columns and elapsed-time columns.
- `collector_status.json`, `collector.lock.json`, and files under `.collector` are operational or raw-history files and are not public exports.

## Raw histories

- Reels continue to use `.collector/reels_history_active.csv`.
- Follower collection moves its wide source history to `.collector/users_history_active.csv`; the existing public `users.csv` is migrated once when no active history exists.

## Reconciliation command

`collector.ps1 reconcile` compares `reels_updated.xlsx` against the active Reel history. It adds missing snapshots and fills only blank stored fields, regenerates all public outputs, then removes `reels_updated.xlsx` only after the regenerated `reels.xlsx` is written successfully. Matching records use canonical Reel URL plus collection timestamp.
