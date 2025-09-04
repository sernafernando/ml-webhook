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

    <pre>```git clone https://github.com/tuusuario/ml-webhook.git
    cd ml-webhook```</pre>

### 2. Backend (Flask)

<pre>```Crear y activar entorno virtual:

    python -m venv .venv
    source .venv/bin/activate   # Linux/Mac
    .venv\Scripts\activate      # Windows ```</pre>

Instalar dependencias:

<pre>```pip install -r requirements.txt```</Pre>

Variables de entorno en un archivo `.env`:

<pre>```
    ML_CLIENT_ID=tu_client_id
    ML_CLIENT_SECRET=tu_client_secret
    ML_REDIRECT_URI=https://tuservidor.com/callback
    ML_REFRESH_TOKEN=tu_refresh_token
    PORT=3000
```</pre>
Ejecutar backend:

<pre>```    python app.py```</pre>

Por defecto corre en: [http://localhost:3000](http://localhost:3000)

---

### 3. Frontend (React + Vite)

Ir a la carpeta `frontend`:

<pre>```    cd frontend
    npm install   # o pnpm install```</pre>

Correr en modo dev:

<pre>```    npm run dev```</pre>

El frontend queda en [http://localhost:5173](http://localhost:5173) y se conecta al backend.

Para compilar versión productiva:

<pre>```    npm run build```</pre>

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
