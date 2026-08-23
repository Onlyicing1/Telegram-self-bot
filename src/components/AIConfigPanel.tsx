import { useState, useEffect, useCallback, useMemo } from 'react';
import type { ProviderStatus, ModelInfo, AIConfig, ModelTestResponse, ModelTestResult, ProviderModels } from '../lib/api';
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
  INVALID_MODEL: 'bg-sky-500/15 text-sky-400 border border-sky-500/30',
  BLOCKED: 'bg-fuchsia-500/15 text-fuchsia-400 border border-fuchsia-500/30',
  AUTH_ERROR: 'bg-red-500/15 text-red-400 border border-red-500/30',
  PROVIDER_ERROR: 'bg-red-500/15 text-red-400 border border-red-500/30',
  RATE_LIMITED: 'bg-orange-500/15 text-orange-400 border border-orange-500/30',
  INSUFFICIENT_CREDITS: 'bg-violet-500/15 text-violet-400 border border-violet-500/30',
  ERROR: 'bg-red-500/15 text-red-400 border border-red-500/30',
  TIMEOUT: 'bg-amber-500/15 text-amber-400 border border-amber-500/30',
  NOT_CONFIGURED: 'bg-slate-500/15 text-slate-400 border border-outline-variant',
  UNKNOWN_ERROR: 'bg-red-500/15 text-red-400 border border-red-500/30',
};

// Statuses that mean "this model failed its availability test".
const FAILED_STATUSES = new Set([
  'UNAVAILABLE', 'AUTH_ERROR', 'PROVIDER_ERROR', 'RATE_LIMITED',
  'INSUFFICIENT_CREDITS', 'TIMEOUT', 'INVALID_MODEL', 'BLOCKED', 'UNKNOWN_ERROR', 'ERROR',
]);

const TEST_LABELS: Record<string, string> = {
  AVAILABLE: 'Available',
  INVALID_MODEL: 'Invalid model',
  AUTH_ERROR: 'Auth error',
  RATE_LIMITED: 'Rate limited',
  INSUFFICIENT_CREDITS: 'No credits',
  TIMEOUT: 'Timeout',
  PROVIDER_ERROR: 'Provider error',
  BLOCKED: 'Blocked',
  NOT_CONFIGURED: 'Not configured',
  UNKNOWN_ERROR: 'Unknown error',
};

function groupByProvider(results: ModelTestResult[]): Array<{ provider: string; display_name: string; icon: string; items: ModelTestResult[] }> {
  const map = new Map<string, { provider: string; display_name: string; icon: string; items: ModelTestResult[] }>();
  for (const r of results) {
    const key = r.provider;
    if (!map.has(key)) {
      map.set(key, { provider: key, display_name: r.display_name, icon: r.icon, items: [] });
    }
    map.get(key)!.items.push(r);
  }
  return Array.from(map.values()).sort((a, b) => a.display_name.localeCompare(b.display_name));
}

export default function AIConfigPanel() {
  const [providers, setProviders] = useState<ProviderStatus[]>([]);
  const [config, setConfig] = useState<AIConfig | null>(null);
  const [modelsByProvider, setModelsByProvider] = useState<ProviderModels[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingModels, setLoadingModels] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Model Testing State
  const [testingModels, setTestingModels] = useState(false);
  const [testResults, setTestResults] = useState<ModelTestResponse | null>(null);
  const [testError, setTestError] = useState<string | null>(null);

  // Model/Provider selection state
  const [selecting, setSelecting] = useState<string | null>(null);
  const [selectError, setSelectError] = useState<string | null>(null);

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

  const loadModelsAll = useCallback(async () => {
    setLoadingModels(true);
    try {
      const res = await api.aiModelsAll();
      setModelsByProvider(res.providers);
    } catch {
      setModelsByProvider([]);
    } finally {
      setLoadingModels(false);
    }
  }, []);

  useEffect(() => {
    loadModelsAll();
  }, [loadModelsAll]);

  // Re-fetch the model grid after a test run so availability stays fresh.
  useEffect(() => {
    if (testResults) {
      loadModelsAll();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [testResults]);

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

  const availabilityMap = useMemo(() => {
    const map = new Map<string, ModelTestResult>();
    if (!testResults) return map;
    for (const r of testResults.results) {
      map.set(`${r.provider}|${r.model}`, r);
    }
    return map;
  }, [testResults]);

  // After a test run, only CURRENTLY USABLE models (AVAILABLE + the
  // user's active selection) belong in the normal selection catalog.
  const usableMap = useMemo(() => {
    if (!testResults) return null;
    const map = new Map<string, ModelTestResult>();
    for (const r of testResults.results) {
      if (r.status === 'AVAILABLE') map.set(`${r.provider}|${r.model}`, r);
    }
    return map;
  }, [testResults]);

  const handleSelectModel = async (provider: string, model: string) => {
    if (selecting) return;
    setSelecting(`${provider}|${model}`);
    setSelectError(null);
    try {
      if (config?.provider !== provider) {
        await api.aiSetProvider(provider);
      }
      await api.aiSetModel(model);
      await load();
      await loadModelsAll();
    } catch (e) {
      setSelectError(e instanceof Error ? e.message : 'Failed to select model');
    } finally {
      setSelecting(null);
    }
  };

  const handleSelectProvider = async (provider: string) => {
    if (selecting || config?.provider === provider) return;
    setSelecting(`provider:${provider}`);
    setSelectError(null);
    try {
      await api.aiSetProvider(provider);
      await load();
      await loadModelsAll();
    } catch (e) {
      setSelectError(e instanceof Error ? e.message : 'Failed to select provider');
    } finally {
      setSelecting(null);
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

  const chatProviders = providers.filter(p => p.capability_kind === 'chat');
  const capabilityProviders = providers.filter(p => p.capability_kind !== 'chat');
  const available = chatProviders.filter(p => p.status === 'available');
  const notConfigured = chatProviders.filter(p => p.status === 'not_configured');
  const invalid = chatProviders.filter(p => p.status === 'invalid');
  const configuredProviderNames = new Set(chatProviders.filter(p => p.has_key).map(p => p.name));

  const summary = testResults?.summary;

  const summaryChips: Array<{ label: string; value: number; tone: string }> = summary
    ? [
        { label: 'Available', value: summary.available, tone: 'text-emerald-400' },
        { label: 'Failed', value: summary.failed, tone: 'text-red-400' },
        { label: 'Rate limited', value: summary.rate_limited, tone: 'text-orange-400' },
        { label: 'Not configured', value: summary.not_configured, tone: 'text-slate-400' },
        { label: 'Invalid', value: summary.invalid, tone: 'text-sky-400' },
        { label: 'No credits', value: summary.insufficient_credits, tone: 'text-violet-400' },
      ].filter(c => c.value > 0)
    : [];

  const groupedResults = testResults ? groupByProvider(testResults.results) : [];

  const cardStatus = (pm: ProviderModels, m: ModelInfo): { label: string; cls: string; failed: boolean } => {
    const isCurrent = config?.provider === pm.provider && config?.model === m.id;
    if (isCurrent) return { label: '✓ Active', cls: 'text-primary border border-primary/40 bg-primary/10', failed: false };
    if (!configuredProviderNames.has(pm.provider)) {
      return { label: '○ Not configured', cls: 'text-slate-500 border border-outline-variant bg-surface-variant/30', failed: false };
    }
    const tested = availabilityMap.get(`${pm.provider}|${m.id}`);
    if (tested) {
      if (tested.status === 'AVAILABLE') {
        return { label: '✓ Available', cls: 'text-emerald-400 border border-emerald-500/30 bg-emerald-500/10', failed: false };
      }
      const label = TEST_LABELS[tested.status] || tested.status;
      return { label: `✕ ${label}`, cls: 'text-red-400/90 border border-red-500/25 bg-red-500/10', failed: true };
    }
    return { label: '? Not tested', cls: 'text-on-surface-variant border border-outline-variant bg-surface-variant/30', failed: false };
  };

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

      {/* Action Error Banner */}
      {(testError || selectError) && (
        <div className="px-4 py-3 rounded-2xl bg-error/10 border border-error/30 text-error text-sm flex items-center justify-between">
          <span>{testError || selectError}</span>
          <button onClick={() => { setTestError(null); setSelectError(null); }} className="text-xs text-on-surface-variant hover:text-on-surface">Dismiss</button>
        </div>
      )}

      {/* Model Test Results */}
      {testResults && (
        <div className="bg-surface-container border border-outline-variant rounded-2xl p-5 space-y-4">
          <div className="flex items-center justify-between flex-wrap gap-2">
            <div>
              <h3 className="text-sm font-semibold text-on-surface uppercase tracking-widest flex items-center gap-2">
                <span>Model Availability Results</span>
                {testResults.partial && (
                  <span className="text-[10px] px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-400 border border-amber-500/30">
                    PARTIAL — overall timeout
                  </span>
                )}
              </h3>
              <p className="text-xs text-on-surface-variant mt-0.5">
                {summary ? `${summary.tested} tested · ${summary.discovered} discovered` : ''}
                {testResults.tested_at ? ` · ${testResults.tested_at.substring(0, 19).replace('T', ' ')}` : ''}
              </p>
            </div>
            <button
              onClick={() => setTestResults(null)}
              className="text-xs text-on-surface-variant hover:text-on-surface px-2 py-1 rounded-lg hover:bg-surface-variant/50"
            >
              Close
            </button>
          </div>

          {/* Summary chips */}
          {summaryChips.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {summaryChips.map(chip => (
                <span key={chip.label} className={`text-xs px-2.5 py-1 rounded-full font-medium bg-surface-variant/40 border border-outline-variant ${chip.tone}`}>
                  {chip.label}: {chip.value}
                </span>
              ))}
            </div>
          )}

          {/* Provider-grouped compact results */}
          <div className="space-y-4">
            {groupedResults.map(group => (
              <div key={group.provider} className="space-y-1.5">
                <h4 className="text-xs font-semibold text-on-surface-variant uppercase tracking-widest flex items-center gap-2">
                  <span>{group.icon}</span>
                  <span>{group.display_name}</span>
                  <span className="flex-1 h-px bg-outline-variant/60" />
                </h4>
                {group.items.map((res, i) => (
                  <div
                    key={`${res.provider}-${res.model}-${i}`}
                    className="bg-surface rounded-xl px-3.5 py-2.5 border border-outline-variant/60"
                  >
                    <div className="flex items-center justify-between gap-2 flex-wrap">
                      <span className="text-sm text-on-surface font-mono truncate min-w-0">
                        {res.model}
                      </span>
                      <div className="flex items-center gap-2 shrink-0">
                        {res.latency_s !== null && (
                          <span className="text-xs text-on-surface-variant font-mono">{res.latency_s}s</span>
                        )}
                        <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${TEST_STATUS_STYLES[res.status] || TEST_STATUS_STYLES.NOT_CONFIGURED}`}>
                          {TEST_LABELS[res.status] || res.status}
                        </span>
                      </div>
                    </div>
                    {(res.http_status !== null || res.error) && (
                      <details className="mt-1.5 group">
                        <summary className="text-[11px] text-on-surface-variant cursor-pointer select-none hover:text-on-surface">
                          {res.http_status !== null ? `HTTP ${res.http_status}${res.retry_after !== null && res.retry_after !== undefined ? ` · retry-after: ${res.retry_after}s` : ''}` : 'Details'}
                          {res.error && ' — tap to expand'}
                        </summary>
                        {res.error && (
                          <p className="mt-1 text-[11px] text-red-400/90 font-mono break-words">{res.error}</p>
                        )}
                      </details>
                    )}
                  </div>
                ))}
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
            Providers
          </h3>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {available.map(p => (
              <button
                key={p.name}
                onClick={() => handleSelectProvider(p.name)}
                disabled={selecting !== null || config?.provider === p.name}
                className={`text-left bg-surface-container rounded-2xl px-4 py-3 border transition-colors ${
                  config?.provider === p.name
                    ? 'border-primary/40 bg-primary/5'
                    : 'border-outline-variant hover:border-primary/30'
                } disabled:opacity-70`}
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-3 min-w-0">
                    <span className="text-lg">{p.icon}</span>
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-on-surface truncate">{p.display_name}</p>
                      <p className="text-xs text-on-surface-variant font-mono truncate">{p.default_model}</p>
                    </div>
                  </div>
                  <span className={`text-xs px-2 py-0.5 rounded-full shrink-0 ${STATUS_STYLES.available}`}>
                    {selecting === `provider:${p.name}` ? '…' : config?.provider === p.name ? '✓ Selected' : 'Select'}
                  </span>
                </div>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Capability status — retrieval providers are visible but never selectable as chat models */}
      {capabilityProviders.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-on-surface-variant uppercase tracking-widest mb-3">
            Capabilities
          </h3>
          <div className="space-y-2">
            {capabilityProviders.map(p => (
              <div key={p.name} className="bg-surface-container rounded-xl px-4 py-3 border border-outline-variant/60">
                <div className="flex items-center justify-between gap-3">
                  <div className="flex items-center gap-3 min-w-0">
                    <span className="text-lg">{p.icon}</span>
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-on-surface truncate">{p.display_name}</p>
                      <p className="text-xs text-on-surface-variant truncate">
                        {(p.capabilities.length > 0 ? p.capabilities.join(', ') : p.capability_kind)} · not a chat provider
                      </p>
                    </div>
                  </div>
                  <span className={`text-xs px-2 py-0.5 rounded-full shrink-0 ${STATUS_STYLES[p.status] || STATUS_STYLES.not_configured}`}>
                    {STATUS_LABELS[p.status] || p.status}
                  </span>
                </div>
                {p.status !== 'available' && (
                  <p className="mt-2 text-[11px] text-on-surface-variant font-mono">{p.env_var}</p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Model Selection — compact multi-column grid, usable-only after a test run */}
      {modelsByProvider.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-on-surface-variant uppercase tracking-widest mb-3">
            Models
            {loadingModels && <span className="ml-2 text-xs font-normal text-on-surface-variant animate-pulse">refreshing…</span>}
            {testResults && usableMap && (
              <span className="ml-2 text-xs font-normal text-on-surface-variant">
                (usable only — run Test Models to refresh)
              </span>
            )}
          </h3>
          {testResults && usableMap && (
            (() => {
              const visible = modelsByProvider
                .map(pm => ({
                  pm,
                  models: pm.models.filter(m =>
                    (config?.provider === pm.provider && config?.model === m.id) ||
                    usableMap.has(`${pm.provider}|${m.id}`)
                  ),
                }))
                .filter(g => g.models.length > 0);
              if (visible.length === 0) {
                return (
                  <div className="bg-surface-container border border-outline-variant rounded-2xl p-5">
                    <p className="text-sm text-on-surface font-medium">No currently usable chat models.</p>
                    <p className="text-xs text-on-surface-variant mt-1">
                      Run <span className="font-medium">Test Models</span> and check the results above — models that fail (invalid, rate limited, no credits, auth errors) are excluded from the selection catalog.
                    </p>
                  </div>
                );
              }
              return (
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-2.5">
                  {visible.map(({ pm, models }) =>
                    models.map(m => {
                      const status = cardStatus(pm, m);
                      const isCurrent = config?.provider === pm.provider && config?.model === m.id;
                      const key = `${pm.provider}|${m.id}`;
                      return (
                        <button
                          key={key}
                          onClick={() => handleSelectModel(pm.provider, m.id)}
                          disabled={selecting !== null || (status.failed && !isCurrent)}
                          title={status.failed && !isCurrent ? `${status.label} — select only models that work for chat` : undefined}
                          className={`text-left bg-surface-container rounded-xl px-3.5 py-3 border transition-colors ${
                            isCurrent
                              ? 'border-primary/50 bg-primary/5'
                              : 'border-outline-variant hover:border-primary/30'
                          } disabled:opacity-60 disabled:cursor-not-allowed`}
                        >
                          <div className="flex items-center gap-1.5 text-[11px] text-on-surface-variant mb-1">
                            <span>{pm.icon}</span>
                            <span className="truncate">{pm.display_name}</span>
                          </div>
                          <p className={`text-sm font-mono truncate ${isCurrent ? 'text-primary' : 'text-on-surface'}`}>
                            {m.name}
                          </p>
                          <div className="mt-1.5 flex items-center justify-between gap-2">
                            <span className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${status.cls}`}>
                              {status.label}
                            </span>
                            {selecting === key && <span className="text-[10px] text-on-surface-variant animate-pulse">saving…</span>}
                            {m.context_length > 0 && !isCurrent && (
                              <span className="text-[10px] text-on-surface-variant shrink-0">
                                {m.context_length >= 1000 ? `${Math.round(m.context_length / 1000)}K` : `${m.context_length}`}
                              </span>
                            )}
                          </div>
                        </button>
                      );
                    })
                  )}
                </div>
              );
            })()
          )}
          {(!testResults || !usableMap) && (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-2.5">
              {modelsByProvider.map(pm =>
                pm.models.map(m => {
                  const status = cardStatus(pm, m);
                  const isCurrent = config?.provider === pm.provider && config?.model === m.id;
                  const key = `${pm.provider}|${m.id}`;
                  return (
                    <button
                      key={key}
                      onClick={() => handleSelectModel(pm.provider, m.id)}
                      disabled={selecting !== null || (status.failed && !isCurrent)}
                      title={status.failed && !isCurrent ? `${status.label} — select only models that work for chat` : undefined}
                      className={`text-left bg-surface-container rounded-xl px-3.5 py-3 border transition-colors ${
                        isCurrent
                          ? 'border-primary/50 bg-primary/5'
                          : 'border-outline-variant hover:border-primary/30'
                      } disabled:opacity-60 disabled:cursor-not-allowed`}
                    >
                      <div className="flex items-center gap-1.5 text-[11px] text-on-surface-variant mb-1">
                        <span>{pm.icon}</span>
                        <span className="truncate">{pm.display_name}</span>
                      </div>
                      <p className={`text-sm font-mono truncate ${isCurrent ? 'text-primary' : 'text-on-surface'}`}>
                        {m.name}
                      </p>
                      <div className="mt-1.5 flex items-center justify-between gap-2">
                        <span className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${status.cls}`}>
                          {status.label}
                        </span>
                        {selecting === key && <span className="text-[10px] text-on-surface-variant animate-pulse">saving…</span>}
                        {m.context_length > 0 && !isCurrent && (
                          <span className="text-[10px] text-on-surface-variant shrink-0">
                            {m.context_length >= 1000 ? `${Math.round(m.context_length / 1000)}K` : `${m.context_length}`}
                          </span>
                        )}
                      </div>
                    </button>
                  );
                })
              )}
            </div>
          )}
          <p className="mt-2 text-[11px] text-on-surface-variant">
            {testResults
              ? 'Showing only models proven usable by the latest Test Models run (plus your active selection).'
              : 'Statuses come from the latest Test Models run — run it to verify models before selecting.'}
          </p>
        </div>
      )}

      {/* Setup Wizard */}
      {available.length === 0 && (
        <div className="bg-surface-container border border-outline-variant rounded-2xl p-5">
          <h3 className="text-sm font-semibold text-on-surface-variant uppercase tracking-widest mb-3">
            Setup Guide
          </h3>
          <p className="text-sm text-on-surface-variant mb-4">
            No chat providers detected. Set an AI provider API key as an environment variable to enable chat:
          </p>
          <div className="space-y-2">
            {chatProviders.map(p => (
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
