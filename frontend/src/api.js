import { API_BASE, AGENT_API_BASE } from './firebase';

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
 * GDPR Art. 15 (Right of Access) — fetches the authenticated user's stored data inventory.
 * Queries agent-api first so active LangGraph conversation memory thread counts are included,
 * falling back to ingestion-api if agent-api is unreachable.
 */
export async function fetchUserDataInventory(authFetch) {
  let lastError = null;
  const bases =
    AGENT_API_BASE && AGENT_API_BASE !== API_BASE
      ? [AGENT_API_BASE, API_BASE]
      : [API_BASE];

  for (const base of bases) {
    try {
      const res = await authFetch(`${base}/compliance/my-data`, {
        method: 'GET',
      });
      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}));
        throw new Error(errBody.detail || `Failed to load data inventory (${res.status})`);
      }
      return await res.json();
    } catch (err) {
      lastError = err;
    }
  }
  throw lastError || new Error('Unable to reach compliance inventory endpoint.');
}

/**
 * GDPR Art. 17 (Right to Erasure) — permanently deletes all live & noncurrent GCS versions,
 * local ChromaDB / upload caches, and LangGraph conversation memory across backend services.
 */
export async function eraseAllUserData(authFetch) {
  let primaryResult = null;
  let primaryError = null;

  try {
    const agentRes = await authFetch(`${AGENT_API_BASE}/compliance/my-data`, {
      method: 'DELETE',
    });
    if (!agentRes.ok) {
      const errBody = await agentRes.json().catch(() => ({}));
      throw new Error(errBody.detail || `Failed to erase user data (${agentRes.status})`);
    }
    primaryResult = await agentRes.json();
  } catch (err) {
    primaryError = err;
  }

  if (API_BASE && API_BASE !== AGENT_API_BASE) {
    try {
      const ingestRes = await authFetch(`${API_BASE}/compliance/my-data`, {
        method: 'DELETE',
      });
      if (!ingestRes.ok) {
        const errBody = await ingestRes.json().catch(() => ({}));
        if (!primaryResult) {
          throw new Error(errBody.detail || `Failed to erase user data (${ingestRes.status})`);
        }
      } else {
        const ingestJson = await ingestRes.json();
        if (!primaryResult) {
          primaryResult = ingestJson;
        }
      }
    } catch (err) {
      if (!primaryResult) {
        throw primaryError || err;
      }
    }
  }

  if (!primaryResult) {
    throw primaryError || new Error('Failed to erase personal data.');
  }

  return primaryResult;
}
