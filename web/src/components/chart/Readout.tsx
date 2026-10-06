import type { ForecastHour } from '../../api';
import { fmtNum } from '../../lib/format';
import { slotRange } from './scale';
import type { Slot } from './scale';

const ROWS: { key: keyof ForecastHour; label: string; cls?: string }[] = [
  { key: 'q95', label: 'q95' },
  { key: 'q90', label: 'q90' },
  { key: 'q75', label: 'q75' },
  { key: 'q50', label: 'q50 median', cls: 'readout__median' },
  { key: 'q25', label: 'q25' },
  { key: 'q10', label: 'q10' },
  { key: 'q05', label: 'q05' },
  { key: 'actual', label: 'Actual', cls: 'readout__actual' },
  { key: 'naive_7d', label: 'Naive 7d' },
];

export function Readout({
  hour,
  slot,
}: {
  hour: ForecastHour | undefined;
  slot: Slot | undefined;
}) {
  return (
    <div className="readout" aria-live="polite">
      {hour && slot ? (
        <>
          <p className="readout__hour">
            {slotRange(slot)}
            {slot.repeated && (
              <span className="readout__note"> second pass, after clocks went back</span>
            )}
          </p>
          <dl className="readout__list">
            {ROWS.map((r) => (
              <div key={r.key} className={`readout__row ${r.cls ?? ''}`}>
                <dt>{r.label}</dt>
                <dd>{fmtNum(hour[r.key] as number | null)}</dd>
              </div>
            ))}
          </dl>
        </>
      ) : (
        <p className="readout__hint">
          Point at the chart, or focus it and use the arrow keys, to read every quantile for an
          hour.
        </p>
      )}
    </div>
  );
}
