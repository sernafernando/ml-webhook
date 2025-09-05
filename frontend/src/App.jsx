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

  // filtro por resource
  const eventosFiltrados = events.filter(
    evt => filter === '' || (evt.resource && evt.resource.includes(filter))
  );

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
                    {t.topic} ({t.count})
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
          <h2>🔹 Topic: {selectedTopic}</h2>
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
                          {evt.db_preview.currency_id} {evt.db_preview.price}

                          {/* ⬇️ reemplazo de tu badge actual */}
                          {evt.db_preview.status && (
                            <div className="mt-1">
                              {getStatusBadge(evt.db_preview.status)}
                            </div>
                          )}

                          {evt.db_preview && evt.db_preview.winner ? (
                            <div className="ptw-line">
                              {evt.db_preview.winner_url ? (
                                <>
                                  🏆 Ganador:{" "}
                                  <a href={evt.db_preview.winner_url} target="_blank" rel="noopener noreferrer">
                                    {evt.db_preview.winner}
                                  </a>{" "}
                                  — {evt.db_preview.winner_price_fmt}
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

          {/* controles de paginación */}
          <div className="controls">
            <label>
              Ver últimos:
              <select value={limit} onChange={e => { setOffset(0); setLimit(Number(e.target.value)); }}>
                <option value={100}>100</option>
                <option value={500}>500</option>
                <option value={1000}>1000</option>
                <option value={5000}>5000</option>
              </select>
            </label>
            <button 
              disabled={offset === 0} 
              onClick={() => setOffset(Math.max(0, offset - limit))}
            >
              ⬅️ Anterior
            </button>
            <button 
              disabled={offset + limit >= pagination.total} 
              onClick={() => setOffset(offset + limit)}
            >
              ➡️ Siguiente
            </button>
            <span style={{ marginLeft: '1rem' }}>
              Mostrando {pagination.offset + 1} - {Math.min(pagination.offset + limit, pagination.total)} 
              de {pagination.total}
            </span>
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
