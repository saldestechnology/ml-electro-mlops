import { fmtNum, fmtPct } from '../lib/format';

/**
 * Observed coverage as a bar on 0–100%, with a tick at the nominal level. `muted` (too few
 * scored days to judge) drops the signal red and greys the bar.
 */
export function Coverage({
  value,
  nominal,
  muted = false,
}: {
  value: number;
  nominal: number;
  muted?: boolean;
}) {
  const diff = value - nominal;
  return (
    <span className={muted ? 'coverage coverage--muted' : 'coverage'}>
      <span className="coverage__value">{fmtPct(value)}</span>
      <span
        className="coverage__track"
        role="img"
        aria-label={`${fmtPct(value)} observed against ${fmtPct(nominal)} nominal`}
      >
        <span
          className="coverage__fill"
          style={{ width: `${Math.min(1, Math.max(0, value)) * 100}%` }}
        />
        <span className="coverage__nominal" style={{ left: `${nominal * 100}%` }} />
      </span>
      <span className="coverage__diff">
        {Math.abs(diff) < 0.005
          ? 'on target'
          : `${diff > 0 ? '+' : '−'}${fmtNum(Math.abs(diff) * 100, 0)} pts vs ${fmtPct(nominal)}`}
      </span>
    </span>
  );
}
