-- Central de Promociones (Seller Promotions API v2) persistida por ml-webhook
-- para que pricing-app la consulte cross-DB (lectura desde la base mlwebhook).
-- Se puebla por write-through best-effort desde los endpoints /api/promociones*.
-- Único vendedor, por eso no se guarda seller_id en las PKs.

-- Catálogo de promociones del vendedor (/seller-promotions/users/{seller_id}).
CREATE TABLE IF NOT EXISTS ml_promotions (
    promotion_id   TEXT PRIMARY KEY,          -- C-MLA..., P-MLA..., DOD-MLA1000, LGH-MLA1000
    promotion_type TEXT NOT NULL,             -- SELLER_CAMPAIGN, DEAL, SMART, DOD, LIGHTNING, ...
    sub_type       TEXT,                       -- e.g. FLEXIBLE_PERCENTAGE
    status         TEXT,                       -- started, pending, ...
    name           TEXT,
    start_date     TIMESTAMPTZ,
    finish_date    TIMESTAMPTZ,
    deadline_date  TIMESTAMPTZ,
    payload        JSONB NOT NULL,             -- snapshot completo de la promo
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ml_promotions_type   ON ml_promotions (promotion_type);
CREATE INDEX IF NOT EXISTS idx_ml_promotions_status ON ml_promotions (status);

-- Relación item x promoción (/seller-promotions/items/{mla} y /promotions/{id}/items).
-- PRICE_DISCOUNT no trae promotion_id: se usa el promotion_type como clave sintética.
CREATE TABLE IF NOT EXISTS ml_item_promotions (
    mla                        TEXT NOT NULL,
    promotion_id               TEXT NOT NULL,  -- id real de la promo, o el type si no hay (PRICE_DISCOUNT)
    promotion_type             TEXT,
    sub_type                   TEXT,
    status                     TEXT,           -- candidate | started
    original_price             NUMERIC(18, 2),
    price                      NUMERIC(18, 2), -- precio activo si status = started
    min_discounted_price       NUMERIC(18, 2),
    max_discounted_price       NUMERIC(18, 2),
    suggested_discounted_price NUMERIC(18, 2),
    payload                    JSONB NOT NULL,
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (mla, promotion_id)
);

CREATE INDEX IF NOT EXISTS idx_ml_item_promotions_promo  ON ml_item_promotions (promotion_id);
CREATE INDEX IF NOT EXISTS idx_ml_item_promotions_status ON ml_item_promotions (status);

-- mluser es el rol de runtime (ml-webhook escribe, pricing-app lee cross-DB).
-- Las migraciones corren con otro owner, así que el GRANT explícito es obligatorio.
GRANT SELECT, INSERT, UPDATE, DELETE ON ml_promotions      TO mluser;
GRANT SELECT, INSERT, UPDATE, DELETE ON ml_item_promotions TO mluser;
