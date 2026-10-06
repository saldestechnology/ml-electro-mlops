// Number formatting: a true minus sign (U+2212) so signed figures align and read correctly.

const fmts = new Map<number, Intl.NumberFormat>();
function nf(digits: number) {
  let f = fmts.get(digits);
  if (!f) {
    f = new Intl.NumberFormat('en-GB', {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
      useGrouping: true,
    });
    fmts.set(digits, f);
  }
  return f;
}

export function fmtNum(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  const s = nf(digits).format(Math.abs(v) < 0.5 * 10 ** -digits ? 0 : v);
  return s.replace('-', '−');
}

/** 0.873 -> "87%" (or with digits). */
export function fmtPct(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  return `${fmtNum(v * 100, digits)}%`;
}

export const mean = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : NaN);

/** A signed figure: "+12.3", "−79.0", "0.0" (true minus, explicit plus). */
export function fmtSigned(v: number | null | undefined, digits = 1): string {
  const s = fmtNum(v, digits);
  return v !== null && v !== undefined && Number.isFinite(v) && s !== fmtNum(0, digits) && v > 0
    ? `+${s}`
    : s;
}
