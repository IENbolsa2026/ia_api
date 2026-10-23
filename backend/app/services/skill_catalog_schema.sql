-- Install once with a role allowed to create extensions. Runtime writes use
-- parameterized queries; no CVs, document text, or candidate identifiers belong here.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS skill_catalog (
    concept_id text PRIMARY KEY,
    label text NOT NULL,
    aliases text[] NOT NULL DEFAULT '{}',
    definition text NOT NULL DEFAULT '',
    positive_signals text[] NOT NULL DEFAULT '{}',
    negative_signals text[] NOT NULL DEFAULT '{}',
    domain text NOT NULL DEFAULT '',
    embedding vector,
    version text NOT NULL DEFAULT '1',
    source text NOT NULL DEFAULT 'custom',
    search_names text[] NOT NULL DEFAULT '{}',
    search_text text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS skill_catalog_domain_idx ON skill_catalog(domain);

-- Embeddings of different dimensions can coexist during model migrations.
-- For large catalogs, an administrator can add an index for the active model:
-- CREATE INDEX skill_catalog_embedding_384_idx ON skill_catalog
-- USING hnsw ((embedding::vector(384)) vector_cosine_ops)
-- WHERE vector_dims(embedding) = 384;
-- The generic query intentionally performs exact cosine top-k; it does not
-- assume that a particular model-specific approximate index is installed.
