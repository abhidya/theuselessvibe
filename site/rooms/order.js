/** Shared ordering is deterministic; time is used only by opt-in rotation. */
export const ROTATION_MS = 60_000;
export const MAX_HISTORY = 100;
const encoder = new TextEncoder();

export function normalizeCode(value) {
  const code = String(value ?? '').trim().toUpperCase();
  if (!/^[A-Z0-9][A-Z0-9-]{0,23}$/.test(code)) {
    throw new Error('Use 1–24 letters, numbers, or hyphens for the room code.');
  }
  return code;
}

export function modulo(position, length) {
  if (!Number.isSafeInteger(position) || !Number.isSafeInteger(length) || length < 1) {
    throw new RangeError('A room needs a valid position and at least one game.');
  }
  return ((position % length) + length) % length;
}

export function clockPosition(now, length) {
  if (!Number.isFinite(now) || now < 0) throw new RangeError('Invalid device time.');
  return modulo(Math.floor(now / ROTATION_MS), length);
}

/** SHA-256 ranking is a seeded permutation, independent of server/index order or locale. */
export async function roomOrder(code, entries, subtle = globalThis.crypto.subtle) {
  code = normalizeCode(code);
  if (!entries.length) throw new Error('No games are available in this catalog yet.');
  const ids = new Set();
  const ranked = [];
  // Bounded batches avoid queuing thousands of crypto operations at once.
  for (let start = 0; start < entries.length; start += 128) {
    const batch = entries.slice(start, start + 128);
    for (const entry of batch) {
      if (typeof entry.id !== 'string' || !entry.id || ids.has(entry.id)) {
        throw new Error('The room catalog contains invalid or duplicate game IDs.');
      }
      ids.add(entry.id);
    }
    ranked.push(...await Promise.all(batch.map(async entry => {
      const bytes = await subtle.digest('SHA-256', encoder.encode(JSON.stringify(['room-v1', code, entry.id])));
      const rank = Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, '0')).join('');
      return { entry, key: `${rank}:${entry.id}` };
    })));
  }
  ranked.sort((a, b) => a.key < b.key ? -1 : a.key > b.key ? 1 : 0);
  return ranked.map(item => item.entry);
}
