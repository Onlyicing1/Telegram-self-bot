import { useState } from 'react';
import type { AIConfig } from '../lib/api';
import { api } from '../lib/api';

interface Props {
  config: AIConfig | null;
  onUpdated: () => void;
}

export default function TriggerConfig({ config, onUpdated }: Props) {
  const [en, setEn] = useState(config?.trigger_en ?? '');
  const [fa, setFa] = useState(config?.trigger_fa ?? '');
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const handleSave = async () => {
    const enVal = en.trim();
    const faVal = fa.trim();

    if (!enVal && !faVal) {
      setMsg({ ok: false, text: 'At least one trigger is required.' });
      return;
    }
    if (enVal && enVal.includes(' ')) {
      setMsg({ ok: false, text: 'English trigger must be a single word.' });
      return;
    }
    if (faVal && faVal.includes(' ')) {
      setMsg({ ok: false, text: 'Persian trigger must be a single word.' });
      return;
    }
    if (enVal && faVal && enVal.toLowerCase() === faVal.toLowerCase()) {
      setMsg({ ok: false, text: 'Triggers must be different.' });
      return;
    }

    setSaving(true);
    setMsg(null);
    try {
      const result = await api.aiUpdateTriggers(enVal, faVal);
      setMsg({ ok: result.success, text: result.message });
      if (result.success) onUpdated();
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : 'Failed to save' });
    } finally {
      setSaving(false);
    }
  };

  const hasTriggers = !!(config?.trigger_en?.trim() || config?.trigger_fa?.trim());

  return (
    <div className="bg-surface-container border border-outline-variant rounded-2xl p-5">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold text-on-surface-variant uppercase tracking-widest">
          AI Triggers
        </h3>
        <span className={`text-xs px-2.5 py-1 rounded-full font-medium ${
          hasTriggers
            ? 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30'
            : 'bg-amber-500/15 text-amber-400 border border-amber-500/30'
        }`}>
          {hasTriggers ? 'Active' : 'Not Set'}
        </span>
      </div>

      <p className="text-xs text-on-surface-variant mb-4 leading-relaxed">
        Configure trigger words to activate AI. The first word of your message is
        matched against these triggers (English is case-insensitive, Persian is exact).
        The trigger word is removed before sending to the AI.
      </p>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-4">
        <div>
          <label className="block text-xs text-on-surface-variant uppercase tracking-widest opacity-60 mb-1.5">
            English Trigger
          </label>
          <input
            type="text"
            value={en}
            onChange={(e) => setEn(e.target.value)}
            placeholder="e.g. Nova"
            className="w-full bg-surface text-on-surface text-sm rounded-xl px-4 py-2.5 border border-outline-variant focus:border-primary/50 focus:outline-none transition-colors"
          />
          <p className="text-xs text-on-surface-variant mt-1 opacity-60">Case-insensitive</p>
        </div>
        <div>
          <label className="block text-xs text-on-surface-variant uppercase tracking-widest opacity-60 mb-1.5">
            Persian Trigger
          </label>
          <input
            type="text"
            value={fa}
            onChange={(e) => setFa(e.target.value)}
            placeholder="مثال: نوا"
            dir="rtl"
            className="w-full bg-surface text-on-surface text-sm rounded-xl px-4 py-2.5 border border-outline-variant focus:border-primary/50 focus:outline-none transition-colors"
          />
          <p className="text-xs text-on-surface-variant mt-1 opacity-60">Exact match</p>
        </div>
      </div>

      <div className="flex items-center gap-3">
        <button
          onClick={handleSave}
          disabled={saving}
          className="text-xs font-medium px-4 py-2 rounded-full bg-primary text-on-primary hover:bg-primary/90 transition-colors disabled:opacity-50"
        >
          {saving ? 'Saving…' : 'Save Triggers'}
        </button>
        {msg && (
          <span className={`text-xs ${msg.ok ? 'text-emerald-400' : 'text-error'}`}>
            {msg.text}
          </span>
        )}
      </div>

      {hasTriggers && (
        <div className="mt-4 pt-3 border-t border-outline-variant">
          <p className="text-xs text-on-surface-variant mb-2">Current configuration:</p>
          <div className="flex gap-3 text-sm">
            {config?.trigger_en && (
              <span className="px-3 py-1 rounded-lg bg-surface border border-outline-variant/50">
                <span className="text-xs text-on-surface-variant mr-1.5">EN:</span>
                <span className="font-mono text-primary">{config.trigger_en}</span>
              </span>
            )}
            {config?.trigger_fa && (
              <span className="px-3 py-1 rounded-lg bg-surface border border-outline-variant/50">
                <span className="text-xs text-on-surface-variant mr-1.5">FA:</span>
                <span className="font-mono text-primary" dir="rtl">{config.trigger_fa}</span>
              </span>
            )}
          </div>
          <p className="text-xs text-on-surface-variant mt-2 opacity-60">
            Example: <code className="font-mono">{config?.trigger_en || config?.trigger_fa} summarize this</code>
          </p>
        </div>
      )}
    </div>
  );
}
