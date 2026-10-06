import { createContext, useContext } from 'react';
import type { Health, ZoneSummary } from '../api';
import type { Loadable } from './useApi';

export interface Status {
  health: Loadable<Health>;
  zones: Loadable<ZoneSummary[]>;
}

export const StatusContext = createContext<Status>({
  health: { status: 'loading' },
  zones: { status: 'loading' },
});

export const useStatus = () => useContext(StatusContext);
