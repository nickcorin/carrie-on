-- Establish the original SQLite schema, including databases created before migration tracking.
CREATE TABLE IF NOT EXISTS queue_items (
    id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, title TEXT NOT NULL,
    account_id TEXT NOT NULL, limit_id TEXT NOT NULL, anchor_message_id TEXT NOT NULL,
    model TEXT NOT NULL, instruction TEXT NOT NULL, reset_windows TEXT NOT NULL,
    enrolled_while_busy INTEGER NOT NULL CHECK(enrolled_while_busy IN (0, 1)),
    queued_at REAL NOT NULL, updated_at REAL NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued', 'sent', 'changed', 'cancelled', 'failed')),
    reason TEXT NOT NULL, deferrals INTEGER NOT NULL DEFAULT 0 CHECK(deferrals >= 0),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0), error TEXT, finished_at REAL,
    create_request TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    id TEXT PRIMARY KEY, queue_item_id TEXT NOT NULL REFERENCES queue_items(id),
    instruction TEXT NOT NULL, started_at REAL NOT NULL, lease_expires_at REAL NOT NULL,
    sending_at REAL, finished_at REAL,
    outcome TEXT CHECK(outcome IN ('sent', 'changed', 'deferred', 'failed', 'expired')),
    reason TEXT, receipt TEXT,
    CHECK(lease_expires_at > started_at),
    CHECK((finished_at IS NULL) = (outcome IS NULL))
);
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
