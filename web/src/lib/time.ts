// Europe/Stockholm calendar helpers. All forecast hours are labelled in Stockholm local time;
// a target day has 23, 24 or 25 hours depending on daylight-saving transitions.

export const TZ = 'Europe/Stockholm';

const partsFmt = new Intl.DateTimeFormat('en-GB', {
  timeZone: TZ,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
});

export interface LocalParts {
  date: string; // YYYY-MM-DD
  hour: number;
  minute: number;
}

export function stockholmParts(instant: Date): LocalParts {
  const p: Record<string, string> = {};
  for (const { type, value } of partsFmt.formatToParts(instant)) p[type] = value;
  return {
    date: `${p.year ?? ''}-${p.month ?? ''}-${p.day ?? ''}`,
    hour: Number(p.hour),
    minute: Number(p.minute),
  };
}

/** Today's calendar date in Stockholm. */
export function stockholmToday(now: Date = new Date()): string {
  return stockholmParts(now).date;
}

/** Whole calendar days from `a` to `b` (ISO dates). */
export function daysBetween(a: string, b: string): number {
  return Math.round((Date.parse(`${b}T12:00:00Z`) - Date.parse(`${a}T12:00:00Z`)) / 86_400_000);
}

/** Shift an ISO calendar date by whole days. */
export function addDays(isoDate: string, days: number): string {
  const d = new Date(`${isoDate}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

/** UTC instants of every local hour of a Stockholm calendar day (23, 24 or 25 of them). */
export function localDayHours(isoDate: string): Date[] {
  // Local midnight is 22:00 or 23:00 UTC the day before.
  const prev = addDays(isoDate, -1);
  let t = [22, 23]
    .map((h) => new Date(`${prev}T${String(h).padStart(2, '0')}:00:00Z`))
    .find((d) => {
      const p = stockholmParts(d);
      return p.date === isoDate && p.hour === 0;
    });
  if (!t) throw new Error(`no local midnight found for ${isoDate}`);
  const out: Date[] = [];
  while (stockholmParts(t).date === isoDate) {
    out.push(t);
    t = new Date(t.getTime() + 3_600_000);
  }
  return out;
}

const dateLong = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'UTC',
  weekday: 'long',
  day: 'numeric',
  month: 'long',
  year: 'numeric',
});
const dateShort = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'UTC',
  weekday: 'short',
  day: 'numeric',
  month: 'short',
});
const dateTiny = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'UTC',
  day: 'numeric',
  month: 'short',
});
const stamp = new Intl.DateTimeFormat('en-GB', {
  timeZone: TZ,
  day: 'numeric',
  month: 'short',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
  timeZoneName: 'short',
});

const asDate = (iso: string) => new Date(`${iso.slice(0, 10)}T12:00:00Z`);

/** "Sunday 25 October 2026" for an ISO calendar date. */
export const formatDateLong = (iso: string) => dateLong.format(asDate(iso)).replace(',', '');
/** "Sun 25 Oct". */
export const formatDateShort = (iso: string) => dateShort.format(asDate(iso)).replace(',', '');
/** "25 Oct". */
export const formatDateTiny = (iso: string) => dateTiny.format(asDate(iso));
/** "24 Oct 2026, 09:05 CEST" for an instant, in Stockholm time. */
export function formatStamp(isoInstant: string | null): string {
  if (!isoInstant) return '—';
  const d = new Date(isoInstant);
  if (Number.isNaN(d.getTime())) return isoInstant;
  return stamp.format(d);
}

export const pad2 = (n: number) => String(n).padStart(2, '0');
