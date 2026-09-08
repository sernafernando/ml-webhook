-- Puente de actividad hacia los consumidores de ventas.
--
-- Guarda el HECHO de que una venta se movio, ya resuelto a que venta aplica.
-- No guarda contenido: los recursos que resuelven el vinculo traen la direccion
-- del comprador y el texto de las conversaciones, y para ordenar por "ultima
-- actividad" alcanza con saber que paso algo, cuando y sobre que.
CREATE TABLE IF NOT EXISTS ml_activity (
    id          BIGSERIAL PRIMARY KEY,
    -- El _id del evento de ML. Hace la ingesta idempotente: ML reintenta y el
    -- mismo evento puede llegar dos veces.
    webhook_id  TEXT UNIQUE,
    topic       TEXT NOT NULL,
    resource    TEXT NOT NULL,
    -- Uno de los dos puede ser NULL: messages resuelve al pack y no a la orden,
    -- y un vinculo que no se pudo resolver no invalida el evento.
    order_id    BIGINT,
    pack_id     BIGINT,
    -- sent lo emite ML y es contra lo que el consumidor aplica su guarda de
    -- secuencia; occurred_at es cuando lo registramos, y sirve para medir el
    -- retraso del puente, no para ordenar.
    sent        TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- El endpoint pagina por id ascendente, con o sin filtro de topic.
CREATE INDEX IF NOT EXISTS idx_ml_activity_id ON ml_activity (id);
CREATE INDEX IF NOT EXISTS idx_ml_activity_topic_id ON ml_activity (topic, id);

-- Para que un consumidor busque la actividad de una venta puntual.
CREATE INDEX IF NOT EXISTS idx_ml_activity_order ON ml_activity (order_id) WHERE order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ml_activity_pack ON ml_activity (pack_id) WHERE pack_id IS NOT NULL;

-- mluser es el rol de runtime: el handler de /webhook escribe aca. Sin el GRANT
-- el INSERT falla y, como corre dentro del mismo camino que el webhook, se
-- pierde la actividad en silencio. Mismo motivo que en webhook_latest.
GRANT SELECT, INSERT, UPDATE, DELETE ON ml_activity TO mluser;
GRANT USAGE, SELECT ON SEQUENCE ml_activity_id_seq TO mluser;
