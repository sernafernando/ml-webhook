-- Tramos PxQ (precio por cantidad) de cada publicación.
--
-- Existen para que pricing-app pueda filtrar la LISTA de productos por "tiene
-- precios mayoristas" sin abrir publicación por publicación. Antes los tramos
-- sólo llegaban cuando un humano entraba a un producto y tocaba Importar, así
-- que el filtro veía una fracción del catálogo. El barrido
-- /admin/sweep-pxq-tiers los persiste para todo el catálogo activo.
--
-- Lectura cross-DB desde pricing-app: SELECT DISTINCT mla resuelve el set de
-- MLAs con tramos en una sola query, y quantity/amount alcanzan para mostrar
-- los tramos en la lista sin volver a ML.
CREATE TABLE IF NOT EXISTS ml_pxq_price_tiers (
    mla         TEXT NOT NULL,
    quantity    INTEGER NOT NULL,       -- conditions.min_purchase_unit
    amount      NUMERIC(18, 2),
    currency_id TEXT,
    price_id    TEXT,                   -- id del nodo de precio en ML
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (mla, quantity)
);

-- Bookkeeping del barrido, separado de los datos a propósito: una publicación
-- SIN tramos no deja ninguna fila en ml_pxq_price_tiers, y sin este registro el
-- barrido no podría saltearla por ?min_age_hours y la re-consultaría siempre.
CREATE TABLE IF NOT EXISTS ml_pxq_tier_scans (
    mla        TEXT PRIMARY KEY,
    tier_count INTEGER NOT NULL DEFAULT 0,
    source     TEXT,                    -- sweep | webhook
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ml_pxq_tier_scans_fetched ON ml_pxq_tier_scans (fetched_at);

-- mluser es el rol de runtime (ml-webhook escribe, pricing-app lee cross-DB).
-- Las migraciones corren con otro owner, así que el GRANT explícito es obligatorio.
GRANT SELECT, INSERT, UPDATE, DELETE ON ml_pxq_price_tiers TO mluser;
GRANT SELECT, INSERT, UPDATE, DELETE ON ml_pxq_tier_scans  TO mluser;
