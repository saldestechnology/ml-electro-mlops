import type { ForecastHour } from '../api';
import { mean } from './format';

export interface DaySummary {
  meanMedian: number;
  peak: { index: number; value: number };
  trough: { index: number; value: number };
  band90: number; // mean width q95 - q05
  actualMean: number | null; // only when every hour has an actual
  hoursWithActual: number;
}

export function summarise(hours: ForecastHour[]): DaySummary {
  let peak = { index: 0, value: -Infinity };
  let trough = { index: 0, value: Infinity };
  hours.forEach((h, index) => {
    if (h.q50 > peak.value) peak = { index, value: h.q50 };
    if (h.q50 < trough.value) trough = { index, value: h.q50 };
  });
  const actuals = hours.map((h) => h.actual).filter((v): v is number => v !== null);
  return {
    meanMedian: mean(hours.map((h) => h.q50)),
    peak,
    trough,
    band90: mean(hours.map((h) => h.q95 - h.q05)),
    actualMean: actuals.length === hours.length && hours.length ? mean(actuals) : null,
    hoursWithActual: actuals.length,
  };
}

// --- forecast error against published actuals --------------------------------------------

/**
 * Parts of the delivery day, by Stockholm local hour (inclusive). Defined once here; keyed on
 * `hour_local`, so the 23- and 25-hour DST days fall into the right part without slot maths.
 */
export const DAY_PARTS = [
  { key: 'night', label: 'Night', from: 0, to: 5 },
  { key: 'morning', label: 'Morning', from: 6, to: 9 },
  { key: 'day', label: 'Day', from: 10, to: 16 },
  { key: 'evening', label: 'Evening', from: 17, to: 21 },
  { key: 'late', label: 'Late', from: 22, to: 23 },
] as const;

export type DayPartKey = (typeof DAY_PARTS)[number]['key'];

export interface PartBias {
  key: DayPartKey;
  label: string;
  from: number;
  to: number;
  /** Mean of q50 − actual over the part's hours that have an actual; null when none do. */
  bias: number | null;
  hours: number; // hours with an actual in this part
}

export interface ForecastError {
  hours: number; // hours with an actual
  totalHours: number; // hours in the delivery day (23, 24 or 25)
  parts: PartBias[];
  /** The part with the largest absolute bias (the only one shown in signal red). */
  largest: DayPartKey | null;
  mae: number; // mean |q50 − actual|
  aboveQ95: number;
  belowQ05: number;
}

export function dayPartOf(hourLocal: number): DayPartKey {
  const p = DAY_PARTS.find((d) => hourLocal >= d.from && hourLocal <= d.to);
  if (!p) throw new RangeError(`hour_local out of range: ${String(hourLocal)}`);
  return p.key;
}

/** Errors of the median against actuals, over the hours that have one; null when none do. */
export function forecastError(hours: ForecastHour[]): ForecastError | null {
  const scored = hours.filter((h): h is ForecastHour & { actual: number } => h.actual !== null);
  if (!scored.length) return null;
  const parts: PartBias[] = DAY_PARTS.map((p) => {
    const errs = scored
      .filter((h) => h.hour_local >= p.from && h.hour_local <= p.to)
      .map((h) => h.q50 - h.actual);
    return { ...p, bias: errs.length ? mean(errs) : null, hours: errs.length };
  });
  let largest: PartBias | null = null;
  for (const p of parts) {
    if (p.bias === null) continue;
    if (!largest || Math.abs(p.bias) > Math.abs(largest.bias ?? 0)) largest = p;
  }
  return {
    hours: scored.length,
    totalHours: hours.length,
    parts,
    largest: largest?.key ?? null,
    mae: mean(scored.map((h) => Math.abs(h.q50 - h.actual))),
    aboveQ95: scored.filter((h) => h.actual > h.q95).length,
    belowQ05: scored.filter((h) => h.actual < h.q05).length,
  };
}
