# Work watch Path A — plugins/work_watch

Local Hermes force/carry plugin. Public-client delegated Graph watcher.

## Layout
- `auth.py` — device code, revoke, refresh via secret_scope/.env
- `reader.py` — mail delta + chats (zero calls when unsigned)
- `judge.py` — rules-first tiers
- `store.py` — SQLite under plugin-data/work_watch
- `service.py` — poll orchestration
- `dashboard/` — FastAPI at /api/plugins/work_watch/
- `desktop/plugin.js` — Settings "Work mail + Teams" + Work watch page

## Env
- `GRAPH_WATCH_CLIENT_ID` — public client app id (GM/IT registers; no secret)
- `GRAPH_WATCH_REFRESH_TOKEN` — set only by successful device-code sign-in
- `GRAPH_WATCH_TENANT` — optional (default organizations)
- `GRAPH_WATCH_CLOUD_JUDGE` — explicit opt-in; default off

## Gateway restart
Architect-only at 0 workers. Builder does not restart.
