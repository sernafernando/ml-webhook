CREATE TABLE IF NOT EXISTS ml_sellers (
    seller_id  BIGINT PRIMARY KEY,
    nickname   TEXT,
    payload    JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
