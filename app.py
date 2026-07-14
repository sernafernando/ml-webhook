from flask import Flask, request, redirect, jsonify, send_from_directory
import os
import requests
import json
import base64
from dotenv import load_dotenv
from datetime import datetime
import time
import psycopg2
from psycopg2.extras import Json, execute_values
from zoneinfo import ZoneInfo
from psycopg2 import pool
from contextlib import contextmanager
import threading

load_dotenv()

# ── Redis for SSE notifications (best-effort, never blocks webhook) ──
_redis_client = None
try:
    import redis as _redis_mod
    _redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    _redis_client = _redis_mod.Redis.from_url(_redis_url, decode_responses=True, socket_connect_timeout=2)
    _redis_client.ping()
    print(f"✅ Redis conectado para SSE ({_redis_url})")
except Exception as _redis_err:
    _redis_client = None
    print(f"⚠️ Redis no disponible — SSE deshabilitado: {_redis_err}")


def sse_notify(channel: str, data: dict = None):
    """
    Publish an SSE event to Redis pub/sub.
    Fire-and-forget: swallows all errors.
    Channel format: sse:{channel} (matches pricing-app SSEConnectionManager pattern).
    """
    if _redis_client is None:
        return
    try:
        import json as _json
        payload = _json.dumps({
            "channel": channel,
            "data": data or {},
            "timestamp": datetime.now(ZoneInfo("UTC")).isoformat(),
        })
        _redis_client.publish(f"sse:{channel}", payload)
    except Exception:
        pass  # Best-effort — never block the webhook

db_pool = pool.ThreadedConnectionPool(
    2, 20,
    dsn=os.getenv("DATABASE_URL")
)

@contextmanager
def db_cursor():
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        db_pool.putconn(conn)

app = Flask(__name__)

# Variables de entorno (todas con prefijo ML_)
ML_CLIENT_ID = os.getenv("ML_CLIENT_ID")
ML_CLIENT_SECRET = os.getenv("ML_CLIENT_SECRET")
ML_REDIRECT_URI = os.getenv("ML_REDIRECT_URI") or os.getenv("REDIRECT_URI")
ML_REFRESH_TOKEN = os.getenv("ML_REFRESH_TOKEN")

ACCESS_TOKEN = None
EXPIRATION = 0

DEBUG_WEBHOOK = os.getenv("DEBUG_WEBHOOK", "0") == "1"
FREE_SHIPPING_MIN_PRICE = float(os.getenv("FREE_SHIPPING_MIN_PRICE", "33000"))
WEBHOOKS_DEFAULT_LIMIT = int(os.getenv("WEBHOOKS_DEFAULT_LIMIT", "100"))
WEBHOOKS_MAX_LIMIT = int(os.getenv("WEBHOOKS_MAX_LIMIT", "500"))
WEBHOOK_PREVIEW_ASYNC = os.getenv("WEBHOOK_PREVIEW_ASYNC", "0") == "1"
WEBHOOKS_CURSOR_MODE = os.getenv("WEBHOOKS_CURSOR_MODE", "0") == "1"
WEBHOOK_TOPICS_CACHE_TTL = float(os.getenv("WEBHOOK_TOPICS_CACHE_TTL", "10"))
PREVIEW_QUEUE_KEY = os.getenv("PREVIEW_QUEUE_KEY", "queue:preview:resources")
PREVIEW_DEAD_QUEUE_KEY = os.getenv("PREVIEW_DEAD_QUEUE_KEY", "queue:preview:dead")
PROMOS_DIRTY_SET_KEY = os.getenv("PROMOS_DIRTY_SET_KEY", "promos:dirty:mlas")
PROMOS_WEBHOOK_ENABLED = os.getenv("PROMOS_WEBHOOK_ENABLED", "1") == "1"

_topics_cache = {"value": None, "expires_at": 0.0}
_topics_cache_lock = threading.Lock()

_sweep_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "processed": 0,
    "skipped": 0,
    "errors": 0,
    "total_enumerated": 0,
    "last_mla": None,
    "dry_run": False,
    "limit": None,
    "min_age_hours": 0,
}
_sweep_lock = threading.Lock()


FAVICON_DIR = "https://ml-webhook.gaussonline.com.ar/assets/white-g-BfxDaKwI.png"

# ---- Rate-limited ML API wrapper ----
_ml_api_lock = threading.Lock()
_ml_api_min_interval = 0.15  # mínimo 150ms entre requests (~6.6 req/s)
_ml_api_last_call = 0.0

def ml_api_get(url, headers=None, params=None, max_retries=3):
    """GET a la API de ML con rate limiting y retry automático en 429."""
    global _ml_api_last_call

    for attempt in range(max_retries):
        # throttle: esperar si estamos muy rápido
        with _ml_api_lock:
            now = time.time()
            wait = _ml_api_min_interval - (now - _ml_api_last_call)
            if wait > 0:
                time.sleep(wait)
            _ml_api_last_call = time.time()

        res = requests.get(url, headers=headers, params=params)

        if res.status_code == 429:
            # retry-after header o backoff exponencial
            retry_after = int(res.headers.get("Retry-After", 0))
            backoff = max(retry_after, (2 ** attempt) * 1.5)
            print(f"⚠️ 429 Rate Limited (intento {attempt+1}/{max_retries}), esperando {backoff:.1f}s...")
            time.sleep(backoff)
            continue

        return res

    # si agotamos retries, devolver la última respuesta (429)
    print(f"❌ Rate limit agotado tras {max_retries} intentos para {url}")
    return res


def _clamp_limit(raw_limit):
    try:
        limit = int(raw_limit) if raw_limit is not None else WEBHOOKS_DEFAULT_LIMIT
    except (TypeError, ValueError):
        limit = WEBHOOKS_DEFAULT_LIMIT

    if limit < 1:
        limit = 1
    if limit > WEBHOOKS_MAX_LIMIT:
        limit = WEBHOOKS_MAX_LIMIT
    return limit


def _encode_webhooks_cursor(received_at: datetime, resource: str):
    raw = f"{received_at.isoformat()}|{resource}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8")


def _decode_webhooks_cursor(cursor: str):
    decoded = base64.urlsafe_b64decode(cursor.encode("utf-8")).decode("utf-8")
    ts_raw, resource = decoded.split("|", 1)
    return datetime.fromisoformat(ts_raw), resource


def _enqueue_preview_job(resource: str, attempt: int = 1):
    if _redis_client is None:
        return False, "redis_unavailable"
    try:
        payload = json.dumps({
            "resource": resource,
            "attempt": attempt,
            "enqueued_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        })
        _redis_client.rpush(PREVIEW_QUEUE_KEY, payload)
        return True, None
    except Exception as err:
        return False, str(err)


def _run_preview_in_background(resource: str):
    def _target():
        try:
            fetch_and_store_preview(resource)
        except Exception:
            pass

    t = threading.Thread(target=_target, daemon=True)
    t.start()

def _upsert_seller_shipping_cost(mla_id, item_data, source):
    """Best-effort: fetch /shipping_options/free and UPSERT cost row.

    Never raises — logs and swallows. Called from webhook flow (source="webhook")
    and from sweep job (source="sweep"). iva_included hardcoded TRUE per PASO 1 finding.
    """
    try:
        seller_id = (item_data or {}).get("seller_id")
        if seller_id is None:
            print(f"⚠️ shipping_cost {mla_id}: seller_id ausente en item_data, skip")
            return

        shipping = (item_data or {}).get("shipping") or {}
        free_shipping = shipping.get("free_shipping")
        logistic_type = shipping.get("logistic_type")

        token = get_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"https://api.mercadolibre.com/users/{seller_id}/shipping_options/free"
        res = ml_api_get(url, headers=headers, params={"item_id": mla_id, "verbose": "true"})

        if res.status_code != 200:
            print(f"⚠️ shipping_cost {mla_id}: status={res.status_code}, skip")
            return

        body = res.json()
        cov = ((body or {}).get("coverage") or {}).get("all_country") or {}
        list_cost = cov.get("list_cost")
        currency_id = cov.get("currency_id")
        billable_weight = cov.get("billable_weight")

        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO ml_seller_shipping_costs
                    (mla_id, seller_id, list_cost, iva_included, currency_id,
                     billable_weight, logistic_type, free_shipping, raw_payload,
                     source, fetched_at)
                VALUES (%s, %s, %s, TRUE, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (mla_id) DO UPDATE SET
                    seller_id = EXCLUDED.seller_id,
                    list_cost = EXCLUDED.list_cost,
                    iva_included = EXCLUDED.iva_included,
                    currency_id = EXCLUDED.currency_id,
                    billable_weight = EXCLUDED.billable_weight,
                    logistic_type = COALESCE(EXCLUDED.logistic_type, ml_seller_shipping_costs.logistic_type),
                    free_shipping = EXCLUDED.free_shipping,
                    raw_payload = EXCLUDED.raw_payload,
                    source = EXCLUDED.source,
                    fetched_at = NOW();
            """, (
                mla_id, seller_id, list_cost, currency_id,
                billable_weight, logistic_type, free_shipping, Json(body),
                source,
            ))
        print(f"✅ shipping_cost {mla_id} upsert (source={source}, list_cost={list_cost})")
    except Exception as e:
        print(f"⚠️ shipping_cost {mla_id}: error {e}")



def _sweep_seller_shipping_costs(limit, dry_run, min_age_hours):
    """Background worker for /admin/sweep-shipping-costs.

    Uses a DEDICATED psycopg2 connection (DATABASE_ADMIN_URL) to bypass the app pool,
    plus batched queries (single skip-check, single cache read, execute_values UPSERTs)
    so the sweep has near-zero impact on pricing-app / webhook workloads.
    """
    import builtins, functools, sys
    print = functools.partial(builtins.print, file=sys.stderr, flush=True)
    conn = None
    try:
        # seller_id (single pool read, then we are done with the pool)
        with db_cursor() as cur:
            cur.execute("SELECT user_id FROM ml_tokens WHERE id = 1")
            row = cur.fetchone()
        if not row or row[0] is None:
            print("❌ sweep: ml_tokens.user_id ausente")
            _sweep_state["errors"] += 1
            return
        seller_id = row[0]

        # dedicated DB connection — no pool contention
        admin_url = os.getenv("DATABASE_ADMIN_URL")
        if not admin_url:
            admin_url = os.getenv("DATABASE_URL")
            print("⚠️ sweep: DATABASE_ADMIN_URL not set, using DATABASE_URL (may be PgBouncer — execute_values can fail in transaction mode)")
        conn = psycopg2.connect(admin_url)
        conn.autocommit = False

        token = get_token()
        headers = {"Authorization": f"Bearer {token}"}

        # enumerate active MLAs via scan API (handles >1000 items)
        all_mlas = []
        scroll_id = None
        while True:
            params = {"status": "active", "search_type": "scan", "limit": 100}
            if scroll_id:
                params["scroll_id"] = scroll_id
            url = f"https://api.mercadolibre.com/users/{seller_id}/items/search"
            res = ml_api_get(url, headers=headers, params=params)
            if res.status_code != 200:
                print(f"❌ sweep: enumeración status={res.status_code}, abort")
                _sweep_state["errors"] += 1
                return
            data_page = res.json()
            page = data_page.get("results") or []
            for entry in page:
                mla = entry if isinstance(entry, str) else (entry or {}).get("id")
                if mla:
                    all_mlas.append(mla)
            scroll_id = data_page.get("scroll_id")
            if not page or not scroll_id:
                break
            if limit and len(all_mlas) >= limit:
                all_mlas = all_mlas[:limit]
                break

        _sweep_state["total_enumerated"] = len(all_mlas)
        print(f"🔍 sweep: {len(all_mlas)} MLAs activos a procesar (dry_run={dry_run}, min_age_hours={min_age_hours})")

        # batch skip-check: one query for ALL MLAs
        fresh_set = set()
        if min_age_hours > 0 and all_mlas:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT mla_id FROM ml_seller_shipping_costs "
                    "WHERE mla_id = ANY(%s) "
                    "AND fetched_at > NOW() - (%s || ' hours')::interval",
                    (all_mlas, min_age_hours),
                )
                fresh_set = {r[0] for r in cur.fetchall()}
            conn.commit()
            _sweep_state["skipped"] = len(fresh_set)
            print(f"🔍 sweep: {len(fresh_set)} MLAs fresh < {min_age_hours}h, skip")

        to_process = [m for m in all_mlas if m not in fresh_set]

        if dry_run:
            _sweep_state["processed"] = len(to_process)
            print(f"✅ sweep dry_run: would process {len(to_process)}, skip {len(fresh_set)}")
            return

        # batch cache read: one query for ALL to_process MLAs
        cache = {}
        if to_process:
            resources = [f"/items/{m}" for m in to_process]
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT resource, extra_data->>'logistic_type', extra_data->>'free_shipping' "
                    "FROM ml_previews WHERE resource = ANY(%s)",
                    (resources,),
                )
                for resource, logistic, fs in cur.fetchall():
                    if not resource:
                        continue
                    parts = resource.split("/")
                    if len(parts) < 3:
                        continue
                    fs_bool = fs.lower() == "true" if fs is not None else None
                    cache[parts[2]] = (logistic, fs_bool)
            conn.commit()
            print(f"🔍 sweep: cache hit for {len(cache)}/{len(to_process)} MLAs (logistic_type/free_shipping)")

        # per-MLA ML call + buffered UPSERTs
        BATCH = 100
        buffer = []

        def _flush():
            if not buffer:
                return
            n = len(buffer)
            try:
                with conn.cursor() as cur:
                    execute_values(
                        cur,
                        """INSERT INTO ml_seller_shipping_costs
                           (mla_id, seller_id, list_cost, iva_included, currency_id,
                            billable_weight, logistic_type, free_shipping, raw_payload,
                            source, fetched_at)
                           VALUES %s
                           ON CONFLICT (mla_id) DO UPDATE SET
                              seller_id = EXCLUDED.seller_id,
                              list_cost = EXCLUDED.list_cost,
                              iva_included = EXCLUDED.iva_included,
                              currency_id = EXCLUDED.currency_id,
                              billable_weight = EXCLUDED.billable_weight,
                              logistic_type = COALESCE(EXCLUDED.logistic_type, ml_seller_shipping_costs.logistic_type),
                              free_shipping = EXCLUDED.free_shipping,
                              raw_payload = EXCLUDED.raw_payload,
                              source = EXCLUDED.source,
                              fetched_at = NOW()""",
                        buffer,
                        template="(%s, %s, %s, TRUE, %s, %s, %s, %s, %s, %s, NOW())",
                        page_size=BATCH,
                    )
                conn.commit()
                buffer.clear()
            except Exception as flush_err:
                try:
                    conn.rollback()
                except Exception:
                    pass
                buffer.clear()
                print(f"❌ sweep: flush FAILED ({type(flush_err).__name__}: {flush_err}). Lost batch of {n} rows.")
                raise

        for mla_id in to_process:
            _sweep_state["last_mla"] = mla_id
            cached_logistic, cached_fs = cache.get(mla_id, (None, None))
            try:
                url = f"https://api.mercadolibre.com/users/{seller_id}/shipping_options/free"
                res = ml_api_get(url, headers=headers, params={"item_id": mla_id, "verbose": "true"})
                if res.status_code != 200:
                    print(f"⚠️ shipping_cost {mla_id}: status={res.status_code}, skip")
                    _sweep_state["errors"] += 1
                    continue
                body = res.json()
                cov = ((body or {}).get("coverage") or {}).get("all_country") or {}
                buffer.append((
                    mla_id, seller_id, cov.get("list_cost"), cov.get("currency_id"),
                    cov.get("billable_weight"), cached_logistic, cached_fs, Json(body),
                    "sweep",
                ))
                _sweep_state["processed"] += 1
                if len(buffer) >= BATCH:
                    try:
                        _flush()
                    except Exception:
                        _sweep_state["errors"] += 1
                        print(f"❌ sweep: aborting after flush failure. processed={_sweep_state['processed']}/{len(to_process)}")
                        return
                    print(f"🔍 sweep: batch flushed, processed={_sweep_state['processed']}/{len(to_process)}")
            except Exception as e:
                print(f"⚠️ shipping_cost {mla_id}: error {e}")
                _sweep_state["errors"] += 1

        try:
            _flush()
        except Exception:
            _sweep_state["errors"] += 1
        print(f"✅ sweep done: processed={_sweep_state['processed']} skipped={_sweep_state['skipped']} errors={_sweep_state['errors']} total={_sweep_state['total_enumerated']}")
    except Exception as e:
        print(f"❌ sweep crash: {e}")
        _sweep_state["errors"] += 1
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
        _sweep_state["finished_at"] = datetime.now(ZoneInfo("UTC")).isoformat()
        _sweep_state["running"] = False


def refresh_token():
    global ACCESS_TOKEN, EXPIRATION

    tok = load_token_from_db() or {}
    refresh = tok.get("refresh_token") or ML_REFRESH_TOKEN
    if not refresh:
        raise Exception("❌ No hay refresh_token ni en DB ni en variables de entorno")

    url = "https://api.mercadolibre.com/oauth/token"
    payload = {
        "grant_type": "refresh_token",
        "client_id": ML_CLIENT_ID,
        "client_secret": ML_CLIENT_SECRET,
        "refresh_token": refresh
    }

    response = requests.post(url, data=payload)
    data = response.json()

    if "access_token" in data:
        # persistimos en DB (incluye refresh nuevo si ML lo manda)
        save_token_to_db(data)

        ACCESS_TOKEN = data["access_token"]
        EXPIRATION = time.time() + int(data.get("expires_in", 0)) - 60
        print("✅ Nuevo access_token obtenido (DB).")
    else:
        print("❌ Error al refrescar token:", data)
        raise Exception("No se pudo refrescar el access_token")
        
def get_token():
    global ACCESS_TOKEN, EXPIRATION

    # si en memoria está vencido, mirá DB
    if ACCESS_TOKEN is None or time.time() >= EXPIRATION:
        tok = load_token_from_db()
        if tok and tok.get("access_token") and time.time() < tok.get("expires_epoch", 0):
            ACCESS_TOKEN = tok["access_token"]
            EXPIRATION = tok["expires_epoch"]
            return ACCESS_TOKEN

        # DB no sirve o está vencido => refrescar
        refresh_token()

    return ACCESS_TOKEN

def _fmt_ars(val):
    try:
        n = float(val)
        s = f"{n:,.2f}"
        # Formato es-AR: miles con punto y decimales con coma
        return "$" + s.replace(",", "X").replace(".", ",").replace("X", ".")
    except:
        return "—" if val in (None, "") else str(val)

def render_json_as_html(data):
    if isinstance(data, dict):
        rows = []
        for k, v in data.items():
            rows.append(
                f"<tr>"
                f"<th scope='row' class='table-dark'>{k}</th>"
                f"<td>{render_json_as_html(v)}</td>"
                f"</tr>"
            )
        return "<table class='table table-dark table-bordered table-sm table-hover'>" + "".join(rows) + "</table>"

    elif isinstance(data, list):
        rows = []
        for i, item in enumerate(data):
            rows.append(
                f"<tr>"
                f"<th scope='row' class='table-dark'>[{i}]</th>"
                f"<td>{render_json_as_html(item)}</td>"
                f"</tr>"
            )
        return "<table class='table table-dark table-bordered table-sm table-hover'>" + "".join(rows) + "</table>"

    else:
        return f"<span class='text-light'>{str(data)}</span>"

def _extract_cancel_detail(order_data: dict):
    """Devuelve (motivo, solicitado_por) de una orden cancelada.

    ML expone el detalle de cancelación en `cancel_detail` (formato nuevo) o,
    en respuestas viejas, en `status_detail`. Soportamos ambos defensivamente.
    """
    detail = order_data.get("cancel_detail") or order_data.get("status_detail") or {}
    if isinstance(detail, dict):
        description = detail.get("description") or detail.get("code")
        requested_by = detail.get("requested_by") or detail.get("group")
        return description, requested_by
    return (str(detail) if detail else None), None


def _store_cancelled_order(order_data: dict):
    """Upsert de una orden cancelada en ml_cancelled_orders.

    Tabla dedicada en la base mlwebhook que pricing-app consulta cross-DB.
    Idempotente por order_id (un mismo webhook puede llegar varias veces).
    """
    order_id = order_data.get("id")
    if order_id is None:
        return

    status_detail, cancelled_by = _extract_cancel_detail(order_data)
    buyer = order_data.get("buyer") or {}
    seller = order_data.get("seller") or {}

    items = []
    for oi in order_data.get("order_items") or []:
        item = oi.get("item") or {}
        items.append({
            "item_id": item.get("id"),
            "seller_sku": item.get("seller_sku") or item.get("seller_custom_field"),
            "title": item.get("title"),
            "quantity": oi.get("quantity"),
            "unit_price": oi.get("unit_price"),
        })

    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO ml_cancelled_orders (
                order_id, pack_id, status, status_detail, cancelled_by,
                date_created, date_closed, total_amount, currency_id,
                buyer_id, buyer_nickname, seller_id, items, payload, updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (order_id) DO UPDATE SET
                pack_id = EXCLUDED.pack_id,
                status = EXCLUDED.status,
                status_detail = EXCLUDED.status_detail,
                cancelled_by = EXCLUDED.cancelled_by,
                date_created = EXCLUDED.date_created,
                date_closed = EXCLUDED.date_closed,
                total_amount = EXCLUDED.total_amount,
                currency_id = EXCLUDED.currency_id,
                buyer_id = EXCLUDED.buyer_id,
                buyer_nickname = EXCLUDED.buyer_nickname,
                seller_id = EXCLUDED.seller_id,
                items = EXCLUDED.items,
                payload = EXCLUDED.payload,
                updated_at = NOW();
        """, (
            order_id,
            order_data.get("pack_id"),
            order_data.get("status"),
            status_detail,
            cancelled_by,
            order_data.get("date_created"),
            order_data.get("date_closed"),
            order_data.get("total_amount"),
            order_data.get("currency_id"),
            buyer.get("id"),
            buyer.get("nickname"),
            seller.get("id"),
            Json(items),
            Json(order_data),
        ))


def fetch_and_store_preview(resource: str):
    try:
        token = get_token()
        headers = {"Authorization": f"Bearer {token}"}

        preview = {"resource": resource}
        extra_data = {}

        # ----- SHIPMENTS -----
        if resource.startswith("/shipments/"):
            res_ship = ml_api_get(f"https://api.mercadolibre.com{resource}", headers=headers)
            ship_data = res_ship.json()

            # item principal del envío
            ship_items = ship_data.get("shipping_items") or []
            first_item = ship_items[0] if ship_items else {}
            item_desc = first_item.get("description", "")
            item_id = first_item.get("id", "")

            # destino
            recv = ship_data.get("receiver_address") or {}
            dest_city = recv.get("city", {}).get("name", "")
            dest_state = recv.get("state", {}).get("name", "")

            # shipping option
            ship_opt = ship_data.get("shipping_option") or {}
            eta = (ship_opt.get("estimated_delivery_time") or {}).get("date")

            preview.update({
                "title": item_desc,
                "status": ship_data.get("status"),
            })

            # shipping_method_id: identifica método de envío (ej: "515282" = Turbo)
            raw_method_id = ship_opt.get("shipping_method_id") if isinstance(ship_opt, dict) else None
            shipping_method_id = str(raw_method_id) if raw_method_id is not None else None

            # tags: lista de etiquetas del shipment (ej: ["turbo"])
            raw_tags = ship_data.get("tags")
            ship_tags = raw_tags if isinstance(raw_tags, list) else []

            # status_history: fechas reales de envío/entrega (para detectar demoras en turbos)
            status_history = ship_data.get("status_history") or {}

            extra_data = {
                "substatus": ship_data.get("substatus"),
                "item_id": item_id,
                "logistic_type": ship_data.get("logistic_type"),
                "shipping_method": ship_opt.get("name"),
                "shipping_method_id": shipping_method_id,
                "tags": ship_tags,
                "destination_city": dest_city,
                "destination_state": dest_state,
                "destination_lat": recv.get("latitude"),
                "destination_lng": recv.get("longitude"),
                "estimated_delivery": eta,
                "order_id": ship_data.get("order_id"),
                "tracking_number": ship_data.get("tracking_number"),
                "receiver_name": recv.get("receiver_name"),
                "date_delivered": status_history.get("date_delivered"),
                "date_shipped": status_history.get("date_shipped"),
            }

        # ----- ITEMS / PRICE_TO_WIN -----
        elif resource.endswith("/price_to_win"):
            item_id = resource.split("/")[2]

            # consulta 1: datos básicos del item (trae catalog_product_id)
            res_item = ml_api_get(f"https://api.mercadolibre.com/items/{item_id}", headers=headers)
            item_data = res_item.json()

            catalog_product_id = item_data.get("catalog_product_id")
            brand_name = next(
                (a.get("value_name") for a in item_data.get("attributes", []) if a.get("id") == "BRAND"),
                ""
            )
            shipping = item_data.get("shipping") or {}

            preview.update({
                "title": item_data.get("title", ""),
                "thumbnail": item_data.get("thumbnail", ""),
                "currency_id": item_data.get("currency_id", ""),
                "permalink": item_data.get("permalink", ""),
                "catalog_product_id": catalog_product_id,
                "brand": brand_name,
            })

            # sale_terms: extraer ALL_METHODS_REBATE_PRICE
            rebate_term = next(
                (t for t in item_data.get("sale_terms") or [] if t.get("id") == "ALL_METHODS_REBATE_PRICE"),
                {}
            )
            rebate_value_name = rebate_term.get("value_name")
            rebate_value_struct_number = (rebate_term.get("value_struct") or {}).get("number")
            # values es un array, tomar el primero si existe
            rebate_values_first = (rebate_term.get("values") or [{}])[0] if rebate_term.get("values") else {}
            rebate_values_name = rebate_values_first.get("name")
            rebate_values_struct_number = (rebate_values_first.get("struct") or {}).get("number")

            # precio de rebate: preferir value_struct.number, fallback a values[0].struct.number
            rebate_price = rebate_value_struct_number or rebate_values_struct_number
            free_shipping = shipping.get("free_shipping")
            free_shipping_error = False
            if free_shipping and rebate_price is not None:
                try:
                    free_shipping_error = float(rebate_price) < FREE_SHIPPING_MIN_PRICE
                except (ValueError, TypeError):
                    pass

            extra_data = {
                "logistic_type": shipping.get("logistic_type"),
                "free_shipping": free_shipping,
                "shipping_mode": shipping.get("mode"),
                "shipping_tags": shipping.get("tags") or [],
                "rebate_value_name": rebate_value_name,
                "rebate_value_struct_number": rebate_value_struct_number,
                "rebate_values_name": rebate_values_name,
                "rebate_values_struct_number": rebate_values_struct_number,
                "free_shipping_error": free_shipping_error,
            }

            # opportunistic: refresh seller shipping cost for this MLA (best-effort)
            _upsert_seller_shipping_cost(item_id, item_data, source="webhook")

            # consulta 2: price_to_win
            res_ptw = ml_api_get(f"https://api.mercadolibre.com/items/{item_id}/price_to_win?version=v2", headers=headers)
            ptw_data = res_ptw.json()

            winner_id = (ptw_data.get("winner") or {}).get("item_id")
            winner_price = (ptw_data.get("winner") or {}).get("price")
            current_price = ptw_data.get("current_price")

            preview.update({
                "price": current_price,
                "status": ptw_data.get("status"),
                "winner": winner_id,
                "winner_price": winner_price,
            })

            # campos de preview listos para el frontend
            if catalog_product_id and winner_id:
                winner_url = f"https://www.mercadolibre.com.ar/p/{catalog_product_id}?pdp_filters=item_id:{winner_id}"
            else:
                winner_url = None

            preview["winner_url"] = winner_url
            preview["winner_price_fmt"] = _fmt_ars(winner_price)
            if winner_url:
                preview["winner_line_html"] = (
                    f'🏆 Ganador: <a href="{winner_url}" target="_blank" rel="noopener noreferrer">{winner_id}</a>'
                    f' — {_fmt_ars(winner_price)}'
                )
            else:
                preview["winner_line_html"] = (
                    f'🏆 Ganador: {winner_id or "—"}'
                    f' — {_fmt_ars(winner_price)}'
                )

        # ----- ITEMS COMUNES -----
        elif resource.startswith("/items/"):
            res_item = ml_api_get(f"https://api.mercadolibre.com{resource}", headers=headers)
            item_data = res_item.json()

            brand_name = next(
                (a.get("value_name") for a in item_data.get("attributes", []) if a.get("id") == "BRAND"),
                ""
            )
            shipping = item_data.get("shipping") or {}

            preview.update({
                "title": item_data.get("title", ""),
                "thumbnail": item_data.get("thumbnail", ""),
                "currency_id": item_data.get("currency_id", ""),
                "price": item_data.get("price"),
                "permalink": item_data.get("permalink", ""),
                "catalog_product_id": item_data.get("catalog_product_id"),
                "brand": brand_name,
            })

            # sale_terms: extraer ALL_METHODS_REBATE_PRICE
            rebate_term = next(
                (t for t in item_data.get("sale_terms") or [] if t.get("id") == "ALL_METHODS_REBATE_PRICE"),
                {}
            )
            rebate_value_name = rebate_term.get("value_name")
            rebate_value_struct_number = (rebate_term.get("value_struct") or {}).get("number")
            rebate_values_first = (rebate_term.get("values") or [{}])[0] if rebate_term.get("values") else {}
            rebate_values_name = rebate_values_first.get("name")
            rebate_values_struct_number = (rebate_values_first.get("struct") or {}).get("number")

            rebate_price = rebate_value_struct_number or rebate_values_struct_number
            free_shipping = shipping.get("free_shipping")
            free_shipping_error = False
            if free_shipping and rebate_price is not None:
                try:
                    free_shipping_error = float(rebate_price) < FREE_SHIPPING_MIN_PRICE
                except (ValueError, TypeError):
                    pass

            extra_data = {
                "logistic_type": shipping.get("logistic_type"),
                "free_shipping": free_shipping,
                "shipping_mode": shipping.get("mode"),
                "shipping_tags": shipping.get("tags") or [],
                "rebate_value_name": rebate_value_name,
                "rebate_value_struct_number": rebate_value_struct_number,
                "rebate_values_name": rebate_values_name,
                "rebate_values_struct_number": rebate_values_struct_number,
                "free_shipping_error": free_shipping_error,
            }

            # opportunistic: refresh seller shipping cost for this MLA (best-effort)
            _mla_for_cost = item_data.get("id") or resource.split("/")[-1]
            _upsert_seller_shipping_cost(_mla_for_cost, item_data, source="webhook")

        # ----- CLAIMS (post-purchase) -----
        elif resource.startswith("/post-purchase/v1/claims/"):
            # Extraer claim_id del resource
            # resource viene como: /post-purchase/v1/claims/5281510459
            claim_id = resource.rstrip("/").split("/")[-1]

            # 1) GET claim principal
            res_claim = ml_api_get(
                f"https://api.mercadolibre.com/post-purchase/v1/claims/{claim_id}",
                headers=headers,
            )
            claim_data = res_claim.json() if res_claim.status_code == 200 else {}

            # 2) GET detail (problema legible, título, responsable, due_date)
            claim_detail = {}
            try:
                res_detail = ml_api_get(
                    f"https://api.mercadolibre.com/post-purchase/v1/claims/{claim_id}/detail",
                    headers=headers,
                )
                if res_detail.status_code == 200:
                    claim_detail = res_detail.json()
            except Exception:
                pass

            # 3) GET reason detail (texto legible del motivo exacto)
            reason_id = claim_data.get("reason_id", "")
            reason_data = {}
            try:
                if reason_id:
                    res_reason = ml_api_get(
                        f"https://api.mercadolibre.com/post-purchase/v1/claims/reasons/{reason_id}",
                        headers=headers,
                    )
                    if res_reason.status_code == 200:
                        reason_data = res_reason.json()
            except Exception:
                pass

            # reason_id → categoría legible (PNR / PDD / CS) como fallback
            if reason_id.startswith("PNR"):
                reason_category = "Producto No Recibido"
            elif reason_id.startswith("PDD"):
                reason_category = "Producto Diferente o Defectuoso"
            elif reason_id.startswith("CS"):
                reason_category = "Compra Cancelada"
            else:
                reason_category = reason_id

            # reason_data.detail es el texto EXACTO: "El producto llegó roto o con piezas dañadas"
            reason_detail = reason_data.get("detail") or ""
            reason_name = reason_data.get("name") or ""
            reason_label = reason_detail or reason_category

            # Resoluciones esperadas y triage del engine de ML
            reason_settings = reason_data.get("settings") or {}
            expected_resolutions = reason_settings.get("expected_resolutions") or []
            triage_tags = reason_settings.get("rules_engine_triage") or []

            # Título para preview: reason_detail > detail.problem > reason_category
            detail_problem = claim_detail.get("problem") or ""
            preview_title = reason_detail or detail_problem or reason_category or f"Reclamo #{claim_id}"

            # Players: extraer buyer y seller info
            players = claim_data.get("players") or []
            complainant = next((p for p in players if p.get("role") == "complainant"), {})
            respondent = next((p for p in players if p.get("role") == "respondent"), {})

            # Acciones pendientes del seller (respondent)
            seller_actions = respondent.get("available_actions") or []
            mandatory_actions = [a for a in seller_actions if a.get("mandatory")]
            nearest_due_date = None
            for a in seller_actions:
                dd = a.get("due_date")
                if dd and (nearest_due_date is None or dd < nearest_due_date):
                    nearest_due_date = dd

            # Resolution (si cerrado)
            resolution = claim_data.get("resolution") or {}

            preview.update({
                "title": preview_title,
                "status": claim_data.get("status"),
            })

            extra_data = {
                "claim_id": claim_data.get("id"),
                "claim_type": claim_data.get("type"),
                "claim_stage": claim_data.get("stage"),
                "claim_version": claim_data.get("claim_version"),
                "resource_type": claim_data.get("resource"),
                "resource_id": claim_data.get("resource_id"),
                "reason_id": reason_id,
                "reason_category": reason_category,
                "reason_label": reason_label,
                "reason_name": reason_name,
                "reason_detail": reason_detail,
                "expected_resolutions": expected_resolutions,
                "triage_tags": triage_tags,
                "fulfilled": claim_data.get("fulfilled"),
                "quantity_type": claim_data.get("quantity_type"),
                "claimed_quantity": claim_data.get("claimed_quantity"),
                # Players
                "complainant_user_id": complainant.get("user_id"),
                "complainant_type": complainant.get("type"),
                "respondent_user_id": respondent.get("user_id"),
                "respondent_type": respondent.get("type"),
                # Acciones del seller
                "seller_actions": [a.get("action") for a in seller_actions],
                "mandatory_actions": [a.get("action") for a in mandatory_actions],
                "nearest_due_date": nearest_due_date,
                # Detail legible
                "detail_title": claim_detail.get("title"),
                "detail_description": claim_detail.get("description"),
                "detail_problem": detail_problem,
                "action_responsible": claim_detail.get("action_responsible"),
                "detail_due_date": claim_detail.get("due_date"),
                # Resolución (si existe)
                "resolution_reason": resolution.get("reason"),
                "resolution_date": resolution.get("date_created"),
                "resolution_benefited": resolution.get("benefited"),
                "resolution_closed_by": resolution.get("closed_by"),
                "resolution_coverage": resolution.get("applied_coverage"),
                # Fechas
                "date_created": claim_data.get("date_created"),
                "last_updated": claim_data.get("last_updated"),
                "site_id": claim_data.get("site_id"),
            }

        # ----- ORDERS -----
        elif resource.startswith("/orders/"):
            res_order = ml_api_get(f"https://api.mercadolibre.com{resource}", headers=headers)
            order_data = res_order.json()

            order_id = order_data.get("id")
            order_status = order_data.get("status")
            order_items = order_data.get("order_items") or []
            first_item = (order_items[0].get("item") if order_items else {}) or {}
            item_title = first_item.get("title") or ""

            preview.update({
                "title": item_title or f"Orden #{order_id}",
                "status": order_status,
            })

            extra_data = {
                "order_id": order_id,
                "pack_id": order_data.get("pack_id"),
                "total_amount": order_data.get("total_amount"),
                "currency_id": order_data.get("currency_id"),
                "date_created": order_data.get("date_created"),
                "date_closed": order_data.get("date_closed"),
            }

            # Persistir cancelaciones en tabla dedicada para que pricing-app
            # las consulte cross-DB. Best-effort: no romper el preview si falla.
            if order_status == "cancelled":
                try:
                    _store_cancelled_order(order_data)
                except Exception as e:
                    print(f"⚠️ No se pudo persistir cancelación de {resource}:", e)

        # ----- CUALQUIER OTRO TOPIC (no romper) -----
        else:
            try:
                res_generic = ml_api_get(f"https://api.mercadolibre.com{resource}", headers=headers)
                generic_data = res_generic.json()
                preview["title"] = generic_data.get("title") or generic_data.get("name") or ""
                preview["status"] = generic_data.get("status")
            except Exception:
                pass

        # --- Persistir preview + extra_data ---
        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO ml_previews (resource, title, price, currency_id, thumbnail, winner, winner_price, status, brand, extra_data, last_updated)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (resource) DO UPDATE SET
                    title = EXCLUDED.title,
                    price = EXCLUDED.price,
                    currency_id = EXCLUDED.currency_id,
                    thumbnail = EXCLUDED.thumbnail,
                    winner = EXCLUDED.winner,
                    winner_price = EXCLUDED.winner_price,
                    status = EXCLUDED.status,
                    brand = EXCLUDED.brand,
                    extra_data = EXCLUDED.extra_data,
                    last_updated = NOW();
            """, (
                preview["resource"],
                preview.get("title"),
                preview.get("price"),
                preview.get("currency_id"),
                preview.get("thumbnail"),
                preview.get("winner"),
                preview.get("winner_price"),
                preview.get("status"),
                preview.get("brand"),
                Json(extra_data),
            ))
            

        # ── SSE notifications (best-effort, per resource type) ──
        if resource.startswith("/shipments/"):
            sse_notify("shipments:webhook", {
                "resource": resource,
                "status": preview.get("status"),
            })
        elif resource.startswith("/items/"):
            # Notify free-shipping channel when items change
            # (FreeShippingBadge will re-fetch the count)
            if extra_data.get("free_shipping_error") is not None:
                sse_notify("free-shipping:count", {
                    "resource": resource,
                    "free_shipping_error": extra_data.get("free_shipping_error"),
                })
        elif resource.startswith("/post-purchase/v1/claims/"):
            sse_notify("claims:updated", {
                "resource": resource,
                "status": preview.get("status"),
                "claim_id": extra_data.get("claim_id"),
            })

        print("🔍 Preview generado:", preview)
        return preview

    except Exception as e:
        print(f"❌ Error obteniendo preview de {resource}:", e)
        return {"resource": resource, "title": "Error"}


    
def render_ml_view(resource, data):
    html_parts = []

    # -------------------------------
    # Caso: /price_to_win
    # -------------------------------
    if "/price_to_win" in resource:
        item_id = data.get("item_id")
        catalog_product_id = data.get("catalog_product_id")
        
        if not catalog_product_id:
            # Aviso en el HTML
            html_parts.append(
                "<div class='alert alert-secondary' role='alert'>"
                "<h4>📦 El MLA no es una publicación de catálogo</h4>"
                "<h5>Mostrando datos del Producto</h5>"
                "</div>"
            )

            try:
                token = get_token()
                res_item = ml_api_get(
                    f"https://api.mercadolibre.com/items/{item_id}",
                    headers={"Authorization": f"Bearer {token}"}
                )
                item_data = res_item.json()

                permalink = item_data.get("permalink")
                if item_id and permalink:
                    html_parts.append(make_item_card(item_id, permalink, item_data))

                # 👇 Agregar también el JSON del item
                html_parts.append(render_json_as_html(item_data))

            except Exception as e:
                html_parts.append(
                    f"<div class='alert alert-danger'>❌ Error al cargar datos del item: {e}</div>"
                )

            # 👈 ahora SÍ cortamos acá, porque ya mostramos card + json
            return "".join(html_parts)
        winner = data.get("winner", {}) or {}
        winner_id = winner.get("item_id")
        current_price = data.get("current_price")
        winner_price = winner.get("price")
        status = data.get("status")
        competitors_sharing = data.get("competitors_sharing_first_place", 0)
        competitors_label = "Competidor" if competitors_sharing == 1 else "Competidores"

        # Card de producto (similar a /items común)
        if item_id and catalog_product_id:
            ml_url = f"https://www.mercadolibre.com.ar/p/{catalog_product_id}?pdp_filters=item_id:{item_id}"
            html_parts.append(make_item_card(item_id, ml_url))

        # Alerts
        if item_id == winner_id:
            html_parts.append("<div class='alert alert-success' role='alert'>🎉 Estás Ganando el Catálogo!</div>")

        if current_price and winner_price and current_price > winner_price:
            diff = current_price - winner_price
            html_parts.append(f"<div class='alert alert-danger' role='alert'>🚫 Estás perdiendo el catálogo por ${diff}</div>")

        if status == "sharing_first_place":
            html_parts.append(f"<div class='alert alert-warning' role='alert'>⚠️ Estás compartiendo el primer lugar con {competitors_sharing} {competitors_label}.</div>")

        def _fmt_money(val):
            try:
                # acepta str o número y lo muestra sin decimales
                return f"{data.get('currency_id','') } {int(round(float(val))):,}".replace(",", ".")
            except Exception:
                return val if val is not None else "—"

        def _render_boosts_list(boost_list):
            if not boost_list:
                return "<em>Sin boosts</em>"
            lis = []
            for b in boost_list:
                st = (b or {}).get("status")
                icon = "🟢" if st == "boosted" else ("⚪" if st in ("opportunity", None) else "🟠")
                desc = (b or {}).get("description") or (b or {}).get("id") or "—"
                lis.append(f"<li class='mb-1'>{icon} {desc} <small class='text-muted'>({st or '—'})</small></li>")
            return "<ul class='mb-0 ps-3'>" + "".join(lis) + "</ul>"

        # Datos del propio item
        price_to_win_val = data.get("price_to_win")
        boosts_self = data.get("boosts", [])
        visit_share = data.get("visit_share") or "—"
        consistent = data.get("consistent")
        comp_share = data.get("competitors_sharing_first_place")
        comp_share_txt = "—" if comp_share in (None, "", []) else comp_share

        # Datos del ganador
        winner_boosts = winner.get("boosts", [])

        html_parts.append(f"""
        <div class="row g-3 mt-2">
          <!-- Tu publicación -->
          <div class="col-md-6">
            <div class="card bg-dark text-light border-info h-100">
              <div class="card-header">📦 Tu publicación</div>
              <div class="card-body">
                <div class="d-flex justify-content-between flex-wrap">
                  <div><strong>Item ID:</strong> {f'<a href="https://www.mercadolibre.com.ar/p/{catalog_product_id}?pdp_filters=item_id:{item_id}" target="_blank" rel="noopener noreferrer">{item_id}</a>' if (catalog_product_id and item_id) else (item_id or "—")}</div>
                  <div><strong>Estado:</strong> {status or "—"}</div>
                </div>
                <div class="mt-2">
                  <div><strong>Precio actual:</strong> {_fmt_money(current_price)}</div>
                  <div><strong>Price to win:</strong> {_fmt_money(price_to_win_val)}</div>
                </div>
                <div class="mt-2 d-flex justify-content-between flex-wrap">
                  <div><strong>Consistente:</strong> {"✅ Sí" if consistent else "❌ No"}</div>
                  <div><strong>Visit share:</strong> {visit_share}</div>
                  <div><strong>Competidores en 1º lugar:</strong> {comp_share_txt}</div>
                </div>
                <hr>
                <h6 class="mb-2">Boosts</h6>
                {_render_boosts_list(boosts_self)}
              </div>
            </div>
          </div>

          <!-- Ganador -->
          <div class="col-md-6">
            <div class="card bg-dark text-light border-success h-100">
              <div class="card-header">🏆 Ganador</div>
              <div class="card-body">
                <div class="d-flex justify-content-between flex-wrap">
                  <div><strong>Item ID:</strong> {f'<a href="https://www.mercadolibre.com.ar/p/{catalog_product_id}?pdp_filters=item_id:{winner_id}" target="_blank" rel="noopener noreferrer">{winner_id}</a>' if (catalog_product_id and winner_id) else (winner_id or "—")}</div>
                  <div><strong>Precio:</strong> {_fmt_money(winner_price)}</div>
                </div>
                <hr>
                <h6 class="mb-2">Boosts</h6>
                {_render_boosts_list(winner_boosts)}
              </div>
            </div>
          </div>
        </div>
        """)    

        return "".join(html_parts)


    # -------------------------------
    # Caso: /items común
    # -------------------------------
    elif resource.startswith("/items/MLA"):
        item_id = data.get("id")
        permalink = data.get("permalink")
        catalog_product_id = data.get("catalog_product_id")
        ml_url = None
        if item_id and catalog_product_id:
            ml_url = f"https://www.mercadolibre.com.ar/p/{catalog_product_id}?pdp_filters=item_id:{item_id}"
        elif item_id:
            ml_url = permalink

        if item_id and ml_url:
            html_parts.append(make_item_card(item_id, ml_url, data))

    # -------------------------------
    # Caso: /post-purchase/v1/claims/
    # -------------------------------
    elif resource.startswith("/post-purchase/v1/claims/"):
        claim_id = resource.rstrip("/").split("/")[-1]

        try:
            token = get_token()
            headers = {"Authorization": f"Bearer {token}"}

            # GET claim principal
            res_claim = ml_api_get(
                f"https://api.mercadolibre.com/post-purchase/v1/claims/{claim_id}",
                headers=headers,
            )
            claim = res_claim.json() if res_claim.status_code == 200 else data

            # GET detail
            claim_detail = {}
            try:
                res_detail = ml_api_get(
                    f"https://api.mercadolibre.com/post-purchase/v1/claims/{claim_id}/detail",
                    headers=headers,
                )
                if res_detail.status_code == 200:
                    claim_detail = res_detail.json()
            except Exception:
                pass

            # Parsear datos clave
            status = claim.get("status", "—")
            stage = claim.get("stage", "—")
            claim_type = claim.get("type", "—")
            reason_id = claim.get("reason_id", "")
            resource_type = claim.get("resource", "—")
            resource_id = claim.get("resource_id", "—")
            fulfilled = claim.get("fulfilled")
            quantity_type = claim.get("quantity_type", "—")
            claimed_qty = claim.get("claimed_quantity", "—")

            # GET reason detail (motivo exacto legible)
            reason_data = {}
            try:
                if reason_id:
                    res_reason = ml_api_get(
                        f"https://api.mercadolibre.com/post-purchase/v1/claims/reasons/{reason_id}",
                        headers=headers,
                    )
                    if res_reason.status_code == 200:
                        reason_data = res_reason.json()
            except Exception:
                pass

            reason_detail_text = reason_data.get("detail") or ""
            reason_name = reason_data.get("name") or ""
            reason_settings = reason_data.get("settings") or {}
            expected_resolutions = reason_settings.get("expected_resolutions") or []
            triage_tags = reason_settings.get("rules_engine_triage") or []

            # Reason category (fallback genérico) + icon
            if reason_id.startswith("PNR"):
                reason_category = "Producto No Recibido"
                reason_icon = "📦❌"
            elif reason_id.startswith("PDD"):
                reason_category = "Producto Diferente o Defectuoso"
                reason_icon = "🔧"
            elif reason_id.startswith("CS"):
                reason_category = "Compra Cancelada"
                reason_icon = "🚫"
            else:
                reason_category = reason_id
                reason_icon = "📋"

            # reason_detail_text es el texto EXACTO del comprador, mucho más útil
            reason_label = reason_detail_text or reason_category

            # Status badge
            if status == "opened":
                status_badge = "<span class='badge bg-warning text-dark'>⏳ Abierto</span>"
            elif status == "closed":
                status_badge = "<span class='badge bg-secondary'>✅ Cerrado</span>"
            else:
                status_badge = f"<span class='badge bg-info'>{status}</span>"

            # Stage badge
            stage_colors = {
                "claim": ("bg-primary", "Reclamo"),
                "dispute": ("bg-danger", "Disputa / Mediación"),
                "recontact": ("bg-warning text-dark", "Recontacto"),
                "stale": ("bg-secondary", "Estancado"),
                "none": ("bg-dark", "N/A"),
            }
            sc, sl = stage_colors.get(stage, ("bg-info", stage))
            stage_badge = f"<span class='badge {sc}'>{sl}</span>"

            # Type badge
            type_labels = {
                "mediations": "Mediación",
                "return": "Devolución",
                "fulfillment": "Fulfillment",
                "ml_case": "Caso ML",
                "cancel_sale": "Cancelación (vendedor)",
                "cancel_purchase": "Cancelación (comprador)",
                "change": "Cambio",
                "service": "Servicio",
            }
            type_label = type_labels.get(claim_type, claim_type)

            # Players
            players = claim.get("players") or []
            complainant = next((p for p in players if p.get("role") == "complainant"), {})
            respondent = next((p for p in players if p.get("role") == "respondent"), {})

            # Acciones del seller
            seller_actions = respondent.get("available_actions") or []
            actions_html = ""
            if seller_actions:
                action_items = []
                for a in seller_actions:
                    action_name = a.get("action", "—")
                    mandatory = a.get("mandatory", False)
                    due = a.get("due_date", "")
                    icon = "🔴" if mandatory else "🟡"
                    due_str = f" <small class='text-muted'>vence: {due}</small>" if due else ""
                    action_items.append(f"<li class='mb-1'>{icon} <code>{action_name}</code>{' <span class=\"badge bg-danger\">obligatoria</span>' if mandatory else ''}{due_str}</li>")
                actions_html = f"<ul class='mb-0 ps-3'>{''.join(action_items)}</ul>"
            else:
                actions_html = "<em class='text-muted'>Sin acciones pendientes</em>"

            # Detail card
            detail_problem = claim_detail.get("problem", "")
            detail_title = claim_detail.get("title", "")
            detail_desc = claim_detail.get("description", "")
            action_responsible = claim_detail.get("action_responsible", "")
            detail_due = claim_detail.get("due_date", "")

            responsible_labels = {
                "seller": "🏪 Vendedor",
                "buyer": "🛒 Comprador",
                "mediator": "⚖️ Mercado Libre",
            }
            responsible_str = responsible_labels.get(action_responsible, action_responsible)

            # Resolution
            resolution = claim.get("resolution") or {}
            resolution_html = ""
            if resolution:
                res_reason = resolution.get("reason", "—")
                res_date = resolution.get("date_created", "—")
                res_benefited = ", ".join(resolution.get("benefited") or ["—"])
                res_closed_by = resolution.get("closed_by", "—")
                res_coverage = resolution.get("applied_coverage")
                coverage_str = "✅ Sí" if res_coverage else "❌ No" if res_coverage is not None else "—"
                resolution_html = f"""
                <div class="card bg-dark text-light border-success mt-3">
                  <div class="card-header">📋 Resolución</div>
                  <div class="card-body">
                    <div class="row">
                      <div class="col-md-6"><strong>Motivo:</strong> <code>{res_reason}</code></div>
                      <div class="col-md-6"><strong>Fecha:</strong> {res_date}</div>
                    </div>
                    <div class="row mt-2">
                      <div class="col-md-4"><strong>Beneficiado:</strong> {res_benefited}</div>
                      <div class="col-md-4"><strong>Cerrado por:</strong> {res_closed_by}</div>
                      <div class="col-md-4"><strong>Cobertura ML:</strong> {coverage_str}</div>
                    </div>
                  </div>
                </div>
                """

            # Order link (si el resource es una orden)
            resource_link = ""
            if resource_type == "order" and resource_id:
                resource_link = f"<a href='/api/ml/render?resource=/orders/{resource_id}' target='_blank' class='text-info'>Ver orden #{resource_id}</a>"
            elif resource_type == "shipment" and resource_id:
                resource_link = f"<a href='/api/ml/render?resource=/shipments/{resource_id}' target='_blank' class='text-info'>Ver envío #{resource_id}</a>"
            else:
                resource_link = f"<code>{resource_type}: {resource_id}</code>"

            html_parts.append(f"""
            <div class="mb-3">
              <h3>{reason_icon} Reclamo #{claim.get('id', claim_id)}</h3>
              <div class="d-flex gap-2 flex-wrap mb-3">
                {status_badge} {stage_badge}
                <span class="badge bg-info">{type_label}</span>
              </div>
            </div>

            {'<div class="alert alert-danger border-0"><h5 class="mb-1">' + reason_icon + ' ' + reason_detail_text + '</h5><small class="text-muted">' + reason_category + ' — ' + reason_id + '</small></div>' if reason_detail_text else ('<div class="alert alert-warning"><strong>' + reason_icon + ' ' + reason_category + '</strong> <small class="text-muted">(' + reason_id + ')</small></div>')}

            {'<div class="alert alert-secondary border-0 mt-0"><em>' + detail_problem + '</em></div>' if detail_problem and detail_problem != reason_detail_text else ''}

            <div class="row g-3">
              <!-- Info principal -->
              <div class="col-md-6">
                <div class="card bg-dark text-light border-info h-100">
                  <div class="card-header">📄 Datos del reclamo</div>
                  <div class="card-body">
                    <div><strong>Motivo:</strong> {reason_icon} {reason_label} <small class="text-muted">({reason_id})</small></div>
                    {'<div class="mt-1"><strong>Nombre interno:</strong> <code>' + reason_name + '</code></div>' if reason_name else ''}
                    <div class="mt-1"><strong>Recurso:</strong> {resource_link}</div>
                    <div class="mt-1"><strong>Entregado:</strong> {'✅ Sí' if fulfilled else '❌ No' if fulfilled is not None else '—'}</div>
                    <div class="mt-1"><strong>Cantidad reclamada:</strong> {claimed_qty} ({quantity_type})</div>
                    <div class="mt-1"><strong>Versión claim:</strong> {claim.get('claim_version', '—')}</div>
                    {'<hr><h6 class="mt-2">🏷️ Clasificación RMA</h6><div><strong>Triage:</strong> ' + ', '.join(f'<span class="badge bg-secondary">{t}</span>' for t in triage_tags) + '</div>' if triage_tags else ''}
                    {'<div class="mt-1"><strong>Resoluciones esperadas:</strong> ' + ', '.join(f'<span class="badge bg-outline-info border border-info">{r}</span>' for r in expected_resolutions) + '</div>' if expected_resolutions else ''}
                    <hr>
                    <div><strong>Creado:</strong> {claim.get('date_created', '—')}</div>
                    <div><strong>Última actualización:</strong> {claim.get('last_updated', '—')}</div>
                  </div>
                </div>
              </div>

              <!-- Responsable y acciones -->
              <div class="col-md-6">
                <div class="card bg-dark text-light border-warning h-100">
                  <div class="card-header">⚡ Acciones pendientes</div>
                  <div class="card-body">
                    {'<div class="mb-2"><strong>Responsable actual:</strong> ' + responsible_str + '</div>' if action_responsible else ''}
                    {'<div class="mb-2"><strong>Estado:</strong> ' + detail_title + '</div>' if detail_title else ''}
                    {'<div class="mb-2"><em>' + detail_desc + '</em></div>' if detail_desc else ''}
                    {('<div class="mb-3"><strong>Fecha límite:</strong> <span class="text-warning">' + detail_due + '</span></div>') if detail_due else ''}
                    <hr>
                    <h6>Acciones del vendedor:</h6>
                    {actions_html}
                  </div>
                </div>
              </div>
            </div>

            <!-- Players -->
            <div class="row g-3 mt-1">
              <div class="col-md-6">
                <div class="card bg-dark text-light border-secondary">
                  <div class="card-body py-2">
                    <strong>🛒 Reclamante:</strong> {complainant.get('type', '—')} — User ID: <code>{complainant.get('user_id', '—')}</code>
                  </div>
                </div>
              </div>
              <div class="col-md-6">
                <div class="card bg-dark text-light border-secondary">
                  <div class="card-body py-2">
                    <strong>🏪 Respondente:</strong> {respondent.get('type', '—')} — User ID: <code>{respondent.get('user_id', '—')}</code>
                  </div>
                </div>
              </div>
            </div>

            {resolution_html}
            """)

        except Exception as e:
            html_parts.append(f"<div class='alert alert-danger'>❌ Error al cargar claim {claim_id}: {e}</div>")

        # Agregar JSON crudo debajo
        html_parts.append("<h5 class='mt-4'>📦 JSON crudo del claim</h5>")
        html_parts.append(render_json_as_html(data))
        return "".join(html_parts)

    elif resource.startswith("/seller-promotions/"):
        token = get_token()
        url = f"https://api.mercadolibre.com{resource}?app_version=v2"
        res = ml_api_get(url, headers={"Authorization": f"Bearer {token}"})

        if res.status_code != 200:
            html_parts.append(
                f"<div class='alert alert-danger'>❌ Error {res.status_code} consultando {resource}: {res.text}</div>"
            )
            return "".join(html_parts)

        offer_data = res.json()

        # 1) Mostrar el JSON de la offer
        html_parts.append(render_json_as_html(offer_data))

        # 2) Si la offer trae item_id, renderizar ABAJO la vista de /items/{id}/price_to_win
        item_id = offer_data.get("item_id")
        if item_id:
            try:
                ptw_res = ml_api_get(
                    f"https://api.mercadolibre.com/items/{item_id}/price_to_win?version=v2",
                    headers={"Authorization": f"Bearer {token}"}
                )
                if ptw_res.status_code == 200:
                    ptw_data = ptw_res.json()
                    html_parts.append("<h4 class='mt-4'>🏁 Catálogo</h4>")
                    # 👉 reusar la misma lógica de tu función, renderizando price_to_win dentro de offer
                    html_parts.append(render_ml_view(f"/items/{item_id}/price_to_win?version=v2", ptw_data))
                else:
                    html_parts.append(
                        f"<div class='alert alert-warning'>⚠️ No se pudo cargar price_to_win de {item_id}: {ptw_res.text}</div>"
                    )
            except Exception as e:
                html_parts.append(
                    f"<div class='alert alert-warning'>⚠️ Error al cargar price_to_win de {item_id}: {e}</div>"
                )

        # Importante: cortamos acá para no ejecutar el bloque genérico final
        return "".join(html_parts)

    # -------------------------------
    # Siempre: tabla JSON
    # -------------------------------
    html_parts.append(render_json_as_html(data))
    return "".join(html_parts)


def make_item_card(item_id, ml_url, ml_data=None):
    """Helper para renderizar una card de un item MLA"""
    try:
        if not ml_data:
            token = get_token()
            res = ml_api_get(
                f"https://api.mercadolibre.com/items/{item_id}",
                headers={"Authorization": f"Bearer {token}"}
            )
            ml_data = res.json()
    except Exception:
        ml_data = {}

    title = ml_data.get("title", f"Item {item_id}")
    price = ml_data.get("price", "—")
    currency = ml_data.get("currency_id", "")
    thumbnail = ml_data.get("thumbnail", "")

    return f"""
        <h3>Producto:</h3>
        <a href="{ml_url}" target="_blank" rel="noopener noreferrer" class="text-decoration-none text-reset">
          <div class="card mb-3 bg-dark text-light border-secondary" style="max-width: 540px;">
            <div class="row g-0">
              <div class="col-md-4 d-flex align-items-center justify-content-center p-2">
                <img src="{thumbnail}" alt="{title}" class="img-fluid rounded-start" style="max-height: 100px; object-fit: cover;" />
              </div>
              <div class="col-md-8">
                <div class="card-body">
                  <h5 class="card-title">{title}</h5>
                  <p class="card-text">{currency} {price}</p>
                  <p class="card-text"><small class="text-muted">Click para ver en Mercado Libre</small></p>
                </div>
              </div>
            </div>
          </div>
        </a>
    """

@app.route("/auth")
def auth():
    auth_url = (
        f"https://auth.mercadolibre.com.ar/authorization?"
        f"response_type=code&client_id={ML_CLIENT_ID}&redirect_uri={ML_REDIRECT_URI}"
    )
    return redirect(auth_url)

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "Falta el parámetro 'code'", 400

    token_url = "https://api.mercadolibre.com/oauth/token"
    payload = {
        "grant_type": "authorization_code",
        "client_id": ML_CLIENT_ID,
        "client_secret": ML_CLIENT_SECRET,
        "code": code,
        "redirect_uri": ML_REDIRECT_URI,
    }

    response = requests.post(token_url, data=payload)
    token_data = response.json()
    print("🔑 Token recibido:", token_data)

    if "access_token" in token_data:
        save_token_to_db(token_data)
        # opcional: actualizar cache local
        global ACCESS_TOKEN, EXPIRATION
        ACCESS_TOKEN = token_data["access_token"]
        EXPIRATION = time.time() + int(token_data.get("expires_in", 0)) - 60
        return "Token obtenido y guardado ✅", 200

    return jsonify(token_data), 400

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        evento = request.get_json(silent=True, force=True)
        if not evento:
            return "JSON inválido o vacío", 400

        resource = evento.get("resource", "")
        results = {
            "received_resource": resource,
            "insert_original": None,
            "preview_refreshed": False,
            "errors": [],
        }
        inserted_count = 0

        # Insert + snapshot en una sola conexión (evita presión sobre el pool)
        try:
            with db_cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO webhooks (topic, user_id, resource, payload, webhook_id)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (webhook_id) DO NOTHING
                    """,
                    (
                        evento.get("topic"),
                        evento.get("user_id"),
                        resource,
                        Json(evento),
                        evento.get("_id"),
                    ),
                )
                results["insert_original"] = {
                    "rowcount": cur.rowcount,
                    "webhook_id": evento.get("_id"),
                }
                inserted_count = cur.rowcount

                if inserted_count > 0:
                    with _topics_cache_lock:
                        _topics_cache["value"] = None
                        _topics_cache["expires_at"] = 0.0

                # Snapshot latest por (topic, resource), best-effort
                if inserted_count > 0 and resource:
                    try:
                        cur.execute(
                            """
                            INSERT INTO webhook_latest (topic, resource, webhook_id, received_at, payload)
                            VALUES (%s, %s, %s, NOW(), %s)
                            ON CONFLICT (topic, resource) DO UPDATE SET
                                webhook_id = EXCLUDED.webhook_id,
                                received_at = EXCLUDED.received_at,
                                payload = EXCLUDED.payload
                            """,
                            (
                                evento.get("topic"),
                                resource,
                                evento.get("_id"),
                                Json(evento),
                            ),
                        )
                    except Exception as e:
                        results["errors"].append(f"upsert_webhook_latest: {e}")
        except Exception as e:
            results["errors"].append(f"insert_original: {e}")

        # Refrescar preview del MISMO resource (no rompe el webhook si falla)
        try:
            if resource.startswith("/seller-promotions/"):
                _process_promotion_webhook(resource)
            elif resource:
                if WEBHOOK_PREVIEW_ASYNC:
                    enqueued, enqueue_err = _enqueue_preview_job(resource)
                    if not enqueued:
                        _run_preview_in_background(resource)
                        if enqueue_err:
                            results["errors"].append(f"enqueue_preview_job: {enqueue_err}")
                else:
                    fetch_and_store_preview(resource)
                results["preview_refreshed"] = True
        except Exception as e:
            results["errors"].append(f"fetch_and_store_preview: {e}")

        if DEBUG_WEBHOOK:
            return jsonify(results), 200
        return "Evento recibido", 200

    except Exception as e:
        # último recurso
        if DEBUG_WEBHOOK:
            return jsonify({"ok": False, "fatal": str(e)}), 500
        return "Error interno", 500


@app.route("/api/webhooks", methods=["GET"])
def get_webhooks():
    try:
        topic = request.args.get("topic")
        if not topic:
            return jsonify({"error": "Falta parámetro 'topic'"}), 400

        limit = _clamp_limit(request.args.get("limit"))
        raw_offset = request.args.get("offset", "0")
        try:
            offset = max(0, int(raw_offset))
        except (TypeError, ValueError):
            return jsonify({"error": "Parámetro 'offset' inválido"}), 400

        cursor_raw = request.args.get("cursor")
        cursor_pair = None
        if cursor_raw:
            try:
                cursor_pair = _decode_webhooks_cursor(cursor_raw)
            except Exception:
                return jsonify({"error": "Parámetro 'cursor' inválido"}), 400

        use_cursor_mode = WEBHOOKS_CURSOR_MODE or bool(cursor_pair)

        # Una sola conexión para count + query principal (evita pool exhaustion).
        # Detectamos snapshot table dentro del mismo bloque; si falla, fallback legado.
        with db_cursor() as cur:
            snapshot_available = True
            try:
                cur.execute("SELECT COUNT(*) FROM webhook_latest WHERE topic = %s", (topic,))
                total = cur.fetchone()[0]
            except Exception:
                snapshot_available = False

            if not snapshot_available:
                # Rollback implícito por el error anterior; reconectar en el mismo cursor.
                cur.connection.rollback()
                cur.execute("""
                    WITH latest AS (
                        SELECT resource, MAX(received_at) AS max_received
                        FROM webhooks
                        WHERE topic = %s
                        GROUP BY resource
                    )
                    SELECT COUNT(*) FROM latest
                """, (topic,))
                total = cur.fetchone()[0]

            if snapshot_available:
                if use_cursor_mode:
                    if cursor_pair:
                        cur.execute("""
                            SELECT
                                wl.payload,
                                p.title, p.price, p.currency_id, p.thumbnail, p.winner, p.winner_price, p.status, wl.received_at, p.brand, p.extra_data,
                                wl.resource
                            FROM webhook_latest wl
                            LEFT JOIN ml_previews p ON p.resource = wl.resource
                            WHERE wl.topic = %s
                              AND (wl.received_at, wl.resource) < (%s, %s)
                            ORDER BY wl.received_at DESC, wl.resource DESC
                            LIMIT %s
                        """, (topic, cursor_pair[0], cursor_pair[1], limit))
                    else:
                        cur.execute("""
                            SELECT
                                wl.payload,
                                p.title, p.price, p.currency_id, p.thumbnail, p.winner, p.winner_price, p.status, wl.received_at, p.brand, p.extra_data,
                                wl.resource
                            FROM webhook_latest wl
                            LEFT JOIN ml_previews p ON p.resource = wl.resource
                            WHERE wl.topic = %s
                            ORDER BY wl.received_at DESC, wl.resource DESC
                            LIMIT %s
                        """, (topic, limit))
                else:
                    cur.execute("""
                        SELECT
                            wl.payload,
                            p.title, p.price, p.currency_id, p.thumbnail, p.winner, p.winner_price, p.status, wl.received_at, p.brand, p.extra_data,
                            wl.resource
                        FROM webhook_latest wl
                        LEFT JOIN ml_previews p ON p.resource = wl.resource
                        WHERE wl.topic = %s
                        ORDER BY wl.received_at DESC, wl.resource DESC
                        LIMIT %s OFFSET %s
                    """, (topic, limit, offset))
            else:
                if use_cursor_mode:
                    if cursor_pair:
                        cur.execute("""
                            WITH latest AS (
                                SELECT resource, MAX(received_at) AS max_received
                                FROM webhooks
                                WHERE topic = %s
                                GROUP BY resource
                            )
                            SELECT
                                w.payload,
                                p.title, p.price, p.currency_id, p.thumbnail, p.winner, p.winner_price, p.status, w.received_at, p.brand, p.extra_data,
                                w.resource
                            FROM latest
                            JOIN webhooks w
                              ON w.resource = latest.resource
                             AND w.received_at = latest.max_received
                            LEFT JOIN ml_previews p ON p.resource = w.resource
                            WHERE (w.received_at, w.resource) < (%s, %s)
                            ORDER BY w.received_at DESC, w.resource DESC
                            LIMIT %s
                        """, (topic, cursor_pair[0], cursor_pair[1], limit))
                    else:
                        cur.execute("""
                            WITH latest AS (
                                SELECT resource, MAX(received_at) AS max_received
                                FROM webhooks
                                WHERE topic = %s
                                GROUP BY resource
                            )
                            SELECT
                                w.payload,
                                p.title, p.price, p.currency_id, p.thumbnail, p.winner, p.winner_price, p.status, w.received_at, p.brand, p.extra_data,
                                w.resource
                            FROM latest
                            JOIN webhooks w
                              ON w.resource = latest.resource
                             AND w.received_at = latest.max_received
                            LEFT JOIN ml_previews p ON p.resource = w.resource
                            ORDER BY w.received_at DESC, w.resource DESC
                            LIMIT %s
                        """, (topic, limit))
                else:
                    cur.execute("""
                        WITH latest AS (
                            SELECT resource, MAX(received_at) AS max_received
                            FROM webhooks
                            WHERE topic = %s
                            GROUP BY resource
                        )
                        SELECT
                            w.payload,
                            p.title, p.price, p.currency_id, p.thumbnail, p.winner, p.winner_price, p.status, w.received_at, p.brand, p.extra_data,
                            w.resource
                        FROM latest
                        JOIN webhooks w
                          ON w.resource = latest.resource
                         AND w.received_at = latest.max_received
                        LEFT JOIN ml_previews p ON p.resource = w.resource
                        ORDER BY w.received_at DESC, w.resource DESC
                        LIMIT %s OFFSET %s
                    """, (topic, limit, offset))

            rows_db = cur.fetchall()

        # 3) Construcción de respuesta (ya fuera del with: el cursor está cerrado)
        rows = []
        for row in rows_db:
            payload = row[0]
            # payload puede venir como jsonb (dict) o como string
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {"raw": payload}

            preview = {
                "title": row[1],
                "price": row[2],
                "currency_id": row[3],
                "thumbnail": row[4],
                "winner": row[5],
                "winner_price": row[6],
                "status": row[7],
                "brand": row[9],
                "extra_data": row[10] or {},
            }

            # Adjuntamos preview siempre (si no hay, vendrá con None en sus campos)
            payload["db_preview"] = preview
            local_dt = row[8].astimezone(ZoneInfo("America/Argentina/Buenos_Aires"))
            payload["received_at"] = local_dt.strftime("%Y-%m-%d %H:%M:%S")
            rows.append(payload)

        next_cursor = None
        if use_cursor_mode and rows_db:
            last_received_at = rows_db[-1][8]
            last_resource = rows_db[-1][11]
            next_cursor = _encode_webhooks_cursor(last_received_at, last_resource)

        return jsonify({
            "topic": topic,
            "events": rows,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "total": total,
                "mode": "cursor" if use_cursor_mode else "offset",
                "next_cursor": next_cursor,
            }
        })

    except Exception as e:
        import traceback
        print("❌ Error leyendo DB:", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500





@app.route("/api/ml/render")
def render_meli_resource():
    resource = request.args.get("resource")
    if not resource:
        return "Falta el parámetro 'resource'", 400

    try:
        token = get_token()

        if "/price_to_win" in resource:
            resource += ("&" if "?" in resource else "?") + "version=v2"
        
        res = ml_api_get(
            f"https://api.mercadolibre.com{resource}",
            headers={"Authorization": f"Bearer {token}"}
        )

        # Si la respuesta de ML no es JSON (ej: ZPL texto plano, ZIP binario), devolver crudo
        ml_content_type = res.headers.get("content-type", "")
        if "application/json" not in ml_content_type:
            return res.content, res.status_code, {"Content-Type": ml_content_type}

        data = res.json()

        # Si piden JSON plano, devolver sin renderizar
        if request.args.get("format") == "json":
            return jsonify(data)

        body = render_ml_view(resource, data)

        final_html = f"""
        <html>
          <head>
            <meta charset="utf-8">
            <title>Consultas ML API</title>
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            <link rel="icon" href={FAVICON_DIR}>
            <link rel="apple-touch-icon" href={FAVICON_DIR}>
          </head>
          <body class="bg-dark text-light p-3" data-bs-theme="dark">
            {body}
          </body>
        </html>
        """
        return final_html, 200
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("❌ Error en renderizado:", e)
        return f"Error interno en renderizado: {e}", 500

@app.route("/api/webhooks/topics", methods=["GET"])
def get_topics():
    try:
        now = time.time()
        if WEBHOOK_TOPICS_CACHE_TTL > 0:
            with _topics_cache_lock:
                if _topics_cache["value"] is not None and now < _topics_cache["expires_at"]:
                    return jsonify(_topics_cache["value"])

        with db_cursor() as cur:
            cur.execute("""
                SELECT topic, COUNT(*)
                FROM webhooks
                GROUP BY topic
                ORDER BY COUNT(*) DESC
            """)
            topics = [{"topic": row[0], "count": row[1]} for row in cur.fetchall()]

        if WEBHOOK_TOPICS_CACHE_TTL > 0:
            with _topics_cache_lock:
                _topics_cache["value"] = topics
                _topics_cache["expires_at"] = time.time() + WEBHOOK_TOPICS_CACHE_TTL

        return jsonify(topics)
    except Exception as e:
        print("❌ Error obteniendo topics:", e)
        return jsonify({"error": str(e)}), 500



@app.route("/api/ml/preview", methods=["GET", "POST"])
def ml_preview():
    resource = request.args.get("resource")
    if not resource:
        return jsonify({"error": "Falta parámetro resource"}), 400

    if not resource.startswith("/items/MLA"):
        return jsonify({"error": "Solo se soportan resources de items"}), 400

    return jsonify(fetch_and_store_preview(resource))

def _render_shipping_cost_section(mla_id, item_data, headers):
    """Fetch /users/{seller_id}/shipping_options/free (verbose) and render the
    FULL coverage values — the same query the persistence path uses to populate
    ml_seller_shipping_costs (what pricing reads). Best-effort, never raises.
    """
    try:
        # seller_id: prefer the item's own seller (matches the persistence
        # path); fall back to our own seller (ml_tokens.user_id, like /debug).
        seller_id = (item_data or {}).get("seller_id")
        if seller_id is None:
            with db_cursor() as cur:
                cur.execute("SELECT user_id FROM ml_tokens WHERE id = 1")
                row = cur.fetchone()
            seller_id = row[0] if row else None
        if seller_id is None:
            return "<div class='alert alert-warning'>⚠️ No se pudo resolver seller_id para el costo de envío.</div>"

        url = f"https://api.mercadolibre.com/users/{seller_id}/shipping_options/free"
        res = ml_api_get(url, headers=headers, params={"item_id": mla_id, "verbose": "true"})
        if res.status_code != 200:
            return f"<div class='alert alert-warning'>⚠️ shipping_options/free status={res.status_code} para {mla_id}.</div>"

        body = res.json()
        cov = ((body or {}).get("coverage") or {}).get("all_country") or {}
        list_cost = cov.get("list_cost")
        currency_id = cov.get("currency_id")
        billable_weight = cov.get("billable_weight")
        discount = cov.get("discount") or {}
        promoted_amount = discount.get("promoted_amount")
        rate = discount.get("rate")
        dtype = discount.get("type")

        raw_json = json.dumps(body, indent=2, ensure_ascii=False)
        request_url = f"{url}?item_id={mla_id}&verbose=true"

        return f"""
        <div class="card bg-secondary text-light mt-4">
          <div class="card-header"><strong>🚚 Costo de envío (shipping_options/free)</strong></div>
          <div class="card-body">
            <p class="small text-warning mb-2">seller_id={seller_id} · <code>{request_url}</code></p>
            <table class="table table-dark table-sm align-middle">
              <tbody>
                <tr><td><strong>list_cost</strong> (← este guardamos / lee pricing)</td><td>{list_cost} {currency_id or ""}</td></tr>
                <tr><td>billable_weight</td><td>{billable_weight}</td></tr>
                <tr><td>discount.promoted_amount</td><td>{promoted_amount}</td></tr>
                <tr><td>discount.rate</td><td>{rate}</td></tr>
                <tr><td>discount.type</td><td>{dtype}</td></tr>
              </tbody>
            </table>
            <details><summary class="small">Ver JSON completo</summary>
              <pre class="small bg-dark p-2 mt-2" style="white-space:pre-wrap;">{raw_json}</pre>
            </details>
          </div>
        </div>
        """
    except Exception as e:
        return f"<div class='alert alert-warning'>⚠️ Error obteniendo costo de envío: {e}</div>"


@app.route("/consulta", methods=["GET", "POST"])
def consulta():
    item_id = None
    mode = "items"
    data = None
    error = None

    if request.method == "POST":
        item_id = request.form.get("item_id")
        mode = request.form.get("mode", "price_to_win")

        if item_id:
            if mode == "items":
                resource = f"/items/{item_id}"
            elif mode == "price_to_win":
                resource = f"/items/{item_id}/price_to_win?version=v2"
            elif mode == "catalog_cards":
                # 👉 redirección al nuevo endpoint
                return redirect(f"/itemsByCatalogCards?product_id={item_id}")
            elif mode == "catalog_competition":
                # Render inline: ejecutamos la lógica acá y la embebemos abajo
                try:
                    token = get_token()
                    headers = {"Authorization": f"Bearer {token}"}
                    inline_body, inline_status = _build_catalog_competition_view(
                        item_id, headers, output="html"
                    )
                    catalog_inline_html = inline_body
                except Exception as e:
                    error = str(e)
                    catalog_inline_html = None

            if mode in ("items", "price_to_win"):
                try:
                    token = get_token()
                    headers = {"Authorization": f"Bearer {token}"}
                    res = ml_api_get(f"https://api.mercadolibre.com{resource}", headers=headers)
                    data = res.json()
                    shipping_cost_html = _render_shipping_cost_section(item_id, data, headers)
                except Exception as e:
                    error = str(e)

    # render HTML
    html_parts = [
        f"""
        <html>
        <head>
            <title>Consultas ML API</title>
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            <link rel="icon" href={FAVICON_DIR}>
            <link rel="apple-touch-icon" href={FAVICON_DIR}>
        </head>
        <body class="bg-dark text-light p-3" data-bs-theme="dark">
            <div class="container">
            <h2 class="mb-3">🔍 Consulta manual de MLA</h2>
            <form method="POST" class="mb-4">
            <div class="input-group mb-3">
                <input type="text" class="form-control" name="item_id" placeholder="Ej: MLA123456 o PROD12345" required>
                <select class="form-select" name="mode">
                <option value="items" {"selected" if mode == "items" else ""}>Consulta Items</option>
                <option value="price_to_win" {"selected" if mode == "price_to_win" else ""}>Consulta Price to Win</option>
                <option value="catalog_cards" {"selected" if mode == "catalog_cards" else ""}>Consulta Items por Catálogo</option>
                <option value="catalog_competition" {"selected" if mode == "catalog_competition" else ""}>Competencia en Catálogo (PDP)</option>
                </select>
                <button class="btn btn-primary" type="submit">Consultar</button>
            </div>
            </form>
        """
    ]

    if error:
        html_parts.append(f"<div class='alert alert-danger'>❌ Error: {error}</div>")

    if data:
        html_parts.append(render_ml_view(resource, data))
        if 'shipping_cost_html' in locals() and shipping_cost_html:
            html_parts.append(shipping_cost_html)
    elif 'catalog_inline_html' in locals() and catalog_inline_html:
        html_parts.append(catalog_inline_html)

    html_parts.append("""
            </div>
            <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
        </body></html>
    """)

    return "".join(html_parts)


# frontend
@app.route("/")
def index():
    return send_from_directory("frontend/dist", "index.html")

@app.route("/<path:path>")
def assets(path):
    return send_from_directory("frontend/dist", path)

@app.route("/debug/dbinfo")
def debug_dbinfo():
    try:
        with db_cursor() as cur:
            cur.execute("SELECT current_database(), current_user, inet_server_addr(), inet_server_port();")
            db, user, host, port = cur.fetchone()

            cur.execute("""
                SELECT COUNT(*)
                FROM webhooks
                WHERE resource LIKE '/items/MLA2243355590%%'
            """)
            count = cur.fetchone()[0]

        return jsonify({
            "db": db,
            "user": user,
            "host": str(host),
            "port": port,
            "webhooks_for_item": count
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Headers tipo navegador
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.mercadolibre.com.ar/",
    "Origin": "https://www.mercadolibre.com.ar",
}


@app.route("/catalogByEan")
def catalog_by_ean():
    site = request.args.get("site", "MLA")
    ean = request.args.get("ean")
    if not ean:
        return "Falta parámetro 'ean'", 400

    try:
        token = get_token()
        headers = {**BROWSER_HEADERS, "Authorization": f"Bearer {token}"}

        # Intento oficial
        url1 = f"https://api.mercadolibre.com/products/search?site_id={site}&product_identifier={ean}"
        r1 = ml_api_get(url1, headers=headers)
        if r1.ok:
            data = r1.json()
        else:
            # Fallback: search por q=
            url2 = f"https://api.mercadolibre.com/sites/{site}/search?q={ean}&limit=15"
            r2 = ml_api_get(url2, headers=headers)
            if not r2.ok:
                return f"❌ Error {r2.status_code}: {r2.text}", r2.status_code
            d2 = r2.json()
            hit = next((res for res in d2.get("results", []) if res.get("catalog_product_id")), None)
            data = {"results": [{"id": hit["catalog_product_id"]}]} if hit else {"results": []}

        body = render_json_as_html(data)
        final_html = f"""
        <html>
          <head>
            <meta charset="utf-8">
            <title>Catalog by EAN</title>
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            <link rel="icon" href={FAVICON_DIR}>
          </head>
          <body class="bg-dark text-light p-3" data-bs-theme="dark">
            <h3>📦 Catálogo por EAN</h3>
            {body}
          </body>
        </html>
        """
        return final_html, 200
    except Exception as e:
        return f"❌ Error: {e}", 500


@app.route("/itemsByCatalog")
def items_by_catalog():
    product_id = request.args.get("product_id")
    if not product_id:
        return "Falta parámetro 'product_id'", 400

    try:
        token = get_token()
        headers = {**BROWSER_HEADERS, "Authorization": f"Bearer {token}"}
        url = f"https://api.mercadolibre.com/products/{product_id}/items"
        res = ml_api_get(url, headers=headers)
        data = res.json()

        # ⚡ si piden json plano devolvemos directo
        if request.args.get("format") == "json":
            return jsonify(data)

        body = render_json_as_html(data)
        final_html = f"""
        <html>
          <head>
            <meta charset="utf-8">
            <title>Items by Catalog</title>
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            <link rel="icon" href={FAVICON_DIR}>
          </head>
          <body class="bg-dark text-light p-3" data-bs-theme="dark">
            <h3>🛒 Publicaciones del catálogo</h3>
            {body}
          </body>
        </html>
        """
        return final_html, res.status_code
    except Exception as e:
        return f"❌ Error: {e}", 500
    
@app.route("/itemsByCatalogCards")
def items_by_catalog_cards():
    product_id = request.args.get("product_id")
    if not product_id:
        return "Falta parámetro 'product_id'", 400

    try:
        token = get_token()
        headers = {"Authorization": f"Bearer {token}"}

        # --- Card general con datos del producto ---
        url_product = f"https://api.mercadolibre.com/products/{product_id}"
        res_product = ml_api_get(url_product, headers=headers)
        product_data = res_product.json()
        title = product_data.get("name", f"Producto {product_id}")
        thumbnail = (product_data.get("pictures") or [{}])[0].get("url", "")

        product_card = f"""
        <div class="card bg-dark text-light border-info mb-4">
          <div class="row g-0">
            <div class="col-md-3 d-flex align-items-center justify-content-center p-2">
              <img src="{thumbnail}" alt="{title}" class="img-fluid rounded-start" style="max-height: 120px; object-fit: cover;" />
            </div>
            <div class="col-md-9">
              <div class="card-body">
                <h4 class="card-title">{title}</h4>
                <p class="card-text"><small class="text-muted">Catálogo {product_id}</small></p>
              </div>
            </div>
          </div>
        </div>
        """

        # --- Publicaciones asociadas ---
        url_items = f"https://api.mercadolibre.com/products/{product_id}/items"
        res_items = ml_api_get(url_items, headers=headers)
        data = res_items.json()

        # ⚡ Si piden JSON plano, cortamos acá y devolvemos directo
        if request.args.get("format") == "json":
            return jsonify({
                "product": product_data,
                "items": data.get("results", [])
            })

        cards = []
        for item in data.get("results", []):
            item_id = item.get("item_id")
            price = item.get("price")
            currency = item.get("currency_id", "")
            warranty = item.get("warranty", "—")
            seller_id = item.get("seller_id")

            # nickname del seller
            nickname = seller_id
            try:
                u = f"https://api.mercadolibre.com/users/{seller_id}"
                r_user = ml_api_get(u, headers=headers)
                if r_user.ok:
                    nickname = r_user.json().get("nickname", seller_id)
            except Exception:
                pass

            permalink = f"https://articulo.mercadolibre.com.ar/{item_id}"

            cards.append(f"""
              <div class="col-md-4 mb-3">
                <div class="card bg-dark text-light border-secondary h-100">
                  <div class="card-body">
                    <h5 class="card-title">
                      <a href="{permalink}" target="_blank" class="text-decoration-none text-light">{item_id}</a>
                    </h5>
                    <p class="card-text">{currency} {price:,.0f}</p>
                    <p class="card-text"><small>Vendedor: {nickname}</small></p>
                    <p class="card-text"><small>{warranty}</small></p>
                  </div>
                </div>
              </div>
            """)

        body = f"""
        <div class="container">
          {product_card}
          <h5 class="mb-3">🛒 Publicaciones asociadas</h5>
          <div class="row">
            {''.join(cards)}
          </div>
        </div>
        """

        html = f"""
        <html>
          <head>
            <meta charset="utf-8">
            <title>Items by Catalog</title>
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            <link rel="icon" href={FAVICON_DIR}>
          </head>
          <body class="bg-dark text-light p-3" data-bs-theme="dark">
            {body}
          </body>
        </html>
        """
        return html, 200

    except Exception as e:
        return f"❌ Error: {e}", 500

    
def _seller_cache_get(seller_id):
    """Returns (nickname, payload) from ml_sellers cache, or (None, None) on miss/error."""
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT nickname, payload FROM ml_sellers WHERE seller_id = %s",
                (seller_id,),
            )
            row = cur.fetchone()
            if row:
                return row[0], row[1]
    except Exception as e:
        print(f"⚠️ seller cache get error for {seller_id}: {e}")
    return None, None


def _seller_cache_put(seller_id, nickname, payload):
    """Upsert seller into ml_sellers. Best-effort: swallows errors so it never breaks the request."""
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO ml_sellers (seller_id, nickname, payload)
                VALUES (%s, %s, %s)
                ON CONFLICT (seller_id) DO UPDATE
                    SET nickname = EXCLUDED.nickname,
                        payload = EXCLUDED.payload,
                        fetched_at = NOW()
                """,
                (seller_id, nickname, Json(payload)),
            )
    except Exception as e:
        print(f"⚠️ seller cache put error for {seller_id}: {e}")


def _process_competitor_item(it):
    """Curate raw fields from /products/{id}/items into something the frontend can use directly.

    Returns a dict with: item_id, seller_id, listing_type_id, listing_label,
    installments, price, original_price, currency_id, shipping_badges, tags, permalink.

    Notes:
      - listing_label: gold_special → "Clásica"; gold_pro → "Premium · N cuotas"
        where N is inferred from tags (12x_campaign / 9x_campaign / 3x_campaign).
        If gold_pro and none of those tags, defaults to 6 cuotas.
      - shipping_badges: ["FULL"] when shipping.logistic_type == "fulfillment",
        ["FLEX"] when "self_service" or "cross_docking". May extend later.
    """
    listing_type = it.get("listing_type_id")
    tags = it.get("tags") or []
    shipping = it.get("shipping") or {}

    if listing_type == "gold_special":
        listing_label = "Clásica"
        installments = None
    elif listing_type == "gold_pro":
        if "12x_campaign" in tags:
            installments = 12
        elif "9x_campaign" in tags:
            installments = 9
        elif "3x_campaign" in tags:
            installments = 3
        else:
            installments = 6
        listing_label = f"Premium · {installments} cuotas"
    else:
        listing_label = listing_type or "—"
        installments = None

    shipping_badges = []
    logistic_type = (shipping.get("logistic_type") or "").lower()
    if logistic_type == "fulfillment":
        shipping_badges.append("FULL")
    elif logistic_type == "self_service":
        shipping_badges.append("FLEX")
    elif logistic_type == "cross_docking":
        shipping_badges.append("ENCOMIENDA")

    return {
        "item_id": it.get("item_id"),
        "seller_id": it.get("seller_id"),
        "listing_type_id": listing_type,
        "listing_label": listing_label,
        "installments": installments,
        "price": it.get("price"),
        "original_price": it.get("original_price"),
        "currency_id": it.get("currency_id"),
        "shipping_badges": shipping_badges,
        "tags": tags,
        "permalink": it.get("permalink"),
    }


def _build_catalog_competition_view(raw_input, headers, output="html"):
    """
    Builds the /catalogCompetition view from a raw input (MLA item id or PROD catalog id).

    output:
      - "html" → returns (body_html_str, status). Body is HTML sin <html> wrap.
      - "json" → returns (jsonify response, status) with raw upstream payloads.
      - "processed" → returns (jsonify response, status) with curated/processed data
                      shaped for external consumers.
    """
    raw_input = (raw_input or "").strip()
    if not raw_input:
        return "<div class='alert alert-warning m-4'>Falta parámetro 'input'</div>", 400

    # 1) Resolver catalog_product_id: probar como item primero, fallback a product
    catalog_product_id = None
    res_item = ml_api_get(f"https://api.mercadolibre.com/items/{raw_input}", headers=headers)
    if res_item.ok:
        item_json = res_item.json()
        catalog_product_id = item_json.get("catalog_product_id")
        if not catalog_product_id:
            return (
                f"<div class='alert alert-warning m-4'>⚠️ El item <strong>{raw_input}</strong> "
                f"no es publicación de catálogo (no tiene <code>catalog_product_id</code>).</div>",
                400,
            )
    else:
        catalog_product_id = raw_input

    # 2) Datos del PDP (incluye buy_box_winner)
    res_product = ml_api_get(
        f"https://api.mercadolibre.com/products/{catalog_product_id}", headers=headers
    )
    if not res_product.ok:
        return (
            f"<div class='alert alert-danger m-4'>❌ No se pudo obtener PDP "
            f"<strong>{catalog_product_id}</strong>: {res_product.text}</div>",
            res_product.status_code,
        )
    product_data = res_product.json()
    title = product_data.get("name", f"Producto {catalog_product_id}")
    thumbnail = (product_data.get("pictures") or [{}])[0].get("url", "")
    buy_box_winner = product_data.get("buy_box_winner") or {}
    winner_item_id = buy_box_winner.get("item_id")

    # 3) Competidores en el PDP
    res_items = ml_api_get(
        f"https://api.mercadolibre.com/products/{catalog_product_id}/items", headers=headers
    )
    if not res_items.ok:
        return (
            f"<div class='alert alert-danger m-4'>❌ No se pudieron obtener competidores: "
            f"{res_items.text}</div>",
            res_items.status_code,
        )
    competitors = res_items.json().get("results", [])

    # 4) Fan-out: lookup de seller con cache (DB + per-request)
    users_raw = {}
    enriched = []
    for it in competitors:
        seller_id = it.get("seller_id")
        user_data = None
        nickname = seller_id

        if seller_id is not None:
            if seller_id in users_raw:
                user_data = users_raw[seller_id]
            else:
                cached_nick, cached_payload = _seller_cache_get(seller_id)
                if cached_payload is not None:
                    user_data = cached_payload
                    users_raw[seller_id] = user_data
                else:
                    try:
                        r_user = ml_api_get(
                            f"https://api.mercadolibre.com/users/{seller_id}", headers=headers
                        )
                        if r_user.ok:
                            user_data = r_user.json()
                            users_raw[seller_id] = user_data
                            _seller_cache_put(
                                seller_id,
                                user_data.get("nickname"),
                                user_data,
                            )
                    except Exception:
                        pass

            if user_data:
                nickname = user_data.get("nickname", seller_id)

        enriched.append({
            "item": it,
            "nickname": nickname,
            "user": user_data,
            "processed": _process_competitor_item(it),
        })

    # 4b) Short-circuit: respuesta JSON
    if output == "json":
        return jsonify({
            "catalog_product_id": catalog_product_id,
            "product": product_data,
            "items": competitors,
            "users": users_raw,
        }), 200

    if output == "processed":
        return jsonify({
            "catalog_product_id": catalog_product_id,
            "product": {
                "id": catalog_product_id,
                "name": title,
                "thumbnail": thumbnail,
                "buy_box_winner_item_id": winner_item_id,
            },
            "competitors": [
                {
                    **row["processed"],
                    "nickname": row["nickname"],
                    "is_winner": row["processed"].get("item_id") == winner_item_id,
                }
                for row in enriched
            ],
        }), 200

    # 5) Render del body HTML (sin wrap <html>)
    def _fmt_money(val, currency="ARS"):
        try:
            n = int(round(float(val)))
            return f"{currency} {n:,}".replace(",", ".")
        except Exception:
            return "—"

    winner_url = (
        f"https://www.mercadolibre.com.ar/p/{catalog_product_id}?pdp_filters=item_id:{winner_item_id}"
        if winner_item_id else f"https://www.mercadolibre.com.ar/p/{catalog_product_id}"
    )

    product_card = f"""
    <div class="card bg-dark text-light border-info mb-4">
      <div class="row g-0">
        <div class="col-md-3 d-flex align-items-center justify-content-center p-2">
          <img src="{thumbnail}" alt="{title}" class="img-fluid rounded-start" style="max-height: 140px; object-fit: cover;" />
        </div>
        <div class="col-md-9">
          <div class="card-body">
            <h4 class="card-title">{title}</h4>
            <p class="card-text mb-1"><small class="text-muted">Catálogo {catalog_product_id} · {len(enriched)} competidores</small></p>
            <p class="card-text mb-0">
              <strong>🏆 Buy Box Winner:</strong>
              {f'<a href="{winner_url}" target="_blank" rel="noopener noreferrer">{winner_item_id}</a>' if winner_item_id else '—'}
            </p>
          </div>
        </div>
      </div>
    </div>
    """

    cards = []
    for row in enriched:
        proc = row["processed"]
        nickname = row["nickname"]
        it_id = proc.get("item_id")
        currency = proc.get("currency_id") or "ARS"
        price = proc.get("price")
        original_price = proc.get("original_price")
        listing_label = proc.get("listing_label") or "—"
        shipping_badges = proc.get("shipping_badges") or []

        is_winner = it_id == winner_item_id
        border_cls = "border-success" if is_winner else "border-secondary"
        winner_badge = "<span class='badge bg-success ms-2'>🏆 Ganador</span>" if is_winner else ""

        pdp_link = f"https://www.mercadolibre.com.ar/p/{catalog_product_id}?pdp_filters=item_id:{it_id}"

        # Precio: si hay original_price > price, mostrar tachado arriba
        try:
            show_strike = (
                original_price is not None
                and price is not None
                and float(original_price) > float(price)
            )
        except Exception:
            show_strike = False
        strike_html = (
            f"<div class='text-muted small text-decoration-line-through'>"
            f"{_fmt_money(original_price, currency)}</div>"
            if show_strike else ""
        )

        def _ship_badge(b):
            if b == "FULL":
                return f"<span class='badge bg-warning text-dark me-1'>{b}</span>"
            if b == "FLEX":
                return f"<span class='badge bg-info text-dark me-1'>{b}</span>"
            if b == "ENCOMIENDA":
                return f"<span class='badge bg-secondary me-1'>{b}</span>"
            return f"<span class='badge bg-light text-dark me-1'>{b}</span>"
        ship_html = "".join(_ship_badge(b) for b in shipping_badges)

        cards.append(f"""
          <div class="col-md-6 col-lg-4 mb-3">
            <div class="card bg-dark text-light {border_cls} h-100">
              <div class="card-header">
                <strong>{nickname}</strong>{winner_badge}
              </div>
              <div class="card-body">
                <div class="mb-2">
                  <a href="{pdp_link}" target="_blank" rel="noopener noreferrer" class="text-info">{it_id}</a>
                </div>
                <div class="mb-2">
                  <span class="badge bg-secondary">{listing_label}</span>
                  {ship_html}
                </div>
                {strike_html}
                <div><strong>Precio:</strong> {_fmt_money(price, currency)}</div>
              </div>
            </div>
          </div>
        """)

    body = f"""
    <div class="container">
      <h2 class="mb-3">⚔️ Competencia en Catálogo (PDP)</h2>
      {product_card}
      <h5 class="mb-3">🛒 Competidores</h5>
      <div class="row">
        {''.join(cards) if cards else "<div class='alert alert-secondary'>Sin competidores en este PDP.</div>"}
      </div>
    </div>
    """
    return body, 200


@app.route("/catalogCompetition")
def catalog_competition():
    raw_input = (request.args.get("input") or "").strip()
    if not raw_input:
        return "Falta parámetro 'input'", 400

    try:
        token = get_token()
        headers = {"Authorization": f"Bearer {token}"}
        fmt = request.args.get("format")
        output = "json" if fmt == "json" else "processed" if fmt == "processed" else "html"
        body, status = _build_catalog_competition_view(raw_input, headers, output)
        if output != "html":
            return body, status
        html = f"""
        <html>
          <head>
            <meta charset="utf-8">
            <title>Competencia en Catálogo · {raw_input}</title>
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            <link rel="icon" href={FAVICON_DIR}>
          </head>
          <body class="bg-dark text-light p-3" data-bs-theme="dark">
            {body}
          </body>
        </html>
        """
        return html, status
    except Exception as e:
        return f"<div class='alert alert-danger m-4'>❌ Error: {e}</div>", 500

@app.route("/seller")
def get_seller():
    seller_id = request.args.get("id")
    if not seller_id:
        return "Falta parámetro 'id'", 400
    try:
        token = get_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"https://api.mercadolibre.com/users/{seller_id}"
        res = ml_api_get(url, headers=headers)
        data = res.json()

        body = render_json_as_html(data)
        html = f"""
        <html>
          <head>
            <meta charset="utf-8">
            <title>Seller {seller_id}</title>
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            <link rel="icon" href={FAVICON_DIR}>
          </head>
          <body class="bg-dark text-light p-3" data-bs-theme="dark">
            <h3>🛍️ Información del vendedor {seller_id}</h3>
            {body}
          </body>
        </html>
        """
        return html, res.status_code
    except Exception as e:
        return f"❌ Error: {e}", 500

@app.route("/debug/token")
def debug_token():
    try:
        token = get_token()
        return jsonify({
            "access_token": token,
            "expires_at": EXPIRATION,
            "expires_in_seconds": int(EXPIRATION - time.time())
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -------------------------------------------------------------
# Central de Promociones (Seller Promotions API v2) — Fase 1
# Endpoints JSON en vivo para consumir desde pricing-app.
# Reusan get_token() + ml_api_get(). Un unico vendedor:
# seller_id sale de ml_tokens.user_id (fila id=1).
# -------------------------------------------------------------
def _promos_seller_id():
    with db_cursor() as cur:
        cur.execute("SELECT user_id FROM ml_tokens WHERE id = 1")
        row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def _ml_promos_get(resource, extra_params=None):
    """GET a la API de seller-promotions forzando app_version=v2.
    Hace passthrough de los query params del cliente (menos app_version).
    Devuelve (payload, status_code)."""
    token = get_token()
    params = {k: v for k, v in request.args.items() if k != "app_version"}
    if extra_params:
        params.update(extra_params)
    params["app_version"] = "v2"
    res = ml_api_get(
        f"https://api.mercadolibre.com{resource}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )
    try:
        return res.json(), res.status_code
    except Exception:
        return {"error": "respuesta no-JSON de ML", "raw": res.text}, res.status_code


def _promo_num(v):
    """Convierte a float o None (para columnas NUMERIC)."""
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _upsert_item_promo(cur, mla, promo_key, promo_type, sub_type, entry):
    cur.execute("""
        INSERT INTO ml_item_promotions (
            mla, promotion_id, promotion_type, sub_type, status,
            original_price, price, min_discounted_price, max_discounted_price,
            suggested_discounted_price, payload, updated_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        ON CONFLICT (mla, promotion_id) DO UPDATE SET
            promotion_type = EXCLUDED.promotion_type,
            sub_type = EXCLUDED.sub_type,
            status = EXCLUDED.status,
            original_price = EXCLUDED.original_price,
            price = EXCLUDED.price,
            min_discounted_price = EXCLUDED.min_discounted_price,
            max_discounted_price = EXCLUDED.max_discounted_price,
            suggested_discounted_price = EXCLUDED.suggested_discounted_price,
            payload = EXCLUDED.payload,
            updated_at = NOW();
    """, (
        mla, promo_key, promo_type, sub_type, entry.get("status"),
        _promo_num(entry.get("original_price")), _promo_num(entry.get("price")),
        _promo_num(entry.get("min_discounted_price")),
        _promo_num(entry.get("max_discounted_price")),
        _promo_num(entry.get("suggested_discounted_price")), Json(entry),
    ))


def _persist_promotions(data):
    """Write-through best-effort: catalogo de promos del vendedor -> ml_promotions.
    Nunca rompe la respuesta en vivo: loguea y sigue si la DB falla."""
    try:
        results = data.get("results") if isinstance(data, dict) else None
        if not results:
            return
        with db_cursor() as cur:
            for p in results:
                if not isinstance(p, dict) or not p.get("id"):
                    continue
                cur.execute("""
                    INSERT INTO ml_promotions (
                        promotion_id, promotion_type, sub_type, status, name,
                        start_date, finish_date, deadline_date, payload, updated_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                    ON CONFLICT (promotion_id) DO UPDATE SET
                        promotion_type = EXCLUDED.promotion_type,
                        sub_type = EXCLUDED.sub_type,
                        status = EXCLUDED.status,
                        name = EXCLUDED.name,
                        start_date = EXCLUDED.start_date,
                        finish_date = EXCLUDED.finish_date,
                        deadline_date = EXCLUDED.deadline_date,
                        payload = EXCLUDED.payload,
                        updated_at = NOW();
                """, (
                    p.get("id"), p.get("type"), p.get("sub_type"), p.get("status"),
                    p.get("name"), p.get("start_date"), p.get("finish_date"),
                    p.get("deadline_date"), Json(p),
                ))
    except Exception as e:
        print("⚠️ write-through ml_promotions fallo:", e)


def _persist_item_promos(mla, data):
    """Write-through: promos (candidate/started) de un item -> ml_item_promotions."""
    try:
        if not isinstance(data, list):
            return
        with db_cursor() as cur:
            for e in data:
                if not isinstance(e, dict):
                    continue
                promo_key = e.get("id") or e.get("type")  # PRICE_DISCOUNT no trae id
                if not promo_key:
                    continue
                _upsert_item_promo(cur, mla, promo_key, e.get("type"), e.get("sub_type"), e)
    except Exception as ex:
        print("⚠️ write-through ml_item_promotions (item) fallo:", ex)


def _persist_promo_items(promo_id, promotion_type, data):
    """Write-through: items dentro de una promo -> ml_item_promotions."""
    try:
        results = data.get("results") if isinstance(data, dict) else None
        if not results:
            return
        with db_cursor() as cur:
            for e in results:
                if not isinstance(e, dict) or not e.get("id"):
                    continue
                _upsert_item_promo(cur, e.get("id"), promo_id, promotion_type,
                                   e.get("sub_type"), e)
    except Exception as ex:
        print("⚠️ write-through ml_item_promotions (promo) fallo:", ex)


def _promo_resource_mla(resource):
    """Extrae el MLA embebido en un resource de seller-promotions.
    Ej: /seller-promotions/offers/OFFER-MLA1632687413-111 -> MLA1632687413."""
    last = (resource or "").rstrip("/").split("/")[-1]
    for part in last.split("-"):
        if part.startswith("MLA"):
            return part
    return None


def _promos_api_get(resource, extra_params=None):
    """GET request-free (sin contexto Flask) a seller-promotions con app_version=v2.
    extra_params: dict opcional (promotion_type, limit, search_after, offset...) que
    requests URL-encodea. Usable desde el worker y el backfill."""
    token = get_token()
    params = {"app_version": "v2"}
    if extra_params:
        params.update(extra_params)
    return ml_api_get(
        f"https://api.mercadolibre.com{resource}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )


def _upsert_item_promo_status(cur, mla, promo_key, promo_type, status, detail):
    """Upsert PARCIAL (webhook): actualiza estado sin pisar precios ni el payload
    de lectura. En fila nueva inserta con precios NULL y el payload del offer/candidate."""
    cur.execute("""
        INSERT INTO ml_item_promotions (mla, promotion_id, promotion_type, status, payload, updated_at)
        VALUES (%s,%s,%s,%s,%s,NOW())
        ON CONFLICT (mla, promotion_id) DO UPDATE SET
            promotion_type = COALESCE(EXCLUDED.promotion_type, ml_item_promotions.promotion_type),
            status = EXCLUDED.status,
            updated_at = NOW();
    """, (mla, promo_key, promo_type, status, Json(detail)))


def reconcile_item_promotions(mla):
    """Reconcilia TODAS las promos de un MLA via /seller-promotions/items/{mla}
    y persiste con precios (reusa _persist_item_promos). Lo usa worker_promos."""
    res = _promos_api_get(f"/seller-promotions/items/{mla}")
    if res.status_code == 200:
        _persist_item_promos(mla, res.json())
        return True
    print(f"⚠️ reconcile promos {mla} -> ML {res.status_code}")
    return False


def _process_promotion_candidate(resource):
    """Candidate (bajo volumen): fetch del detalle + upsert de estado."""
    res = _promos_api_get(resource)
    if res.status_code != 200:
        print(f"⚠️ candidate {resource} -> ML {res.status_code}")
        return
    d = res.json()
    mla = d.get("item_id")
    if not mla:
        return
    promo_key = d.get("promotion_id") or d.get("type")
    if not promo_key:
        return
    status = d.get("status")
    if isinstance(status, dict):
        status = status.get("id")
    with db_cursor() as cur:
        _upsert_item_promo_status(cur, mla, promo_key, d.get("type"), status, d)


def _process_promotion_webhook(resource):
    """Router de webhooks seller-promotions. Best-effort, nunca rompe el webhook.
    - candidates (~2/min): procesa sync (fetch + upsert estado).
    - offers (flood): encola el MLA en un set redis; lo reconcilia worker_promos."""
    if not PROMOS_WEBHOOK_ENABLED:
        return
    try:
        if "/candidates/" in resource:
            _process_promotion_candidate(resource)
        elif "/offers/" in resource:
            mla = _promo_resource_mla(resource)
            if mla and _redis_client is not None:
                _redis_client.sadd(PROMOS_DIRTY_SET_KEY, mla)
    except Exception as e:
        print("⚠️ promo webhook fallo:", e)


@app.route("/api/promociones", methods=["GET"])
def api_promociones():
    """Lista todas las promociones del vendedor (Central de Promociones v2).
    Passthrough de GET /seller-promotions/users/{seller_id}.
    Filtros/paginacion de ML via query params (status, promotion_type, limit, offset, search_after...)."""
    try:
        seller_id = _promos_seller_id()
        if seller_id is None:
            return jsonify({"error": "seller_id no disponible (ml_tokens.user_id fila id=1)"}), 500
        data, status = _ml_promos_get(f"/seller-promotions/users/{seller_id}")
        if status == 200:
            _persist_promotions(data)
        return jsonify(data), status
    except Exception as e:
        print("❌ Error en /api/promociones:", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/promociones/<promo_id>/items", methods=["GET"])
def api_promociones_items(promo_id):
    """Items dentro de una promocion.
    Passthrough de GET /seller-promotions/promotions/{promo_id}/items.
    ML exige ?promotion_type=... (ej: DEAL, PRICE_DISCOUNT, MARKETPLACE_CAMPAIGN, etc)."""
    try:
        if not request.args.get("promotion_type"):
            return jsonify({"error": "Falta query param 'promotion_type' (requerido por ML)"}), 400
        data, status = _ml_promos_get(f"/seller-promotions/promotions/{promo_id}/items")
        if status == 200:
            _persist_promo_items(promo_id, request.args.get("promotion_type"), data)
        return jsonify(data), status
    except Exception as e:
        print("❌ Error en /api/promociones/items:", e)
        return jsonify({"error": str(e)}), 500


def _ml_promos_write(method, resource, json_body=None, extra_params=None):
    """POST/DELETE a seller-promotions. SINGLE-SHOT: no reintenta (toca precios reales).
    Un 5xx/timeout en escritura es ambiguo (no sabes si ML lo aplico), por eso no hay retry.
    Respeta el throttle global de ML. Devuelve (payload, status_code)."""
    global _ml_api_last_call
    token = get_token()
    params = {k: v for k, v in request.args.items() if k != "app_version"}
    if extra_params:
        params.update(extra_params)
    params["app_version"] = "v2"
    with _ml_api_lock:
        wait = _ml_api_min_interval - (time.time() - _ml_api_last_call)
        if wait > 0:
            time.sleep(wait)
        _ml_api_last_call = time.time()
    res = requests.request(
        method,
        f"https://api.mercadolibre.com{resource}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        json=json_body,
        timeout=30,
    )
    if not res.content:
        # ML devuelve body vacio en DELETE (y algunos POST) exitosos.
        return {"ok": res.ok}, res.status_code
    try:
        return res.json(), res.status_code
    except Exception:
        return {"error": "respuesta no-JSON de ML", "raw": res.text}, res.status_code


def _promos_price_guard(body):
    """Devuelve lista de errores si detecta precios invalidos (<=0 o no numericos).
    Type-agnostic: solo chequea campos de precio presentes, no impone estructura de payload."""
    errs = []

    def _check(name, val):
        if val is None:
            return
        try:
            f = float(val)
        except (TypeError, ValueError):
            errs.append(f"{name} no es numerico: {val!r}")
            return
        if f <= 0:
            errs.append(f"{name} debe ser > 0 (recibido {f})")

    _check("deal_price", body.get("deal_price"))
    _check("top_deal_price", body.get("top_deal_price"))
    offers = body.get("offers")
    if isinstance(offers, list):
        for idx, off in enumerate(offers):
            if isinstance(off, dict):
                for k in ("new_price", "deal_price", "price"):
                    if k in off:
                        _check(f"offers[{idx}].{k}", off.get(k))
    return errs


@app.route("/api/promociones/item/<mla>", methods=["GET", "POST", "DELETE"])
def api_promociones_item(mla):
    """Promociones de un item puntual (Central de Promociones v2).
    GET    -> promos disponibles/activas para el item (lectura).
    POST   -> inscribe/actualiza el item en una promo (ESCRITURA: toca precio real).
    DELETE -> saca el item de una promo (ESCRITURA)."""
    if request.method == "GET":
        try:
            data, status = _ml_promos_get(f"/seller-promotions/items/{mla}")
            if status == 200:
                _persist_item_promos(mla, data)
            return jsonify(data), status
        except Exception as e:
            print("❌ Error en GET /api/promociones/item:", e)
            return jsonify({"error": str(e)}), 500

    # Kill-switch de ops para toda escritura (emergencia sin deploy).
    if os.getenv("PROMOS_WRITE_ENABLED", "1") != "1":
        return jsonify({"error": "Escritura de promociones deshabilitada (PROMOS_WRITE_ENABLED != 1)"}), 503

    if request.method == "POST":
        try:
            body = request.get_json(silent=True)
            if not isinstance(body, dict):
                return jsonify({"error": "Body JSON requerido (objeto con promotion_id, promotion_type, deal_price/offers...)"}), 400
            if not body.get("promotion_type"):
                return jsonify({"error": "Falta 'promotion_type' en el body (requerido por ML)"}), 400
            price_errs = _promos_price_guard(body)
            if price_errs:
                return jsonify({"error": "Precios invalidos", "detalle": price_errs}), 400
            print(f"📝 PROMO WRITE POST mla={mla} type={body.get('promotion_type')} "
                  f"promo_id={body.get('promotion_id')} deal_price={body.get('deal_price')} "
                  f"top_deal_price={body.get('top_deal_price')}")
            data, status = _ml_promos_write("POST", f"/seller-promotions/items/{mla}", json_body=body)
            print(f"📝 PROMO WRITE POST mla={mla} -> {status}")
            return jsonify(data), status
        except Exception as e:
            print("❌ Error en POST /api/promociones/item:", e)
            return jsonify({"error": str(e)}), 500

    if request.method == "DELETE":
        try:
            if not request.args.get("promotion_type"):
                return jsonify({"error": "Falta query param 'promotion_type' (requerido por ML para DELETE)"}), 400
            print(f"🗑️ PROMO WRITE DELETE mla={mla} type={request.args.get('promotion_type')} "
                  f"promo_id={request.args.get('promotion_id')}")
            data, status = _ml_promos_write("DELETE", f"/seller-promotions/items/{mla}")
            print(f"🗑️ PROMO WRITE DELETE mla={mla} -> {status}")
            return jsonify(data), status
        except Exception as e:
            print("❌ Error en DELETE /api/promociones/item:", e)
            return jsonify({"error": str(e)}), 500


@app.route("/debug/promos")
def debug_promos():
    """TEMPORARY — probe empirica read-only para Central de Promociones (v2).
    Sin escritura. Vuelca:
      - la lista de promos del vendedor (/seller-promotions/users/{seller_id})
      - si ?mla=MLA...  -> promos disponibles/activas de ese item
      - si ?promo_id=&promotion_type=  -> items dentro de esa promo
    Sirve para ver la estructura EXACTA que espera ML antes de escribir.
    Borrar una vez confirmado el contrato de payload."""
    try:
        token = get_token()

        def _probe(resource, extra=None):
            params = {"app_version": "v2"}
            if extra:
                params.update(extra)
            res = ml_api_get(
                f"https://api.mercadolibre.com{resource}",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
            try:
                return {"status": res.status_code, "data": res.json()}
            except Exception:
                return {"status": res.status_code, "raw": res.text}

        seller_id = _promos_seller_id()
        if seller_id is None:
            return jsonify({"error": "seller_id no disponible (ml_tokens.user_id fila id=1)"}), 500

        out = {"seller_id": seller_id}
        out["promotions"] = _probe(f"/seller-promotions/users/{seller_id}")

        mla = (request.args.get("mla") or "").strip()
        if mla:
            out["item"] = {"mla": mla, **_probe(f"/seller-promotions/items/{mla}")}

        promo_id = (request.args.get("promo_id") or "").strip()
        promotion_type = (request.args.get("promotion_type") or "").strip()
        if promo_id and promotion_type:
            out["promo_items"] = {
                "promo_id": promo_id,
                "promotion_type": promotion_type,
                **_probe(f"/seller-promotions/promotions/{promo_id}/items",
                         {"promotion_type": promotion_type}),
            }

        return jsonify(out)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# TEMPORARY — empirical probe for /users/{user_id}/shipping_options/free
# Remove once seller_shipping_costs schema is finalized.
@app.route("/debug/seller-shipping-cost")
def debug_seller_shipping_cost():
    try:
        # 1) seller_id desde ml_tokens.user_id (fila id=1)
        with db_cursor() as cur:
            cur.execute("SELECT user_id FROM ml_tokens WHERE id = 1")
            row = cur.fetchone()
        if not row or row[0] is None:
            return jsonify({"error": "ml_tokens.user_id no disponible (fila id=1)"}), 500
        seller_id = row[0]

        # 2) MLAs: explicit ?mla=... + autofill desde webhooks hasta 3
        target = 3
        raw_mla = (request.args.get("mla") or "").strip()
        explicit = [m.strip() for m in raw_mla.split(",") if m.strip()] if raw_mla else []
        mlas = list(dict.fromkeys(explicit))  # dedup preservando orden

        if len(mlas) < target:
            need = target - len(mlas)
            with db_cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT ON (resource) resource
                    FROM webhooks
                    WHERE topic = 'items'
                      AND resource ~ '^/items/MLA[0-9]+$'
                    ORDER BY resource, received_at DESC
                """)
                rows = cur.fetchall()
            recent = [r[0].split("/")[2] for r in rows]
            for mla in recent:
                if mla in mlas:
                    continue
                mlas.append(mla)
                if len(mlas) >= target:
                    break

        if not mlas:
            return jsonify({"error": "no hay MLAs para probar (pasá ?mla=... o llená webhooks)"}), 400

        token = get_token()
        headers = {"Authorization": f"Bearer {token}"}

        results = []
        for mla in mlas:
            url = f"https://api.mercadolibre.com/users/{seller_id}/shipping_options/free"
            params = {"item_id": mla, "verbose": "true"}
            res = ml_api_get(url, headers=headers, params=params)

            entry = {
                "mla_id": mla,
                "request_url": f"{url}?item_id={mla}&verbose=true",
                "status_code": res.status_code,
                "response_headers": {
                    k: v for k, v in res.headers.items()
                    if k.lower() in ("content-type", "x-rate-limit-remaining", "x-rate-limit-reset", "retry-after")
                },
            }
            try:
                entry["body"] = res.json()
            except Exception:
                entry["body_text"] = res.text

            results.append(entry)
            print(f"🔍 /shipping_options/free MLA={mla} status={res.status_code}")

        return jsonify({
            "seller_id": seller_id,
            "mlas_probed": mlas,
            "results": results,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



# Operational: trigger or inspect the seller shipping-cost sweep job.
@app.route("/admin/sweep-shipping-costs")
def admin_sweep_shipping_costs():
    # status-only mode: ?status=1
    if request.args.get("status"):
        return jsonify(dict(_sweep_state))

    force = request.args.get("force") == "1"
    dry_run = request.args.get("dry_run") == "1"
    try:
        limit_raw = request.args.get("limit")
        limit = int(limit_raw) if limit_raw else None
    except ValueError:
        return jsonify({"error": "limit must be int"}), 400
    try:
        min_age_hours = int(request.args.get("min_age_hours", 0))
    except ValueError:
        return jsonify({"error": "min_age_hours must be int"}), 400

    with _sweep_lock:
        if _sweep_state["running"] and not force:
            return jsonify({"error": "sweep already running", "state": dict(_sweep_state)}), 409
        _sweep_state.update({
            "running": True,
            "started_at": datetime.now(ZoneInfo("UTC")).isoformat(),
            "finished_at": None,
            "processed": 0,
            "skipped": 0,
            "errors": 0,
            "total_enumerated": 0,
            "last_mla": None,
            "dry_run": dry_run,
            "limit": limit,
            "min_age_hours": min_age_hours,
        })

    t = threading.Thread(
        target=_sweep_seller_shipping_costs,
        args=(limit, dry_run, min_age_hours),
        daemon=True,
    )
    t.start()

    return jsonify({
        "status": "started",
        "dry_run": dry_run,
        "limit": limit,
        "min_age_hours": min_age_hours,
        "poll": "/admin/sweep-shipping-costs?status=1",
    }), 202


def save_token_to_db(token_data: dict):
    expires_in = int(token_data.get("expires_in", 0))  # <-- tiene que existir antes del execute

    with db_cursor() as cur:
        cur.execute("""
            UPDATE ml_tokens
               SET access_token = %s,
                   refresh_token = COALESCE(%s, refresh_token),
                   token_type = %s,
                   scope = %s,
                   user_id = %s,
                   expires_at = NOW() + (%s || ' seconds')::interval - interval '60 seconds',
                   updated_at = NOW()
             WHERE id = 1
        """, (
            token_data.get("access_token"),
            token_data.get("refresh_token"),
            token_data.get("token_type"),
            token_data.get("scope"),
            token_data.get("user_id"),
            expires_in,  # <-- ahora sí existe
        ))

def load_token_from_db():
    with db_cursor() as cur:
        cur.execute("""
            SELECT access_token, refresh_token, EXTRACT(EPOCH FROM expires_at) AS expires_epoch
            FROM ml_tokens
            WHERE id = 1
        """)
        row = cur.fetchone()
        if not row:
            return None
        access_token, refresh_token, expires_epoch = row
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_epoch": float(expires_epoch) if expires_epoch is not None else 0.0
        }

if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    app.run(host="0.0.0.0", port=port)


