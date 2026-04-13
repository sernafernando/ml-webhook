CREATE TABLE IF NOT EXISTS webhook_latest (
    topic TEXT NOT NULL,
    resource TEXT NOT NULL,
    webhook_id TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload JSONB NOT NULL,
    PRIMARY KEY (topic, resource)
);

CREATE INDEX IF NOT EXISTS idx_webhook_latest_topic_received_resource
    ON webhook_latest (topic, received_at DESC, resource DESC);
