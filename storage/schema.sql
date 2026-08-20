CREATE TABLE IF NOT EXISTS daily_model_usage (
    usage_date date NOT NULL,
    provider text NOT NULL CHECK (provider IN ('codex', 'claude', 'openrouter', 'pi', 'antigravity')),
    model text NOT NULL,
    input_tokens bigint NOT NULL CHECK (input_tokens >= 0),
    output_tokens bigint NOT NULL CHECK (output_tokens >= 0),
    cache_read_tokens bigint NOT NULL CHECK (cache_read_tokens >= 0),
    cache_write_tokens bigint NOT NULL CHECK (cache_write_tokens >= 0),
    total_tokens bigint NOT NULL CHECK (total_tokens >= 0),
    event_count integer NOT NULL CHECK (event_count >= 0),
    estimated_cost_usd numeric(18, 6),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (usage_date, provider, model)
);

CREATE INDEX IF NOT EXISTS daily_model_usage_model_date_idx
    ON daily_model_usage (model, usage_date DESC);

ALTER TABLE daily_model_usage
    DROP CONSTRAINT IF EXISTS daily_model_usage_provider_check;
ALTER TABLE daily_model_usage
    ADD CONSTRAINT daily_model_usage_provider_check
    CHECK (provider IN ('codex', 'claude', 'openrouter', 'pi', 'antigravity'));

CREATE OR REPLACE VIEW daily_usage_summary AS
SELECT
    usage_date,
    sum(total_tokens) AS total_tokens,
    sum(estimated_cost_usd) AS estimated_cost_usd,
    bool_and(estimated_cost_usd IS NOT NULL) AS cost_complete,
    sum(event_count) AS event_count,
    count(*) AS model_count
FROM daily_model_usage
GROUP BY usage_date
ORDER BY usage_date DESC;
