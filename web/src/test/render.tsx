import { render } from '@testing-library/react';
import { vi } from 'vitest';
import { MemoryRouter } from 'react-router';
import { App } from '../App';
import { createClient } from '../api';
import type { FetchLike } from '../api';
import { fixtureFetch } from '../fixtures';

/** Pin the clock (Date only, timers stay real) to a fixed Stockholm afternoon. */
export function pinToday(isoInstant = '2026-10-24T13:00:00Z') {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date(isoInstant));
}

export function renderApp(path: string, fetchImpl: FetchLike = fixtureFetch) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App client={createClient(fetchImpl)} />
    </MemoryRouter>,
  );
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

/** A fresh deployment: no forecasts, no scores, no served states yet. */
export const emptyFetch: FetchLike = (url) => {
  const zone = /\/zones\/(SE\d)/.exec(url)?.[1] ?? 'SE1';
  if (url.endsWith('/health'))
    return Promise.resolve(json({ status: 'ok', env: 'dev', git_sha: 'unknown' }));
  if (url.endsWith('/zones'))
    return Promise.resolve(
      json(
        ['SE1', 'SE2', 'SE3', 'SE4'].map((z) => ({
          zone: z,
          latest_origin: null,
          target_date: null,
          model_version: null,
          forecast_made_at: null,
          last_fit: null,
          stale: true,
        })),
      ),
    );
  if (url.endsWith('/origins')) return Promise.resolve(json({ zone, origins: [] }));
  if (url.includes('/performance'))
    return Promise.resolve(
      json({
        zone,
        days: 30,
        n_origins_scored: 0,
        live: null,
        daily: [],
        backtest: { pinball: 5, naive_7d_pinball: 11 },
      }),
    );
  return Promise.resolve(json({ detail: `no served state for ${zone}` }, 404));
};
