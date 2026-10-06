import { scaleLinear } from 'd3-scale';
import type { Performance } from '../../api';
import { addDays } from '../../lib/time';

export interface DailyDomains {
  start: string; // first origin date on the x axis
  days: number; // number of days on the x axis
  max: number; // shared y max
}

export const dayIndex = (start: string, d: string) =>
  Math.round((Date.parse(`${d}T12:00:00Z`) - Date.parse(`${start}T12:00:00Z`)) / 86_400_000);

/** One x range and one y scale for every zone, so the panels compare directly. */
export function dailyDomains(perfs: Performance[]): DailyDomains {
  const dates = perfs.flatMap((p) => p.daily.map((d) => d.origin_date)).sort();
  const last = dates[dates.length - 1];
  const days = Math.max(...perfs.map((p) => p.days), 1);
  const end = last ?? new Date().toISOString().slice(0, 10);
  const values = perfs.flatMap((p) => p.daily.flatMap((d) => [d.pinball, d.naive_7d_pinball]));
  const max = values.length ? Math.max(...values) : 10;
  const nice = scaleLinear().domain([0, max]).nice(4).domain()[1] ?? max;
  return { start: addDays(end, -(days - 1)), days, max: nice };
}
