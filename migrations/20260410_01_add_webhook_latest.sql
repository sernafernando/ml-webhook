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

-- mluser es el rol de runtime (el handler de /webhook escribe el snapshot
-- en la misma transacción que el INSERT en webhooks). Las migraciones corren
-- con otro owner, así que el GRANT explícito es obligatorio: sin él el INSERT
-- falla, aborta la transacción y se pierde TAMBIÉN el webhook original.
GRANT SELECT, INSERT, UPDATE, DELETE ON webhook_latest TO mluser;
