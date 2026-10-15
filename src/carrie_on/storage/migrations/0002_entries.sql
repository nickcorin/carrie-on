-- Store entry lifecycle independently from immutable delivery ownership.
-- depends: 0001_queue
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
    original_instructions TEXT NOT NULL,
    requested_limit_id TEXT,
    CHECK(anchor_message_id IS NOT NULL OR anchor_turn_id IS NOT NULL),
    CHECK((state = 'failed' AND length(trim(error)) > 0) OR state = 'cancelled' OR (state NOT IN ('failed','cancelled') AND error = ''))
);

-- Old failed/changed records were terminal. Retryable errors belonged to queued records.
INSERT INTO queue_entries (
    id, chat_id, title, account_id, limit_id, anchor_message_id, model,
    instructions, reset_windows, enrolled_while_busy, queued_at, updated_at,
    state, error, revision, finished_at, original_instructions, requested_limit_id
)
SELECT id, chat_id, title, account_id, limit_id, anchor_message_id, model,
    instruction, reset_windows, enrolled_while_busy, queued_at, updated_at,
    CASE
        WHEN state = 'changed' THEN 'stale'
        WHEN state = 'failed' THEN 'cancelled'
        WHEN state = 'queued' AND length(trim(COALESCE(error, ''))) > 0 THEN 'failed'
        ELSE state
    END,
    CASE
        WHEN state = 'failed' THEN COALESCE(error, reason)
        WHEN state = 'queued' AND length(trim(COALESCE(error, ''))) > 0 THEN error
        ELSE ''
    END,
    revision, finished_at,
    json_extract(create_request, '$.instruction'), json_extract(create_request, '$.limit_id')
FROM queue_items;

CREATE UNIQUE INDEX pending_chat ON queue_entries(chat_id) WHERE state IN ('queued', 'failed');
CREATE INDEX queue_order ON queue_entries(queued_at, id);
CREATE INDEX entry_state ON queue_entries(state);

CREATE TABLE delivery_attempts (
    id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES queue_entries(id),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL CHECK(expires_at >= created_at)
);
INSERT INTO delivery_attempts
SELECT id, queue_item_id, started_at,
    CASE WHEN finished_at IS NULL THEN lease_expires_at
         ELSE max(started_at, min(lease_expires_at, finished_at)) END
FROM deliveries;
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

CREATE TABLE queue_imports (id TEXT PRIMARY KEY);
DROP TABLE deliveries;
DROP TABLE queue_items;
DROP TABLE metadata;
PRAGMA user_version = 3;
