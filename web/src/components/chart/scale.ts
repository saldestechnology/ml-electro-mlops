// Pure layout logic for the D+1 fan chart, kept free of React so it is easy to test.
//
// The x axis is a sequence of hourly *slots* in delivery order, not a clock: a target day has
// 24 slots normally, 23 on the spring-forward day (local 02 is skipped) and 25 on the
// fall-back day (local 02 occurs twice). Slot i spans [i, i + 1) and is labelled with its
// Stockholm hour; the second occurrence of a repeated hour is marked with a prime.

import { scaleLinear } from 'd3-scale';
import type { ForecastHour } from '../../api';
import { pad2 } from '../../lib/time';

export type DstKind = 'none' | 'short' | 'long';

export interface Slot {
  index: number;
  hour: number;
  label: string; // "02" or "02′" for the repeated hour
  repeated: boolean;
}

export interface DayLayout {
  slots: Slot[];
  dst: DstKind;
  /** The local hour that is skipped (short day) or repeated (long day), if any. */
  transitionHour: number | null;
}

export function dayLayout(hours: Pick<ForecastHour, 'hour_local'>[]): DayLayout {
  const seen = new Set<number>();
  const slots = hours.map((h, index) => {
    const repeated = seen.has(h.hour_local);
    seen.add(h.hour_local);
    return {
      index,
      hour: h.hour_local,
      label: `${pad2(h.hour_local)}${repeated ? '′' : ''}`,
      repeated,
    };
  });
  const repeatedSlot = slots.find((s) => s.repeated);
  if (hours.length === 25 || repeatedSlot) {
    return { slots, dst: 'long', transitionHour: repeatedSlot?.hour ?? null };
  }
  if (hours.length === 23) {
    let skipped: number | null = null;
    for (let h = 0; h < 24; h++) if (!seen.has(h)) skipped = h;
    return { slots, dst: 'short', transitionHour: skipped };
  }
  return { slots, dst: 'none', transitionHour: null };
}

/** Slots that get a tick label: every `step` local hours, by clock hour, never the repeat. */
export function tickSlots(layout: DayLayout, step: number): Slot[] {
  return layout.slots.filter((s) => !s.repeated && s.hour % step === 0);
}

type Numeric = number | null | undefined;

/** A y domain covering every plotted value, padded and rounded to nice numbers. */
export function yDomain(
  hours: Pick<ForecastHour, 'q05' | 'q95' | 'actual' | 'naive_7d'>[],
): [number, number] {
  const values: number[] = [];
  for (const h of hours) {
    for (const v of [h.q05, h.q95, h.actual, h.naive_7d] as Numeric[]) {
      if (typeof v === 'number' && Number.isFinite(v)) values.push(v);
    }
  }
  if (!values.length) return [0, 100];
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  // Prices are read against zero: show it when the data comes reasonably close.
  if (lo > 0 && lo < 0.35 * hi) lo = 0;
  if (lo === hi) {
    lo -= 1;
    hi += 1;
  }
  const pad = (hi - lo) * 0.04;
  const nice = scaleLinear()
    .domain([lo === 0 ? 0 : lo - pad, hi + pad])
    .nice(6)
    .domain();
  return [nice[0] ?? lo, nice[1] ?? hi];
}

/** "02′:00–03:00" style label for the hour a slot covers. */
export function slotRange(slot: Slot): string {
  const next = (slot.hour + 1) % 24;
  return `${slot.label}:00–${pad2(next)}:00`;
}

/** Map a pointer x (in chart coordinates) to the slot under it. */
export function slotAt(x: number, innerWidth: number, n: number): number {
  if (n <= 0 || innerWidth <= 0) return 0;
  const i = Math.floor((x / innerWidth) * n);
  return Math.min(n - 1, Math.max(0, i));
}

/** Step-line points: each value holds over its whole slot [i, i + 1). */
export function stepPoints(values: (number | null)[]): [number, number][][] {
  const runs: [number, number][][] = [];
  let run: [number, number][] = [];
  values.forEach((v, i) => {
    if (v === null) {
      if (run.length) runs.push(run);
      run = [];
      return;
    }
    run.push([i, v], [i + 1, v]);
  });
  if (run.length) runs.push(run);
  return runs;
}
