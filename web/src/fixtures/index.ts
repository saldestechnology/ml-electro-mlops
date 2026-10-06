// Deterministic fixtures that mirror the HTTP contract exactly, served through a fake `fetch`
// so the real client (URL building + parsing) runs unchanged in fixture mode.
//
// Every date is relative to "today" (Stockholm), which is the real current date unless a
// fixed one is injected (the tests do). The story they tell:
// - SE1, SE2: fresh; the latest origin is today and forecasts tomorrow. They also served the
//   origin before the most recent daylight-saving change, so a 23- or 25-hour day is always
//   reachable in the origin picker (or is tomorrow itself).
// - SE3: stale; its last forecast was made two days ago.
// - SE4: a champion that started serving today, so nothing has been scored yet.
// Actual prices reach the dataset with the nightly refresh: delivery days up to today have
// actuals, tomorrow does not.

import type {
  DailyPerformance,
  Forecast,
  ForecastHour,
  Health,
  ModelCard,
  Performance,
  QuantileKey,
  Zone,
  ZoneSummary,
} from '../api';
import { QUANTILE_KEYS, ZONES } from '../api';
import { addDays, localDayHours, stockholmParts, stockholmToday } from '../lib/time';

/** Days of contiguous serving history before today. */
const HISTORY_DAYS = 45;

const QUANTILES = [0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95];
const Z: Record<QuantileKey, number> = {
  q05: -1.645,
  q10: -1.2816,
  q25: -0.6745,
  q50: 0,
  q75: 0.6745,
  q90: 1.2816,
  q95: 1.645,
};

const BACKTEST: Record<Zone, { pinball: number; naive_7d_pinball: number }> = {
  SE1: { pinball: 4.61, naive_7d_pinball: 11.12 },
  SE2: { pinball: 4.53, naive_7d_pinball: 11.48 },
  SE3: { pinball: 5.25, naive_7d_pinball: 11.61 },
  SE4: { pinball: 6.39, naive_7d_pinball: 13.69 },
};

interface PriceProfile {
  level: number; // typical daily mean, EUR/MWh
  swing: number; // relative amplitude of the morning/evening peaks
  vol: number; // relative day-to-day volatility
  version: string; // registry version
}

const PRICE: Record<Zone, PriceProfile> = {
  SE1: { level: 44, swing: 0.35, vol: 0.5, version: '4' },
  SE2: { level: 46, swing: 0.4, vol: 0.5, version: '4' },
  SE3: { level: 66, swing: 0.55, vol: 0.42, version: '3' },
  SE4: { level: 88, swing: 0.6, vol: 0.42, version: '5' },
};

interface Serving {
  origins: string[]; // newest first
  lastFit: string;
}

/** Target date of the most recent daylight-saving change on or before `day` (23/25 hours). */
export function lastDstDay(day: string): string {
  for (let d = day, i = 0; i < 400; d = addDays(d, -1), i++) {
    if (localDayHours(d).length !== 24) return d;
  }
  throw new Error('no DST change within 400 days');
}

/** The Sunday on or before `day`. */
function sundayOnOrBefore(day: string): string {
  return addDays(day, -new Date(`${day}T12:00:00Z`).getUTCDay());
}

function servingPlan(today: string): Record<Zone, Serving> {
  const range = (from: string, to: string) => {
    const out: string[] = [];
    for (let d = to; d >= from; d = addDays(d, -1)) out.push(d);
    return out;
  };
  const start = addDays(today, -HISTORY_DAYS);
  const dstOrigin = addDays(lastDstDay(addDays(today, 1)), -1);
  const withDst = (origins: string[]) =>
    origins.includes(dstOrigin) ? origins : [...origins, dstOrigin];
  const fit = sundayOnOrBefore(today);
  return {
    SE1: { origins: withDst(range(start, today)), lastFit: fit },
    SE2: { origins: withDst(range(start, today)), lastFit: fit },
    SE3: {
      origins: range(start, addDays(today, -2)),
      lastFit: sundayOnOrBefore(addDays(today, -2)),
    },
    SE4: { origins: [today], lastFit: today },
  };
}

// --- deterministic noise -------------------------------------------------------------------

function hash(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}
/** Uniform [0, 1) from a key. */
function uniform(key: string): number {
  let t = hash(key) + 0x6d2b79f5;
  t = Math.imul(t ^ (t >>> 15), t | 1);
  t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
}
/** Standard normal from a key (Box-Muller). */
function normal(key: string): number {
  const u = Math.max(uniform(`${key}:a`), 1e-9);
  const v = uniform(`${key}:b`);
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}
const round2 = (x: number) => Math.round(x * 100) / 100;

// --- the synthetic price process ------------------------------------------------------------

function dayLevel(zone: Zone, date: string): number {
  const p = PRICE[zone];
  const weekend = [0, 6].includes(new Date(`${date}T12:00:00Z`).getUTCDay()) ? 0.82 : 1;
  return p.level * weekend * Math.exp(p.vol * normal(`${zone}:${date}:level`) - p.vol ** 2 / 2);
}

function shape(zone: Zone, hourLocal: number): number {
  const s = PRICE[zone].swing;
  const bump = (c: number, w: number) => Math.exp(-((hourLocal - c) ** 2) / (2 * w * w));
  return 1 - 0.35 * s * bump(3.5, 2.2) + s * bump(8, 1.6) + 0.85 * s * bump(18.5, 2);
}

/** "True" price for a local hour; `dup` distinguishes the repeated hour on the 25-hour day. */
function actualPrice(zone: Zone, date: string, hourLocal: number, dup = 0): number {
  const base = dayLevel(zone, date) * shape(zone, hourLocal);
  const noise = 0.1 * base * normal(`${zone}:${date}:${hourLocal}:${dup}:act`);
  return round2(base + noise);
}

/**
 * A systematic miss of the median, as a fraction of the day's level, by local hour. Mirrors
 * staging on 7 Oct 2026, when SE3 was badly under-forecast at night: applied to SE3's newest
 * forecast day only (which has actuals), so its other days and the scores stay unremarkable.
 */
function medianBias(zone: Zone, target: string, today: string, hourLocal: number): number {
  if (zone !== 'SE3' || target !== addDays(today, -1)) return 0;
  if (hourLocal <= 5) return -1.1;
  if (hourLocal <= 9) return -0.25;
  if (hourLocal <= 16) return -0.45;
  if (hourLocal <= 21) return -0.1;
  return -0.2;
}

function buildForecast(zone: Zone, originDate: string, today: string): Forecast {
  const hasActual = (date: string) => date <= today;
  const target = addDays(originDate, 1);
  const instants = localDayHours(target);
  const seen = new Map<number, number>();
  const level = dayLevel(zone, target);
  // The model gets the day's level roughly right and the hourly shape well.
  const fcLevel = level * Math.exp(0.3 * normal(`${zone}:${target}:fcday`) - 0.045);
  const hours: ForecastHour[] = instants.map((t) => {
    const hourLocal = stockholmParts(t).hour;
    const dup = seen.get(hourLocal) ?? 0;
    seen.set(hourLocal, dup + 1);
    const truth = actualPrice(zone, target, hourLocal, dup);
    // The model's median misses the truth by a modest error; spread grows with the level.
    const sigma = 0.3 * level * shape(zone, hourLocal) + 3;
    const median =
      fcLevel * shape(zone, hourLocal) +
      0.04 * level * normal(`${zone}:${target}:${hourLocal}:${dup}:err`) +
      medianBias(zone, target, today, hourLocal) * level;
    const q = {} as Record<QuantileKey, number>;
    for (const k of QUANTILE_KEYS) {
      const z = Z[k];
      q[k] = round2(median + z * sigma * (z > 0 ? 1.1 : 0.9));
    }
    const naiveDate = addDays(target, -7);
    return {
      target_time: t.toISOString().replace('.000Z', '+00:00'),
      hour_local: hourLocal,
      ...q,
      actual: hasActual(target) ? truth : null,
      naive_7d: hasActual(naiveDate) ? actualPrice(zone, naiveDate, hourLocal) : null,
    };
  });
  return {
    zone,
    origin_date: originDate,
    origin: localIso(originDate, 9),
    target_date: target,
    model: 'ensemble_hourly_exp',
    model_version: PRICE[zone].version,
    forecast_made_at: `${originDate}T07:05:${String(10 + (hash(zone + originDate) % 40)).padStart(2, '0')}+00:00`,
    quantiles: QUANTILES,
    hours,
  };
}

/** ISO instant with the Stockholm offset for a local hour of a date, e.g. 2026-10-24T09:00:00+02:00. */
function localIso(date: string, hour: number): string {
  const t = localDayHours(date).find((d) => stockholmParts(d).hour === hour);
  if (!t) throw new Error(`no local ${hour}:00 on ${date}`);
  const offset = (hour - t.getUTCHours() + 24) % 24;
  return `${date}T${String(hour).padStart(2, '0')}:00:00+${String(offset).padStart(2, '0')}:00`;
}

function pinball(q: number, y: number, f: number): number {
  const e = y - f;
  return Math.max(q * e, (q - 1) * e);
}

function scoreDay(f: Forecast): DailyPerformance | null {
  const scored = f.hours.filter((h) => h.actual !== null && h.naive_7d !== null);
  if (!scored.length) return null;
  let loss = 0;
  let naive = 0;
  let cov90 = 0;
  for (const h of scored) {
    const y = h.actual ?? 0;
    QUANTILE_KEYS.forEach((k, i) => {
      const qv = QUANTILES[i] ?? 0.5;
      loss += pinball(qv, y, h[k]);
      naive += pinball(qv, y, h.naive_7d ?? 0);
    });
    if (y >= h.q05 && y <= h.q95) cov90++;
  }
  const n = scored.length * QUANTILE_KEYS.length;
  return {
    origin_date: f.origin_date,
    pinball: round2(loss / n),
    naive_7d_pinball: round2(naive / n),
    coverage_90: round2(cov90 / scored.length),
  };
}

function buildPerformance(zone: Zone, days: number, origins: string[], today: string): Performance {
  const cutoff = addDays(today, -days);
  const forecasts = origins.filter((o) => o > cutoff).map((o) => buildForecast(zone, o, today));
  const daily = forecasts
    .map(scoreDay)
    .filter((d): d is DailyPerformance => d !== null)
    .sort((a, b) => a.origin_date.localeCompare(b.origin_date));
  let live: Performance['live'] = null;
  if (daily.length) {
    const hours = forecasts.flatMap((f) => f.hours).filter((h) => h.actual !== null);
    const inside = (lo: QuantileKey, hi: QuantileKey) =>
      hours.filter((h) => (h.actual ?? 0) >= h[lo] && (h.actual ?? 0) <= h[hi]).length /
      hours.length;
    const pb = daily.reduce((s, d) => s + d.pinball, 0) / daily.length;
    const nb = daily.reduce((s, d) => s + d.naive_7d_pinball, 0) / daily.length;
    live = {
      pinball: round2(pb),
      naive_7d_pinball: round2(nb),
      skill: Math.round((1 - pb / nb) * 1000) / 1000,
      coverage_50: round2(inside('q25', 'q75')),
      coverage_90: round2(inside('q05', 'q95')),
      mae_median: round2(
        hours.reduce((s, h) => s + Math.abs((h.actual ?? 0) - h.q50), 0) / hours.length,
      ),
    };
  }
  return {
    zone,
    days,
    n_origins_scored: daily.length,
    live,
    daily,
    backtest: BACKTEST[zone],
  };
}

const GIT_SHA = 'b9467dd3c1e2';

function buildModel(zone: Zone, plan: Serving): ModelCard {
  const { origins, lastFit } = plan;
  const last = origins[0] ?? null;
  const first = [...origins].sort()[0] ?? null;
  const backups = origins.slice(1, 6);
  return {
    zone,
    spec: 'ensemble_hourly_exp',
    model_version: PRICE[zone].version,
    first_origin: first,
    last_origin: last,
    last_fit: lastFit,
    origins_served: origins.length,
    train_start: '2021-01-01',
    datasets: {
      train: `true_lead/${lastFit}`,
      eval: `true_lead/${last ?? lastFit}`,
      live: `true_lead/${last ?? lastFit}`,
    },
    versions: {
      python: '3.12.11',
      pricefc: '0.6.0',
      lightgbm: '4.6.0',
      timesfm: '2.5.0',
      torch: '2.8.0',
      numpy: '2.3.3',
      pandas: '2.3.2',
      'scikit-learn': '1.7.2',
    },
    git_sha: GIT_SHA,
    saved_at: last ? `${last}T07:05:41+00:00` : null,
    backups,
  };
}

/** Contract-shaped fixtures as seen on `today` (Stockholm calendar date). */
export function createFixtures(today: string) {
  const plan = servingPlan(today);
  return {
    today,
    health: (): Health => ({ status: 'ok', env: 'fixtures', git_sha: GIT_SHA }),
    zones: (): ZoneSummary[] =>
      ZONES.map((zone) => {
        const latest = plan[zone].origins[0] ?? null;
        const f = latest ? buildForecast(zone, latest, today) : null;
        return {
          zone,
          latest_origin: latest,
          target_date: f?.target_date ?? null,
          model_version: f?.model_version ?? null,
          forecast_made_at: f?.forecast_made_at ?? null,
          last_fit: plan[zone].lastFit,
          stale: latest === null || latest < today,
        };
      }),
    origins: (zone: Zone) => ({ zone, origins: plan[zone].origins }),
    forecast: (zone: Zone, origin?: string | null): Forecast | null => {
      const o = origin ?? plan[zone].origins[0];
      return o && plan[zone].origins.includes(o) ? buildForecast(zone, o, today) : null;
    },
    performance: (zone: Zone, days: number) =>
      buildPerformance(zone, days, plan[zone].origins, today),
    model: (zone: Zone) => buildModel(zone, plan[zone]),
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** A `fetch` stand-in answering the contract's endpoints from fixtures for a fixed `today`. */
export function createFixtureFetch(today: string) {
  const fixtures = createFixtures(today);
  return (input: string): Promise<Response> => answer(fixtures, input);
}

/** The same, for the real current date in Stockholm (re-evaluated on every request). */
export function fixtureFetch(input: string): Promise<Response> {
  return answer(createFixtures(stockholmToday()), input);
}

function answer(fixtures: ReturnType<typeof createFixtures>, input: string): Promise<Response> {
  const url = new URL(input, 'http://fixtures.local');
  const path = url.pathname.replace(/\/+$/, '');
  const m = /^\/api\/zones\/([^/]+)(?:\/(origins|forecast|performance|model))?$/.exec(path);
  let res: Response;
  if (path === '/api/health') res = json(fixtures.health());
  else if (path === '/api/zones') res = json(fixtures.zones());
  else if (m?.[2] && (ZONES as readonly string[]).includes(m[1] ?? '')) {
    const zone = m[1] as Zone;
    switch (m[2]) {
      case 'origins':
        res = json(fixtures.origins(zone));
        break;
      case 'forecast': {
        const origin = url.searchParams.get('origin');
        if (origin !== null && !/^\d{4}-\d{2}-\d{2}$/.test(origin)) {
          res = json({ detail: 'origin must be YYYY-MM-DD' }, 422);
          break;
        }
        const f = fixtures.forecast(zone, origin);
        res = f ? json(f) : json({ detail: `no forecast for ${zone} origin ${origin ?? ''}` }, 404);
        break;
      }
      case 'performance':
        res = json(fixtures.performance(zone, Number(url.searchParams.get('days') ?? 30)));
        break;
      default:
        res = json(fixtures.model(zone));
    }
  } else if (m) res = json({ detail: `unknown zone ${m[1] ?? ''}` }, 404);
  else res = json({ detail: 'Not Found' }, 404);
  return Promise.resolve(res);
}
