import { createClient } from '../api';
import { ZONES } from '../api';
import { stockholmParts } from '../lib/time';
import { createFixtureFetch, fixtureFetch, lastDstDay } from '.';

// A fixed "today" (Sat 24 Oct 2026): tomorrow is the 25-hour day.
const client = createClient(createFixtureFetch('2026-10-24'));

describe('fixtures', () => {
  it('serve every endpoint through the real client and parsers', async () => {
    expect((await client.health()).status).toBe('ok');
    const zones = await client.zones();
    expect(zones.map((z) => z.zone)).toEqual([...ZONES]);
    for (const z of ZONES) {
      const origins = await client.origins(z);
      expect(origins.origins.length).toBeGreaterThan(0);
      expect([...origins.origins].sort().reverse()).toEqual(origins.origins); // newest first
      await client.forecast(z);
      await client.performance(z, 30);
      await client.model(z);
    }
  });

  it('cover the cases the UI must handle', async () => {
    const zones = await client.zones();
    expect(zones.filter((z) => z.stale).map((z) => z.zone)).toEqual(['SE3']);
    expect(zones.find((z) => z.zone === 'SE3')?.latest_origin).toBe('2026-10-22');
    const se4 = await client.performance('SE4', 30);
    expect(se4.live).toBeNull();
    expect(se4.daily).toEqual([]);
    const se1 = await client.performance('SE1', 30);
    expect(se1.live).not.toBeNull();
    expect(se1.daily.length).toBeGreaterThan(20);
    // SE3: a single scored day (below the minimum sample); SE2: a gap of three missed days
    const se3 = await client.performance('SE3', 30);
    expect(se3.daily.map((d) => d.origin_date)).toEqual(['2026-10-22']);
    expect(se3.n_origins_scored).toBe(1);
    const se2 = (await client.performance('SE2', 30)).daily.map((d) => d.origin_date);
    expect(se2).toContain('2026-10-14');
    expect(se2).not.toContain('2026-10-15');
    expect(se2).not.toContain('2026-10-17');
    expect(se2).toContain('2026-10-18');
  });

  it('include a 25-hour DST day with correct local hours', async () => {
    const f = await client.forecast('SE1');
    expect(f.target_date).toBe('2026-10-25');
    expect(f.hours).toHaveLength(25);
    expect(f.hours.map((h) => h.hour_local).filter((h) => h === 2)).toHaveLength(2);
    for (const h of f.hours) {
      const p = stockholmParts(new Date(h.target_time));
      expect(p.date).toBe(f.target_date);
      expect(p.hour).toBe(h.hour_local);
    }
  });

  it('have actuals on past days and none on the newest target day', async () => {
    const latest = await client.forecast('SE1');
    expect(latest.hours.every((h) => h.actual === null)).toBe(true);
    const past = await client.forecast('SE1', '2026-10-20');
    expect(past.hours.every((h) => h.actual !== null && h.naive_7d !== null)).toBe(true);
  });

  it('always reach the most recent DST change through an earlier origin', async () => {
    expect(lastDstDay('2026-10-07')).toBe('2026-03-29');
    const early = createClient(createFixtureFetch('2026-10-06'));
    const origins = (await early.origins('SE1')).origins;
    expect(origins[0]).toBe('2026-10-06');
    expect(origins).toContain('2026-03-28');
    const f = await early.forecast('SE1', '2026-03-28');
    expect(f.hours).toHaveLength(23);
    expect(f.hours.map((h) => h.hour_local)).not.toContain(2);
    expect(f.hours.every((h) => h.actual !== null)).toBe(true);
  });

  it('follow the real clock by default', async () => {
    const live = createClient(fixtureFetch);
    const zones = await live.zones();
    const today = stockholmParts(new Date()).date;
    expect(zones.find((z) => z.zone === 'SE1')?.latest_origin).toBe(today);
  });

  it('answer unknown origins with a 404 detail', async () => {
    await expect(client.forecast('SE2', '2020-01-01')).rejects.toMatchObject({ status: 404 });
  });
});
