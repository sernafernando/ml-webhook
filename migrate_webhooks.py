import os
import json
import re
import psycopg2
from psycopg2.extras import Json
from dotenv import load_dotenv

# cargar variables de entorno (.env con DATABASE_URL)
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
WEBHOOKS_DIR = os.path.join(os.path.dirname(__file__), "webhooks")

def extract_json_objects(text):
    """Devuelve todos los objetos JSON válidos dentro de un texto."""
    decoder = json.JSONDecoder()
    objs = []
    idx = 0
    while idx < len(text):
        try:
            obj, pos = decoder.raw_decode(text[idx:])
            objs.append(obj)
            idx += pos
        except json.JSONDecodeError:
            idx += 1
    return objs

def migrate():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()

    count = 0
    for fname in sorted(os.listdir(WEBHOOKS_DIR)):
        if fname.endswith(".json"):
            path = os.path.join(WEBHOOKS_DIR, fname)
            try:
                with open(path) as f:
                    text = f.read().strip()
                    objects = extract_json_objects(text)

                if not objects:
                    print(f"❌ {fname}: no se encontraron objetos JSON válidos")
                    continue

                for data in objects:
                    if not isinstance(data, dict):
                        print(f"⚠️ Saltando fragmento no-dict en {fname}: {type(data).__name__}")
                        continue

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
                    count += 1
                    print(f"✅ Insertado desde {fname}")

                # borrar archivo después de insertar todos sus objetos
                os.remove(path)

            except Exception as e:
                print(f"❌ Error con {fname}: {e}")

    cur.close()
    conn.close()
    print(f"\n📦 Migración completa. Total insertados y borrados: {count}")

if __name__ == "__main__":
    migrate()
