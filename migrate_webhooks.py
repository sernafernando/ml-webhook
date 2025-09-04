import os
import json
import psycopg2
from psycopg2.extras import Json
from dotenv import load_dotenv

# cargar variables de entorno (.env con DATABASE_URL)
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
WEBHOOKS_DIR = os.path.join(os.path.dirname(__file__), "webhooks")

def load_json_tolerant(path):
    """Intenta cargar JSON, recorta basura extra si la hay."""
    with open(path) as f:
        text = f.read().strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            if "Extra data" in str(e):
                cut = text.rfind("}")
                if cut != -1:
                    try:
                        return json.loads(text[:cut+1])
                    except Exception as inner_e:
                        print(f"❌ {path}: incluso recortado sigue mal -> {inner_e}")
                        raise inner_e
            raise e

def migrate():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()

    count = 0
    for fname in sorted(os.listdir(WEBHOOKS_DIR)):
        if fname.endswith(".json"):
            path = os.path.join(WEBHOOKS_DIR, fname)
            try:
                data = load_json_tolerant(path)

                topic = data.get("topic", "otros")
                user_id = data.get("user_id")
                resource = data.get("resource")

                cur.execute(
                    """
                    INSERT INTO webhooks (topic, user_id, resource, payload)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (topic, user_id, resource, Json(data)),
                )

                os.remove(path)  # borrar archivo después de insertar
                count += 1
                print(f"✅ Insertado y borrado: {fname}")

            except Exception as e:
                print(f"❌ Error con {fname}: {e}")

    cur.close()
    conn.close()
    print(f"\n📦 Migración completa. Total insertados y borrados: {count}")

if __name__ == "__main__":
    migrate()
