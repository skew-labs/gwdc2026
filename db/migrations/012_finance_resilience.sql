ALTER TABLE finance_service_journal
  ADD COLUMN IF NOT EXISTS journal_version smallint NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS stream_id text,
  ADD COLUMN IF NOT EXISTS stream_sequence bigint,
  ADD COLUMN IF NOT EXISTS occurred_at timestamptz NOT NULL DEFAULT clock_timestamp();

ALTER TABLE finance_service_journal
  DROP CONSTRAINT IF EXISTS finance_service_journal_version_shape;
ALTER TABLE finance_service_journal
  ADD CONSTRAINT finance_service_journal_version_shape CHECK (
    (journal_version = 1 AND stream_id IS NULL AND stream_sequence IS NULL)
    OR
    (journal_version = 2 AND stream_id IS NOT NULL AND stream_sequence > 0)
  );

CREATE UNIQUE INDEX IF NOT EXISTS finance_service_journal_stream_sequence
  ON finance_service_journal(stream_id, stream_sequence)
  WHERE journal_version = 2;

CREATE TABLE IF NOT EXISTS finance_service_journal_heads (
  stream_id text PRIMARY KEY,
  sequence bigint NOT NULL CHECK (sequence >= 0),
  event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE OR REPLACE FUNCTION finance_reject_journal_rewrite()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'finance service journal is append-only';
END;
$$;

DROP TRIGGER IF EXISTS finance_service_journal_append_only
  ON finance_service_journal;
CREATE TRIGGER finance_service_journal_append_only
  BEFORE UPDATE OR DELETE ON finance_service_journal
  FOR EACH ROW EXECUTE FUNCTION finance_reject_journal_rewrite();

REVOKE UPDATE, DELETE, TRUNCATE ON finance_service_journal FROM PUBLIC,
  gwdc_finance_api, gwdc_finance_worker;
REVOKE ALL ON finance_service_journal_heads FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON finance_service_journal_heads
  TO gwdc_finance_api, gwdc_finance_worker;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public
  TO gwdc_finance_api, gwdc_finance_worker;

REVOKE ALL ON FUNCTION finance_reject_journal_rewrite() FROM PUBLIC;
