import type { Performance } from '../../api';
import { consecutiveRuns, dailyDomains, dailyPoints } from './daily';

const perf = (zone: Performance['zone'], dates: string[]): Performance => ({
  zone,
  days: 30,
  n_origins_scored: dates.length,
  live: null,
  daily: dates.map((d, k) => ({
    origin_date: d,
    pinball: 5 + k,
    naive_7d_pinball: 10 + k,
    coverage_90: 0.9,
  })),
  backtest: { pinball: 5, naive_7d_pinball: 11 },
});

describe('daily pinball geometry', () => {
  it('breaks runs at missing days', () => {
    const pts = [1, 2, 3, 6, 8, 9].map((i) => ({ i }));
    expect(consecutiveRuns(pts).map((r) => r.map((p) => p.i))).toEqual([[1, 2, 3], [6], [8, 9]]);
    expect(consecutiveRuns([])).toEqual([]);
    expect(consecutiveRuns([{ i: 4 }])).toEqual([[{ i: 4 }]]);
  });

  it('keeps a single scored day as a point and shares the y scale across zones', () => {
    const one = perf('SE3', ['2026-10-05']);
    const many = perf('SE1', ['2026-10-01', '2026-10-02', '2026-10-04', '2026-10-05']);
    const domains = dailyDomains([one, many]);
    expect(domains.days).toBe(30);
    expect(domains.start).toBe('2026-09-06');
    expect(domains.max).toBeGreaterThanOrEqual(13); // the largest naive value across both
    const pts = dailyPoints(one, domains);
    expect(pts).toEqual([{ i: 29, pinball: 5, naive: 10 }]);
    expect(consecutiveRuns(dailyPoints(many, domains)).map((r) => r.length)).toEqual([2, 2]);
  });
});
