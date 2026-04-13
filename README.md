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
