-- ML charges the seller shipping by item-price bracket. list_cost is now quoted at the
-- current sale_price (promo price when active); cost_by_bracket holds the cost of every
-- bracket, [{"min_price": 0, "list_cost": 0}, {"min_price": 33000, ...}, ...], ascending,
-- so pricing can price any candidate price without a live ML call.
--
-- DEPLOY ORDER (both UPSERT paths write these columns):
--   1. run this migration
--   2. deploy the app
--   3. run the shipping-cost sweep (sweep_shipping_costs.py) to fill the brackets
-- Deploying before step 1 makes every shipping-cost upsert fail (logged only) and the
-- sweep flush raise. Rollback: deploy the previous app first, then
-- migrations/down/20261112_01_add_shipping_cost_brackets.sql.
ALTER TABLE ml_seller_shipping_costs
    ADD COLUMN IF NOT EXISTS sale_price      NUMERIC(18,4),
    ADD COLUMN IF NOT EXISTS cost_by_bracket JSONB;
