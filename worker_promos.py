import time

from app import (
    _redis_client,
    PROMOS_DIRTY_SET_KEY,
    reconcile_item_promotions,
)

# Drena el set redis de MLAs "sucios" (encolados por los webhooks public_offers)
# y reconcilia cada MLA una sola vez llamando /seller-promotions/items/{mla}.
# El set dedup-ea el flood de offers (~decenas de miles/hora) a MLAs únicos.

BATCH = 50          # MLAs por ciclo (SPOP con count)
IDLE_SLEEP = 5      # segundos de espera cuando el set está vacío


def run_worker():
    if _redis_client is None:
        raise RuntimeError("Redis no está disponible. No se puede iniciar worker_promos.")

    print(f"🔄 worker_promos drenando set: {PROMOS_DIRTY_SET_KEY}")
    while True:
        mlas = _redis_client.spop(PROMOS_DIRTY_SET_KEY, BATCH)
        if not mlas:
            time.sleep(IDLE_SLEEP)
            continue

        ok = 0
        for mla in mlas:
            try:
                if reconcile_item_promotions(mla):
                    ok += 1
            except Exception as err:
                print(f"❌ reconcile {mla} falló: {err}")
                try:
                    # reintento en el próximo ciclo (best-effort)
                    _redis_client.sadd(PROMOS_DIRTY_SET_KEY, mla)
                except Exception:
                    pass
        print(f"✅ promos reconciliados: {ok}/{len(mlas)}")


if __name__ == "__main__":
    run_worker()
