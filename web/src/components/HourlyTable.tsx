import type { ForecastHour } from '../api';
import type { DayLayout } from './chart/scale';
import { fmtNum, fmtSigned } from '../lib/format';
import './HourlyTable.css';

const COLS = ['q05', 'q10', 'q25', 'q50', 'q75', 'q90', 'q95'] as const;

export function HourlyTable({ hours, layout }: { hours: ForecastHour[]; layout: DayLayout }) {
  const hasActual = hours.some((h) => h.actual !== null);
  const hasNaive = hours.some((h) => h.naive_7d !== null);
  return (
    <section className="hourly" aria-labelledby="hourly-title">
      <h2 id="hourly-title" className="hourly__title">
        Hour by hour <span className="unit">EUR/MWh</span>
      </h2>
      <div className="hourly__scroll" tabIndex={0} role="region" aria-labelledby="hourly-title">
        <table className="hourly__table">
          <thead>
            <tr>
              <th scope="col" className="hourly__hour">
                Hour
              </th>
              {COLS.map((c) => (
                <th key={c} scope="col" className={c === 'q50' ? 'hourly__median' : undefined}>
                  {c}
                </th>
              ))}
              {hasActual && <th scope="col">Actual</th>}
              {hasActual && (
                <th scope="col" className="hourly__bias">
                  <abbr title="Bias of the median: q50 − actual">Bias</abbr>
                </th>
              )}
              {hasNaive && <th scope="col">Naive 7d</th>}
            </tr>
          </thead>
          <tbody>
            {hours.map((h, i) => {
              const slot = layout.slots[i];
              return (
                <tr key={h.target_time}>
                  <th scope="row" className="hourly__hour">
                    {slot?.label ?? ''}
                  </th>
                  {COLS.map((c) => (
                    <td key={c} className={c === 'q50' ? 'hourly__median' : undefined}>
                      {fmtNum(h[c])}
                    </td>
                  ))}
                  {hasActual && <td className="hourly__actual">{fmtNum(h.actual)}</td>}
                  {hasActual && (
                    <td className="hourly__bias">
                      {fmtSigned(h.actual === null ? null : h.q50 - h.actual)}
                    </td>
                  )}
                  {hasNaive && <td>{fmtNum(h.naive_7d)}</td>}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
