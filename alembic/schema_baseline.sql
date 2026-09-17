-- fd_open_data schema baseline — the schema the code produces on an empty database.
--
-- Produced by running Base.metadata.create_all() against an empty PostgreSQL
-- database and dumping the result, so it is what the models describe — not what
-- any live database happens to contain. It is the reference the Alembic baseline
-- revision must reproduce, and the thing to diff against when a live database and
-- the code disagree.
--
-- Regenerate:
--   createdb fdbaseline && FD_OPEN_DATA_MCP_DATABASE_URL=postgresql://<user>@<host>/fdbaseline \
--     python -c 'from fd_open_data_mcp import db; db.get_database()'
--   pg_dump --schema-only --no-owner --no-privileges <db> > alembic/schema_baseline.sql
--
-- Verified 2026-09-18 against the canonical fd_open_data on guangzhou-xinru:
-- 27 tables, zero column/type/nullability differences across every shared table.
-- The canonical database additionally holds 4 tables no model describes —
-- mig_concept_map, mig_entity_map, migration_orphans (no code references) and
-- source_provider_routes (owned by fd-proxy-service) — which this baseline
-- deliberately excludes.

\restrict xUduasItGvPBDr5XgYJ9vDK1fEob9128O6b0eP50UBCKjVWPSE7BTMk1kx37dhd
SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;
SET default_tablespace = '';
SET default_table_access_method = heap;
CREATE TABLE public.ban_rules (
    id integer NOT NULL,
    source character varying(64) NOT NULL,
    rule_type character varying(16) NOT NULL,
    pattern character varying(255) NOT NULL,
    classification character varying(16) NOT NULL,
    streak_min integer NOT NULL,
    priority integer NOT NULL,
    enabled boolean NOT NULL,
    created_at timestamp without time zone NOT NULL
);
CREATE SEQUENCE public.ban_rules_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.ban_rules_id_seq OWNED BY public.ban_rules.id;
CREATE TABLE public.clusters (
    id integer NOT NULL,
    name character varying(64) NOT NULL,
    api_server character varying(255) NOT NULL,
    namespace character varying(64) NOT NULL,
    image character varying(255) NOT NULL,
    tags jsonb,
    capacity integer NOT NULL,
    kubeconfig_secret character varying(128),
    enabled boolean NOT NULL,
    runtime_hints jsonb,
    created_at timestamp without time zone NOT NULL
);
CREATE SEQUENCE public.clusters_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.clusters_id_seq OWNED BY public.clusters.id;
CREATE TABLE public.columns (
    id integer NOT NULL,
    function_id integer NOT NULL,
    name character varying(255) NOT NULL,
    type character varying(64),
    description character varying,
    meaning character varying NOT NULL,
    semantic_type character varying(64),
    frequency character varying(32),
    datasource character varying(64),
    created_at timestamp without time zone
);
CREATE SEQUENCE public.columns_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.columns_id_seq OWNED BY public.columns.id;
CREATE TABLE public.concept_bindings (
    id integer NOT NULL,
    concept_id integer NOT NULL,
    column_id integer NOT NULL,
    confidence double precision NOT NULL,
    provenance character varying(32) NOT NULL,
    reviewed boolean NOT NULL,
    created_at timestamp without time zone
);
CREATE SEQUENCE public.concept_bindings_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.concept_bindings_id_seq OWNED BY public.concept_bindings.id;
CREATE TABLE public.concept_embeddings (
    id integer NOT NULL,
    concept_id integer NOT NULL,
    embedding jsonb NOT NULL,
    model character varying(128) NOT NULL,
    created_at timestamp without time zone
);
CREATE SEQUENCE public.concept_embeddings_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.concept_embeddings_id_seq OWNED BY public.concept_embeddings.id;
CREATE TABLE public.concept_families (
    id integer NOT NULL,
    code character varying(128) NOT NULL,
    name_en character varying(255),
    name_zh character varying(255),
    description character varying,
    value_type character varying(32),
    unit_type character varying(32),
    dimensions jsonb,
    uri character varying(512),
    deprecated boolean NOT NULL,
    created_at timestamp without time zone
);
CREATE SEQUENCE public.concept_families_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.concept_families_id_seq OWNED BY public.concept_families.id;
CREATE TABLE public.concept_mappings (
    id integer NOT NULL,
    concept_id integer NOT NULL,
    vocabulary character varying(64) NOT NULL,
    term character varying(255) NOT NULL,
    relation character varying(16) NOT NULL,
    confidence double precision NOT NULL,
    provenance character varying(32) NOT NULL,
    reviewed boolean NOT NULL,
    created_at timestamp without time zone
);
CREATE SEQUENCE public.concept_mappings_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.concept_mappings_id_seq OWNED BY public.concept_mappings.id;
CREATE TABLE public.concepts (
    id integer NOT NULL,
    code character varying(128) NOT NULL,
    name_en character varying(255),
    name_zh character varying(255),
    category character varying(255),
    unit character varying(64),
    measure character varying(64),
    frequency character varying(32) NOT NULL,
    entity_type character varying(32) NOT NULL,
    concept_code character varying(128),
    source character varying(64),
    verified boolean NOT NULL,
    deprecated boolean NOT NULL,
    created_at timestamp without time zone
);
CREATE SEQUENCE public.concepts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.concepts_id_seq OWNED BY public.concepts.id;
CREATE TABLE public.coverage_waves (
    id integer NOT NULL,
    entity_type character varying(32) NOT NULL,
    frequency_bucket character varying(32) NOT NULL,
    coverage_state character varying(16) NOT NULL,
    concept_ids jsonb NOT NULL,
    date_policy jsonb NOT NULL,
    mode character varying(16) NOT NULL,
    status character varying(16) NOT NULL,
    policy_ids jsonb,
    rows_new integer,
    concepts_before integer,
    concepts_after integer,
    detail character varying,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);
CREATE SEQUENCE public.coverage_waves_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.coverage_waves_id_seq OWNED BY public.coverage_waves.id;
CREATE TABLE public.crawl_policies (
    id integer NOT NULL,
    name character varying(128) NOT NULL,
    enabled boolean NOT NULL,
    concept_ids jsonb NOT NULL,
    entity_type character varying(32) NOT NULL,
    entity_ids jsonb,
    date_policy jsonb NOT NULL,
    frequency character varying(32) NOT NULL,
    mode character varying(16) NOT NULL,
    source_filter jsonb,
    force boolean NOT NULL,
    executor character varying(16) NOT NULL,
    script character varying(255),
    script_args jsonb,
    cron_expr character varying(128) NOT NULL,
    timezone character varying(64) NOT NULL,
    last_run_at timestamp without time zone,
    created_at timestamp without time zone
);
CREATE SEQUENCE public.crawl_policies_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.crawl_policies_id_seq OWNED BY public.crawl_policies.id;
CREATE TABLE public.data_census (
    id integer NOT NULL,
    store character varying(64) NOT NULL,
    kind character varying(16) NOT NULL,
    approx_rows integer,
    exact boolean NOT NULL,
    total_size_bytes integer,
    chunks integer,
    time_range_end character varying(64),
    error character varying,
    sampled_at timestamp without time zone NOT NULL
);
CREATE SEQUENCE public.data_census_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.data_census_id_seq OWNED BY public.data_census.id;
CREATE TABLE public.entities (
    id integer NOT NULL,
    entity_type character varying(32) NOT NULL,
    code character varying(128) NOT NULL,
    name_en character varying(255),
    name_zh character varying(255),
    metadata_json jsonb,
    updated_at timestamp without time zone
);
CREATE SEQUENCE public.entities_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.entities_id_seq OWNED BY public.entities.id;
CREATE TABLE public.entity_embeddings (
    id integer NOT NULL,
    entity_id integer NOT NULL,
    embedding text NOT NULL,
    model character varying(128) NOT NULL,
    created_at timestamp without time zone
);
CREATE SEQUENCE public.entity_embeddings_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.entity_embeddings_id_seq OWNED BY public.entity_embeddings.id;
CREATE TABLE public.entity_relationships (
    id integer NOT NULL,
    source_id integer NOT NULL,
    relation_type character varying(64) NOT NULL,
    target_id integer NOT NULL,
    valid_from timestamp without time zone,
    valid_to timestamp without time zone,
    metadata_json jsonb,
    created_at timestamp without time zone
);
CREATE SEQUENCE public.entity_relationships_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.entity_relationships_id_seq OWNED BY public.entity_relationships.id;
CREATE TABLE public.entity_source_identifiers (
    id integer NOT NULL,
    entity_type character varying(32) NOT NULL,
    entity_id integer NOT NULL,
    source character varying(64) NOT NULL,
    identifier character varying(255) NOT NULL,
    created_at timestamp without time zone
);
CREATE SEQUENCE public.entity_source_identifiers_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.entity_source_identifiers_id_seq OWNED BY public.entity_source_identifiers.id;
CREATE TABLE public.executions (
    id integer NOT NULL,
    schedule_id integer,
    concept_id integer,
    status character varying(32) NOT NULL,
    started_at timestamp without time zone NOT NULL,
    finished_at timestamp without time zone,
    detail character varying
);
CREATE SEQUENCE public.executions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.executions_id_seq OWNED BY public.executions.id;
CREATE TABLE public.fetch_log (
    id integer NOT NULL,
    source character varying(64) NOT NULL,
    concept_id integer,
    entity_type character varying(32),
    entity_id integer,
    latency_ms integer,
    status character varying(32) NOT NULL,
    detail character varying,
    proxy_id integer,
    classification character varying(16),
    real_source character varying(64),
    function_id integer,
    cluster_id integer,
    "timestamp" timestamp without time zone NOT NULL
);
CREATE SEQUENCE public.fetch_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.fetch_log_id_seq OWNED BY public.fetch_log.id;
CREATE TABLE public.functions (
    id integer NOT NULL,
    source_id integer NOT NULL,
    command character varying(255) NOT NULL,
    category character varying(255),
    description character varying,
    parameters json,
    verified boolean NOT NULL,
    scanner_mode character varying(32) NOT NULL,
    frequency character varying(32),
    real_sources jsonb,
    bulk_history boolean NOT NULL,
    bulk_snapshot boolean NOT NULL,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);
CREATE SEQUENCE public.functions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.functions_id_seq OWNED BY public.functions.id;
CREATE TABLE public.policy_runs (
    id integer NOT NULL,
    policy_id integer,
    origin character varying(16) NOT NULL,
    status character varying(32) NOT NULL,
    plan_json jsonb,
    job_ref character varying(255),
    cluster_id integer,
    started_at timestamp without time zone NOT NULL,
    finished_at timestamp without time zone,
    detail character varying,
    plan_cells integer,
    rows_attempted integer,
    rows_new integer,
    cancelled_by character varying(128)
);
CREATE SEQUENCE public.policy_runs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.policy_runs_id_seq OWNED BY public.policy_runs.id;
CREATE TABLE public.proxies (
    id integer NOT NULL,
    scheme character varying(16) NOT NULL,
    ip character varying(64) NOT NULL,
    port integer,
    auth character varying(255),
    status character varying(16) NOT NULL,
    label character varying(64),
    cluster_id integer,
    provider text,
    provider_meta jsonb,
    max_concurrency integer,
    created_at timestamp without time zone NOT NULL,
    retired_at timestamp without time zone
);
CREATE SEQUENCE public.proxies_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.proxies_id_seq OWNED BY public.proxies.id;
CREATE TABLE public.schedules (
    id integer NOT NULL,
    concept_id integer NOT NULL,
    cron_expr character varying(128) NOT NULL,
    timezone character varying(64) NOT NULL,
    enabled boolean NOT NULL,
    last_run_at timestamp without time zone,
    created_at timestamp without time zone
);
CREATE SEQUENCE public.schedules_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.schedules_id_seq OWNED BY public.schedules.id;
CREATE TABLE public.semantic_observations (
    id integer NOT NULL,
    concept_id integer NOT NULL,
    entity_type character varying(32) NOT NULL,
    entity_id integer NOT NULL,
    date character varying(64) NOT NULL,
    granularity character varying(8) DEFAULT 'day'::character varying NOT NULL,
    value character varying(255),
    unit character varying(64),
    source_used character varying(64) NOT NULL,
    fetched_at timestamp without time zone NOT NULL
);
CREATE SEQUENCE public.semantic_observations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.semantic_observations_id_seq OWNED BY public.semantic_observations.id;
CREATE TABLE public.source_probes (
    id integer NOT NULL,
    source character varying(64) NOT NULL,
    command character varying(128) NOT NULL,
    params json NOT NULL,
    enabled boolean NOT NULL,
    updated_at timestamp without time zone
);
CREATE SEQUENCE public.source_probes_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.source_probes_id_seq OWNED BY public.source_probes.id;
CREATE TABLE public.source_proxy_health (
    id integer NOT NULL,
    source character varying(64) NOT NULL,
    proxy_id integer NOT NULL,
    state character varying(16) NOT NULL,
    fail_streak integer NOT NULL,
    success_streak integer NOT NULL,
    last_fetch_at timestamp without time zone,
    last_success_at timestamp without time zone,
    banned_at timestamp without time zone,
    cooldown_until timestamp without time zone,
    open_cycles integer NOT NULL,
    permanent boolean NOT NULL,
    updated_at timestamp without time zone
);
CREATE SEQUENCE public.source_proxy_health_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.source_proxy_health_id_seq OWNED BY public.source_proxy_health.id;
CREATE TABLE public.source_rankings (
    id integer NOT NULL,
    source character varying(64) NOT NULL,
    concept_id integer NOT NULL,
    quality double precision NOT NULL,
    accessibility double precision NOT NULL,
    freshness_fit double precision NOT NULL,
    fetch_count integer NOT NULL,
    fail_count integer NOT NULL,
    updated_at timestamp without time zone
);
CREATE SEQUENCE public.source_rankings_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.source_rankings_id_seq OWNED BY public.source_rankings.id;
CREATE TABLE public.source_rate_limits (
    id integer NOT NULL,
    source character varying(64) NOT NULL,
    max_qps double precision NOT NULL,
    max_concurrent integer NOT NULL,
    updated_at timestamp without time zone
);
CREATE SEQUENCE public.source_rate_limits_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.source_rate_limits_id_seq OWNED BY public.source_rate_limits.id;
CREATE TABLE public.sources (
    id integer NOT NULL,
    name character varying(64) NOT NULL,
    label character varying(128) NOT NULL,
    description character varying,
    url character varying(512),
    scanner_version character varying(32),
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);
CREATE SEQUENCE public.sources_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.sources_id_seq OWNED BY public.sources.id;
ALTER TABLE ONLY public.ban_rules ALTER COLUMN id SET DEFAULT nextval('public.ban_rules_id_seq'::regclass);
ALTER TABLE ONLY public.clusters ALTER COLUMN id SET DEFAULT nextval('public.clusters_id_seq'::regclass);
ALTER TABLE ONLY public.columns ALTER COLUMN id SET DEFAULT nextval('public.columns_id_seq'::regclass);
ALTER TABLE ONLY public.concept_bindings ALTER COLUMN id SET DEFAULT nextval('public.concept_bindings_id_seq'::regclass);
ALTER TABLE ONLY public.concept_embeddings ALTER COLUMN id SET DEFAULT nextval('public.concept_embeddings_id_seq'::regclass);
ALTER TABLE ONLY public.concept_families ALTER COLUMN id SET DEFAULT nextval('public.concept_families_id_seq'::regclass);
ALTER TABLE ONLY public.concept_mappings ALTER COLUMN id SET DEFAULT nextval('public.concept_mappings_id_seq'::regclass);
ALTER TABLE ONLY public.concepts ALTER COLUMN id SET DEFAULT nextval('public.concepts_id_seq'::regclass);
ALTER TABLE ONLY public.coverage_waves ALTER COLUMN id SET DEFAULT nextval('public.coverage_waves_id_seq'::regclass);
ALTER TABLE ONLY public.crawl_policies ALTER COLUMN id SET DEFAULT nextval('public.crawl_policies_id_seq'::regclass);
ALTER TABLE ONLY public.data_census ALTER COLUMN id SET DEFAULT nextval('public.data_census_id_seq'::regclass);
ALTER TABLE ONLY public.entities ALTER COLUMN id SET DEFAULT nextval('public.entities_id_seq'::regclass);
ALTER TABLE ONLY public.entity_embeddings ALTER COLUMN id SET DEFAULT nextval('public.entity_embeddings_id_seq'::regclass);
ALTER TABLE ONLY public.entity_relationships ALTER COLUMN id SET DEFAULT nextval('public.entity_relationships_id_seq'::regclass);
ALTER TABLE ONLY public.entity_source_identifiers ALTER COLUMN id SET DEFAULT nextval('public.entity_source_identifiers_id_seq'::regclass);
ALTER TABLE ONLY public.executions ALTER COLUMN id SET DEFAULT nextval('public.executions_id_seq'::regclass);
ALTER TABLE ONLY public.fetch_log ALTER COLUMN id SET DEFAULT nextval('public.fetch_log_id_seq'::regclass);
ALTER TABLE ONLY public.functions ALTER COLUMN id SET DEFAULT nextval('public.functions_id_seq'::regclass);
ALTER TABLE ONLY public.policy_runs ALTER COLUMN id SET DEFAULT nextval('public.policy_runs_id_seq'::regclass);
ALTER TABLE ONLY public.proxies ALTER COLUMN id SET DEFAULT nextval('public.proxies_id_seq'::regclass);
ALTER TABLE ONLY public.schedules ALTER COLUMN id SET DEFAULT nextval('public.schedules_id_seq'::regclass);
ALTER TABLE ONLY public.semantic_observations ALTER COLUMN id SET DEFAULT nextval('public.semantic_observations_id_seq'::regclass);
ALTER TABLE ONLY public.source_probes ALTER COLUMN id SET DEFAULT nextval('public.source_probes_id_seq'::regclass);
ALTER TABLE ONLY public.source_proxy_health ALTER COLUMN id SET DEFAULT nextval('public.source_proxy_health_id_seq'::regclass);
ALTER TABLE ONLY public.source_rankings ALTER COLUMN id SET DEFAULT nextval('public.source_rankings_id_seq'::regclass);
ALTER TABLE ONLY public.source_rate_limits ALTER COLUMN id SET DEFAULT nextval('public.source_rate_limits_id_seq'::regclass);
ALTER TABLE ONLY public.sources ALTER COLUMN id SET DEFAULT nextval('public.sources_id_seq'::regclass);
ALTER TABLE ONLY public.ban_rules
    ADD CONSTRAINT ban_rules_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.clusters
    ADD CONSTRAINT clusters_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.columns
    ADD CONSTRAINT columns_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.concept_bindings
    ADD CONSTRAINT concept_bindings_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.concept_embeddings
    ADD CONSTRAINT concept_embeddings_concept_id_model_key UNIQUE (concept_id, model);
ALTER TABLE ONLY public.concept_embeddings
    ADD CONSTRAINT concept_embeddings_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.concept_families
    ADD CONSTRAINT concept_families_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.concept_mappings
    ADD CONSTRAINT concept_mappings_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.concepts
    ADD CONSTRAINT concepts_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.coverage_waves
    ADD CONSTRAINT coverage_waves_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.crawl_policies
    ADD CONSTRAINT crawl_policies_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.data_census
    ADD CONSTRAINT data_census_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.entities
    ADD CONSTRAINT entities_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.entity_embeddings
    ADD CONSTRAINT entity_embeddings_entity_id_model_key UNIQUE (entity_id, model);
ALTER TABLE ONLY public.entity_embeddings
    ADD CONSTRAINT entity_embeddings_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.entity_relationships
    ADD CONSTRAINT entity_relationships_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.entity_source_identifiers
    ADD CONSTRAINT entity_source_identifiers_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.executions
    ADD CONSTRAINT executions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.fetch_log
    ADD CONSTRAINT fetch_log_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.functions
    ADD CONSTRAINT functions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.policy_runs
    ADD CONSTRAINT policy_runs_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.proxies
    ADD CONSTRAINT proxies_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.schedules
    ADD CONSTRAINT schedules_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.semantic_observations
    ADD CONSTRAINT semantic_observations_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.source_probes
    ADD CONSTRAINT source_probes_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.source_proxy_health
    ADD CONSTRAINT source_proxy_health_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.source_rankings
    ADD CONSTRAINT source_rankings_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.source_rate_limits
    ADD CONSTRAINT source_rate_limits_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.sources
    ADD CONSTRAINT sources_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.concept_bindings
    ADD CONSTRAINT uq_concept_column UNIQUE (concept_id, column_id);
ALTER TABLE ONLY public.concepts
    ADD CONSTRAINT uq_concept_identity UNIQUE (code, entity_type, measure, unit, frequency);
ALTER TABLE ONLY public.concept_mappings
    ADD CONSTRAINT uq_concept_mapping UNIQUE (concept_id, vocabulary, term, relation);
ALTER TABLE ONLY public.entity_source_identifiers
    ADD CONSTRAINT uq_entity_source UNIQUE (entity_type, entity_id, source);
ALTER TABLE ONLY public.entities
    ADD CONSTRAINT uq_entity_type_code UNIQUE (entity_type, code);
ALTER TABLE ONLY public.columns
    ADD CONSTRAINT uq_function_column UNIQUE (function_id, name);
ALTER TABLE ONLY public.entity_relationships
    ADD CONSTRAINT uq_rel_edges UNIQUE (source_id, relation_type, target_id, valid_from);
ALTER TABLE ONLY public.semantic_observations
    ADD CONSTRAINT uq_sem_obs UNIQUE (concept_id, entity_type, entity_id, date, granularity);
ALTER TABLE ONLY public.source_rankings
    ADD CONSTRAINT uq_source_concept_rank UNIQUE (source, concept_id);
ALTER TABLE ONLY public.functions
    ADD CONSTRAINT uq_source_function UNIQUE (source_id, command);
ALTER TABLE ONLY public.source_probes
    ADD CONSTRAINT uq_source_probe UNIQUE (source);
ALTER TABLE ONLY public.source_proxy_health
    ADD CONSTRAINT uq_source_proxy UNIQUE (source, proxy_id);
ALTER TABLE ONLY public.source_rate_limits
    ADD CONSTRAINT uq_source_rate_limit UNIQUE (source);
CREATE INDEX idx_concept_embeddings_concept_id ON public.concept_embeddings USING btree (concept_id);
CREATE INDEX idx_entity_embeddings_entity_id ON public.entity_embeddings USING btree (entity_id);
CREATE INDEX idx_entity_embeddings_model ON public.entity_embeddings USING btree (model);
CREATE INDEX ix_ban_rules_source ON public.ban_rules USING btree (source);
CREATE UNIQUE INDEX ix_clusters_name ON public.clusters USING btree (name);
CREATE INDEX ix_columns_function_id ON public.columns USING btree (function_id);
CREATE INDEX ix_concept_bindings_column_id ON public.concept_bindings USING btree (column_id);
CREATE INDEX ix_concept_bindings_concept_id ON public.concept_bindings USING btree (concept_id);
CREATE UNIQUE INDEX ix_concept_families_code ON public.concept_families USING btree (code);
CREATE INDEX ix_concept_mappings_concept_id ON public.concept_mappings USING btree (concept_id);
CREATE INDEX ix_concept_mappings_term ON public.concept_mappings USING btree (term);
CREATE INDEX ix_concept_mappings_vocabulary ON public.concept_mappings USING btree (vocabulary);
CREATE INDEX ix_concepts_code ON public.concepts USING btree (code);
CREATE INDEX ix_concepts_concept_code ON public.concepts USING btree (concept_code);
CREATE INDEX ix_coverage_waves_entity_type ON public.coverage_waves USING btree (entity_type);
CREATE INDEX ix_coverage_waves_status ON public.coverage_waves USING btree (status);
CREATE UNIQUE INDEX ix_crawl_policies_name ON public.crawl_policies USING btree (name);
CREATE UNIQUE INDEX ix_data_census_store ON public.data_census USING btree (store);
CREATE INDEX ix_entities_entity_type ON public.entities USING btree (entity_type);
CREATE INDEX ix_entity_relationships_relation_type ON public.entity_relationships USING btree (relation_type);
CREATE INDEX ix_entity_relationships_source_id ON public.entity_relationships USING btree (source_id);
CREATE INDEX ix_entity_relationships_target_id ON public.entity_relationships USING btree (target_id);
CREATE INDEX ix_executions_concept_id ON public.executions USING btree (concept_id);
CREATE INDEX ix_executions_schedule_id ON public.executions USING btree (schedule_id);
CREATE INDEX ix_fetch_log_cluster_id ON public.fetch_log USING btree (cluster_id);
CREATE INDEX ix_fetch_log_concept_id ON public.fetch_log USING btree (concept_id);
CREATE INDEX ix_fetch_log_function_id ON public.fetch_log USING btree (function_id);
CREATE INDEX ix_fetch_log_proxy_id ON public.fetch_log USING btree (proxy_id);
CREATE INDEX ix_fetch_log_real_source ON public.fetch_log USING btree (real_source);
CREATE INDEX ix_fetch_log_source ON public.fetch_log USING btree (source);
CREATE INDEX ix_functions_command ON public.functions USING btree (command);
CREATE INDEX ix_functions_source_id ON public.functions USING btree (source_id);
CREATE INDEX ix_policy_runs_cluster_id ON public.policy_runs USING btree (cluster_id);
CREATE INDEX ix_policy_runs_policy_id ON public.policy_runs USING btree (policy_id);
CREATE INDEX ix_proxies_cluster_id ON public.proxies USING btree (cluster_id);
CREATE INDEX ix_schedules_concept_id ON public.schedules USING btree (concept_id);
CREATE INDEX ix_semantic_observations_concept_id ON public.semantic_observations USING btree (concept_id);
CREATE INDEX ix_source_probes_source ON public.source_probes USING btree (source);
CREATE INDEX ix_source_proxy_health_proxy_id ON public.source_proxy_health USING btree (proxy_id);
CREATE INDEX ix_source_proxy_health_source ON public.source_proxy_health USING btree (source);
CREATE INDEX ix_source_rankings_concept_id ON public.source_rankings USING btree (concept_id);
CREATE INDEX ix_source_rankings_source ON public.source_rankings USING btree (source);
CREATE INDEX ix_source_rate_limits_source ON public.source_rate_limits USING btree (source);
CREATE UNIQUE INDEX ix_sources_name ON public.sources USING btree (name);
ALTER TABLE ONLY public.columns
    ADD CONSTRAINT columns_function_id_fkey FOREIGN KEY (function_id) REFERENCES public.functions(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.concept_bindings
    ADD CONSTRAINT concept_bindings_column_id_fkey FOREIGN KEY (column_id) REFERENCES public.columns(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.concept_bindings
    ADD CONSTRAINT concept_bindings_concept_id_fkey FOREIGN KEY (concept_id) REFERENCES public.concepts(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.concept_mappings
    ADD CONSTRAINT concept_mappings_concept_id_fkey FOREIGN KEY (concept_id) REFERENCES public.concepts(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.entity_relationships
    ADD CONSTRAINT entity_relationships_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.entities(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.entity_relationships
    ADD CONSTRAINT entity_relationships_target_id_fkey FOREIGN KEY (target_id) REFERENCES public.entities(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.executions
    ADD CONSTRAINT executions_schedule_id_fkey FOREIGN KEY (schedule_id) REFERENCES public.schedules(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.fetch_log
    ADD CONSTRAINT fetch_log_function_id_fkey FOREIGN KEY (function_id) REFERENCES public.functions(id);
ALTER TABLE ONLY public.functions
    ADD CONSTRAINT functions_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.sources(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.policy_runs
    ADD CONSTRAINT policy_runs_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.policy_runs
    ADD CONSTRAINT policy_runs_policy_id_fkey FOREIGN KEY (policy_id) REFERENCES public.crawl_policies(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.proxies
    ADD CONSTRAINT proxies_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.clusters(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.schedules
    ADD CONSTRAINT schedules_concept_id_fkey FOREIGN KEY (concept_id) REFERENCES public.concepts(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.semantic_observations
    ADD CONSTRAINT semantic_observations_concept_id_fkey FOREIGN KEY (concept_id) REFERENCES public.concepts(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.source_proxy_health
    ADD CONSTRAINT source_proxy_health_proxy_id_fkey FOREIGN KEY (proxy_id) REFERENCES public.proxies(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.source_rankings
    ADD CONSTRAINT source_rankings_concept_id_fkey FOREIGN KEY (concept_id) REFERENCES public.concepts(id) ON DELETE CASCADE;
\unrestrict xUduasItGvPBDr5XgYJ9vDK1fEob9128O6b0eP50UBCKjVWPSE7BTMk1kx37dhd
