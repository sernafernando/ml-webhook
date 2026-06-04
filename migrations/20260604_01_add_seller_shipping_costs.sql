CREATE TABLE IF NOT EXISTS ml_seller_shipping_costs (
    mla_id          TEXT PRIMARY KEY,
    seller_id       BIGINT,
    list_cost       NUMERIC(18,4),
    iva_included    BOOLEAN NOT NULL DEFAULT TRUE,
    currency_id     TEXT,
    billable_weight NUMERIC(18,4),
    logistic_type   TEXT,
    free_shipping   BOOLEAN,
    raw_payload     JSONB,
    source          TEXT,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ml_seller_shipping_costs_fetched_at
    ON ml_seller_shipping_costs (fetched_at);
