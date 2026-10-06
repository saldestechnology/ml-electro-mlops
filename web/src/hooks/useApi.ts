import { createContext, useContext, useEffect, useState } from 'react';
import type { ApiClient } from '../api';

export const ApiContext = createContext<ApiClient | null>(null);

export function useClient(): ApiClient {
  const c = useContext(ApiContext);
  if (!c) throw new Error('ApiContext missing');
  return c;
}

export type Loadable<T> =
  { status: 'loading' } | { status: 'error'; error: Error } | { status: 'ok'; data: T };

interface Settled<T> {
  key: string;
  result: Loadable<T>;
}

/**
 * Fetch with the app's client whenever `key` changes. The result remembers which key it
 * belongs to, so a stale response is never shown for a new key (and no state is set
 * synchronously inside the effect).
 */
export function useApi<T>(
  key: string | null,
  load: (client: ApiClient, signal: AbortSignal) => Promise<T>,
): Loadable<T> {
  const client = useClient();
  const [settled, setSettled] = useState<Settled<T> | null>(null);
  useEffect(() => {
    if (key === null) return;
    const ctrl = new AbortController();
    load(client, ctrl.signal).then(
      (data) => {
        if (!ctrl.signal.aborted) setSettled({ key, result: { status: 'ok', data } });
      },
      (e: unknown) => {
        if (ctrl.signal.aborted) return;
        const error = e instanceof Error ? e : new Error(String(e));
        setSettled({ key, result: { status: 'error', error } });
      },
    );
    return () => {
      ctrl.abort();
    };
    // `load` is expected to be described fully by `key`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, key]);
  if (key === null || !settled || settled.key !== key) return { status: 'loading' };
  return settled.result;
}
