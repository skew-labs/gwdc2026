DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gwdc_finance_api') THEN
    CREATE ROLE gwdc_finance_api NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gwdc_finance_worker') THEN
    CREATE ROLE gwdc_finance_worker NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
END
$$;

REVOKE ALL ON finance_service_records, finance_service_jobs,
  finance_service_outbox, finance_public_observations, finance_service_routines,
  finance_service_journal FROM PUBLIC;

ALTER TABLE finance_service_records FORCE ROW LEVEL SECURITY;
ALTER TABLE finance_service_jobs FORCE ROW LEVEL SECURITY;
ALTER TABLE finance_service_routines FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS finance_record_scope ON finance_service_records;
CREATE POLICY finance_record_scope ON finance_service_records
  USING (scope_hash = current_setting('app.finance_scope_hash', true)
         OR pg_has_role(current_user, 'gwdc_finance_worker', 'member'))
  WITH CHECK (scope_hash = current_setting('app.finance_scope_hash', true)
              OR pg_has_role(current_user, 'gwdc_finance_worker', 'member'));

DROP POLICY IF EXISTS finance_job_scope ON finance_service_jobs;
CREATE POLICY finance_job_scope ON finance_service_jobs
  USING (scope_hash = current_setting('app.finance_scope_hash', true)
         OR pg_has_role(current_user, 'gwdc_finance_worker', 'member'))
  WITH CHECK (scope_hash = current_setting('app.finance_scope_hash', true)
              OR pg_has_role(current_user, 'gwdc_finance_worker', 'member'));

DROP POLICY IF EXISTS finance_routine_scope ON finance_service_routines;
CREATE POLICY finance_routine_scope ON finance_service_routines
  USING (scope_hash = current_setting('app.finance_scope_hash', true)
         OR pg_has_role(current_user, 'gwdc_finance_worker', 'member'))
  WITH CHECK (scope_hash = current_setting('app.finance_scope_hash', true)
              OR pg_has_role(current_user, 'gwdc_finance_worker', 'member'));

GRANT USAGE ON SCHEMA public TO gwdc_finance_api, gwdc_finance_worker;
GRANT SELECT, INSERT, UPDATE ON finance_service_records, finance_service_routines
  TO gwdc_finance_api;
GRANT SELECT, INSERT ON finance_service_jobs, finance_service_outbox
  TO gwdc_finance_api;
GRANT SELECT, INSERT ON finance_service_journal TO gwdc_finance_api;
GRANT SELECT ON finance_public_observations TO gwdc_finance_api;

GRANT SELECT, INSERT, UPDATE ON finance_service_records, finance_service_jobs,
  finance_service_outbox, finance_public_observations, finance_service_routines,
  finance_service_journal TO gwdc_finance_worker;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public
  TO gwdc_finance_api, gwdc_finance_worker;

REVOKE ALL ON FUNCTION finance_claim_job(text,text,timestamptz,integer,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION finance_claim_job(text,text,timestamptz,integer,text)
  TO gwdc_finance_worker;
