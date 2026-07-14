"""Backfill/refresh de ml_item_promotions.

Recorre TODAS las promociones del vendedor y, por cada una, pagina sus ítems
(candidate + started) y hace UPSERT en ml_item_promotions. Así la tabla queda
COMPLETA para pricing-app, no solo con lo que llegó por webhook.

Vía eficiente: por promoción (una llamada por página de ~50 ítems), no por ítem.
Idempotente por PK (mla, promotion_id). Reusa la persistencia de app.py.

Uso:
    python backfill_promotions.py

Nota: PRICE_DISCOUNT NO aparece en la lista de promos del vendedor (es por-ítem,
sin promotion_id propio), así que NO se cubre acá — queda cubierto por la lectura
en vivo del ítem (write-through) y los webhooks.
"""

import os
from dotenv import load_dotenv

load_dotenv()

from app import (
    _promos_seller_id,
    _promos_api_get,
    _persist_promotions,
    _persist_promo_items,
)

PAGE_LIMIT = 50


def _fetch_all_promos(seller_id):
    """Lista completa de promos del vendedor (pagina por offset).
    De paso refresca ml_promotions."""
    promos = []
    offset = 0
    while True:
        res = _promos_api_get(
            f"/seller-promotions/users/{seller_id}",
            {"offset": offset, "limit": PAGE_LIMIT},
        )
        if res.status_code != 200:
            print(f"❌ users list offset={offset} -> {res.status_code}: {res.text[:150]}")
            break
        data = res.json()
        _persist_promotions(data)
        results = data.get("results") or []
        promos.extend(results)
        total = (data.get("paging") or {}).get("total", 0)
        offset += PAGE_LIMIT
        if offset >= total or not results:
            break
    return promos


def _backfill_promo(promo):
    """Pagina los ítems de UNA promo y los persiste. Devuelve cuántos vio."""
    promo_id = promo.get("id")
    ptype = promo.get("type")
    if not promo_id or not ptype:
        return 0

    seen = 0
    pages = 0
    search_after = None
    while True:
        params = {"promotion_type": ptype, "limit": PAGE_LIMIT}
        if search_after:
            params["search_after"] = search_after
        res = _promos_api_get(f"/seller-promotions/promotions/{promo_id}/items", params)
        if res.status_code != 200:
            print(f"  ⚠️ {promo_id} ({ptype}) page{pages} -> {res.status_code}: {res.text[:120]}")
            break
        data = res.json()
        results = data.get("results") or []
        if not results:
            break
        _persist_promo_items(promo_id, ptype, data)
        seen += len(results)
        pages += 1
        search_after = (data.get("paging") or {}).get("searchAfter")
        if not search_after:
            break
    print(f"  ✔ {promo_id} ({ptype}): {seen} ítems en {pages} págs")
    return seen


def run_backfill():
    seller_id = _promos_seller_id()
    if seller_id is None:
        print("❌ seller_id no disponible (ml_tokens.user_id fila id=1)")
        return

    promos = _fetch_all_promos(seller_id)
    print(f"🔄 {len(promos)} promociones para backfill (seller {seller_id})")

    total_items = 0
    for promo in promos:
        total_items += _backfill_promo(promo)

    print(f"📦 Backfill completo: {total_items} ítems upserteados en ml_item_promotions")


if __name__ == "__main__":
    run_backfill()
