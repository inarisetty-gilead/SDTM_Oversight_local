-- ══════════════════════════════════════════════════════════════════════════════
-- SDTM Designer — Consolidated Database Schema
-- Combines schema.sql + schema_v2.sql into a single idempotent migration.
-- PostgreSQL with pgvector extension
-- ══════════════════════════════════════════════════════════════════════════════

CREATE EXTENSION IF NOT EXISTS vector;

-- ┌─────────────────────────────────────────────────────────────────────────────
-- │ CORE TABLES
-- └─────────────────────────────────────────────────────────────────────────────

-- ── Programs (parent of studies; keyed by 3-digit GSAIL product code) ───────
-- product_code is stored numeric. "P123" is presentation only — storing the
-- prefix is what produced the P540 / GS-US-123 / 540 mess in the old
-- free-text studies.program_code column.
CREATE TABLE IF NOT EXISTS programs (
    id SERIAL PRIMARY KEY,
    product_code SMALLINT NOT NULL UNIQUE CHECK (product_code BETWEEN 100 AND 999),
    product_name TEXT,
    therapeutic_area VARCHAR(100),
    product_stage VARCHAR(50),
    source VARCHAR(20) DEFAULT 'gsail',
    synced_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- ── Studies ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS studies (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL UNIQUE,
    study_name TEXT,
    program_id INTEGER REFERENCES programs(id),
    gsail_study_id VARCHAR(50),
    legacy_program_code VARCHAR(50),
    legacy_program_type VARCHAR(20),
    study_number VARCHAR(50),
    protocol_number VARCHAR(100),
    therapeutic_area VARCHAR(100),
    sdtm_ig_version VARCHAR(20),
    phase VARCHAR(20),
    status VARCHAR(30) DEFAULT 'active',
    owner_email VARCHAR(200),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_studies_study_id ON studies(study_id);
CREATE INDEX IF NOT EXISTS idx_studies_status ON studies(status);

-- The table declaration above is a no-op against an existing studies table
-- (it is guarded by IF NOT EXISTS), so the live database needs these
-- explicitly. Do not write the literal declaration phrase in a comment here:
-- _expected_tables() strips comments for exactly that reason, but keeping the
-- phrase out avoids relying on it.
ALTER TABLE studies ADD COLUMN IF NOT EXISTS program_id INTEGER REFERENCES programs(id);
ALTER TABLE studies ADD COLUMN IF NOT EXISTS gsail_study_id VARCHAR(50);
ALTER TABLE studies ADD COLUMN IF NOT EXISTS legacy_program_code VARCHAR(50);
ALTER TABLE studies ADD COLUMN IF NOT EXISTS legacy_program_type VARCHAR(20);
CREATE INDEX IF NOT EXISTS idx_studies_program_id ON studies(program_id);

-- This file re-executes IN FULL whenever its checksum changes. An unguarded
-- DROP COLUMN works once and then errors on every later schema edit, because
-- the UPDATE would reference a column that no longer exists. _sqlite_ddl()
-- strips $$...$$ bodies, so SQLite skips this and converges via its differ.
-- Both guards qualify on current_schema(), matching _pg_missing_tables() in
-- src/db.py: an unqualified lookup can match `studies` in a different schema on
-- the search_path than the ALTER would touch.
-- Each column is copied to its legacy_* twin BEFORE being dropped, so the
-- migration stays recoverable from the database alone.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = current_schema()
               AND table_name = 'studies' AND column_name = 'program_code') THEN
    UPDATE studies SET legacy_program_code = program_code
      WHERE program_code IS NOT NULL AND legacy_program_code IS NULL;
    ALTER TABLE studies DROP COLUMN program_code;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = current_schema()
               AND table_name = 'studies' AND column_name = 'program_type') THEN
    UPDATE studies SET legacy_program_type = program_type
      WHERE program_type IS NOT NULL AND legacy_program_type IS NULL;
    ALTER TABLE studies DROP COLUMN program_type;
  END IF;
END $$;

-- ── RAG Chunks (SDTM IG variable context for search_rag) ────────────────────
CREATE TABLE IF NOT EXISTS sdtm_rag_chunks (
    id SERIAL PRIMARY KEY,
    domain VARCHAR(10) NOT NULL,
    variable VARCHAR(50) NOT NULL,
    label TEXT,
    mapping_rule TEXT,
    mapping_action VARCHAR(50),
    input_variables TEXT,
    text TEXT NOT NULL,
    source VARCHAR(50),
    sheet VARCHAR(100),
    embedding vector(1024),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_domain ON sdtm_rag_chunks(domain);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_variable ON sdtm_rag_chunks(variable);
CREATE INDEX IF NOT EXISTS idx_rag_embedding ON sdtm_rag_chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ┌─────────────────────────────────────────────────────────────────────────────
-- │ STUDY METADATA & DESIGN
-- └─────────────────────────────────────────────────────────────────────────────

-- ── Trial Design (extracted from protocols) ─────────────────────────────────
CREATE TABLE IF NOT EXISTS trial_design (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    design_type VARCHAR(30) NOT NULL,
    data JSONB NOT NULL,
    version INTEGER DEFAULT 1,
    extracted_by VARCHAR(200),
    agent_run_id VARCHAR(200),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trial_design_study ON trial_design(study_id);
CREATE INDEX IF NOT EXISTS idx_trial_design_type ON trial_design(design_type);

-- ── Study Metadata (parsed per-column raw metadata) ─────────────────────────
CREATE TABLE IF NOT EXISTS study_metadata (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    data JSONB NOT NULL,
    version INTEGER DEFAULT 1,
    uploaded_by VARCHAR(200),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_study_metadata_study ON study_metadata(study_id);

-- ── Study Files (tracks file associations without coupling to storage) ───────
CREATE TABLE IF NOT EXISTS study_files (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    file_key TEXT NOT NULL,
    file_name VARCHAR(500) NOT NULL,
    file_type VARCHAR(30) NOT NULL,
    file_size BIGINT DEFAULT 0,
    uploaded_by VARCHAR(200),
    uploaded_at TIMESTAMP DEFAULT NOW(),
    UNIQUE (study_id, file_key)
);

CREATE INDEX IF NOT EXISTS idx_study_files_study ON study_files(study_id);
CREATE INDEX IF NOT EXISTS idx_study_files_type ON study_files(file_type);

-- Annotated CRF (uploaded blank CRF and extracted annotations)
CREATE TABLE IF NOT EXISTS crf (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    filename VARCHAR(500),
    storage_key VARCHAR(500),
    data JSONB NOT NULL,
    updated_by VARCHAR(200),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(study_id)
);

CREATE INDEX IF NOT EXISTS idx_crf_study ON crf(study_id);

-- ── SAP (Statistical Analysis Plan) ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sap (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    data JSONB NOT NULL,
    version INTEGER DEFAULT 1,
    generated_by VARCHAR(200),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sap_study ON sap(study_id);

-- ── TLF TOC (shell list, derived from the SAP) ─────────────────────────────
CREATE TABLE IF NOT EXISTS tlf_toc (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    data JSONB NOT NULL,
    version INTEGER DEFAULT 1,
    generated_by VARCHAR(200),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tlf_toc_study ON tlf_toc(study_id);

-- ┌─────────────────────────────────────────────────────────────────────────────
-- │ MAPPING ENGINE
-- └─────────────────────────────────────────────────────────────────────────────

-- ── Mapping Run (SDTM-Designer-shaped result: specs + metadata_status + summary)
CREATE TABLE IF NOT EXISTS mapping_run (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    data JSONB NOT NULL,
    version INTEGER DEFAULT 1,
    created_by VARCHAR(200),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mapping_run_study ON mapping_run(study_id);

-- ADaM spec (versioned analysis dataset specification per study)
CREATE TABLE IF NOT EXISTS adam_spec (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    data JSONB NOT NULL,
    version INTEGER DEFAULT 1,
    generated_by VARCHAR(200),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_adam_spec_study ON adam_spec(study_id);

-- ── Mapping Results ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS mapping_results (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    source_file VARCHAR(200),
    source_column VARCHAR(200) NOT NULL,
    source_label TEXT,
    target_domain VARCHAR(10),
    target_variable VARCHAR(50),
    target_label TEXT,
    method VARCHAR(50) NOT NULL,
    confidence NUMERIC(4, 3),
    reasoning TEXT,
    status VARCHAR(30) DEFAULT 'pending',
    version INTEGER DEFAULT 1,
    approved_by VARCHAR(200),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mapping_study ON mapping_results(study_id);
CREATE INDEX IF NOT EXISTS idx_mapping_source ON mapping_results(source_column);
CREATE INDEX IF NOT EXISTS idx_mapping_status ON mapping_results(status);

-- ── Mapping Versions (row-level change history for audit trail) ─────────────
CREATE TABLE IF NOT EXISTS mapping_versions (
    id SERIAL PRIMARY KEY,
    mapping_result_id INTEGER NOT NULL REFERENCES mapping_results(id),
    study_id VARCHAR(100) NOT NULL,
    version INTEGER NOT NULL,
    source_file VARCHAR(200),
    source_column VARCHAR(200),
    source_label TEXT,
    target_domain VARCHAR(10),
    target_variable VARCHAR(50),
    target_label TEXT,
    method VARCHAR(50),
    confidence NUMERIC(4, 3),
    reasoning TEXT,
    status VARCHAR(30),
    changed_by VARCHAR(200) NOT NULL,
    change_field VARCHAR(50),
    old_value TEXT,
    new_value TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mapver_mapping ON mapping_versions(mapping_result_id);
CREATE INDEX IF NOT EXISTS idx_mapver_study ON mapping_versions(study_id);
CREATE INDEX IF NOT EXISTS idx_mapver_version ON mapping_versions(version);

-- ── Mapping Lineage (data provenance per mapping) ───────────────────────────
CREATE TABLE IF NOT EXISTS mapping_lineage (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    mapping_result_id INTEGER REFERENCES mapping_results(id),
    source_file VARCHAR(200),
    source_column VARCHAR(200) NOT NULL,
    source_label TEXT,
    target_domain VARCHAR(10),
    target_variable VARCHAR(50),
    method VARCHAR(50) NOT NULL,
    confidence NUMERIC(4, 3),
    rag_chunks_used INTEGER[],
    ml_model_version VARCHAR(100),
    precedent_study_id VARCHAR(100),
    agent_run_id VARCHAR(200),
    version INTEGER DEFAULT 1,
    status VARCHAR(30) DEFAULT 'pending',
    reviewed_by VARCHAR(200),
    reviewed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lineage_study ON mapping_lineage(study_id);
CREATE INDEX IF NOT EXISTS idx_lineage_mapping ON mapping_lineage(mapping_result_id);
CREATE INDEX IF NOT EXISTS idx_lineage_method ON mapping_lineage(method);
CREATE INDEX IF NOT EXISTS idx_lineage_source ON mapping_lineage(source_column);

-- ── Variable Catalog ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS variable_catalog (
    id SERIAL PRIMARY KEY,
    domain VARCHAR(10) NOT NULL,
    variable_name VARCHAR(50) NOT NULL,
    variable_label TEXT,
    data_type VARCHAR(20),
    core VARCHAR(20),
    controlled_terms TEXT,
    origin VARCHAR(50),
    spec_source VARCHAR(50),
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(domain, variable_name, spec_source)
);

CREATE INDEX IF NOT EXISTS idx_catalog_domain ON variable_catalog(domain);
CREATE INDEX IF NOT EXISTS idx_catalog_variable ON variable_catalog(variable_name);

-- ┌─────────────────────────────────────────────────────────────────────────────
-- │ BUILD ENGINE (v2)
-- └─────────────────────────────────────────────────────────────────────────────

-- ── Data Prep Pipelines ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS prep_pipelines (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    steps JSONB NOT NULL DEFAULT '[]',
    manifest JSONB DEFAULT '{}',
    version INTEGER DEFAULT 1,
    created_by VARCHAR(200),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_prep_study ON prep_pipelines(study_id);

-- ── SDTM Build Blocks (per domain structured mapping blocks) ────────────────
CREATE TABLE IF NOT EXISTS sdtm_build_blocks (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    domain VARCHAR(10) NOT NULL,
    blocks JSONB NOT NULL DEFAULT '[]',
    prep_steps JSONB DEFAULT '[]',
    settings JSONB DEFAULT '{}',
    reviewed JSONB DEFAULT '[]',
    spec_hash VARCHAR(64),
    rev VARCHAR(50) DEFAULT '',
    version INTEGER DEFAULT 1,
    created_by VARCHAR(200),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(study_id, domain)
);

CREATE INDEX IF NOT EXISTS idx_build_study ON sdtm_build_blocks(study_id);
CREATE INDEX IF NOT EXISTS idx_build_domain ON sdtm_build_blocks(domain);

-- ── Build Versions (snapshot history per domain) ────────────────────────────
CREATE TABLE IF NOT EXISTS sdtm_build_versions (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    domain VARCHAR(10) NOT NULL,
    version INTEGER NOT NULL,
    blocks JSONB NOT NULL,
    label VARCHAR(200),
    saved_by VARCHAR(200),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_buildver_study ON sdtm_build_versions(study_id);

-- ┌─────────────────────────────────────────────────────────────────────────────
-- │ LEARNING & PRECEDENT (v2)
-- └─────────────────────────────────────────────────────────────────────────────

-- ── Learned Mappings (feedback loop — user-approved mappings) ───────────────
CREATE TABLE IF NOT EXISTS learned_mappings (
    id SERIAL PRIMARY KEY,
    column_name VARCHAR(200) NOT NULL,
    source_label TEXT,
    domain VARCHAR(10) NOT NULL,
    variable VARCHAR(50) NOT NULL,
    supp BOOLEAN DEFAULT FALSE,
    study_id VARCHAR(100),
    approved_by VARCHAR(200),
    approved_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(column_name, domain, variable)
);

CREATE INDEX IF NOT EXISTS idx_learned_col ON learned_mappings(column_name);
CREATE INDEX IF NOT EXISTS idx_learned_domain ON learned_mappings(domain);

-- ── Historical Precedent (cross-study variable frequency) ───────────────────
CREATE TABLE IF NOT EXISTS historical_precedent (
    id SERIAL PRIMARY KEY,
    source_token VARCHAR(200) NOT NULL,
    domain VARCHAR(10) NOT NULL,
    variable VARCHAR(50) NOT NULL,
    frequency INTEGER DEFAULT 1,
    confidence NUMERIC(4,3) DEFAULT 0,
    last_seen_study VARCHAR(100),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(source_token, domain, variable)
);

CREATE INDEX IF NOT EXISTS idx_hist_token ON historical_precedent(source_token);

-- ┌─────────────────────────────────────────────────────────────────────────────
-- │ REVIEW & DELIVERY (v2)
-- └─────────────────────────────────────────────────────────────────────────────

-- ── Codelist Extensions (study-specific codelist additions) ─────────────────
CREATE TABLE IF NOT EXISTS codelist_extensions (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    codelist_name VARCHAR(200) NOT NULL,
    coded_value VARCHAR(200) NOT NULL,
    decoded_value TEXT,
    added_by VARCHAR(200),
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(study_id, codelist_name, coded_value)
);

CREATE INDEX IF NOT EXISTS idx_cl_study ON codelist_extensions(study_id);

-- ── Spec Overrides (domain-level spec customizations per study) ─────────────
CREATE TABLE IF NOT EXISTS spec_overrides (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    domain VARCHAR(10) NOT NULL,
    overrides JSONB NOT NULL DEFAULT '[]',
    created_by VARCHAR(200),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(study_id, domain)
);

CREATE INDEX IF NOT EXISTS idx_specov_study ON spec_overrides(study_id);

-- ── Study Overrides (per-study mapping/run selection state) ────────────────
CREATE TABLE IF NOT EXISTS study_overrides (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    data JSONB NOT NULL DEFAULT '{}',
    updated_by VARCHAR(200),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(study_id)
);

CREATE INDEX IF NOT EXISTS idx_study_overrides_study ON study_overrides(study_id);

-- ── Review Decisions (audit trail of build review accept/reject) ────────────
CREATE TABLE IF NOT EXISTS review_decisions (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    domain VARCHAR(10) NOT NULL,
    variable VARCHAR(50) NOT NULL,
    decision VARCHAR(20) NOT NULL,
    previous_state JSONB,
    new_state JSONB,
    reviewer VARCHAR(200),
    comment TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_review_study ON review_decisions(study_id);

-- ── Package Jobs (delivery package generation tracking) ─────────────────────
CREATE TABLE IF NOT EXISTS package_jobs (
    id SERIAL PRIMARY KEY,
    job_id VARCHAR(200) NOT NULL UNIQUE,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    status VARCHAR(30) DEFAULT 'pending',
    package_key VARCHAR(500),
    domains JSONB DEFAULT '[]',
    formats JSONB DEFAULT '["parquet","xpt"]',
    include_define_xml BOOLEAN DEFAULT TRUE,
    include_programs BOOLEAN DEFAULT TRUE,
    error TEXT,
    created_by VARCHAR(200),
    created_at TIMESTAMP DEFAULT NOW(),
    completed_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_pkg_study ON package_jobs(study_id);

-- ┌─────────────────────────────────────────────────────────────────────────────
-- │ OBSERVABILITY & AUDIT
-- └─────────────────────────────────────────────────────────────────────────────

-- ── Agent Feedback ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_feedback (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100),
    agent_name VARCHAR(100) NOT NULL,
    agent_run_id VARCHAR(200),
    feedback_level VARCHAR(10) NOT NULL,
    action VARCHAR(30) NOT NULL,
    original_output JSONB,
    corrected_output JSONB,
    user_email VARCHAR(200) NOT NULL,
    comment TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_feedback_study ON agent_feedback(study_id);
CREATE INDEX IF NOT EXISTS idx_feedback_agent ON agent_feedback(agent_name);
CREATE INDEX IF NOT EXISTS idx_feedback_action ON agent_feedback(action);

-- ── Audit Log ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    user_email VARCHAR(200) NOT NULL,
    action VARCHAR(100) NOT NULL,
    resource VARCHAR(200),
    study_id VARCHAR(100),
    agent_name VARCHAR(100),
    timestamp TIMESTAMP NOT NULL DEFAULT NOW(),
    request_hash VARCHAR(64),
    response_hash VARCHAR(64),
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    model_id VARCHAR(200),
    latency_ms INTEGER,
    details JSONB
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_email);
CREATE INDEX IF NOT EXISTS idx_audit_study ON audit_log(study_id);
CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit_log(agent_name);

-- Structured payload for actions that need more than the 200-character `resource`
-- (Lead Review records the revision, hash and status change here). NULL on older rows.
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS details JSONB;

-- ── Token Usage (aggregated per agent per day) ──────────────────────────────
CREATE TABLE IF NOT EXISTS token_usage (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    agent_name VARCHAR(100) NOT NULL,
    study_id VARCHAR(100),
    user_email VARCHAR(200),
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    invocation_count INTEGER DEFAULT 0,
    model_id VARCHAR(200),
    UNIQUE(date, agent_name, study_id, user_email)
);

CREATE INDEX IF NOT EXISTS idx_usage_date ON token_usage(date);
CREATE INDEX IF NOT EXISTS idx_usage_agent ON token_usage(agent_name);

-- Generated Programs (SAS/Python code per domain)
CREATE TABLE IF NOT EXISTS generated_programs (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    domain VARCHAR(10) NOT NULL,
    language VARCHAR(20) NOT NULL,
    program_name VARCHAR(200),
    code TEXT,
    description TEXT,
    created_by VARCHAR(200),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_programs_study ON generated_programs(study_id);
CREATE INDEX IF NOT EXISTS idx_programs_domain ON generated_programs(domain);

-- SDTM Output (built dataset records)
CREATE TABLE IF NOT EXISTS sdtm_output (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    domain VARCHAR(10) NOT NULL,
    row_count INTEGER DEFAULT 0,
    file_key VARCHAR(500),
    format VARCHAR(20) DEFAULT 'parquet',
    build_status VARCHAR(30) DEFAULT 'complete',
    data_preview JSONB,
    built_by VARCHAR(200),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_output_study ON sdtm_output(study_id);
CREATE INDEX IF NOT EXISTS idx_output_domain ON sdtm_output(domain);

-- Chat Sessions (conversational agent history)
CREATE TABLE IF NOT EXISTS chat_sessions (
    id SERIAL PRIMARY KEY,
    session_id VARCHAR(200) NOT NULL,
    study_id VARCHAR(100) NOT NULL,
    user_email VARCHAR(200) NOT NULL,
    role VARCHAR(20) NOT NULL,
    message TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_sessions(session_id);
CREATE INDEX IF NOT EXISTS idx_chat_study ON chat_sessions(study_id);

-- Ingestion Jobs (RAG / catalog ingestion tracking)
CREATE TABLE IF NOT EXISTS ingestion_jobs (
    id SERIAL PRIMARY KEY,
    job_id VARCHAR(200) NOT NULL UNIQUE,
    source VARCHAR(100) NOT NULL,
    s3_key VARCHAR(500) NOT NULL,
    mode VARCHAR(20) NOT NULL DEFAULT 'rag',
    spec_source VARCHAR(50),
    clear_existing BOOLEAN DEFAULT FALSE,
    status VARCHAR(30) NOT NULL DEFAULT 'pending',
    chunks_processed INTEGER DEFAULT 0,
    error_message TEXT,
    submitted_by VARCHAR(200),
    created_at TIMESTAMP DEFAULT NOW(),
    completed_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ingest_job_id ON ingestion_jobs(job_id);
CREATE INDEX IF NOT EXISTS idx_ingest_status ON ingestion_jobs(status);

-- Domain Catalog (admin-configurable CDISC domain metadata)
CREATE TABLE IF NOT EXISTS domain_catalog (
    id SERIAL PRIMARY KEY,
    domain VARCHAR(10) NOT NULL UNIQUE,
    observation_class VARCHAR(50) NOT NULL,
    label TEXT,
    description TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_domain_catalog_class ON domain_catalog(observation_class);

-- Role Permissions (admin-configurable IDM group -> role mapping)
CREATE TABLE IF NOT EXISTS role_permissions (
    id SERIAL PRIMARY KEY,
    role VARCHAR(30) NOT NULL,
    idm_group VARCHAR(200) NOT NULL,
    description TEXT,
    created_by VARCHAR(200),
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(role, idm_group)
);

CREATE INDEX IF NOT EXISTS idx_role_perm_role ON role_permissions(role);

-- App Settings (key-value config store)
CREATE TABLE IF NOT EXISTS app_settings (
    id SERIAL PRIMARY KEY,
    setting_key VARCHAR(100) NOT NULL UNIQUE,
    setting_value TEXT NOT NULL,
    description TEXT,
    updated_by VARCHAR(200),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Agent Jobs (async background job tracking)
CREATE TABLE IF NOT EXISTS agent_jobs (
    id SERIAL PRIMARY KEY,
    job_id VARCHAR(200) NOT NULL UNIQUE,
    agent_name VARCHAR(100) NOT NULL,
    study_id VARCHAR(100),
    job_type VARCHAR(100) NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'pending',
    result_summary TEXT,
    error TEXT,
    user_email VARCHAR(200) NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    completed_at TIMESTAMP,
    duration_ms INTEGER,
    served_model VARCHAR(200),
    input_tokens INTEGER,
    output_tokens INTEGER
);

CREATE INDEX IF NOT EXISTS idx_agent_jobs_job_id ON agent_jobs(job_id);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_user ON agent_jobs(user_email);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_status ON agent_jobs(status);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_study ON agent_jobs(study_id);

-- The table declaration above is a no-op against an existing agent_jobs table
-- (it is guarded by IF NOT EXISTS), so the live database needs these
-- explicitly. Nullable: provenance is only known at job completion, and only
-- one of the eleven agent runtimes emits it at all today.
ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS served_model VARCHAR(200);
ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS input_tokens INTEGER;
ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS output_tokens INTEGER;

-- ┌─────────────────────────────────────────────────────────────────────────────
-- │ VERIFICATION & AUDIT
-- └─────────────────────────────────────────────────────────────────────────────

-- Individual mapping variable verification status and results
CREATE TABLE IF NOT EXISTS mapping_verification (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    domain VARCHAR(10) NOT NULL,
    variable VARCHAR(50) NOT NULL,
    verification_status VARCHAR(30) DEFAULT 'pending',
    verification_type VARCHAR(50) NOT NULL,
    checks_passed INTEGER DEFAULT 0,
    checks_failed INTEGER DEFAULT 0,
    checks_warnings INTEGER DEFAULT 0,
    issues JSONB DEFAULT '[]',
    verified_by VARCHAR(200),
    verification_agent VARCHAR(100),
    verification_run_id VARCHAR(200),
    verification_started_at TIMESTAMP,
    verification_completed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(study_id, domain, variable)
);

CREATE INDEX IF NOT EXISTS idx_mapverif_study ON mapping_verification(study_id);
CREATE INDEX IF NOT EXISTS idx_mapverif_domain ON mapping_verification(study_id, domain);
CREATE INDEX IF NOT EXISTS idx_mapverif_status ON mapping_verification(verification_status);
CREATE INDEX IF NOT EXISTS idx_mapverif_type ON mapping_verification(verification_type);

-- Full specification verification runs across one or more domains
CREATE TABLE IF NOT EXISTS spec_verification_runs (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    run_number INTEGER NOT NULL,
    domains_included TEXT[],
    total_variables INTEGER DEFAULT 0,
    overall_status VARCHAR(30) DEFAULT 'in_progress',
    variables_passed INTEGER DEFAULT 0,
    variables_failed INTEGER DEFAULT 0,
    variables_warnings INTEGER DEFAULT 0,
    total_checks_run INTEGER DEFAULT 0,
    checks_passed INTEGER DEFAULT 0,
    checks_failed INTEGER DEFAULT 0,
    checks_warnings INTEGER DEFAULT 0,
    summary JSONB DEFAULT '{}',
    critical_issues JSONB DEFAULT '[]',
    initiated_by VARCHAR(200) NOT NULL,
    verification_type VARCHAR(50) DEFAULT 'full',
    agent_run_id VARCHAR(200),
    started_at TIMESTAMP DEFAULT NOW(),
    completed_at TIMESTAMP,
    duration_seconds INTEGER,
    UNIQUE(study_id, run_number)
);

CREATE INDEX IF NOT EXISTS idx_specverif_study ON spec_verification_runs(study_id);
CREATE INDEX IF NOT EXISTS idx_specverif_status ON spec_verification_runs(overall_status);
CREATE INDEX IF NOT EXISTS idx_specverif_completed ON spec_verification_runs(study_id, completed_at DESC);

-- Point-in-time verification state snapshots
CREATE TABLE IF NOT EXISTS verification_snapshots (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    spec_verification_run_id INTEGER REFERENCES spec_verification_runs(id),
    domain VARCHAR(10) NOT NULL,
    variable VARCHAR(50) NOT NULL,
    verification_status VARCHAR(30) NOT NULL,
    checks_passed INTEGER DEFAULT 0,
    checks_failed INTEGER DEFAULT 0,
    checks_warnings INTEGER DEFAULT 0,
    issues JSONB DEFAULT '[]',
    mapping_version INTEGER,
    mapping_snapshot JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_versnap_study ON verification_snapshots(study_id);
CREATE INDEX IF NOT EXISTS idx_versnap_run ON verification_snapshots(spec_verification_run_id);
CREATE INDEX IF NOT EXISTS idx_versnap_domain_var ON verification_snapshots(study_id, domain, variable);

-- Comprehensive mapping change audit trail
CREATE TABLE IF NOT EXISTS mapping_audit_trail (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id),
    mapping_result_id INTEGER REFERENCES mapping_results(id),
    change_type VARCHAR(50) NOT NULL,
    change_source VARCHAR(50) NOT NULL,
    changed_by VARCHAR(200) NOT NULL,
    agent_name VARCHAR(100),
    fields_changed TEXT[],
    before_snapshot JSONB,
    after_snapshot JSONB,
    change_diff JSONB,
    version_before INTEGER,
    version_after INTEGER,
    change_reason TEXT,
    ip_address VARCHAR(50),
    user_agent TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mapaudit_study ON mapping_audit_trail(study_id);
CREATE INDEX IF NOT EXISTS idx_audit_mapping ON mapping_audit_trail(mapping_result_id);
CREATE INDEX IF NOT EXISTS idx_mapaudit_user ON mapping_audit_trail(changed_by);
CREATE INDEX IF NOT EXISTS idx_audit_type ON mapping_audit_trail(change_type);
CREATE INDEX IF NOT EXISTS idx_audit_created ON mapping_audit_trail(created_at DESC);

-- Keep only the newest 20 mapping history rows per mapping result
CREATE OR REPLACE FUNCTION cleanup_old_mapping_versions()
RETURNS TRIGGER AS $$
DECLARE
    version_count INTEGER;
    oldest_versions INTEGER[];
BEGIN
    SELECT COUNT(*) INTO version_count
    FROM mapping_versions
    WHERE mapping_result_id = NEW.mapping_result_id;

    IF version_count > 20 THEN
        SELECT ARRAY_AGG(id) INTO oldest_versions
        FROM (
            SELECT id
            FROM mapping_versions
            WHERE mapping_result_id = NEW.mapping_result_id
            ORDER BY version DESC, created_at DESC
            OFFSET 20
        ) old_versions;

        DELETE FROM mapping_versions
        WHERE id = ANY(oldest_versions);
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_cleanup_mapping_versions ON mapping_versions;
CREATE TRIGGER trg_cleanup_mapping_versions
    AFTER INSERT ON mapping_versions
    FOR EACH ROW
    EXECUTE FUNCTION cleanup_old_mapping_versions();

-- Latest verification status per study/domain/variable
CREATE OR REPLACE VIEW v_latest_verification AS
SELECT DISTINCT ON (mv.study_id, mv.domain, mv.variable)
    mv.study_id,
    mv.domain,
    mv.variable,
    mv.verification_status,
    mv.verification_type,
    mv.checks_passed,
    mv.checks_failed,
    mv.checks_warnings,
    mv.issues,
    mv.verified_by,
    mv.verification_completed_at,
    mr.id AS mapping_id
FROM mapping_verification mv
LEFT JOIN mapping_results mr
    ON mv.study_id = mr.study_id
    AND mv.domain = mr.target_domain
    AND mv.variable = mr.target_variable
ORDER BY mv.study_id, mv.domain, mv.variable,
    mv.verification_completed_at DESC NULLS LAST, mv.created_at DESC;

-- Most recently completed verification run per study
CREATE OR REPLACE VIEW v_last_completed_verification AS
SELECT DISTINCT ON (study_id)
    id AS run_id,
    study_id,
    run_number,
    overall_status,
    domains_included,
    total_variables,
    variables_passed,
    variables_failed,
    variables_warnings,
    total_checks_run,
    checks_passed,
    checks_failed,
    checks_warnings,
    summary,
    critical_issues,
    completed_at,
    initiated_by
FROM spec_verification_runs
WHERE completed_at IS NOT NULL
ORDER BY study_id, completed_at DESC;

-- Verification progress summary by study and domain
CREATE OR REPLACE VIEW v_verification_progress AS
SELECT
    mv.study_id,
    mv.domain,
    COUNT(*) AS total_variables,
    COUNT(*) FILTER (WHERE mv.verification_status = 'passed') AS passed,
    COUNT(*) FILTER (WHERE mv.verification_status = 'failed') AS failed,
    COUNT(*) FILTER (WHERE mv.verification_status = 'needs_review') AS needs_review,
    COUNT(*) FILTER (WHERE mv.verification_status = 'pending') AS pending,
    ROUND(COUNT(*) FILTER (WHERE mv.verification_status = 'passed')::NUMERIC
        / NULLIF(COUNT(*), 0) * 100, 1) AS pass_rate_pct
FROM mapping_verification mv
GROUP BY mv.study_id, mv.domain;

CREATE INDEX IF NOT EXISTS idx_audit_study_created ON mapping_audit_trail(study_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_verif_study_status ON mapping_verification(study_id, verification_status);

-- ── Lead Review ─────────────────────────────────────────────────────────────
-- Review sign-off per domain and release, plus an append-only event log holding every comment and
-- action. Both are kept indefinitely: foreign keys RESTRICT, and triggers (below) refuse deletes.

-- Current sign-off state of one domain in one release. Rows change only under a row lock.
CREATE TABLE IF NOT EXISTS lead_review_domain (
    id BIGSERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id) ON DELETE RESTRICT,
    release_main INTEGER NOT NULL,
    release_semver VARCHAR(20) NOT NULL,
    release_label VARCHAR(200),
    domain VARCHAR(20) NOT NULL CHECK (domain = UPPER(domain) AND domain <> ''),
    status VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'reviewed')),
    state_version INTEGER NOT NULL DEFAULT 0,
    reviewed_by VARCHAR(200),
    reviewed_at TIMESTAMPTZ,
    reviewed_revision INTEGER,
    reviewed_domain_hash VARCHAR(64),
    stats_snapshot JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (study_id, release_main, domain),
    CHECK (status <> 'reviewed'
           OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL AND reviewed_domain_hash IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_lead_review_domain_release ON lead_review_domain(study_id, release_main);

-- Append-only: one row per comment, reply or action. `seq` numbers a study's events without gaps,
-- and each row's hash covers the previous row's hash, so a missing or altered row is detectable.
CREATE TABLE IF NOT EXISTS lead_review_event (
    id BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    study_id VARCHAR(100) NOT NULL REFERENCES studies(study_id) ON DELETE RESTRICT,
    seq BIGINT NOT NULL,
    thread_id UUID,
    event_type VARCHAR(30) NOT NULL CHECK (event_type IN (
        'comment', 'reply', 'address', 'resolve', 'reopen_thread',
        'approve', 'reopen_domain')),
    release_main INTEGER NOT NULL,
    release_semver VARCHAR(20) NOT NULL,
    mapping_revision INTEGER,
    domain VARCHAR(20) NOT NULL CHECK (domain = UPPER(domain) AND domain <> ''),
    variables VARCHAR(64)[],
    body TEXT CHECK (char_length(body) <= 4000),
    user_email VARCHAR(200) NOT NULL,
    domain_hash VARCHAR(64),
    status_before VARCHAR(20),
    status_after VARCHAR(20),
    client_request_id UUID NOT NULL,
    prev_hash VARCHAR(64) NOT NULL,
    event_hash VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (study_id, seq),
    UNIQUE (study_id, client_request_id),
    CHECK (event_type NOT IN ('comment', 'reply', 'address', 'resolve', 'reopen_thread')
           OR thread_id IS NOT NULL),
    CHECK (event_type NOT IN ('comment', 'reply', 'reopen_domain')
           OR char_length(BTRIM(COALESCE(body, ''))) > 0)
);

CREATE INDEX IF NOT EXISTS idx_lead_review_event_domain
    ON lead_review_event(study_id, release_main, domain, seq);
CREATE INDEX IF NOT EXISTS idx_lead_review_event_thread ON lead_review_event(study_id, thread_id);

-- The event log can never be rewritten or emptied, and sign-off rows can never be removed.
CREATE OR REPLACE FUNCTION lead_review_event_append_only()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'lead_review_event is append-only: % is not allowed', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION lead_review_domain_keep_rows()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'lead_review_domain rows are retained indefinitely: % is not allowed', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_lead_review_event_no_change ON lead_review_event;
CREATE TRIGGER trg_lead_review_event_no_change
    BEFORE UPDATE OR DELETE ON lead_review_event
    FOR EACH ROW EXECUTE FUNCTION lead_review_event_append_only();

DROP TRIGGER IF EXISTS trg_lead_review_event_no_truncate ON lead_review_event;
CREATE TRIGGER trg_lead_review_event_no_truncate
    BEFORE TRUNCATE ON lead_review_event
    FOR EACH STATEMENT EXECUTE FUNCTION lead_review_event_append_only();

DROP TRIGGER IF EXISTS trg_lead_review_domain_no_delete ON lead_review_domain;
CREATE TRIGGER trg_lead_review_domain_no_delete
    BEFORE DELETE ON lead_review_domain
    FOR EACH ROW EXECUTE FUNCTION lead_review_domain_keep_rows();

DROP TRIGGER IF EXISTS trg_lead_review_domain_no_truncate ON lead_review_domain;
CREATE TRIGGER trg_lead_review_domain_no_truncate
    BEFORE TRUNCATE ON lead_review_domain
    FOR EACH STATEMENT EXECUTE FUNCTION lead_review_domain_keep_rows();
