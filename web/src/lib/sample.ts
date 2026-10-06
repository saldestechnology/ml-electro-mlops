// Minimum sample before live skill and coverage are worth reading. Below it a single good or
// bad day dominates the figure, so the Performance page shows it muted with a caveat.

/** Scored forecast days a zone needs before its skill and coverage are shown at full weight. */
export const MIN_DAYS = 7;

/** True when a zone has some scores but fewer than MIN_DAYS scored days. */
export const tooFewDays = (scored: number) => scored > 0 && scored < MIN_DAYS;

/** "1 day scored", "3 days scored". */
export const daysScored = (n: number) => `${n} ${n === 1 ? 'day' : 'days'} scored`;

/** The caveat shown next to a figure from too small a sample. */
export const tooEarlyNote = (n: number) => `${daysScored(n)} — too early to judge`;
