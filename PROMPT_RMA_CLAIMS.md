# CONTEXTO: Consumir Claims de MercadoLibre para el sistema RMA

Tenemos un servicio webhook (`ml-webhook`) que recibe notificaciones de MercadoLibre (topics: `claims`, `claims_actions`) y almacena previews enriquecidos en PostgreSQL. Los datos de claims ya están procesados y listos para ser consumidos por el sistema de RMA.

---

## OPCIÓN 1: Consumo directo desde PostgreSQL (RECOMENDADO)

Ambos sistemas comparten la misma base de datos PostgreSQL.

**Conexión:**
```
postgresql://mluser:GaussDB1214@localhost:5432/mlwebhook
```

### Tablas relevantes

**`webhooks`** — Cada notificación cruda recibida de ML:
| Columna | Tipo | Descripción |
|---------|------|-------------|
| `id` | serial PK | ID interno auto-incremental |
| `topic` | text | Topic de la notificación (`claims`, `claims_actions`, `items`, `shipments`, etc.) |
| `user_id` | bigint | User ID de ML del vendedor |
| `resource` | text | Path del recurso (ej: `/post-purchase/v1/claims/5281510459`) |
| `payload` | jsonb | JSON crudo del webhook tal como llegó de ML |
| `webhook_id` | uuid | ID único del webhook (dedup con `ON CONFLICT`) |
| `received_at` | timestamptz | Fecha/hora de recepción |

**`ml_previews`** — Preview enriquecido con datos extraídos de la API de ML:
| Columna | Tipo | Descripción |
|---------|------|-------------|
| `resource` | text PK | Path del recurso (misma key que `webhooks.resource`) |
| `title` | text | Para claims: texto legible del motivo exacto de la reason API |
| `price` | numeric | No aplica para claims (null) |
| `currency_id` | text | No aplica para claims (null) |
| `thumbnail` | text | No aplica para claims (null) |
| `winner` | text | No aplica para claims (null) |
| `winner_price` | numeric | No aplica para claims (null) |
| `status` | text | Estado del claim: `opened` o `closed` |
| `brand` | text | No aplica para claims (null) |
| `extra_data` | jsonb | **ACA ESTÁ TODO LO QUE IMPORTA PARA RMA** (ver estructura abajo) |
| `last_updated` | timestamptz | Última actualización del preview |

### Query para obtener claims con datos de RMA

```sql
-- Claims abiertos, más recientes primero
SELECT
    w.resource,
    w.received_at,
    p.title AS motivo_legible,
    p.status AS claim_status,
    p.extra_data->>'claim_id' AS claim_id,
    p.extra_data->>'claim_type' AS claim_type,
    p.extra_data->>'claim_stage' AS claim_stage,
    p.extra_data->>'resource_type' AS resource_type,
    p.extra_data->>'resource_id' AS resource_id,
    p.extra_data->>'reason_id' AS reason_id,
    p.extra_data->>'reason_category' AS reason_category,
    p.extra_data->>'reason_detail' AS reason_detail,
    p.extra_data->>'reason_name' AS reason_name,
    p.extra_data->'triage_tags' AS triage_tags,
    p.extra_data->'expected_resolutions' AS expected_resolutions,
    p.extra_data->>'fulfilled' AS fulfilled,
    p.extra_data->>'quantity_type' AS quantity_type,
    p.extra_data->>'claimed_quantity' AS claimed_quantity,
    p.extra_data->>'complainant_user_id' AS buyer_id,
    p.extra_data->>'respondent_user_id' AS seller_id,
    p.extra_data->'seller_actions' AS seller_actions,
    p.extra_data->'mandatory_actions' AS mandatory_actions,
    p.extra_data->>'nearest_due_date' AS nearest_due_date,
    p.extra_data->>'detail_problem' AS detail_problem,
    p.extra_data->>'action_responsible' AS action_responsible,
    p.extra_data->>'detail_due_date' AS detail_due_date,
    p.extra_data->>'resolution_reason' AS resolution_reason,
    p.extra_data->>'resolution_closed_by' AS resolution_closed_by,
    p.extra_data->>'date_created' AS claim_created,
    p.extra_data->>'last_updated' AS claim_updated
FROM webhooks w
JOIN ml_previews p ON p.resource = w.resource
WHERE w.topic IN ('claims', 'claims_actions')
  AND w.resource LIKE '/post-purchase/v1/claims/%'
ORDER BY w.received_at DESC;
```

### Query solo claims abiertos con acciones urgentes

```sql
SELECT
    p.extra_data->>'claim_id' AS claim_id,
    p.title AS motivo,
    p.extra_data->>'claim_stage' AS stage,
    p.extra_data->'mandatory_actions' AS acciones_obligatorias,
    p.extra_data->>'nearest_due_date' AS vence,
    p.extra_data->>'action_responsible' AS responsable,
    p.extra_data->'triage_tags' AS triage,
    p.extra_data->'expected_resolutions' AS resoluciones_esperadas,
    p.extra_data->>'resource_id' AS order_id
FROM ml_previews p
WHERE p.resource LIKE '/post-purchase/v1/claims/%'
  AND p.status = 'opened'
  AND p.extra_data->'mandatory_actions' != '[]'::jsonb
ORDER BY p.extra_data->>'nearest_due_date' ASC;
```

---

## OPCIÓN 2: Consumo vía HTTP API

Si se necesita acceso remoto o desde otro servidor:

```
GET https://ml-webhook.gaussonline.com.ar/api/webhooks?topic=claims&limit=100&offset=0
```

Respuesta:
```json
{
  "topic": "claims",
  "events": [ ... ],
  "pagination": { "limit": 100, "offset": 0, "total": 42 }
}
```

Cada elemento de `events` tiene:
```json
{
  "resource": "/post-purchase/v1/claims/5281510459",
  "topic": "claims",
  "user_id": 413658225,
  "received_at": "2026-03-04 14:30:00",
  "db_preview": {
    "title": "Llegó lo que compré en buenas condiciones pero no lo quiero",
    "status": "opened",
    "extra_data": { ... }
  }
}
```

### Renderizado visual de un claim (HTML)

```
GET https://ml-webhook.gaussonline.com.ar/api/ml/render?resource=/post-purchase/v1/claims/5281510459
```

### FALLBACK para claims viejos sin `extra_data` — Consulta JSON en tiempo real

Los claims que llegaron ANTES de este deploy no tienen `extra_data` enriquecido en `ml_previews`. Para esos casos, podés consultar la API de ML en tiempo real a través del endpoint de render usando el parámetro `format=json`:

```
GET https://ml-webhook.gaussonline.com.ar/api/ml/render?resource=/post-purchase/v1/claims/5281510459&format=json
```

Esto devuelve el JSON crudo de la API de ML (sin renderizar HTML), con la misma estructura que devuelve `GET https://api.mercadolibre.com/post-purchase/v1/claims/{claim_id}`. El token de autenticación lo maneja el webhook internamente, así que no necesitás preocuparte por eso.

**Lógica sugerida para el sistema RMA:**
1. Primero intentá leer de `ml_previews.extra_data` (DB directa, opción 1)
2. Si `extra_data` es `null`, vacío, o no tiene los campos de claims (`claim_id`, `triage_tags`, etc.), hacé el fallback al endpoint HTTP con `&format=json` para obtener los datos en tiempo real
3. Los campos del JSON crudo de ML son los mismos que se documentan en la sección "ESTRUCTURA COMPLETA DE extra_data", pero vienen en la estructura original de la API de ML (ver campos `id`, `status`, `type`, `stage`, `reason_id`, `players`, `resolution`, etc.)

---

## ESTRUCTURA COMPLETA DE `extra_data` (jsonb)

Esto es lo que el webhook procesa por cada claim. Son 3 llamadas a la API de ML combinadas:

```jsonc
{
  // === IDENTIFICACIÓN DEL CLAIM ===
  "claim_id": 5281510459,
  "claim_type": "mediations",       // mediations | return | fulfillment | ml_case | cancel_sale | cancel_purchase | change | service
  "claim_stage": "claim",           // claim | dispute | recontact | stale | none
  "claim_version": 2.0,

  // === RECURSO RECLAMADO ===
  "resource_type": "order",         // order | shipment | payment | purchase
  "resource_id": 2000007819609432,  // ID de la orden/envío/pago reclamado

  // === MOTIVO — CLAVE PARA RMA ===
  "reason_id": "PDD9939",                                                          // Código ML
  "reason_category": "Producto Diferente o Defectuoso",                             // Categoría general (PNR/PDD/CS)
  "reason_detail": "Llegó lo que compré en buenas condiciones pero no lo quiero",   // Texto EXACTO de la API de reasons — USAR ESTE COMO PRIMERA INSTANCIA
  "reason_name": "repentant_buyer",                                                 // Nombre interno de ML
  "reason_label": "Llegó lo que compré en buenas condiciones pero no lo quiero",    // = reason_detail si existe, sino reason_category

  // === CLASIFICACIÓN AUTOMÁTICA PARA RMA ===
  "triage_tags": ["repentant"],                             // Tags del engine de ML para categorizar
  "expected_resolutions": ["change_product", "return_product"],  // Qué espera el comprador

  // Posibles triage_tags:
  //   "repentant"   → Arrepentimiento (producto OK pero no lo quiere)
  //   "defective"   → Defectuoso / problema de fábrica
  //   "not_working" → No funciona
  //   "different"   → Producto diferente al publicado
  //   "incomplete"  → Incompleto / faltan piezas

  // Posibles expected_resolutions:
  //   "return_product"  → Devolución física
  //   "change_product"  → Cambio de producto
  //   "refund"          → Reembolso
  //   "product"         → Genérico
  //   "other"           → Otro

  // === ESTADO DE ENTREGA ===
  "fulfilled": true,               // true = producto entregado, false = no entregado
  "quantity_type": "total",         // total | partial
  "claimed_quantity": 1,

  // === PLAYERS ===
  "complainant_user_id": 1325224382,   // Buyer ML user ID
  "complainant_type": "buyer",
  "respondent_user_id": 1330467461,    // Seller ML user ID
  "respondent_type": "seller",

  // === ACCIONES PENDIENTES DEL SELLER ===
  "seller_actions": ["refund", "send_message_to_complainant", "open_dispute", "allow_return"],
  "mandatory_actions": ["refund"],                          // Acciones OBLIGATORIAS
  "nearest_due_date": "2026-03-06T14:30:00.000-03:00",     // Fecha límite más próxima

  // === DETAIL LEGIBLE (del endpoint /claims/{id}/detail) ===
  "detail_title": "El comprador quiere devolver el producto",
  "detail_description": "Tenés hasta el viernes 6 de marzo para responder.",
  "detail_problem": "Nos dijeron que el producto llegó en buenas condiciones pero no lo quieren",
  "action_responsible": "seller",       // seller | buyer | mediator
  "detail_due_date": "2026-03-06T22:33:00.000-04:00",

  // === RESOLUCIÓN (solo cuando status = "closed") ===
  "resolution_reason": "payment_refunded",      // Cómo se resolvió
  "resolution_date": "2026-03-05T10:00:00.000-03:00",
  "resolution_benefited": ["complainant"],       // Quién salió beneficiado
  "resolution_closed_by": "seller",              // Quién cerró: seller | buyer | mediator
  "resolution_coverage": false,                  // Si ML aplicó cobertura

  // Posibles resolution_reason:
  //   payment_refunded, item_returned, prefered_to_keep_product, partial_refunded,
  //   opened_claim_by_mistake, worked_out_with_seller, seller_sent_product,
  //   seller_explained_functions, already_shipped, respondent_timeout,
  //   coverage_decision, item_changed, change_expired, low_cost, ...

  // === FECHAS ===
  "date_created": "2026-03-04T08:28:44.000-04:00",
  "last_updated": "2026-03-04T14:30:00.000-04:00",
  "site_id": "MLA"
}
```

---

## LÓGICA DE CLASIFICACIÓN SUGERIDA PARA RMA

Para asignar automáticamente el tipo de RMA, usá esta prioridad:

### 1. `triage_tags` → Lo más específico (viene del engine de ML)
| Tag | Tipo RMA sugerido |
|-----|-------------------|
| `"defective"` | Garantía / Defecto de fábrica |
| `"not_working"` | No funciona |
| `"different"` | Producto equivocado |
| `"incomplete"` | Incompleto / Faltan piezas |
| `"repentant"` | Arrepentimiento (sin defecto) |

### 2. `expected_resolutions` → Acción esperada por el comprador
| Resolución | Acción RMA |
|------------|------------|
| `"return_product"` | Gestionar devolución física |
| `"change_product"` | Gestionar cambio de producto |
| `"refund"` | Gestionar reembolso |

### 3. `reason_category` → Fallback general
| Categoría | ¿Requiere RMA? |
|-----------|-----------------|
| PDD (Producto Diferente o Defectuoso) | ✅ Sí |
| PNR (Producto No Recibido) | ❌ No — es logístico |
| CS (Compra Cancelada) | ❌ No aplica |

### 4. `fulfilled` → Contexto de entrega
- `true` → Producto fue entregado, puede haber producto físico que gestionar
- `false` → Producto NO entregado, no hay RMA físico

### 5. `mandatory_actions` + `nearest_due_date` → Urgencia
- Si hay acciones obligatorias con fecha límite → el RMA es URGENTE
- El campo `action_responsible` dice quién tiene que actuar (`seller` = nosotros)
