import { useState, useEffect, useCallback } from 'react';
import type { ProviderStatus, ModelInfo, AIConfig, ModelTestResponse } from '../lib/api';
import { api } from '../lib/api';

import TriggerConfig from './TriggerConfig';

const STATUS_STYLES: Record<string, string> = {
  available: 'text-emerald-400 bg-emerald-400/10',
  detected: 'text-sky-400 bg-sky-400/10',
  invalid: 'text-red-400 bg-red-400/10',
  not_configured: 'text-slate-500 bg-slate-500/10',
};

const STATUS_LABELS: Record<string, string> = {
  available: 'Available',
  detected: 'Detected',
  invalid: 'Invalid Key',
  not_configured: 'Not Configured',
};

const TEST_STATUS_STYLES: Record<string, string> = {
  AVAILABLE: 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30',
  UNAVAILABLE: 'bg-red-500/15 text-red-400 border border-red-500/30',
  ERROR: 'bg-red-500/15 text-red-400 border border-red-500/30',
  TIMEOUT: 'bg-amber-500/15 text-amber-400 border border-amber-500/30',
  NOT_CONFIGURED: 'bg-slate-500/15 text-slate-400 border border-outline-variant',
};

export default function AIConfigPanel() {
  const [providers, setProviders] = useState<ProviderStatus[]>([]);
  const [config, setConfig] = useState<AIConfig | null>(null);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingModels, setLoadingModels] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Model Testing State
  const [testingModels, setTestingModels] = useState(false);
  const [testResults, setTestResults] = useState<ModelTestResponse | null>(null);
  const [testError, setTestError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [providersRes, configRes] = await Promise.all([
        api.aiProviders(),
        api.aiConfig(),
      ]);
      setProviders(providersRes.providers);
      setConfig(configRes);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const loadModels = useCallback(async (providerName: string) => {
    if (!providerName) return;
    setLoadingModels(true);
    try {
      const res = await api.aiModels(providerName);
      setModels(res.models);
    } catch {
      setModels([]);
    } finally {
      setLoadingModels(false);
    }
  }, []);

  useEffect(() => {
    if (config?.provider) {
      loadModels(config.provider);
    }
  }, [config?.provider, loadModels]);

  const handleTestModels = async () => {
    setTestingModels(true);
    setTestError(null);
    try {
      const res = await api.aiTestModels();
      setTestResults(res);
    } catch (e) {
      setTestError(e instanceof Error ? e.message : 'Test request failed');
    } finally {
      setTestingModels(false);
    }
  };

  if (loading) {
    return <div className="flex items-center justify-center h-48 text-on-surface-variant text-sm">Loading…</div>;
  }

  if (error) {
    return (
      <div className="px-4 py-3 rounded-xl bg-error/10 border border-error/30 text-error text-sm">
        {error}
      </div>
    );
  }

  const available = providers.filter(p => p.status === 'available');
  const notConfigured = providers.filter(p => p.status === 'not_configured');
  const invalid = providers.filter(p => p.status === 'invalid');

  return (
    <div className="space-y-6">
      {/* Status Card */}
      <div className="bg-surface-container border border-outline-variant rounded-2xl p-5">
        <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
          <div className="flex items-center gap-3">
            <h2 className="text-sm font-semibold text-on-surface-variant uppercase tracking-widest">AI Assistant</h2>
            <button
              onClick={handleTestModels}
              disabled={testingModels}
              className="px-3 py-1 rounded-xl bg-primary/10 hover:bg-primary/20 border border-primary/30 text-primary text-xs font-medium transition-colors disabled:opacity-50 flex items-center gap-1.5"
            >
              {testingModels ? (
                <>
                  <span className="inline-block animate-spin">⏳</span>
                  <span>Testing Models…</span>
                </>
              ) : (
                <>
                  <span>🧪</span>
                  <span>Test Models</span>
                </>
              )}
            </button>
          </div>
          <span className={`text-xs px-3 py-1 rounded-full font-medium ${
            available.length > 0 && config?.trigger_en && config.trigger_en.trim() !== '' || available.length > 0 && config?.trigger_fa && config.trigger_fa.trim() !== ''
              ? 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30'
              : 'bg-surface-variant text-on-surface-variant border border-outline-variant'
          }`}>
            {available.length > 0 && ((config?.trigger_en && config.trigger_en.trim() !== '') || (config?.trigger_fa && config.trigger_fa.trim() !== '')) ? 'Ready' : 'Not Configured'}
          </span>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div>
            <span className="text-xs text-on-surface-variant uppercase tracking-widest opacity-60">Provider</span>
            <p className="text-sm text-on-surface mt-0.5">
              {config?.provider ? config.provider.charAt(0).toUpperCase() + config.provider.slice(1) : '—'}
            </p>
          </div>
          <div>
            <span className="text-xs text-on-surface-variant uppercase tracking-widest opacity-60">Model</span>
            <p className="text-sm text-on-surface mt-0.5 font-mono truncate">
              {config?.model || '—'}
            </p>
          </div>
          <div>
            <span className="text-xs text-on-surface-variant uppercase tracking-widest opacity-60">Temperature</span>
            <p className="text-sm text-on-surface mt-0.5">{config?.temperature ?? '—'}</p>
          </div>
          <div>
            <span className="text-xs text-on-surface-variant uppercase tracking-widest opacity-60">Max Tokens</span>
            <p className="text-sm text-on-surface mt-0.5">{config?.max_tokens ?? '—'}</p>
          </div>
        </div>

        {config?.last_request_at && (
          <div className="mt-3 pt-3 border-t border-outline-variant text-xs text-on-surface-variant">
            Last request: {config.last_request_at.substring(0, 19).replace('T', ' ')}
            {config.last_latency_ms > 0 && ` · ${Math.round(config.last_latency_ms)}ms`}
          </div>
        )}
      </div>

      {/* Test Error Banner */}
      {testError && (
        <div className="px-4 py-3 rounded-2xl bg-error/10 border border-error/30 text-error text-sm flex items-center justify-between">
          <span>{testError}</span>
          <button onClick={() => setTestError(null)} className="text-xs text-on-surface-variant hover:text-on-surface">Dismiss</button>
        </div>
      )}

      {/* Model Test Results */}
      {testResults && (
        <div className="bg-surface-container border border-outline-variant rounded-2xl p-5 space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <h3 className="text-sm font-semibold text-on-surface uppercase tracking-widest flex items-center gap-2">
                <span>Model Availability Results</span>
              </h3>
              <p className="text-xs text-on-surface-variant mt-0.5">
                {testResults.summary.available} available · {testResults.summary.unavailable + testResults.summary.error + testResults.summary.timeout} failed · {testResults.summary.not_configured} not configured
              </p>
            </div>
            <button
              onClick={() => setTestResults(null)}
              className="text-xs text-on-surface-variant hover:text-on-surface px-2 py-1 rounded-lg hover:bg-surface-variant/50"
            >
              Close
            </button>
          </div>

          <div className="space-y-2.5">
            {testResults.results.map((res, i) => (
              <div
                key={`${res.provider}-${res.model}-${i}`}
                className="bg-surface rounded-xl p-3.5 border border-outline-variant/60 space-y-1.5"
              >
                <div className="flex items-center justify-between flex-wrap gap-2">
                  <div className="flex items-center gap-2.5 min-w-0">
                    <span className="text-base">{res.icon}</span>
                    <span className="text-sm font-medium text-on-surface">{res.display_name}</span>
                    <span className="text-xs text-on-surface-variant font-mono truncate max-w-[180px]">
                      {res.model}
                    </span>
                  </div>

                  <div className="flex items-center gap-2 shrink-0">
                    {res.latency_s !== null && (
                      <span className="text-xs text-on-surface-variant font-mono">
                        {res.latency_s}s
                      </span>
                    )}
                    <span
                      className={`text-xs px-2.5 py-0.5 rounded-full font-medium ${
                        TEST_STATUS_STYLES[res.status] || TEST_STATUS_STYLES.NOT_CONFIGURED
                      }`}
                    >
                      {res.status}
                    </span>
                  </div>
                </div>

                {res.error && (
                  <p className="text-xs text-red-400/90 font-mono pl-7 break-words">
                    {res.error}
                  </p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Trigger Configuration */}
      {available.length > 0 && (
        <TriggerConfig config={config} onUpdated={load} />
      )}

      {/* Available Providers */}
      {available.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-on-surface-variant uppercase tracking-widest mb-3">
            Available Providers
          </h3>
          <div className="space-y-2">
            {available.map(p => (
              <div
                key={p.name}
                className={`bg-surface-container rounded-2xl px-5 py-4 border transition-colors ${
                  config?.provider === p.name
                    ? 'border-primary/40 bg-primary/5'
                    : 'border-outline-variant hover:border-primary/30'
                }`}
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <span className="text-lg">{p.icon}</span>
                    <div>
                      <p className="text-sm font-medium text-on-surface">{p.display_name}</p>
                      <p className="text-xs text-on-surface-variant font-mono">{p.default_model}</p>
                    </div>
                  </div>
                  <span className={`text-xs px-2 py-0.5 rounded-full ${STATUS_STYLES.available}`}>
                    {config?.provider === p.name ? '✓ Selected' : 'Available'}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Models */}
      {config?.provider && models.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-on-surface-variant uppercase tracking-widest mb-3">
            Models for {config.provider.charAt(0).toUpperCase() + config.provider.slice(1)}
          </h3>
          <div className="bg-surface-container border border-outline-variant rounded-2xl overflow-hidden">
            <div className="divide-y divide-outline-variant/50 max-h-64 overflow-y-auto">
              {models.map(m => (
                <div
                  key={m.id}
                  className={`px-5 py-3 flex items-center justify-between ${
                    config.model === m.id ? 'bg-primary/5' : 'hover:bg-surface-variant/30'
                  } transition-colors`}
                >
                  <div className="min-w-0">
                    <p className="text-sm text-on-surface font-mono truncate">{m.name}</p>
                    {m.context_length > 0 && (
                      <p className="text-xs text-on-surface-variant">
                        {m.context_length >= 1000
                          ? `${Math.round(m.context_length / 1000)}K context`
                          : `${m.context_length} context`}
                      </p>
                    )}
                  </div>
                  {config.model === m.id && (
                    <span className="text-xs text-primary font-medium shrink-0">✓ Active</span>
                  )}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Setup Wizard */}
      {available.length === 0 && (
        <div className="bg-surface-container border border-outline-variant rounded-2xl p-5">
          <h3 className="text-sm font-semibold text-on-surface-variant uppercase tracking-widest mb-3">
            Setup Guide
          </h3>
          <p className="text-sm text-on-surface-variant mb-4">
            No providers detected. Set one API key as an environment variable to enable AI:
          </p>
          <div className="space-y-2">
            {providers.map(p => (
              <div key={p.name} className="flex items-center justify-between px-4 py-3 bg-surface rounded-xl border border-outline-variant/50">
                <div className="flex items-center gap-3">
                  <span className="text-lg">{p.icon}</span>
                  <div>
                    <p className="text-sm text-on-surface">{p.display_name}</p>
                    <p className="text-xs text-on-surface-variant font-mono">{p.env_var}</p>
                  </div>
                </div>
                <span className={`text-xs px-2 py-0.5 rounded-full ${STATUS_STYLES.not_configured}`}>
                  {STATUS_LABELS.not_configured}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Invalid Keys */}
      {invalid.length > 0 && (
        <div className="bg-red-500/5 border border-red-500/30 rounded-2xl p-4">
          <h3 className="text-sm font-semibold text-red-400 mb-2">⚠️ Invalid API Keys</h3>
          <div className="space-y-1">
            {invalid.map(p => (
              <div key={p.name} className="flex items-center gap-2 text-sm">
                <span>{p.icon}</span>
                <span className="text-on-surface">{p.display_name}</span>
                <span className="text-xs text-on-surface-variant">— key may be expired or wrong</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Not Configured */}
      {notConfigured.length > 0 && available.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-on-surface-variant uppercase tracking-widest mb-3">
            Other Providers
          </h3>
          <div className="space-y-2">
            {notConfigured.map(p => (
              <div key={p.name} className="flex items-center justify-between px-4 py-3 bg-surface-container rounded-xl border border-outline-variant/50">
                <div className="flex items-center gap-3">
                  <span className="text-lg opacity-40">{p.icon}</span>
                  <span className="text-sm text-on-surface-variant">{p.display_name}</span>
                </div>
                <span className={`text-xs px-2 py-0.5 rounded-full ${STATUS_STYLES.not_configured}`}>
                  {STATUS_LABELS.not_configured}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
