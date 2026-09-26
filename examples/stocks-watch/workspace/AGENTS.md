# stocks-watch

Example workspace for coara cron / file-watch event sources with optional `handle: janitor`.

- Watchlist thresholds and data paths are configured in janitor prompts or `{coara_home}/workspaces-meta/stocks-watch.yaml`.
- Drop JSON payloads under `inbox/` to fire the file-watch event source (when enabled); set `handle: janitor` when the caretaker should review the event.
