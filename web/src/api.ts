// Typed client for the pricefc read-only HTTP API (see the web contract).
// Every response is checked at runtime against zod schemas of the contract shape, so a drift
// between the API and the frontend fails loudly in one place instead of rendering garbage.
// The exported types are inferred from the schemas: one source of truth.

import { z } from 'zod';

export const ZONES = ['SE1', 'SE2', 'SE3', 'SE4'] as const;
export type Zone = (typeof ZONES)[number];

export const QUANTILE_KEYS = ['q05', 'q10', 'q25', 'q50', 'q75', 'q90', 'q95'] as const;
export type QuantileKey = (typeof QUANTILE_KEYS)[number];

// ---------------------------------------------------------------------------------------
// Schemas (the contract). Unknown keys are stripped; nullable fields also accept a missing key.

const zoneSchema = z.enum(ZONES, { error: 'SE1..SE4' });
const str = z.string();
const num = z.number(); // finite numbers only (zod rejects NaN and +-Infinity)
const strOrNull = z
  .string()
  .nullish()
  .transform((v) => v ?? null);
const numOrNull = z
  .number()
  .nullish()
  .transform((v) => v ?? null);

/** String map that tolerates numbers (stringified) and drops null/missing entries. */
const strMap = z
  .record(z.string(), z.union([z.string(), z.number().transform(String), z.null(), z.undefined()]))
  .transform((m) => {
    const out: Record<string, string> = {};
    for (const [k, v] of Object.entries(m)) if (v !== null && v !== undefined) out[k] = v;
    return out;
  });

export const healthSchema = z.object({ status: str, env: str, git_sha: str });
export type Health = z.infer<typeof healthSchema>;

export const zoneSummarySchema = z.object({
  zone: zoneSchema,
  latest_origin: strOrNull,
  target_date: strOrNull,
  model_version: strOrNull,
  forecast_made_at: strOrNull,
  last_fit: strOrNull,
  stale: z.boolean(),
});
export type ZoneSummary = z.infer<typeof zoneSummarySchema>;

export const originsSchema = z.object({ zone: zoneSchema, origins: z.array(str) });
export type Origins = z.infer<typeof originsSchema>;

const forecastHourSchema = z.object({
  target_time: str,
  hour_local: num.refine((n) => Number.isInteger(n) && n >= 0 && n <= 23, {
    error: 'integer 0..23',
  }),
  q05: num,
  q10: num,
  q25: num,
  q50: num,
  q75: num,
  q90: num,
  q95: num,
  actual: numOrNull,
  naive_7d: numOrNull,
});
export type ForecastHour = z.infer<typeof forecastHourSchema>;

export const forecastSchema = z.object({
  zone: zoneSchema,
  origin_date: str,
  origin: str,
  target_date: str,
  model: str,
  model_version: str,
  forecast_made_at: str,
  quantiles: z.array(num),
  hours: z.array(forecastHourSchema),
});
export type Forecast = z.infer<typeof forecastSchema>;

const livePerformanceSchema = z.object({
  pinball: num,
  naive_7d_pinball: num,
  skill: num,
  coverage_50: num,
  coverage_90: num,
  mae_median: num,
});
export type LivePerformance = z.infer<typeof livePerformanceSchema>;

const dailyPerformanceSchema = z.object({
  origin_date: str,
  pinball: num,
  naive_7d_pinball: num,
  coverage_90: num,
});
export type DailyPerformance = z.infer<typeof dailyPerformanceSchema>;

export const performanceSchema = z.object({
  zone: zoneSchema,
  days: num,
  n_origins_scored: num,
  live: livePerformanceSchema.nullish().transform((v) => v ?? null),
  daily: z.array(dailyPerformanceSchema),
  backtest: z.object({ pinball: num, naive_7d_pinball: num }),
});
export type Performance = z.infer<typeof performanceSchema>;

export const modelCardSchema = z.object({
  zone: zoneSchema,
  spec: str,
  model_version: strOrNull,
  first_origin: strOrNull,
  last_origin: strOrNull,
  last_fit: strOrNull,
  origins_served: num,
  train_start: strOrNull,
  datasets: strMap,
  versions: strMap,
  git_sha: str,
  saved_at: strOrNull,
  backups: z.array(str),
});
export type ModelCard = z.infer<typeof modelCardSchema>;

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

/** `forecast.hours[3].q50` style path from a zod issue path. */
function formatPath(root: string, path: readonly PropertyKey[]): string {
  return path.reduce<string>(
    (acc, k) => (typeof k === 'number' ? `${acc}[${k}]` : `${acc}.${String(k)}`),
    root,
  );
}

function valueAt(input: unknown, path: readonly PropertyKey[]): unknown {
  let cur = input;
  for (const k of path) {
    if (typeof cur !== 'object' || cur === null) return undefined;
    cur = (cur as Record<PropertyKey, unknown>)[k];
  }
  return cur;
}

/** Parse `input` with `schema`, reporting the first failure as a ContractError naming its path. */
function parseContract<S extends z.ZodType>(schema: S, input: unknown, root: string): z.infer<S> {
  const r = schema.safeParse(input);
  if (r.success) return r.data;
  const issue = r.error.issues[0];
  if (!issue) throw new ContractError(root, 'valid value', input);
  const expected = issue.code === 'invalid_type' ? issue.expected : issue.message;
  throw new ContractError(formatPath(root, issue.path), expected, valueAt(input, issue.path));
}

export const parseHealth = (v: unknown): Health => parseContract(healthSchema, v, 'health');
export const parseZones = (v: unknown): ZoneSummary[] =>
  parseContract(z.array(zoneSummarySchema), v, 'zones');
export const parseOrigins = (v: unknown): Origins => parseContract(originsSchema, v, 'origins');
export const parseForecast = (v: unknown): Forecast => parseContract(forecastSchema, v, 'forecast');
export const parsePerformance = (v: unknown): Performance =>
  parseContract(performanceSchema, v, 'performance');
export const parseModel = (v: unknown): ModelCard => parseContract(modelCardSchema, v, 'model');

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
