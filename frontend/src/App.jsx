import { useEffect, useState } from 'react';
import './App.css';
import { FaMoon, FaSun } from 'react-icons/fa';

function App() {
  const [theme, setTheme] = useState('dark');
  const [filter, setFilter] = useState('');
  const [limit, setLimit] = useState(5000);
  const [offset, setOffset] = useState(0);
  const [topics, setTopics] = useState([]);
  const [selectedTopic, setSelectedTopic] = useState(
    localStorage.getItem("selectedTopic") || null
  );
  const [events, setEvents] = useState([]);
  const [pagination, setPagination] = useState({ limit: 5000, offset: 0, total: 0 });
  const [loadingPreview, setLoadingPreview] = useState({});

  // tema
  useEffect(() => {
    const savedTheme = localStorage.getItem('theme') || 'dark';
    setTheme(savedTheme);
  }, []);

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('theme', theme);
  }, [theme]);

  useEffect(() => {
    if (selectedTopic) {
      localStorage.setItem("selectedTopic", selectedTopic);
    }
  }, [selectedTopic]);

  // cargar topics
  useEffect(() => {
    const fetchTopics = async () => {
      try {
        const res = await fetch("/api/webhooks/topics");
        const data = await res.json();
        setTopics(data);

        // si lo que hay en localStorage no está en la lista, elegir el primero
        if ((!selectedTopic || !data.some(t => t.topic === selectedTopic)) && data.length > 0) {
          setSelectedTopic(data[0].topic);
        }
      } catch (err) {
        console.error("Error al cargar topics:", err);
      }
    };
    fetchTopics();
  }, []); // solo al montar

  // cargar eventos del topic seleccionado
  useEffect(() => {
    const fetchEvents = async () => {
      if (!selectedTopic) return;
      try {
        const res = await fetch(`/api/webhooks?topic=${selectedTopic}&limit=${limit}&offset=${offset}`);
        const data = await res.json();
        setEvents(data.events || []);
        setPagination(data.pagination || { limit, offset, total: 0 });
      } catch (err) {
        console.error("Error al cargar eventos:", err);
      }
    };

    fetchEvents();
    const interval = setInterval(fetchEvents, 5000);
    return () => clearInterval(interval);
  }, [selectedTopic, limit, offset]);

  const fmtARS = (val) => {
    if (val === null || val === undefined || val === "") return "—";
    const n = Number(val);
    if (isNaN(n)) return val;
    return "$" + n.toLocaleString("es-AR", { minimumFractionDigits: 2 });
  };

  const toggleTheme = () => {
    setTheme(prev => (prev === 'dark' ? 'light' : 'dark'));
  };
  
  // 👇 helper para buscar dentro de cualquier campo del preview
  function deepIncludes(obj, needle, depth = 2, seen = new WeakSet()) {
    if (!needle) return true;
    if (obj == null) return false;
    const t = typeof obj;
    if (t === "string" || t === "number" || t === "boolean") {
      return String(obj).toLowerCase().includes(needle);
    }
    if (obj instanceof Date) {
      return obj.toISOString().toLowerCase().includes(needle);
    }
    if (depth <= 0) return false;
    if (Array.isArray(obj)) {
      return obj.some(v => deepIncludes(v, needle, depth - 1, seen));
    }
    if (t === "object") {
      if (seen.has(obj)) return false;
      seen.add(obj);
      return Object.values(obj).some(v => deepIncludes(v, needle, depth - 1, seen));
    }
    try {
      return String(obj).toLowerCase().includes(needle);
    } catch {
      return false;
    }
  }
  // filtro por resource
  const needle = (filter || "").trim().toLowerCase();

  const eventosFiltrados = Array.isArray(events) ? events.filter(evt => {
    if (!needle) return true;

    try {
      const resourceHit = (evt?.resource || "").toLowerCase().includes(needle);
      const pv = evt?.db_preview ?? evt?.preview ?? {};
      const previewHit = deepIncludes(pv, needle, 2);

      return resourceHit || previewHit;
    } catch (e) {
      console.error("Filtro: error evaluando evento", e, evt);
      return true; // fallback: no ocultar
    }
  }) : [];

  const topicLabels = {
    "items": "🛒 Publicaciones",
    "shipments": "🚚 Envíos",
    "orders_v2": "📦 Órdenes",
    "price_suggestion": "💡 Sugerencias de precio",
    "payments": "💳 Pagos",
    "items_prices": "💲 Precios de Items",
    "stock-locations": "🏬 Depósitos",
    "public_offers": "📢 Ofertas públicas",
    "public_candidates": "📝 Candidatos públicos",
    "orders_feedback": "⭐ Feedback de órdenes",
    "flex-handshakes": "⚡ Flex Handshakes",
    "post_purchase": "🔄 Post-compra",
    "messages": "✉️ Mensajes",
    "catalog_item_competition_status": "📊 Competencia de catálogo",
    "user-products-families": "👨‍👩‍👧 Familias de productos",
    "questions": "❓ Preguntas",
    "fbm_stock_operations": "📦 Operaciones FBM",
  };

  const getStatusBadge = (status) => {
    const map = {
      winning: { className: "bg-success", label: "Ganando" },
      sharing_first_place: { className: "bg-warning text-dark", label: "Compartiendo primer lugar" },
      competing: { className: "bg-danger", label: "Perdiendo" },
      not_listed: { className: "bg-secondary", label: "No listado" },
    };
    const m = map[status] || { className: "bg-secondary", label: status };
    return <span className={`badge ${m.className}`}>{m.label}</span>;
  };

  return (
    <div className={`App ${theme}-theme`}>
      <h1 className="app-title">📦 Webhooks Recibidos</h1>

      {/* selector de topic + filtro */}
      {topics.length > 0 && (
        <div className="mb-3">
          <div className="row g-2 align-items-center">
            <div className="col-md-6">
              <label className="form-label fw-bold">Topic</label>
              <select
                className="form-select"
                value={selectedTopic || ""}
                onChange={e => { 
                  setSelectedTopic(e.target.value); 
                  setOffset(0); 
                }}
              >
                {topics.map(t => (
                  <option key={t.topic} value={t.topic}>
                    {topicLabels[t.topic] || t.topic} ({t.count})
                  </option>
                ))}
              </select>
            </div>
            <div className="col-md-6">
              <label className="form-label fw-bold">Filtrar por resource</label>
              <div className="input-group">
                <span className="input-group-text">🔍</span>
                <input
                  type="text"
                  className="form-control"
                  placeholder="Ej: MLA123..."
                  value={filter}
                  onChange={e => setFilter(e.target.value)}
                />
              </div>
            </div>
          </div>
        </div>
      )}


      {/* tabla del topic seleccionado */}
      {selectedTopic && (
        <section style={{ marginBottom: '2rem' }}>
          <h2>🔹 Topic: {topicLabels[selectedTopic] || selectedTopic}</h2>
          <table className="webhook-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Timestamp</th>
                <th>resource</th>
                <th>Raw JSON</th>
                <th>Preview</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {eventosFiltrados.map((evt, i) => (
                <tr key={i}>
                  <td>{i + 1 + pagination.offset}</td>
                  <td>{evt.received_at || "-"}</td>
                  <td>{evt.resource}</td>
                  <td>
                    <details>
                      <summary>Ver</summary>
                      <pre>{JSON.stringify(evt, null, 2)}</pre>
                    </details>
                  </td>
                  <td>
                    {evt.db_preview && evt.db_preview.title ? (
                    <div className="d-flex align-items-center gap-2">
                      <img
                        src={evt.db_preview.thumbnail?.replace(/^http:\/\//i, "https://")}
                        alt={evt.db_preview.title}
                        style={{ width: '50px', height: '50px', objectFit: 'cover' }}
                      />
                      <div>
                        <strong>{evt.db_preview.title}</strong><br />

                        {/* ⬇️ NUEVO: mostrar la marca si viene */}
                        {evt.db_preview.brand ? (
                          <>
                            <small className="text-light">Marca: {evt.db_preview.brand}</small><br />
                          </>
                        ) : null}

                        {evt.db_preview.currency_id} {evt.db_preview.price}

                        {/* badge de estado */}
                        {evt.db_preview.status && (
                          <div className="mt-1">
                            {getStatusBadge(evt.db_preview.status)}
                          </div>
                        )}

                        {/* línea de ganador */}
                        {evt.db_preview && evt.db_preview.winner ? (
                          <div className="ptw-line">
                            {evt.db_preview.winner_url ? (
                              <>
                                🏆 Ganador:{" "}
                                <a href={evt.db_preview.winner_url} target="_blank" rel="noopener noreferrer">
                                  {evt.db_preview.winner}
                                </a>{" "}
                                — {evt.db_preview.winner_price_fmt ?? fmtARS(evt.db_preview.winner_price)}
                              </>
                            ) : (
                              <>🏆 Ganador: {evt.db_preview.winner} — {fmtARS(evt.db_preview.winner_price)}</>
                            )}
                          </div>
                        ) : null}
                      </div>
                    </div>
                  ) : (
                    <em>-</em>
                  )}
                  </td>
                  <td>
                    <button
                      className="btn btn-sm btn-outline-primary me-2"
                      onClick={() =>
                        window.open(
                          `/api/ml/render?resource=${encodeURIComponent(evt.resource)}`,
                          '_blank'
                        )
                      }
                    >
                      Ver detalle
                    </button>

                    {evt.db_preview && evt.db_preview.title ? (
                      <button
                        className="btn btn-sm btn-outline-info"
                        onClick={async () => {
                          setLoadingPreview(prev => ({ ...prev, [evt.resource]: true }));
                          await fetch(`/api/ml/preview?resource=${encodeURIComponent(evt.resource)}`, { method: "POST" });
                          const res = await fetch(`/api/webhooks?topic=${selectedTopic}&limit=${limit}&offset=${offset}`);
                          const data = await res.json();
                          setEvents(data.events || []);
                          setPagination(data.pagination || { limit, offset, total: 0 });
                          setLoadingPreview(prev => ({ ...prev, [evt.resource]: false }));
                        }}
                        disabled={loadingPreview[evt.resource]}
                      >
                        {loadingPreview[evt.resource] ? (
                          <span>
                            <span className="spinner-border spinner-border-sm me-1" role="status"></span>
                            Refrescando...
                          </span>
                        ) : (
                          "🔄 Refrescar"
                        )}
                      </button>
                    ) : (
                      <button
                        className="btn btn-sm btn-outline-secondary"
                        onClick={async () => {
                          setLoadingPreview(prev => ({ ...prev, [evt.resource]: true }));
                          await fetch(`/api/ml/preview?resource=${encodeURIComponent(evt.resource)}`, { method: "POST" });
                          const res = await fetch(`/api/webhooks?topic=${selectedTopic}&limit=${limit}&offset=${offset}`);
                          const data = await res.json();
                          setEvents(data.events || []);
                          setPagination(data.pagination || { limit, offset, total: 0 });
                          setLoadingPreview(prev => ({ ...prev, [evt.resource]: false }));
                        }}
                        disabled={loadingPreview[evt.resource]}
                      >
                        {loadingPreview[evt.resource] ? (
                          <span>
                            <span className="spinner-border spinner-border-sm me-1" role="status"></span>
                            Generando...
                          </span>
                        ) : (
                          "🔄 Generar"
                        )}
                      </button>
                    )}
                  </td>

                </tr>
              ))}
            </tbody>
          </table>

          <div className="d-flex justify-content-between align-items-center my-3">
            {/* Selector de límite */}
            <div className="d-flex align-items-center">
              <label className="me-2 fw-bold">Ver últimos:</label>
              <select
                className="form-select form-select-sm"
                style={{ width: "auto" }}
                value={limit}
                onChange={e => { setOffset(0); setLimit(Number(e.target.value)); }}
              >
                <option value={100}>100</option>
                <option value={500}>500</option>
                <option value={1000}>1000</option>
                <option value={5000}>5000</option>
              </select>
            </div>

            {/* Paginación Bootstrap */}
            <nav>
              <ul className="pagination pagination-sm mb-0">
                <li className={`page-item ${offset === 0 ? "disabled" : ""}`}>
                  <button
                    className="page-link"
                    onClick={() => setOffset(Math.max(0, offset - limit))}
                  >
                    ⬅️ Anterior
                  </button>
                </li>
                <li className={`page-item ${offset + limit >= pagination.total ? "disabled" : ""}`}>
                  <button
                    className="page-link"
                    onClick={() => setOffset(offset + limit)}
                  >
                    Siguiente ➡️
                  </button>
                </li>
              </ul>
            </nav>

            {/* Info de rango */}
            <div>
              <span className="badge bg-secondary">
                Mostrando {pagination.offset + 1} - {Math.min(pagination.offset + limit, pagination.total)} de {pagination.total}
              </span>
            </div>
          </div>


        </section>
      )}

      <button className="theme-floating-button" onClick={toggleTheme}>
        {theme === 'dark' ? <FaSun /> : <FaMoon />}
      </button>
    </div>
  );
}

export default App;
