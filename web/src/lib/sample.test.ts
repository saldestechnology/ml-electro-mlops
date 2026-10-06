import { MIN_DAYS, daysScored, tooEarlyNote, tooFewDays } from './sample';

describe('minimum sample', () => {
  it('flags zones with some but fewer than MIN_DAYS scored days', () => {
    expect(tooFewDays(0)).toBe(false);
    expect(tooFewDays(1)).toBe(true);
    expect(tooFewDays(MIN_DAYS - 1)).toBe(true);
    expect(tooFewDays(MIN_DAYS)).toBe(false);
  });

  it('pluralises the caveat', () => {
    expect(daysScored(1)).toBe('1 day scored');
    expect(daysScored(2)).toBe('2 days scored');
    expect(tooEarlyNote(1)).toBe('1 day scored — too early to judge');
    expect(tooEarlyNote(3)).toBe('3 days scored — too early to judge');
  });
});
