-- Remove saved creation inputs and batch import markers.
-- depends: 0002_entries
ALTER TABLE queue_entries DROP COLUMN original_instructions;
ALTER TABLE queue_entries DROP COLUMN requested_limit_id;
DROP TABLE queue_imports;
PRAGMA user_version = 4;
