# 📦 ML Webhook Viewer

Aplicación para recibir y visualizar **webhooks de Mercado Libre** en tiempo real.  
Incluye un backend en **Flask** (Python) y un frontend en **React (Vite)** con soporte de tema claro/oscuro.

---

## 🚀 Requisitos

- **Python 3.10+**
- **Node.js 18+** (para el frontend)
- **pip / venv**
- **npm / pnpm / yarn**

---

## ⚙️ Instalación

### 1. Clonar el repo

    git clone https://github.com/tuusuario/ml-webhook.git
    cd ml-webhook

### 2. Backend (Flask)

Crear y activar entorno virtual:

    python -m venv .venv
    source .venv/bin/activate   # Linux/Mac
    .venv\Scripts\activate      # Windows 

Instalar dependencias:

    pip install -r requirements.txt

Variables de entorno en un archivo `.env`:


    ML_CLIENT_ID=tu_client_id
    ML_CLIENT_SECRET=tu_client_secret
    ML_REDIRECT_URI=https://tuservidor.com/callback
    ML_REFRESH_TOKEN=tu_refresh_token
    PORT=3000
    WEBHOOK_PREVIEW_ASYNC=1
    WEBHOOKS_DEFAULT_LIMIT=100
    WEBHOOKS_MAX_LIMIT=500
    WEBHOOKS_CURSOR_MODE=0
    WEBHOOK_TOPICS_CACHE_TTL=10
    REDIS_URL=redis://localhost:6379/0

Ejecutar backend:

    python app.py
Por defecto corre en: [http://localhost:3000](http://localhost:3000)

Si activás `WEBHOOK_PREVIEW_ASYNC=1`, levantá también el worker de previews:

    python worker_preview.py

### 2.1 Tests backend (pytest)

Bootstrap mínimo de testing:

    python -m venv .venv
    source .venv/bin/activate
    python -m pip install -r requirements.txt

Ejecutar tests backend:

    ./.venv/bin/python -m pytest tests/backend -q

---

### 3. Frontend (React + Vite)

Ir a la carpeta `frontend`:

    cd frontend
    npm install   # o pnpm install

Correr en modo dev:

    npm run dev

El frontend queda en [http://localhost:5173](http://localhost:5173) y se conecta al backend.

Para compilar versión productiva:

    npm run build

Los archivos compilados se sirven desde `frontend/dist/` por el backend Flask.

---

## 📡 Endpoints principales

- `POST /webhook` → recibe eventos de Mercado Libre, los guarda en `webhooks/` y responde `Evento recibido` (texto plano; con `DEBUG_WEBHOOK=1` devuelve JSON diagnóstico)
- `GET /api/webhooks` → devuelve todos los eventos agrupados por topic
- `GET /api/ml?resource=/items/{id}` → consulta la API de ML con token automático
- `GET /api/ml/render?resource=...` → muestra respuesta parseada en HTML
- `/` → frontend con visualizador de webhooks

### Lectura para consumidores de ingesta

Rutas pensadas para que otras apps lean de ML sin depender de `/api/ml/render`,
que es un proxy de lectura arbitraria y está marcado para cerrarse.

- `GET /api/ml/orders?resource=...` → órdenes y envíos. Acepta `/orders/search`,
  `/orders/{id}`, `/orders/{id}/discounts`, `/shipments/{id}`,
  `/shipments/{id}/costs`, `/shipments/{id}/items` y `/packs/{id}`. Devuelve el
  cuerpo de ML tal cual y preserva su status.
- `GET /api/ml/billing?resource=...` → facturación: períodos, documentos, detalle,
  resumen y detalle por orden (`/billing/integration/group/{ML|MP}/order/details`,
  que no necesita el período). Throttleado a una llamada cada 15s, porque el
  límite de la API de facturación (5/min) es **de la cuenta** y un consumidor que
  la llame por orden deja sin facturación al resto de la app.
- `GET /api/ml/payment?payment_id={id}` → neto liquidado y retenciones, desde
  Mercado Pago. La respuesta se **proyecta** a los montos: el pago crudo trae la
  tarjeta del comprador, su IP y sus datos de contacto.
- `GET /api/ml/activity?since={cursor}&topics=a,b&limit={n}` → qué ventas se
  movieron. Devuelve el **hecho**, no el contenido: `topic`, `order_id`,
  `pack_id`, `resource`, `sent` y `occurred_at`, y nada más. Ordená por `sent`
  (lo emite ML); `occurred_at` es cuándo lo registramos y sirve para medir el
  retraso del puente. El cursor es opaco: guardalo y devolvelo. Un cursor
  inválido da 400 en vez de devolver todo desde cero, que haría reprocesar el
  histórico como si fueran novedades.
  Tópicos del puente: `orders_v2`, `payments`, `shipments`, `post_purchase`,
  `messages`. `questions` no entra: es preventa, no tiene orden.
  Opcionalmente avisa por un ping sin payload (`ACTIVITY_PING_URL`), que es
  best-effort: si se pierde, el próximo pull lo levanta igual.
- `GET /api/ml/claims?resource=...` → reclamos y devoluciones, para saber por qué
  se canceló una venta. Acepta `/post-purchase/v1/claims/search` y
  `/post-purchase/v1/claims/{id}`. También se **proyecta**: quedan el motivo, el
  estado, las fechas y a qué orden aplica, sin `players[]`. Los subrecursos de
  conversación (`/messages`, `/attachments`) quedan afuera a propósito.

#### Paginar el detalle de facturación

**`offset` topea en 10.000** (`offset + limit`), y un período puede tener más de
22.000 cargos. Como vienen por fecha ascendente, lo que queda afuera es lo más
reciente. Paginar así:

```
.../details?document_type=BILL&limit=1000&from_id=0&sort_by=ID&order_by=ASC
.../details?document_type=BILL&limit=1000&from_id={last_id}&sort_by=ID&order_by=ASC
```

`from_id` toma el `last_id` que devuelve la respuesta anterior. Verificado: cero
solapamiento entre páginas. `sort=date_desc`, `order=desc`, `search_type=scan`,
`search_after` y `last_id` como parámetro devuelven 200 pero **se ignoran**, y
repiten la primera página.

Para mirar órdenes puntuales no hace falta paginar nada: el detalle acepta
`order_ids` (también `item_ids`, `document_ids`, `detail_ids`), y
`/billing/integration/group/{ML|MP}/order/details?order_ids=...` las pide sin
siquiera saber a qué período pertenecen.

> **El detalle no devuelve `paging`.** `total`, `limit`, `offset` y `last_id`
> vienen en el nivel superior de la respuesta, junto a `results`. Un consumidor
> que lea `paging.total` obtiene `None` siempre, y cualquier corte o chequeo de
> completitud basado en eso queda muerto en silencio.

#### Cómo se arma el neto de una venta

Verificado contra 514 pagos reales de la cuenta (487 `approved`, 20 `refunded`):

```
net_received_amount == transaction_amount + shipping_amount − Σ(cargos del vendedor)
```

Un cargo de `charges_details` **no** es del vendedor si cumple alguna de estas:

| Exclusión | Por qué |
|---|---|
| `type == "coupon"` | lo pone ML (su `supplier` trae `meli_campaign`, y `amounts.seller` es 0) |
| `type == "bonus"` | bonificación, no cargo |
| `name == "financing_fee"` | lo paga el comprador — **no** confundir con `financing_add_on_fee`, que sí es del vendedor |
| `"payer" in name` | `tax_withholding_payer-*`, retención del comprador |

Para una venta devuelta, el neto efectivo es **cero**, y se verifica así:

```
transaction_amount_refunded − Σ(refunded de cargos del vendedor) == net_received_amount
```

Sin ese contraste una venta cancelada se muestra como si hubiera dejado plata:
`net_received_amount` sigue siendo positivo en un pago `refunded`.

En `/orders/{id}/discounts`, **filtrar por `type` antes de leer `amounts`**: el
array `details` mezcla dos tipos con la misma forma y sentido opuesto.
`type: "coupon"` lo paga ML; `type: "discount"` trae `offer_id` y es una
promoción propia ya incluida en el `unit_price` — restarla sería contar dos
veces el mismo descuento.

> **Los clientes Python tienen que mandar un `User-Agent` explícito.**
> Hay Cloudflare adelante y bloquea la firma `Python-urllib/*` con
> `403 error code: 1010`. Verificado: `Python-urllib/3.14` → 403, mientras
> `curl/8.5.0`, `python-requests/2.32.5`, `Mozilla/5.0` y hasta el `User-Agent`
> vacío → 200. No es volumen ni el token: un 403 por credencial dice
> `PA_UNAUTHORIZED_RESULT_FROM_POLICIES`, no `error code: 1010`.
> `urllib.request` no manda `User-Agent` propio, así que hay que ponérselo.

---

## 📝 Notas

- Los eventos entrantes se guardan en la base de datos PostgreSQL (tabla `webhooks`).
- El token de acceso se refresca automáticamente usando el `ML_REFRESH_TOKEN`.
- El frontend soporta **modo oscuro/claro** con un botón flotante.
- Si `WEBHOOK_PREVIEW_ASYNC=1`, el endpoint `/webhook` encola previews y el procesamiento lo hace `worker_preview.py`.
- Las migraciones SQL de performance y snapshot están en `migrations/`.

---

## 📄 Licencia

MIT
