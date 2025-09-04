import { useEffect, useState } from 'react';
import './App.css';
import { FaMoon, FaSun } from 'react-icons/fa';

function App() {
  const [webhooks, setWebhooks] = useState({});
  const [theme, setTheme] = useState('dark');
  const [selectedTopic, setSelectedTopic] = useState('');
  const [filter, setFilter] = useState('');

  useEffect(() => {
    const savedTheme = localStorage.getItem('theme') || 'dark';
    setTheme(savedTheme);
  }, []);

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('theme', theme);
  }, [theme]);

  useEffect(() => {
    const fetchWebhooks = async () => {
      try {
        const res = await fetch('/api/webhooks');
        const data = await res.json();
        setWebhooks(data);

        // si todavía no hay topic seleccionado, agarrar el primero
        if (!selectedTopic && Object.keys(data).length > 0) {
          setSelectedTopic(Object.keys(data)[0]);
        }
      } catch (err) {
        console.error("Error al cargar webhooks:", err);
      }
    };

    fetchWebhooks();
    const interval = setInterval(fetchWebhooks, 5000);
    return () => clearInterval(interval);
  }, [selectedTopic]);

  const toggleTheme = () => {
    setTheme(prev => (prev === 'dark' ? 'light' : 'dark'));
  };

  const topics = Object.keys(webhooks);
  const eventos = webhooks[selectedTopic] || [];
  const eventosFiltrados = eventos.filter(
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
            value={selectedTopic}
            onChange={e => setSelectedTopic(e.target.value)}
          >
            {topics.map(t => (
              <option key={t} value={t}>
                {t} ({webhooks[t].length})
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
              </tr>
            </thead>
            <tbody>
              {eventosFiltrados.map((evt, i) => (
                <tr key={i}>
                  <td>{i + 1}</td>
                  <td>{evt.user_id}</td>
                  <td>{evt.resource}</td>
                  <td>
                    <details>
                      <summary>Ver</summary>
                      <pre>{JSON.stringify(evt, null, 2)}</pre>
                    </details>
                    <button
                      className="ml-button"
                      onClick={() =>
                        window.open(
                          `/api/ml/render?resource=${encodeURIComponent(evt.resource)}`,
                          '_blank'
                        )
                      }
                    >
                      Ver detalle
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <button className="theme-floating-button" onClick={toggleTheme}>
        {theme === 'dark' ? <FaSun /> : <FaMoon />}
      </button>
    </div>
  );
}

export default App;
