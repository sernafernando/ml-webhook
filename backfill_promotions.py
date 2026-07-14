"""Backfill/refresh de ml_item_promotions.

Recorre TODAS las promociones del vendedor y, por cada una, pagina sus ítems
(candidate + started) y hace UPSERT en ml_item_promotions. Así la tabla queda
COMPLETA para pricing-app, no solo con lo que llegó por webhook.

Además reconcilia status stale: como el endpoint por-promo devuelve candidate Y
started, cualquier fila 'started' de una promo backfilleada por completo que quedó
con updated_at viejo = ML ya no la reporta ahí (el ítem salió de esa promo) = se
baja a 'finished'. Así 'started' refleja la realidad de ML y pricing-app no ve
promos "aplicadas" fantasma.

Vía eficiente: por promoción (una llamada por página de ~50 ítems), no por ítem.
Idempotente por PK (mla, promotion_id).

Uso:
    python backfill_promotions.py

Nota: PRICE_DISCOUNT NO aparece en la lista de promos del vendedor (es por-ítem,
sin promotion_id propio), así que NO se cubre acá ni se limpia — queda cubierto por
la lectura en vivo del ítem (write-through) y los webhooks.
"""

import os
from dotenv import load_dotenv

load_dotenv()

from app import (
    _promos_seller_id,
    _promos_api_get,
    _persist_promotions,
    _persist_promo_items,
    db_cursor,
)

PAGE_LIMIT = 50


def _db_now():
    with db_cursor() as cur:
        cur.execute("SELECT NOW()")
        return cur.fetchone()[0]


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
    """Pagina los ítems de UNA promo y los persiste.
    Devuelve (vistos, completa) — completa=True si terminó de paginar sin error."""
    promo_id = promo.get("id")
    ptype = promo.get("type")
    if not promo_id or not ptype:
        return 0, False

    seen = 0
    pages = 0
    complete = False
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
            complete = True  # llegamos al final (o promo vacía)
            break
        _persist_promo_items(promo_id, ptype, data)
        seen += len(results)
        pages += 1
        search_after = (data.get("paging") or {}).get("searchAfter")
        if not search_after:
            complete = True
            break
    print(f"  ✔ {promo_id} ({ptype}): {seen} ítems en {pages} págs{'' if complete else ' (INCOMPLETA)'}")
    return seen, complete


def _finish_stale_started(done_promos, run_start):
    """Baja a 'finished' las filas 'started' de promos backfilleadas por completo
    que NO se refrescaron en esta corrida (updated_at < run_start) => ML ya no las
    reporta. Scopeado a done_promos para no tocar promos con error ni PRICE_DISCOUNT."""
    if not done_promos:
        return 0
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE ml_item_promotions
            SET status = 'finished', updated_at = NOW()
            WHERE status = 'started'
              AND promotion_id = ANY(%s)
              AND updated_at < %s
            """,
            (done_promos, run_start),
        )
        return cur.rowcount


def run_backfill():
    seller_id = _promos_seller_id()
    if seller_id is None:
        print("❌ seller_id no disponible (ml_tokens.user_id fila id=1)")
        return

    run_start = _db_now()
    promos = _fetch_all_promos(seller_id)
    print(f"🔄 {len(promos)} promociones para backfill (seller {seller_id})")

    total_items = 0
    done_promos = []
    for promo in promos:
        seen, complete = _backfill_promo(promo)
        total_items += seen
        if complete and promo.get("id"):
            done_promos.append(promo["id"])

    finished = _finish_stale_started(done_promos, run_start)
    print(
        f"📦 Backfill completo: {total_items} ítems upserteados | "
        f"{finished} filas 'started' stale -> 'finished' "
        f"({len(done_promos)}/{len(promos)} promos backfilleadas enteras)"
    )


if __name__ == "__main__":
    run_backfill()
