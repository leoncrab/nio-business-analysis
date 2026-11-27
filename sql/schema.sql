PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS source_manifest(
 source_id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL,
 published_date TEXT NOT NULL, timezone TEXT NOT NULL, available_at INTEGER NOT NULL,
 published_at_local TEXT NOT NULL DEFAULT '',
 path TEXT NOT NULL, sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metric_dictionary(
 metric TEXT PRIMARY KEY, label TEXT NOT NULL, unit TEXT NOT NULL, definition TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS monthly_actual(
 record_id TEXT PRIMARY KEY, period TEXT NOT NULL, metric TEXT NOT NULL REFERENCES metric_dictionary,
 value INTEGER NOT NULL, raw_value TEXT NOT NULL, raw_unit TEXT NOT NULL,
 source_id TEXT NOT NULL REFERENCES source_manifest, locator TEXT NOT NULL,
 scope TEXT NOT NULL, revision INTEGER NOT NULL, quality_status TEXT DEFAULT 'verified',
 UNIQUE(period,metric,scope,revision)
);
CREATE TABLE IF NOT EXISTS quarterly_actual(
 record_id TEXT PRIMARY KEY, period TEXT NOT NULL, metric TEXT NOT NULL REFERENCES metric_dictionary,
 value INTEGER NOT NULL, raw_value TEXT NOT NULL, raw_unit TEXT NOT NULL,
 source_id TEXT NOT NULL REFERENCES source_manifest, locator TEXT NOT NULL,
 scope TEXT NOT NULL CHECK(scope='GROUP'), revision INTEGER NOT NULL,
 accounting_basis TEXT NOT NULL DEFAULT 'US_GAAP', period_type TEXT DEFAULT 'quarter',
 quality_status TEXT DEFAULT 'verified', UNIQUE(period,metric,scope,revision)
);
CREATE TABLE IF NOT EXISTS company_guidance(
 period TEXT NOT NULL, metric TEXT NOT NULL REFERENCES metric_dictionary,
 low INTEGER NOT NULL, high INTEGER NOT NULL CHECK(high>=low),
 source_id TEXT NOT NULL REFERENCES source_manifest, locator TEXT NOT NULL,
 PRIMARY KEY(period,metric,source_id)
);
CREATE TABLE IF NOT EXISTS model_assumptions(
 run_id TEXT PRIMARY KEY, model_version TEXT NOT NULL, inputs_json TEXT NOT NULL, parameters_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS forecast_vintage(
 run_id TEXT NOT NULL REFERENCES model_assumptions, period TEXT NOT NULL, as_of INTEGER NOT NULL,
 origin TEXT NOT NULL, model TEXT NOT NULL, scenario TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL,
 PRIMARY KEY(run_id,metric)
);
CREATE TABLE IF NOT EXISTS analysis_facts(
 fact_id TEXT PRIMARY KEY, as_of INTEGER NOT NULL, run_id TEXT, metric TEXT NOT NULL,
 period TEXT NOT NULL, kind TEXT NOT NULL, unit TEXT NOT NULL, value REAL NOT NULL,
 formula TEXT NOT NULL, record_ids TEXT NOT NULL, source_ids TEXT NOT NULL
);
CREATE VIEW IF NOT EXISTS monthly_records AS
 SELECT a.*,s.available_at FROM monthly_actual a JOIN source_manifest s USING(source_id) WHERE quality_status='verified';
CREATE VIEW IF NOT EXISTS quarterly_records AS
 SELECT a.*,s.available_at FROM quarterly_actual a JOIN source_manifest s USING(source_id) WHERE quality_status='verified';
CREATE VIEW IF NOT EXISTS guidance_records AS
 SELECT a.*,s.available_at FROM company_guidance a JOIN source_manifest s USING(source_id);
