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

Ejecutar backend:

    python app.py
Por defecto corre en: [http://localhost:3000](http://localhost:3000)

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

- `POST /webhook` → recibe eventos de Mercado Libre y los guarda en `webhooks/`
- `GET /api/webhooks` → devuelve todos los eventos agrupados por topic
- `GET /api/ml?resource=/items/{id}` → consulta la API de ML con token automático
- `GET /api/ml/render?resource=...` → muestra respuesta parseada en HTML
- `/` → frontend con visualizador de webhooks

---

## 📝 Notas

- Los eventos entrantes se guardan en la carpeta `webhooks/` y en `last_webhook.json`.
- El token de acceso se refresca automáticamente usando el `ML_REFRESH_TOKEN`.
- El frontend soporta **modo oscuro/claro** con un botón flotante.

---

## 📄 Licencia

MIT
