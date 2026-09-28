BEGIN;

CREATE TABLE IF NOT EXISTS finance_service_records (
  scope_hash text NOT NULL,
  tenant_id text NOT NULL,
  owner_id text NOT NULL,
  wallet text NOT NULL,
  network text NOT NULL,
  record_kind text NOT NULL,
  record_id text NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  body_json jsonb NOT NULL,
  body_hash text NOT NULL CHECK (body_hash ~ '^[0-9a-f]{64}$'),
  updated_at timestamptz NOT NULL,
  PRIMARY KEY (scope_hash, record_kind, record_id)
);

CREATE TABLE IF NOT EXISTS finance_service_jobs (
  job_id text PRIMARY KEY CHECK (job_id ~ '^[0-9a-f]{64}$'),
  scope_hash text CHECK (scope_hash IS NULL OR scope_hash ~ '^[0-9a-f]{64}$'),
  scope_json jsonb,
  account_key text CHECK (account_key IS NULL OR account_key ~ '^[0-9a-f]{64}$'),
  role text NOT NULL CHECK (role IN ('ALPHA','VAULT','WATCH')),
  routine_id text NOT NULL,
  job_kind text NOT NULL,
  subject_id text NOT NULL,
  dependency_hash text NOT NULL CHECK (dependency_hash ~ '^[0-9a-f]{64}$'),
  payload_json jsonb NOT NULL,
  payload_hash text NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
  priority integer NOT NULL CHECK (priority BETWEEN -100 AND 100),
  status text NOT NULL CHECK (status IN
    ('PENDING','LEASED','RETRY_WAIT','SUCCEEDED','FAILED','HELD_EXPIRED')),
  attempts integer NOT NULL CHECK (attempts BETWEEN 0 AND 8),
  max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 8),
  created_at timestamptz NOT NULL,
  available_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL,
  lease_owner text,
  lease_token text CHECK (lease_token IS NULL OR lease_token ~ '^[0-9a-f]{64}$'),
  lease_expires_at timestamptz,
  result_json jsonb,
  result_hash text CHECK (result_hash IS NULL OR result_hash ~ '^[0-9a-f]{64}$'),
  error_code text,
  CHECK (created_at < expires_at),
  CHECK ((status = 'LEASED') =
    (lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS finance_service_job_identity
  ON finance_service_jobs (COALESCE(scope_hash, 'public'), job_kind, subject_id,
                           dependency_hash);
CREATE INDEX IF NOT EXISTS finance_service_job_ready
  ON finance_service_jobs (status, available_at, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS finance_service_job_account
  ON finance_service_jobs (account_key, status, lease_expires_at);

CREATE OR REPLACE FUNCTION finance_claim_job(
  p_worker_id text,
  p_lease_token text,
  p_now timestamptz,
  p_lease_seconds integer,
  p_role text DEFAULT NULL
) RETURNS SETOF finance_service_jobs
LANGUAGE plpgsql
AS $$
DECLARE
  selected_job_id text;
BEGIN
  IF p_lease_seconds < 5 OR p_lease_seconds > 3600 THEN
    RAISE EXCEPTION 'worker lease duration outside policy';
  END IF;

  UPDATE finance_service_jobs
     SET status = CASE WHEN attempts >= max_attempts THEN 'FAILED' ELSE 'RETRY_WAIT' END,
         available_at = p_now,
         error_code = CASE WHEN attempts >= max_attempts
                           THEN 'LEASE_EXPIRED_MAX_ATTEMPTS' ELSE 'LEASE_EXPIRED' END,
         lease_owner = NULL,
         lease_token = NULL,
         lease_expires_at = NULL
   WHERE status = 'LEASED' AND lease_expires_at <= p_now;

  SELECT candidate.job_id
    INTO selected_job_id
    FROM finance_service_jobs AS candidate
   WHERE candidate.status IN ('PENDING','RETRY_WAIT')
     AND candidate.available_at <= p_now
     AND candidate.expires_at > p_now
     AND (p_role IS NULL OR candidate.role = p_role)
     AND (candidate.account_key IS NULL OR NOT EXISTS (
       SELECT 1 FROM finance_service_jobs AS running
        WHERE running.account_key = candidate.account_key
          AND running.status = 'LEASED'))
   ORDER BY candidate.priority DESC, candidate.created_at, candidate.job_id
   FOR UPDATE SKIP LOCKED
   LIMIT 1;

  IF selected_job_id IS NULL THEN
    RETURN;
  END IF;

  UPDATE finance_service_jobs
     SET status = 'LEASED',
         attempts = attempts + 1,
         lease_owner = p_worker_id,
         lease_token = p_lease_token,
         lease_expires_at = p_now + make_interval(secs => p_lease_seconds),
         error_code = NULL
   WHERE job_id = selected_job_id;

  RETURN QUERY SELECT * FROM finance_service_jobs WHERE job_id = selected_job_id;
END;
$$;

CREATE TABLE IF NOT EXISTS finance_service_outbox (
  event_id text PRIMARY KEY,
  job_id text NOT NULL REFERENCES finance_service_jobs(job_id),
  event_kind text NOT NULL,
  payload_hash text NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
  status text NOT NULL CHECK (status IN ('PENDING','DELIVERED')),
  created_at timestamptz NOT NULL,
  delivered_at timestamptz
);

CREATE TABLE IF NOT EXISTS finance_public_observations (
  source_id text NOT NULL,
  observation_key text NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  value_json jsonb NOT NULL,
  value_hash text NOT NULL CHECK (value_hash ~ '^[0-9a-f]{64}$'),
  observed_at timestamptz NOT NULL,
  valid_until timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  PRIMARY KEY (source_id, observation_key),
  CHECK (observed_at < valid_until)
);

CREATE TABLE IF NOT EXISTS finance_service_routines (
  scope_hash text NOT NULL,
  scope_json jsonb NOT NULL,
  role text NOT NULL CHECK (role IN ('ALPHA','VAULT','WATCH')),
  routine_id text NOT NULL,
  responsibility text NOT NULL,
  status text NOT NULL CHECK (status IN ('ACTIVE','PAUSED','HELD','FAILED')),
  interval_seconds integer NOT NULL CHECK (interval_seconds BETWEEN 30 AND 86400),
  next_due_at timestamptz NOT NULL,
  dependency_hash text NOT NULL CHECK (dependency_hash ~ '^[0-9a-f]{64}$'),
  updated_at timestamptz NOT NULL,
  PRIMARY KEY (scope_hash, routine_id)
);

CREATE TABLE IF NOT EXISTS finance_service_journal (
  ordinal bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_id text UNIQUE NOT NULL,
  event_kind text NOT NULL,
  scope_hash text,
  input_hash text NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
  output_hash text NOT NULL CHECK (output_hash ~ '^[0-9a-f]{64}$'),
  body_json jsonb NOT NULL,
  previous_hash text NOT NULL CHECK (previous_hash ~ '^[0-9a-f]{64}$'),
  event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$')
);

ALTER TABLE finance_service_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE finance_service_routines ENABLE ROW LEVEL SECURITY;
ALTER TABLE finance_service_jobs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS finance_record_scope ON finance_service_records;
CREATE POLICY finance_record_scope ON finance_service_records
  USING (scope_hash = current_setting('app.finance_scope_hash', true))
  WITH CHECK (scope_hash = current_setting('app.finance_scope_hash', true));

DROP POLICY IF EXISTS finance_routine_scope ON finance_service_routines;
CREATE POLICY finance_routine_scope ON finance_service_routines
  USING (scope_hash = current_setting('app.finance_scope_hash', true))
  WITH CHECK (scope_hash = current_setting('app.finance_scope_hash', true));

DROP POLICY IF EXISTS finance_job_scope ON finance_service_jobs;
CREATE POLICY finance_job_scope ON finance_service_jobs
  USING (scope_hash = current_setting('app.finance_scope_hash', true))
  WITH CHECK (scope_hash = current_setting('app.finance_scope_hash', true));

COMMIT;
