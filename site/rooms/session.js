import { clockPosition, modulo, normalizeCode, roomOrder, MAX_HISTORY } from './order.js';

const STORAGE_KEY = 'useless-vibe:rooms:v1';
const MAX_ROOMS = 20;

function readPositions(storage) {
  try {
    const value = JSON.parse(storage?.getItem(STORAGE_KEY) || '[]');
    return Array.isArray(value) ? value.slice(-MAX_ROOMS).filter(row => row && typeof row.key === 'string') : [];
  } catch { return []; }
}

/** Testable room state. No sockets, host, remote control, or timer-start timestamps. */
export function createRoomSession({ loadCatalog, storage = null, now = Date.now, onChange = () => {} }) {
  let room = null;
  let serial = 0;
  let busy = false;
  let error = '';
  let wanted = null;

  function snapshot() {
    return {
      code: room?.code ?? '', edition: room?.catalog.edition ?? '',
      position: room?.position ?? 0, total: room?.order.length ?? 0,
      auto: room?.auto ?? false, site: room?.site ?? null, busy, error,
      history: room ? room.visits.map(position => ({ ...room.order[position], position })) : [],
    };
  }
  function emit(navigation = 'none') { onChange(snapshot(), navigation); }
  function save() {
    if (!room) return;
    try {
      const rows = readPositions(storage).filter(row => row.key !== room.key);
      rows.push({ key: room.key, position: room.position, visits: room.visits });
      storage?.setItem(STORAGE_KEY, JSON.stringify(rows.slice(-MAX_ROOMS)));
    } catch { /* Private/blocked/full storage does not prevent joining or playing. */ }
  }
  async function select(position, navigation = 'push') {
    if (!room) return;
    const current = room;
    position = modulo(position, current.order.length);
    const request = ++serial;
    wanted = position;
    busy = true;
    error = '';
    emit();
    try {
      const site = await current.catalog.siteFor(current.order[position]);
      if (request !== serial || current !== room) return;
      if (current.auto && position !== clockPosition(now(), current.order.length)) {
        // A request that crosses a minute boundary must not display the old slot.
        return select(clockPosition(now(), current.order.length), 'replace');
      }
      current.position = position;
      current.site = site;
      if (current.visits.at(-1) !== position) current.visits.push(position);
      current.visits = current.visits.slice(-MAX_HISTORY);
      busy = false;
      wanted = null;
      save();
      emit(navigation);
    } catch (failure) {
      if (request !== serial || current !== room) return;
      busy = false;
      wanted = null;
      current.auto = false;
      error = failure.message || 'The game could not load. Try joining the room again.';
      emit();
    }
  }
  return {
    get state() { return snapshot(); },
    async join(rawCode, { edition = null, position = null, navigation = 'push' } = {}) {
      const request = ++serial;
      // Joining and refreshing always start in manual mode, including stored sessions.
      room = null;
      busy = true;
      wanted = null;
      error = '';
      emit();
      try {
        const code = normalizeCode(rawCode);
        const catalog = await loadCatalog(edition);
        const order = await roomOrder(code, catalog.entries);
        if (request !== serial) return;
        const key = `${code}:${catalog.edition}`;
        const saved = readPositions(storage).find(row => row.key === key);
        const valid = value => Number.isSafeInteger(value) && value >= 0 && value < order.length;
        const start = valid(position) ? position : valid(saved?.position) ? saved.position : 0;
        const visits = Array.isArray(saved?.visits) ? saved.visits.filter(valid).slice(-MAX_HISTORY) : [];
        room = { key, code, catalog, order, position: start, visits, site: null, auto: false };
        return select(start, navigation);
      } catch (failure) {
        if (request !== serial) return;
        busy = false;
        error = failure.message || 'This room could not load. Try again.';
        emit();
      }
    },
    async move(delta) {
      if (!room || !Number.isSafeInteger(delta)) return;
      room.auto = false;
      return select(room.position + delta);
    },
    async go(position, navigation = 'push') {
      if (!room || !Number.isSafeInteger(position)) return;
      room.auto = false;
      return select(position, navigation);
    },
    async setAuto(enabled) {
      if (!room?.site) return;
      room.auto = Boolean(enabled);
      if (room.auto) return select(clockPosition(now(), room.order.length), 'replace');
      // Cancel an in-flight clock request; switching off keeps the displayed game.
      ++serial;
      busy = false;
      wanted = null;
      emit();
    },
    async tick() {
      if (!room?.auto) return;
      const position = clockPosition(now(), room.order.length);
      if ((!busy && room.site && position === room.position) || (busy && wanted === position)) return;
      return select(position, 'replace');
    },
    leave() {
      ++serial;
      room = null;
      busy = false;
      error = '';
      wanted = null;
      emit();
    },
  };
}

export function shareRoomUrl(href, code, edition) {
  const url = new URL(href);
  url.search = '';
  url.hash = '';
  url.searchParams.set('room', normalizeCode(code));
  url.searchParams.set('v', edition);
  // A shared link never opts anyone into auto-rotation or copies the sender's position.
  return url.href;
}
