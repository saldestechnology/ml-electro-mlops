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
