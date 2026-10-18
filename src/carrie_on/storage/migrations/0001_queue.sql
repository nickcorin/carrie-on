-- Create the queue schema.
CREATE TABLE queue_entries (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    title TEXT NOT NULL,
    account_id TEXT NOT NULL,
    limit_id TEXT NOT NULL,
    anchor_message_id TEXT,
    anchor_turn_id TEXT,
    model TEXT NOT NULL,
    instructions TEXT NOT NULL,
    reset_windows TEXT NOT NULL,
    enrolled_while_busy INTEGER NOT NULL CHECK(enrolled_while_busy IN (0, 1)),
    queued_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued', 'failed', 'stale', 'cancelled', 'sent')),
    error TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    finished_at REAL,
    CHECK(anchor_message_id IS NOT NULL OR anchor_turn_id IS NOT NULL),
    CHECK((state = 'failed' AND length(trim(error)) > 0) OR state = 'cancelled' OR (state NOT IN ('failed','cancelled') AND error = ''))
);

CREATE UNIQUE INDEX pending_chat ON queue_entries(chat_id) WHERE state IN ('queued', 'failed');
CREATE INDEX queue_order ON queue_entries(queued_at, id);
CREATE INDEX entry_state ON queue_entries(state);

CREATE TABLE delivery_attempts (
    id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES queue_entries(id),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL CHECK(expires_at >= created_at)
);
CREATE INDEX delivery_ownership ON delivery_attempts(entry_id, expires_at);
CREATE TRIGGER exclusive_delivery BEFORE INSERT ON delivery_attempts
WHEN EXISTS (
    SELECT 1 FROM delivery_attempts
    WHERE entry_id = NEW.entry_id
      AND expires_at > NEW.created_at AND created_at < NEW.expires_at
)
BEGIN
    SELECT RAISE(ABORT, 'delivery_active');
END;
