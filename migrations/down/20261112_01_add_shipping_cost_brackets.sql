-- Down of migrations/20261112_01_add_shipping_cost_brackets.sql.
-- Run ONLY after the app that writes these columns is rolled back: the new UPSERTs
-- reference them. pricing-app reads cost_by_bracket through to_jsonb(row), so it keeps
-- working without the column (every cost becomes a plain list_cost again).
ALTER TABLE ml_seller_shipping_costs
    DROP COLUMN IF EXISTS cost_by_bracket,
    DROP COLUMN IF EXISTS sale_price;
