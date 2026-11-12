-- ML charges the seller shipping by item-price bracket. list_cost is now quoted at the
-- current sale_price (promo price when active); cost_by_bracket holds the cost of every
-- bracket, [{"min_price": 0, "list_cost": 0}, {"min_price": 33000, ...}, ...], ascending,
-- so pricing can price any candidate price without a live ML call.
ALTER TABLE ml_seller_shipping_costs
    ADD COLUMN IF NOT EXISTS sale_price      NUMERIC(18,4),
    ADD COLUMN IF NOT EXISTS cost_by_bracket JSONB;
