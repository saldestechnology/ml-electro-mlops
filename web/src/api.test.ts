import {
  ApiError,
  ContractError,
  createClient,
  parseForecast,
  parseModel,
  parsePerformance,
  parseZones,
} from './api';
import type { FetchLike } from './api';

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

// Literal payloads written straight from the contract, independent of the fixture generator.
const ZONES_BODY = [
  {
    zone: 'SE1',
    latest_origin: '2026-10-06',
    target_date: '2026-10-07',
    model_version: '4',
    forecast_made_at: '2026-10-06T07:05:12+00:00',
    last_fit: '2026-10-04',
    stale: false,
  },
  {
    zone: 'SE2',
    latest_origin: null,
    target_date: null,
    model_version: null,
    forecast_made_at: null,
    last_fit: null,
    stale: true,
  },
  {
    zone: 'SE3',
    latest_origin: '2026-10-05',
    target_date: '2026-10-06',
    model_version: '3',
    forecast_made_at: '2026-10-05T07:05:40+00:00',
    last_fit: '2026-10-04',
    stale: true,
  },
  {
    zone: 'SE4',
    latest_origin: '2026-10-06',
    target_date: '2026-10-07',
    model_version: '5',
    forecast_made_at: '2026-10-06T07:05:33+00:00',
    last_fit: null,
    stale: false,
  },
];

const hour = (i: number, extra: Record<string, unknown> = {}) => ({
  target_time: `2026-10-06T${String(22 + i).padStart(2, '0')}:00:00+00:00`,
  hour_local: i,
  q05: 10 + i,
  q10: 12 + i,
  q25: 15 + i,
  q50: 20 + i,
  q75: 25 + i,
  q90: 29 + i,
  q95: 32 + i,
  actual: null,
  naive_7d: 18.5,
  ...extra,
});

const FORECAST_BODY = {
  zone: 'SE3',
  origin_date: '2026-10-06',
  origin: '2026-10-06T09:00:00+02:00',
  target_date: '2026-10-07',
  model: 'ensemble_hourly_exp',
  model_version: '3',
  forecast_made_at: '2026-10-06T07:05:12+00:00',
  quantiles: [0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95],
  hours: [hour(0, { actual: 21.2 }), hour(1, { naive_7d: null })],
};

describe('contract parsers', () => {
  it('parses /api/zones including nulls', () => {
    const z = parseZones(ZONES_BODY);
    expect(z.map((s) => s.zone)).toEqual(['SE1', 'SE2', 'SE3', 'SE4']);
    expect(z[1]?.latest_origin).toBeNull();
    expect(z[2]?.stale).toBe(true);
  });

  it('parses a forecast with nullable actual and naive', () => {
    const f = parseForecast(FORECAST_BODY);
    expect(f.hours).toHaveLength(2);
    expect(f.hours[0]?.actual).toBe(21.2);
    expect(f.hours[1]?.naive_7d).toBeNull();
    expect(f.quantiles).toHaveLength(7);
  });

  it('rejects contract drift loudly', () => {
    expect(() => parseForecast({ ...FORECAST_BODY, hours: [hour(0, { q50: 'x' })] })).toThrow(
      ContractError,
    );
    expect(() => parseForecast({ ...FORECAST_BODY, hours: [hour(24)] })).toThrow(/hour_local/);
    expect(() => parseZones([{ ...ZONES_BODY[0], zone: 'NO1' }])).toThrow(/SE1..SE4/);
  });

  it('names the failing path for a missing field', () => {
    const { model, ...rest } = FORECAST_BODY;
    expect(model).toBeDefined();
    expect(() => parseForecast(rest)).toThrow(
      'contract mismatch at forecast.model: expected string',
    );
    const { q50, ...noQ50 } = hour(1);
    expect(q50).toBeDefined();
    expect(() => parseForecast({ ...FORECAST_BODY, hours: [hour(0), noQ50] })).toThrow(
      /forecast\.hours\[1\]\.q50: expected number/,
    );
  });

  it('names the failing path for a wrong type', () => {
    expect(() => parseZones([{ ...ZONES_BODY[0] }, { ...ZONES_BODY[1], stale: 'no' }])).toThrow(
      /zones\[1\]\.stale: expected boolean, got "no"/,
    );
    expect(() => parseZones({})).toThrow(/zones: expected array/);
  });

  it('rejects non-finite numbers', () => {
    for (const bad of [Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(() => parseForecast({ ...FORECAST_BODY, hours: [hour(0, { q05: bad })] })).toThrow(
        ContractError,
      );
    }
    expect(() => parseForecast({ ...FORECAST_BODY, quantiles: [Infinity] })).toThrow(
      /forecast\.quantiles\[0\]/,
    );
  });

  it('parses performance with and without live scores', () => {
    const base = {
      zone: 'SE4',
      days: 30,
      n_origins_scored: 0,
      live: null,
      daily: [],
      backtest: { pinball: 6.39, naive_7d_pinball: 13.69 },
    };
    expect(parsePerformance(base).live).toBeNull();
    const scored = parsePerformance({
      ...base,
      n_origins_scored: 1,
      live: {
        pinball: 5,
        naive_7d_pinball: 12,
        skill: 0.583,
        coverage_50: 0.5,
        coverage_90: 0.875,
        mae_median: 14.2,
      },
      daily: [{ origin_date: '2026-10-05', pinball: 5, naive_7d_pinball: 12, coverage_90: 0.875 }],
    });
    expect(scored.live?.skill).toBeCloseTo(0.583);
    expect(scored.daily[0]?.origin_date).toBe('2026-10-05');
  });

  it('parses a model card', () => {
    const m = parseModel({
      zone: 'SE1',
      spec: 'ensemble_hourly_exp',
      model_version: null,
      first_origin: '2026-09-01',
      last_origin: '2026-10-06',
      last_fit: '2026-10-04',
      origins_served: 36,
      train_start: '2021-01-01',
      datasets: { train: 'a', eval: 'b', live: 'c' },
      versions: { python: '3.12.11', lightgbm: '4.6.0' },
      git_sha: 'unknown',
      saved_at: '2026-10-06T07:05:41+00:00',
      backups: ['2026-10-05'],
    });
    expect(m.model_version).toBeNull();
    expect(m.versions.lightgbm).toBe('4.6.0');
  });
});

describe('client', () => {
  it('builds the contract URLs', async () => {
    const calls: string[] = [];
    const fetchImpl: FetchLike = (url) => {
      calls.push(url);
      if (url.endsWith('/forecast?origin=2026-10-06') || url.endsWith('/forecast'))
        return Promise.resolve(json(FORECAST_BODY));
      if (url.includes('/performance'))
        return Promise.resolve(
          json({
            zone: 'SE3',
            days: 7,
            n_origins_scored: 0,
            live: null,
            daily: [],
            backtest: { pinball: 5.25, naive_7d_pinball: 11.61 },
          }),
        );
      if (url.endsWith('/origins'))
        return Promise.resolve(json({ zone: 'SE3', origins: ['2026-10-06'] }));
      if (url.endsWith('/health'))
        return Promise.resolve(json({ status: 'ok', env: 'dev', git_sha: 'unknown' }));
      return Promise.resolve(json(ZONES_BODY));
    };
    const c = createClient(fetchImpl);
    await c.health();
    await c.zones();
    await c.origins('SE3');
    await c.forecast('SE3');
    await c.forecast('SE3', '2026-10-06');
    await c.performance('SE3', 7);
    expect(calls).toEqual([
      '/api/health',
      '/api/zones',
      '/api/zones/SE3/origins',
      '/api/zones/SE3/forecast',
      '/api/zones/SE3/forecast?origin=2026-10-06',
      '/api/zones/SE3/performance?days=7',
    ]);
  });

  it('surfaces {"detail"} errors as ApiError', async () => {
    const c = createClient(() => Promise.resolve(json({ detail: 'no such origin' }, 404)));
    await expect(c.forecast('SE1', '2020-01-01')).rejects.toMatchObject({
      name: 'ApiError',
      status: 404,
      detail: 'no such origin',
    });
    await expect(c.model('SE1')).rejects.toBeInstanceOf(ApiError);
  });
});
