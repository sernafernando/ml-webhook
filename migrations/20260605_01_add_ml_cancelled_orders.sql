-- Órdenes de MercadoLibre canceladas, persistidas por el webhook de ml-webhook
-- para que pricing-app las consulte cross-DB (lectura desde la base mlwebhook).
-- Sólo se escriben órdenes con status = 'cancelled'. Idempotente por order_id.

CREATE TABLE IF NOT EXISTS ml_cancelled_orders (
    order_id       BIGINT PRIMARY KEY,        -- id de la orden ML (mlo_id)
    pack_id        BIGINT,                     -- pack al que pertenece, si aplica
    status         TEXT NOT NULL,              -- estado capturado ('cancelled')
    status_detail  TEXT,                       -- motivo (cancel_detail/status_detail)
    cancelled_by   TEXT,                       -- requested_by / group (buyer, seller, ML)
    date_created   TIMESTAMPTZ,                -- alta de la orden
    date_closed    TIMESTAMPTZ,                -- cierre/cancelación de la orden
    total_amount   NUMERIC(18, 2),
    currency_id    TEXT,
    buyer_id       BIGINT,
    buyer_nickname TEXT,
    seller_id      BIGINT,
    items          JSONB,                      -- [{item_id, seller_sku, title, quantity, unit_price}]
    payload        JSONB NOT NULL,             -- snapshot completo de la orden
    cancelled_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ml_cancelled_orders_date_closed
    ON ml_cancelled_orders (date_closed DESC);

CREATE INDEX IF NOT EXISTS idx_ml_cancelled_orders_cancelled_at
    ON ml_cancelled_orders (cancelled_at DESC);

-- mluser es el rol de runtime (ml-webhook escribe, pricing-app lee/escribe
-- el backfill cross-DB). Las migraciones corren con otro owner, así que el
-- GRANT explícito es obligatorio.
GRANT SELECT, INSERT, UPDATE, DELETE ON ml_cancelled_orders TO mluser;
