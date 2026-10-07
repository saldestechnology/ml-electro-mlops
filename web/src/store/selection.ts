import { create } from 'zustand';
import { isZone } from '../api';
import type { Zone } from '../api';

export const DEFAULT_ZONE: Zone = 'SE3';

const ORIGIN_RE = /^\d{4}-\d{2}-\d{2}$/;

/**
 * What the user is looking at: the bidding zone and the forecast origin date ("the day").
 * `origin: null` means the zone's latest. The URL (`/?zone=&origin=`) stays the source for
 * deep links; the forecast page copies it in on every navigation, and links are built from
 * this state so the chosen day survives a zone switch. Nothing is persisted beyond the URL.
 */
export interface SelectionState {
  zone: Zone;
  origin: string | null;
  setZone: (zone: Zone) => void;
  setOrigin: (origin: string | null) => void;
  /** Replace both at once (URL -> store). A no-op when nothing changes. */
  sync: (zone: Zone, origin: string | null) => void;
  reset: () => void;
}

const initial = { zone: DEFAULT_ZONE, origin: null } as const;

export const useSelection = create<SelectionState>()((set, get) => ({
  ...initial,
  setZone: (zone) => {
    set({ zone });
  },
  setOrigin: (origin) => {
    set({ origin });
  },
  sync: (zone, origin) => {
    const s = get();
    if (s.zone !== zone || s.origin !== origin) set({ zone, origin });
  },
  reset: () => {
    set({ ...initial });
  },
}));

/** Back to the initial selection; for tests. */
export function resetSelection() {
  useSelection.getState().reset();
}

/** Read a selection from URL search params; anything malformed falls back to the defaults. */
export function selectionFromParams(params: URLSearchParams): {
  zone: Zone;
  origin: string | null;
} {
  const z = params.get('zone');
  const o = params.get('origin');
  return { zone: isZone(z) ? z : DEFAULT_ZONE, origin: o !== null && ORIGIN_RE.test(o) ? o : null };
}

/** The forecast page URL for a zone and day (`null` = latest, no `origin` param). */
export function forecastPath(zone: Zone, origin: string | null): string {
  return origin ? `/?zone=${zone}&origin=${origin}` : `/?zone=${zone}`;
}
