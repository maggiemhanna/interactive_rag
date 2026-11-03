import { AGENT_API_BASE } from './firebase';

/**
 * Formats byte counts into human-readable storage strings (e.g., "1.4 MB").
 */
export function formatStorageBytes(bytes) {
  const num = Number(bytes) || 0;
  if (num <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const idx = Math.min(Math.floor(Math.log(num) / Math.log(1024)), units.length - 1);
  const value = num / Math.pow(1024, idx);
  return `${idx === 0 ? Math.round(value) : value.toFixed(1)} ${units[idx]}`;
}

/**
 * GDPR Art. 15 (Right of Access) — fetches the authenticated user's stored data inventory
 * from the unified `src.manage` control-plane router mounted on `agent-api`.
 */
export async function fetchUserDataInventory(authFetch) {
  const res = await authFetch(`${AGENT_API_BASE}/compliance/my-data`, {
    method: 'GET',
  });
  if (!res.ok) {
    const errBody = await res.json().catch(() => ({}));
    throw new Error(errBody.detail || `Failed to load data inventory (${res.status})`);
  }
  return await res.json();
}

/**
 * GDPR Art. 17 (Right to Erasure) — permanently deletes all live & noncurrent GCS versions,
 * local ChromaDB / upload caches, and LangGraph conversation memory via `agent-api`.
 */
export async function eraseAllUserData(authFetch) {
  const res = await authFetch(`${AGENT_API_BASE}/compliance/my-data`, {
    method: 'DELETE',
  });
  if (!res.ok) {
    const errBody = await res.json().catch(() => ({}));
    throw new Error(errBody.detail || `Failed to erase user data (${res.status})`);
  }
  return await res.json();
}

