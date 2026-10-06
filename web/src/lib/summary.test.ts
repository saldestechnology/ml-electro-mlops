import type { ForecastHour } from '../api';
import { fmtSigned } from './format';
import { localDayHours, stockholmParts } from './time';
import { DAY_PARTS, dayPartOf, forecastError } from './summary';

/** Hours of a delivery day with q50 = actual + bias(hour) and a ±10 band around q50. */
function day(
  date: string,
  bias: (hourLocal: number) => number,
  actual: (hourLocal: number, i: number) => number | null = () => 50,
): ForecastHour[] {
  return localDayHours(date).map((t, i) => {
    const hour_local = stockholmParts(t).hour;
    const a = actual(hour_local, i);
    const q50 = (a ?? 50) + bias(hour_local);
    return {
      target_time: t.toISOString(),
      hour_local,
      q05: q50 - 10,
      q10: q50 - 8,
      q25: q50 - 4,
      q50,
      q75: q50 + 4,
      q90: q50 + 8,
      q95: q50 + 10,
      actual: a,
      naive_7d: null,
    };
  });
}

describe('day parts', () => {
  it('cover every local hour exactly once', () => {
    for (let h = 0; h < 24; h++) {
      expect(DAY_PARTS.filter((p) => h >= p.from && h <= p.to)).toHaveLength(1);
    }
    expect([0, 5, 6, 9, 10, 16, 17, 21, 22, 23].map(dayPartOf)).toEqual([
      'night',
      'night',
      'morning',
      'morning',
      'day',
      'day',
      'evening',
      'evening',
      'late',
      'late',
    ]);
  });
});

describe('forecast error', () => {
  it('is null when no hour has an actual', () => {
    expect(
      forecastError(
        day(
          '2026-10-07',
          () => 0,
          () => null,
        ),
      ),
    ).toBeNull();
  });

  it('gives the bias per part, MAE, band misses and the largest part', () => {
    const bias = (h: number) => (h <= 5 ? -80 : h >= 17 && h <= 21 ? 12 : -2);
    const e = forecastError(day('2026-10-07', bias));
    expect(e).not.toBeNull();
    if (!e) return;
    expect(e.hours).toBe(24);
    expect(e.totalHours).toBe(24);
    expect(e.parts.map((p) => p.bias)).toEqual([-80, -2, -2, 12, -2]);
    expect(e.parts.map((p) => p.hours)).toEqual([6, 4, 7, 5, 2]);
    expect(e.largest).toBe('night');
    expect(e.mae).toBeCloseTo((6 * 80 + 5 * 12 + 13 * 2) / 24);
    // night: actual 80 above q50, beyond q95 = q50 + 10; evening: actual 12 below q50
    expect(e.aboveQ95).toBe(6);
    expect(e.belowQ05).toBe(5);
  });

  it('works on the 25-hour day (02 twice) by local hour', () => {
    const e = forecastError(day('2026-10-25', (h) => (h === 2 ? -30 : 0)));
    expect(e?.totalHours).toBe(25);
    const night = e?.parts.find((p) => p.key === 'night');
    expect(night?.hours).toBe(7);
    expect(night?.bias).toBeCloseTo(-60 / 7);
  });

  it('works on the 23-hour day (no 02)', () => {
    const e = forecastError(day('2026-03-29', () => 5));
    expect(e?.totalHours).toBe(23);
    expect(e?.parts.find((p) => p.key === 'night')?.hours).toBe(5);
    expect(e?.parts.every((p) => p.bias === 5)).toBe(true);
  });

  it('uses only the hours with actuals when the day is partly published', () => {
    const e = forecastError(
      day(
        '2026-10-07',
        () => -4,
        (h) => (h < 12 ? 40 : null),
      ),
    );
    expect(e?.hours).toBe(12);
    expect(e?.totalHours).toBe(24);
    expect(e?.parts.map((p) => p.hours)).toEqual([6, 4, 2, 0, 0]);
    expect(e?.parts.find((p) => p.key === 'evening')?.bias).toBeNull();
    expect(e?.largest).toBe('night'); // ties keep the first part
  });
});

describe('signed figures', () => {
  it('use a true minus and an explicit plus', () => {
    expect(fmtSigned(-79.04)).toBe('−79.0');
    expect(fmtSigned(12.3)).toBe('+12.3');
    expect(fmtSigned(0.01)).toBe('0.0');
    expect(fmtSigned(null)).toBe('—');
  });
});
