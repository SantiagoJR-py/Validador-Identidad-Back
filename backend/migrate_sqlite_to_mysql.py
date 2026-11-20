"""One-off: copy the old SQLite event log into the configured database.

    python -m backend.migrate_sqlite_to_mysql backend/ids_events.sqlite3
Only `events` is copied; `results` is a 15-minute encrypted cache and is not worth migrating.
"""
import sqlite3
import sys
from datetime import datetime, timezone

import backend.app.main as m

src = sys.argv[1] if len(sys.argv) > 1 else 'backend/ids_events.sqlite3'
m.setup_db()
rows = sqlite3.connect(src).execute('SELECT session_id,event_type,detail,created_at FROM events ORDER BY id').fetchall()
with m.engine().begin() as db:
    db.execute(m.EVENTS.insert(), [dict(session_id=s, event_type=t[:40], detail=d, created_at=datetime.fromtimestamp(c, timezone.utc).replace(tzinfo=None)) for s, t, d, c in rows])
print(f'{len(rows)} events migrated')
