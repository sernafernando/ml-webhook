import { useEffect, useState } from 'react';
import './App.css';
import { FaMoon, FaSun } from 'react-icons/fa';

function App() {
  const [theme, setTheme] = useState('dark');
  const [filter, setFilter] = useState('');
  const [limit, setLimit] = useState(500);
  const [offset, setOffset] = useState(0);
  const [topics, setTopics] = useState([]);
  const [selectedTopic, setSelectedTopic] = useState(
    localStorage.getItem("selectedTopic") || null
  );
  const [events, setEvents] = useState([]);
  const [pagination, setPagination] = useState({ limit: 500, offset: 0, total: 0 });
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

  const toggleTheme = () => {
    setTheme(prev => (prev === 'dark' ? 'light' : 'dark'));
  };

  // filtro por resource
  const eventosFiltrados = events.filter(
    evt => filter === '' || (evt.resource && evt.resource.includes(filter))
  );

  return (
    <div className={`App ${theme}-theme`}>
      <h1 className="app-title">📦 Webhooks Recibidos</h1>

      {/* selector de topic */}
      {topics.length > 0 && (
        <div style={{ marginBottom: '1rem' }}>
          <label style={{ marginRight: '1rem' }}>Topic:</label>
          <select
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

          {/* filtro */}
          <input
            type="text"
            placeholder="Filtrar por resource..."
            value={filter}
            onChange={e => setFilter(e.target.value)}
            style={{ marginLeft: '1rem' }}
          />
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
                <th>user_id</th>
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
                  <td>{evt.user_id}</td>
                  <td>{evt.resource}</td>
                  <td>
                    <details>
                      <summary>Ver</summary>
                      <pre>{JSON.stringify(evt, null, 2)}</pre>
                    </details>
                  </td>
                  <td>
                    {evt.preview && evt.preview.title ? (
                      <div className="d-flex align-items-center gap-2">
                        <img
                          src={evt.preview.thumbnail?.replace(/^http:\/\//i, "https://")}
                          alt={evt.preview.title}
                          style={{ width: '50px', height: '50px', objectFit: 'cover' }}
                        />
                        <div>
                          <strong>{evt.preview.title}</strong><br />
                          {evt.preview.currency_id} {evt.preview.price}

                          {evt.preview.status && (
                            <div className="mt-1">
                              <span className="badge bg-info text-dark">{evt.preview.status}</span>
                            </div>
                          )}

                          {evt.preview.winner && (
                            <div className="mt-1">
                              🏆 Ganador: {evt.preview.winner} ({evt.preview.currency_id} {evt.preview.winner_price})
                            </div>
                          )}
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

                    {evt.preview && evt.preview.title ? (
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
