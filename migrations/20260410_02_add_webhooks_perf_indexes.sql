CREATE INDEX IF NOT EXISTS idx_webhooks_topic_received_at
    ON webhooks (topic, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_webhooks_topic_resource_received_at
    ON webhooks (topic, resource, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_webhooks_topic
    ON webhooks (topic);

CREATE INDEX IF NOT EXISTS idx_ml_previews_resource
    ON ml_previews (resource);
