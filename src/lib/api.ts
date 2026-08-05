export interface SavedItem {
  id: number;
  save_code: string;
  save_type: 'forward' | 'deep';
  origin_chat_id: number | null;
  origin_msg_id: number | null;
  saved_chat_id: number | null;
  saved_msg_id: number | null;
  sender_name: string | null;
  sender_id: number | null;
  mime_type: string | null;
  file_id: string | null;
  file_size: number | null;
  media_type: string | null;
  file_name: string | null;
  tags: string[];
  caption: string | null;
  owner_id: number;
  created_at: string;
}

export interface BioState {
  id: number;
  owner_id: number;
  template: string;
  mood: string;
  custom_text: string;
  is_active: boolean;
  last_bio: string;
  updated_at: string;
}

export interface BotLog {
  id: number;
  owner_id: number;
  level: 'INFO' | 'WARN' | 'ERROR';
  message: string;
  context: Record<string, unknown>;
  created_at: string;
}

export interface ProviderStatus {
  name: string;
  display_name: string;
  env_var: string;
  status: 'available' | 'detected' | 'invalid' | 'not_configured';
  has_key: boolean;
  validated: boolean;
  default_model: string;
  base_url: string;
  icon: string;
}

export interface ModelInfo {
  id: string;
  name: string;
  provider: string;
  context_length: number;
  description: string;
  is_alias: boolean;
}

export interface AIConfig {
  provider: string;
  model: string;
  temperature: number;
  max_tokens: number;
  system_prompt: string;
  history_budget: number;
  is_configured: boolean;
  last_request_at: string | null;
  last_latency_ms: number;
  trigger_en: string | null;
  trigger_fa: string | null;
}

const BASE = '/api';

async function fetchJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);
  return res.json() as Promise<T>;
}

export const api = {
  saves: (limit = 50, offset = 0) =>
    fetchJSON<{ items: SavedItem[]; total: number }>(`/saves?limit=${limit}&offset=${offset}`),
  save: (code: string) =>
    fetchJSON<SavedItem>(`/saves/${code}`),
  bio: () =>
    fetchJSON<BioState>(`/bio`),
  logs: (limit = 100) =>
    fetchJSON<{ logs: BotLog[] }>(`/logs?limit=${limit}`),
  aiProviders: () =>
    fetchJSON<{ providers: ProviderStatus[] }>(`/ai/providers`),
  aiModels: (provider: string) =>
    fetchJSON<{ provider: string; models: ModelInfo[] }>(`/ai/models/${provider}`),
  aiConfig: () =>
    fetchJSON<AIConfig>(`/ai/config`),
  aiUpdateTriggers: (triggerEn: string, triggerFa: string) =>
    fetch(`${BASE}/ai/triggers`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ trigger_en: triggerEn, trigger_fa: triggerFa }),
    }).then(async (res) => {
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      return data as { success: boolean; message: string };
    }),
};
