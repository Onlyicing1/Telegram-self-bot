import { useState, useEffect, useCallback } from 'react';
import type { SavedItem, BioState, BotLog } from './lib/api';
import { api } from './lib/api';
import SavedItems from './components/SavedItems';
import BioStatus from './components/BioStatus';
import LogViewer from './components/LogViewer';
import AIConfigPanel from './components/AIConfigPanel';

type Tab = 'saves' | 'bio' | 'ai' | 'logs';

export default function App() {
  const [tab, setTab] = useState<Tab>('saves');
  const [saves, setSaves] = useState<SavedItem[]>([]);
  const [savesTotal, setSavesTotal] = useState(0);
  const [bio, setBio] = useState<BioState | null>(null);
  const [logs, setLogs] = useState<BotLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [savesRes, bioRes, logsRes] = await Promise.all([
        api.saves(),
        api.bio(),
        api.logs(),
      ]);
      setSaves(savesRes.items);
      setSavesTotal(savesRes.total);
      setBio(bioRes);
      setLogs(logsRes.logs);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load data');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const interval = setInterval(load, 30_000);
    return () => clearInterval(interval);
  }, [load]);

  const tabs: { id: Tab; label: string; count?: number }[] = [
    { id: 'saves', label: 'Saves', count: savesTotal },
    { id: 'bio', label: 'Bio Engine' },
    { id: 'ai', label: 'AI' },
    { id: 'logs', label: 'Logs', count: logs.length },
  ];

  return (
    <div className="min-h-screen bg-surface text-on-surface font-sans">
      <header className="border-b border-outline-variant bg-surface-container sticky top-0 z-10">
        <div className="max-w-5xl mx-auto px-4 sm:px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-full bg-primary flex items-center justify-center text-on-primary text-sm font-bold">
              L
            </div>
            <div>
              <h1 className="text-base font-semibold tracking-tight text-on-surface">LifeOS</h1>
              <p className="text-xs text-on-surface-variant">Telegram Self-Bot Dashboard</p>
            </div>
          </div>
          <button
            onClick={load}
            className="text-xs text-primary hover:text-primary/80 transition-colors px-3 py-1.5 rounded-full border border-primary/30 hover:bg-primary/10"
          >
            {loading ? 'Syncing…' : 'Refresh'}
          </button>
        </div>

        <div className="max-w-5xl mx-auto px-4 sm:px-6 flex gap-1 pb-0">
          {tabs.map(t => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
                tab === t.id
                  ? 'border-primary text-primary'
                  : 'border-transparent text-on-surface-variant hover:text-on-surface'
              }`}
            >
              {t.label}
              {t.count !== undefined && (
                <span className={`ml-1.5 text-xs px-1.5 py-0.5 rounded-full ${
                  tab === t.id ? 'bg-primary/20 text-primary' : 'bg-surface-variant text-on-surface-variant'
                }`}>
                  {t.count}
                </span>
              )}
            </button>
          ))}
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-4 sm:px-6 py-6">
        {error && (
          <div className="mb-4 px-4 py-3 rounded-xl bg-error/10 border border-error/30 text-error text-sm">
            {error}
          </div>
        )}

        {loading && !saves.length && !bio && !logs.length ? (
          <div className="flex items-center justify-center h-64 text-on-surface-variant text-sm">
            Loading…
          </div>
        ) : (
          <>
            {tab === 'saves' && <SavedItems items={saves} total={savesTotal} />}
            {tab === 'bio' && <BioStatus state={bio} />}
            {tab === 'ai' && <AIConfigPanel />}
            {tab === 'logs' && <LogViewer logs={logs} />}
          </>
        )}
      </main>
    </div>
  );
}
