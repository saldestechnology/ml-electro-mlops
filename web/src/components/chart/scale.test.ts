import { localDayHours, stockholmParts } from '../../lib/time';
import { dayLayout, slotAt, slotRange, stepPoints, tickSlots, yDomain } from './scale';

const hoursOf = (date: string) =>
  localDayHours(date).map((t) => ({ hour_local: stockholmParts(t).hour }));

describe('day layout', () => {
  it('has 24 plain slots on a normal day', () => {
    const l = dayLayout(hoursOf('2026-10-07'));
    expect(l.slots).toHaveLength(24);
    expect(l.dst).toBe('none');
    expect(l.slots.map((s) => s.label).slice(0, 3)).toEqual(['00', '01', '02']);
  });

  it('handles the 23-hour spring-forward day (no 02)', () => {
    const l = dayLayout(hoursOf('2026-03-29'));
    expect(l.slots).toHaveLength(23);
    expect(l.dst).toBe('short');
    expect(l.transitionHour).toBe(2);
    expect(l.slots.map((s) => s.hour).slice(0, 3)).toEqual([0, 1, 3]);
  });

  it('handles the 25-hour fall-back day (02 twice)', () => {
    const l = dayLayout(hoursOf('2026-10-25'));
    expect(l.slots).toHaveLength(25);
    expect(l.dst).toBe('long');
    expect(l.transitionHour).toBe(2);
    expect(l.slots.map((s) => s.label).slice(0, 5)).toEqual(['00', '01', '02', '02′', '03']);
    const repeated = l.slots[3];
    expect(repeated && slotRange(repeated)).toBe('02′:00–03:00');
  });

  it('ticks by clock hour and never on the repeated hour', () => {
    const long = dayLayout(hoursOf('2026-10-25'));
    expect(tickSlots(long, 3).map((s) => s.label)).toEqual([
      '00',
      '03',
      '06',
      '09',
      '12',
      '15',
      '18',
      '21',
    ]);
    // On the long day 03 is the fifth slot, not the fourth.
    expect(tickSlots(long, 3)[1]?.index).toBe(4);
    const short = dayLayout(hoursOf('2026-03-29'));
    expect(tickSlots(short, 6).map((s) => s.index)).toEqual([0, 5, 11, 17]);
  });
});

describe('scales', () => {
  it('maps pointer x to slots, clamped', () => {
    expect(slotAt(0, 250, 25)).toBe(0);
    expect(slotAt(249.9, 250, 25)).toBe(24);
    expect(slotAt(500, 250, 25)).toBe(24);
    expect(slotAt(-5, 240, 24)).toBe(0);
    expect(slotAt(105, 240, 24)).toBe(10);
  });

  it('y domain covers every plotted value and includes zero when close', () => {
    const [lo, hi] = yDomain([
      { q05: 12, q95: 80, actual: 95, naive_7d: null },
      { q05: 20, q95: 70, actual: null, naive_7d: 3 },
    ]);
    expect(lo).toBe(0);
    expect(hi).toBeGreaterThanOrEqual(95);
  });

  it('y domain extends below zero for negative prices', () => {
    const [lo, hi] = yDomain([{ q05: -14.2, q95: 40, actual: null, naive_7d: null }]);
    expect(lo).toBeLessThan(-14.2);
    expect(hi).toBeGreaterThan(40);
  });

  it('y domain does not force zero for high price levels', () => {
    const [lo] = yDomain([{ q05: 150, q95: 210, actual: null, naive_7d: null }]);
    expect(lo).toBeGreaterThan(100);
  });

  it('step points hold each value across its slot and break on nulls', () => {
    expect(stepPoints([1, 2, null, 4])).toEqual([
      [
        [0, 1],
        [1, 1],
        [1, 2],
        [2, 2],
      ],
      [
        [3, 4],
        [4, 4],
      ],
    ]);
  });
});
