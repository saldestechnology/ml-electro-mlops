// Typed client for the pricefc read-only HTTP API (see the web contract).
// Every response is checked at runtime against the contract shape, so a drift between the
// API and the frontend fails loudly in one place instead of rendering garbage.

export const ZONES = ['SE1', 'SE2', 'SE3', 'SE4'] as const;
export type Zone = (typeof ZONES)[number];

export const QUANTILE_KEYS = ['q05', 'q10', 'q25', 'q50', 'q75', 'q90', 'q95'] as const;
export type QuantileKey = (typeof QUANTILE_KEYS)[number];

export interface Health {
  status: string;
  env: string;
  git_sha: string;
}

export interface ZoneSummary {
  zone: Zone;
  latest_origin: string | null;
  target_date: string | null;
  model_version: string | null;
  forecast_made_at: string | null;
  last_fit: string | null;
  stale: boolean;
}

export interface Origins {
  zone: Zone;
  origins: string[];
}

export type ForecastHour = Record<QuantileKey, number> & {
  target_time: string;
  hour_local: number;
  actual: number | null;
  naive_7d: number | null;
};

export interface Forecast {
  zone: Zone;
  origin_date: string;
  origin: string;
  target_date: string;
  model: string;
  model_version: string;
  forecast_made_at: string;
  quantiles: number[];
  hours: ForecastHour[];
}

export interface LivePerformance {
  pinball: number;
  naive_7d_pinball: number;
  skill: number;
  coverage_50: number;
  coverage_90: number;
  mae_median: number;
}

export interface DailyPerformance {
  origin_date: string;
  pinball: number;
  naive_7d_pinball: number;
  coverage_90: number;
}

export interface Performance {
  zone: Zone;
  days: number;
  n_origins_scored: number;
  live: LivePerformance | null;
  daily: DailyPerformance[];
  backtest: { pinball: number; naive_7d_pinball: number };
}

export interface ModelCard {
  zone: Zone;
  spec: string;
  model_version: string | null;
  first_origin: string | null;
  last_origin: string | null;
  last_fit: string | null;
  origins_served: number;
  train_start: string | null;
  datasets: Record<string, string>;
  versions: Record<string, string>;
  git_sha: string;
  saved_at: string | null;
  backups: string[];
}

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  constructor(status: number, detail: string) {
    super(status ? `${status}: ${detail}` : detail);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

/** Thrown when a response does not have the shape the contract promises. */
export class ContractError extends Error {
  constructor(path: string, expected: string, got: unknown) {
    super(`contract mismatch at ${path}: expected ${expected}, got ${JSON.stringify(got)}`);
    this.name = 'ContractError';
  }
}

// ---------------------------------------------------------------------------------------
// Small runtime validators (no schema library: the contract is small and fixed).

type Obj = Record<string, unknown>;

function obj(v: unknown, path: string): Obj {
  if (typeof v !== 'object' || v === null || Array.isArray(v)) {
    throw new ContractError(path, 'object', v);
  }
  return v as Obj;
}
function arr(v: unknown, path: string): unknown[] {
  if (!Array.isArray(v)) throw new ContractError(path, 'array', v);
  return v;
}
function str(v: unknown, path: string): string {
  if (typeof v !== 'string') throw new ContractError(path, 'string', v);
  return v;
}
function strOrNull(v: unknown, path: string): string | null {
  return v === null || v === undefined ? null : str(v, path);
}
function num(v: unknown, path: string): number {
  if (typeof v !== 'number' || !Number.isFinite(v)) throw new ContractError(path, 'number', v);
  return v;
}
function numOrNull(v: unknown, path: string): number | null {
  return v === null || v === undefined ? null : num(v, path);
}
function bool(v: unknown, path: string): boolean {
  if (typeof v !== 'boolean') throw new ContractError(path, 'boolean', v);
  return v;
}
function zone(v: unknown, path: string): Zone {
  const s = str(v, path);
  if (!(ZONES as readonly string[]).includes(s)) throw new ContractError(path, 'SE1..SE4', v);
  return s as Zone;
}
function strMap(v: unknown, path: string): Record<string, string> {
  const o = obj(v, path);
  const out: Record<string, string> = {};
  for (const [k, val] of Object.entries(o)) {
    if (val === null || val === undefined) continue;
    out[k] = typeof val === 'number' ? String(val) : str(val, `${path}.${k}`);
  }
  return out;
}

export function parseHealth(v: unknown): Health {
  const o = obj(v, 'health');
  return {
    status: str(o.status, 'health.status'),
    env: str(o.env, 'health.env'),
    git_sha: str(o.git_sha, 'health.git_sha'),
  };
}

export function parseZones(v: unknown): ZoneSummary[] {
  return arr(v, 'zones').map((item, i) => {
    const p = `zones[${i}]`;
    const o = obj(item, p);
    return {
      zone: zone(o.zone, `${p}.zone`),
      latest_origin: strOrNull(o.latest_origin, `${p}.latest_origin`),
      target_date: strOrNull(o.target_date, `${p}.target_date`),
      model_version: strOrNull(o.model_version, `${p}.model_version`),
      forecast_made_at: strOrNull(o.forecast_made_at, `${p}.forecast_made_at`),
      last_fit: strOrNull(o.last_fit, `${p}.last_fit`),
      stale: bool(o.stale, `${p}.stale`),
    };
  });
}

export function parseOrigins(v: unknown): Origins {
  const o = obj(v, 'origins');
  return {
    zone: zone(o.zone, 'origins.zone'),
    origins: arr(o.origins, 'origins.origins').map((d, i) => str(d, `origins.origins[${i}]`)),
  };
}

export function parseForecast(v: unknown): Forecast {
  const o = obj(v, 'forecast');
  const hours = arr(o.hours, 'forecast.hours').map((item, i): ForecastHour => {
    const p = `forecast.hours[${i}]`;
    const h = obj(item, p);
    const hourLocal = num(h.hour_local, `${p}.hour_local`);
    if (!Number.isInteger(hourLocal) || hourLocal < 0 || hourLocal > 23) {
      throw new ContractError(`${p}.hour_local`, 'integer 0..23', hourLocal);
    }
    return {
      target_time: str(h.target_time, `${p}.target_time`),
      hour_local: hourLocal,
      q05: num(h.q05, `${p}.q05`),
      q10: num(h.q10, `${p}.q10`),
      q25: num(h.q25, `${p}.q25`),
      q50: num(h.q50, `${p}.q50`),
      q75: num(h.q75, `${p}.q75`),
      q90: num(h.q90, `${p}.q90`),
      q95: num(h.q95, `${p}.q95`),
      actual: numOrNull(h.actual, `${p}.actual`),
      naive_7d: numOrNull(h.naive_7d, `${p}.naive_7d`),
    };
  });
  return {
    zone: zone(o.zone, 'forecast.zone'),
    origin_date: str(o.origin_date, 'forecast.origin_date'),
    origin: str(o.origin, 'forecast.origin'),
    target_date: str(o.target_date, 'forecast.target_date'),
    model: str(o.model, 'forecast.model'),
    model_version: str(o.model_version, 'forecast.model_version'),
    forecast_made_at: str(o.forecast_made_at, 'forecast.forecast_made_at'),
    quantiles: arr(o.quantiles, 'forecast.quantiles').map((q, i) =>
      num(q, `forecast.quantiles[${i}]`),
    ),
    hours,
  };
}

export function parsePerformance(v: unknown): Performance {
  const o = obj(v, 'performance');
  let live: LivePerformance | null = null;
  if (o.live !== null && o.live !== undefined) {
    const l = obj(o.live, 'performance.live');
    live = {
      pinball: num(l.pinball, 'performance.live.pinball'),
      naive_7d_pinball: num(l.naive_7d_pinball, 'performance.live.naive_7d_pinball'),
      skill: num(l.skill, 'performance.live.skill'),
      coverage_50: num(l.coverage_50, 'performance.live.coverage_50'),
      coverage_90: num(l.coverage_90, 'performance.live.coverage_90'),
      mae_median: num(l.mae_median, 'performance.live.mae_median'),
    };
  }
  const b = obj(o.backtest, 'performance.backtest');
  return {
    zone: zone(o.zone, 'performance.zone'),
    days: num(o.days, 'performance.days'),
    n_origins_scored: num(o.n_origins_scored, 'performance.n_origins_scored'),
    live,
    daily: arr(o.daily, 'performance.daily').map((item, i) => {
      const p = `performance.daily[${i}]`;
      const d = obj(item, p);
      return {
        origin_date: str(d.origin_date, `${p}.origin_date`),
        pinball: num(d.pinball, `${p}.pinball`),
        naive_7d_pinball: num(d.naive_7d_pinball, `${p}.naive_7d_pinball`),
        coverage_90: num(d.coverage_90, `${p}.coverage_90`),
      };
    }),
    backtest: {
      pinball: num(b.pinball, 'performance.backtest.pinball'),
      naive_7d_pinball: num(b.naive_7d_pinball, 'performance.backtest.naive_7d_pinball'),
    },
  };
}

export function parseModel(v: unknown): ModelCard {
  const o = obj(v, 'model');
  return {
    zone: zone(o.zone, 'model.zone'),
    spec: str(o.spec, 'model.spec'),
    model_version: strOrNull(o.model_version, 'model.model_version'),
    first_origin: strOrNull(o.first_origin, 'model.first_origin'),
    last_origin: strOrNull(o.last_origin, 'model.last_origin'),
    last_fit: strOrNull(o.last_fit, 'model.last_fit'),
    origins_served: num(o.origins_served, 'model.origins_served'),
    train_start: strOrNull(o.train_start, 'model.train_start'),
    datasets: strMap(o.datasets, 'model.datasets'),
    versions: strMap(o.versions, 'model.versions'),
    git_sha: str(o.git_sha, 'model.git_sha'),
    saved_at: strOrNull(o.saved_at, 'model.saved_at'),
    backups: arr(o.backups, 'model.backups').map((d, i) => str(d, `model.backups[${i}]`)),
  };
}

// ---------------------------------------------------------------------------------------
// Client

export type FetchLike = (input: string, init?: { signal?: AbortSignal }) => Promise<Response>;

export interface ApiClient {
  health(signal?: AbortSignal): Promise<Health>;
  zones(signal?: AbortSignal): Promise<ZoneSummary[]>;
  origins(zone: Zone, signal?: AbortSignal): Promise<Origins>;
  forecast(zone: Zone, origin?: string | null, signal?: AbortSignal): Promise<Forecast>;
  performance(zone: Zone, days?: number, signal?: AbortSignal): Promise<Performance>;
  model(zone: Zone, signal?: AbortSignal): Promise<ModelCard>;
}

export function createClient(fetchImpl: FetchLike, base = '/api'): ApiClient {
  async function get<T>(path: string, parse: (v: unknown) => T, signal?: AbortSignal) {
    const res = await fetchImpl(`${base}${path}`, signal ? { signal } : {});
    if (!res.ok) {
      let detail = res.statusText || 'request failed';
      try {
        const body = (await res.json()) as unknown;
        if (typeof body === 'object' && body !== null && 'detail' in body) {
          const d = body.detail;
          detail = typeof d === 'string' ? d : JSON.stringify(d);
        }
      } catch {
        // body was not JSON; keep the status text
      }
      throw new ApiError(res.status, detail);
    }
    return parse(await res.json());
  }
  const z = (zone: Zone) => encodeURIComponent(zone);
  return {
    health: (signal) => get('/health', parseHealth, signal),
    zones: (signal) => get('/zones', parseZones, signal),
    origins: (zone, signal) => get(`/zones/${z(zone)}/origins`, parseOrigins, signal),
    forecast: (zone, origin, signal) =>
      get(
        `/zones/${z(zone)}/forecast${origin ? `?origin=${encodeURIComponent(origin)}` : ''}`,
        parseForecast,
        signal,
      ),
    performance: (zone, days = 30, signal) =>
      get(`/zones/${z(zone)}/performance?days=${days}`, parsePerformance, signal),
    model: (zone, signal) => get(`/zones/${z(zone)}/model`, parseModel, signal),
  };
}

export const useFixtures = import.meta.env.VITE_USE_FIXTURES === '1';

/** The app's default client: the real API, or the in-memory fixtures when VITE_USE_FIXTURES=1. */
export async function defaultClient(): Promise<ApiClient> {
  if (useFixtures) {
    const { fixtureFetch } = await import('./fixtures');
    return createClient(fixtureFetch);
  }
  return createClient((input, init) => fetch(input, init));
}

export function isZone(v: string | null | undefined): v is Zone {
  return !!v && (ZONES as readonly string[]).includes(v);
}
