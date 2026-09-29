CREATE TABLE IF NOT EXISTS finance_service_journal_audit (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  integrity boolean NOT NULL,
  audited_root text,
  audited_at timestamptz,
  failure_code text
);

INSERT INTO finance_service_journal_audit
  (singleton,integrity,audited_root,audited_at,failure_code)
VALUES (true,true,NULL,NULL,NULL)
ON CONFLICT (singleton) DO NOTHING;

REVOKE ALL ON finance_service_journal_audit FROM PUBLIC;
GRANT SELECT, UPDATE ON finance_service_journal_audit
  TO gwdc_finance_api, gwdc_finance_worker;
