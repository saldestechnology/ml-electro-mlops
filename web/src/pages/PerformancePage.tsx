import { ZONES } from '../api';
import type { Performance, Zone } from '../api';
import { DailyPinballChart } from '../components/chart/DailyPinballChart';
import { dailyDomains } from '../components/chart/daily';
import { Coverage } from '../components/Coverage';
import { EmptyState, ErrorState, Loading } from '../components/States';
import { useApi } from '../hooks/useApi';
import type { Loadable } from '../hooks/useApi';
import { fmtNum, fmtPct } from '../lib/format';
import './PerformancePage.css';

const DAYS = 30;

type PerZone = { zone: Zone; result: Loadable<Performance> };

export function PerformancePage() {
  const all = useApi(`performance:${DAYS}`, async (c, s) => {
    const settled = await Promise.allSettled(ZONES.map((z) => c.performance(z, DAYS, s)));
    return settled.map((r, i): PerZone => ({
      zone: ZONES[i] ?? 'SE1',
      result:
        r.status === 'fulfilled'
          ? { status: 'ok', data: r.value }
          : {
              status: 'error',
              error: r.reason instanceof Error ? r.reason : new Error(String(r.reason)),
            },
    }));
  });

  return (
    <div className="perf">
      <header className="grid perf__head">
        <h1>Performance</h1>
        <p className="lede">
          Live scores of the served model over the last {DAYS} days, against the seasonal naive
          forecast (same hour one week earlier). Pinball loss averages the quantile loss over all
          hours and the seven quantiles, in EUR/MWh; lower is better. Skill is 1 − pinball ÷ naive
          pinball.
        </p>
      </header>
      {all.status === 'loading' && <Loading what="scores" />}
      {all.status === 'error' && (
        <div className="grid">
          <ErrorState error={all.error} what="scores" />
        </div>
      )}
      {all.status === 'ok' && <PerformanceView zones={all.data} />}
    </div>
  );
}

function PerformanceView({ zones }: { zones: PerZone[] }) {
  const ok = zones.flatMap((z) => (z.result.status === 'ok' ? [z.result.data] : []));
  const domains = dailyDomains(ok);
  const anyScored = ok.some((p) => p.live !== null);

  return (
    <>
      <section className="grid perf__section" aria-labelledby="glance">
        <h2 id="glance" className="perf__h2">
          At a glance
        </h2>
        <div className="perf__matrix" role="region" aria-labelledby="glance" tabIndex={0}>
          <table className="matrix">
            <thead>
              <tr>
                <th scope="col" className="matrix__metric">
                  <span className="visually-hidden">Metric</span>
                </th>
                {zones.map((z) => (
                  <th key={z.zone} scope="col" className="matrix__zone">
                    {z.zone}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              <Row label="Skill vs naive 7d" note="live, higher is better" zones={zones} big>
                {(p) => (p.live ? fmtPct(p.live.skill) : null)}
              </Row>
              <Row label="Pinball, live" note="EUR/MWh" zones={zones}>
                {(p) => (p.live ? fmtNum(p.live.pinball, 2) : null)}
              </Row>
              <Row label="Pinball, naive 7d" note="EUR/MWh, same days" zones={zones}>
                {(p) => (p.live ? fmtNum(p.live.naive_7d_pinball, 2) : null)}
              </Row>
              <Row label="Coverage, 50% band" note="share of actuals in q25–q75" zones={zones}>
                {(p) => (p.live ? <Coverage value={p.live.coverage_50} nominal={0.5} /> : null)}
              </Row>
              <Row label="Coverage, 90% band" note="share of actuals in q05–q95" zones={zones}>
                {(p) => (p.live ? <Coverage value={p.live.coverage_90} nominal={0.9} /> : null)}
              </Row>
              <Row label="MAE of the median" note="EUR/MWh" zones={zones}>
                {(p) => (p.live ? fmtNum(p.live.mae_median, 2) : null)}
              </Row>
              <Row label="Days scored" note={`of the last ${DAYS}`} zones={zones}>
                {(p) => String(p.n_origins_scored)}
              </Row>
              <Row label="Backtest pinball" note="reference, EUR/MWh" zones={zones} reference>
                {(p) => fmtNum(p.backtest.pinball, 2)}
              </Row>
              <Row label="Backtest naive 7d" note="reference, EUR/MWh" zones={zones} reference>
                {(p) => fmtNum(p.backtest.naive_7d_pinball, 2)}
              </Row>
              <Row label="Backtest skill" note="reference" zones={zones} reference>
                {(p) => fmtPct(1 - p.backtest.pinball / p.backtest.naive_7d_pinball)}
              </Row>
            </tbody>
          </table>
        </div>
        {!anyScored && (
          <EmptyState title="Nothing scored yet">
            <p>
              A forecast day is scored once its actual prices reach the dataset, usually the morning
              after delivery. The backtest reference above is what to expect.
            </p>
          </EmptyState>
        )}
      </section>

      <section className="grid perf__section" aria-labelledby="daily">
        <h2 id="daily" className="perf__h2">
          Daily pinball loss
        </h2>
        <p className="perf__note">
          Model (red) and naive 7-day (dashed) per forecast day; same scales in every panel.
        </p>
        {zones.map((z) => (
          <div key={z.zone} className="perf__panel">
            <h3 className="perf__panelzone">{z.zone}</h3>
            {z.result.status === 'error' && (
              <ErrorState error={z.result.error} what={`scores for ${z.zone}`} />
            )}
            {z.result.status === 'ok' &&
              (z.result.data.daily.length ? (
                <DailyPinballChart perf={z.result.data} domains={domains} />
              ) : (
                <div className="perf__empty">
                  <p className="perf__emptytitle">Nothing scored yet</p>
                  <p>
                    {z.zone} has no forecast day with published actuals in the last {DAYS} days.
                  </p>
                </div>
              ))}
          </div>
        ))}
      </section>
    </>
  );
}

function Row({
  label,
  note,
  zones,
  children,
  big = false,
  reference = false,
}: {
  label: string;
  note: string;
  zones: PerZone[];
  children: (p: Performance) => React.ReactNode;
  big?: boolean;
  reference?: boolean;
}) {
  return (
    <tr className={`${big ? 'matrix__big' : ''} ${reference ? 'matrix__ref' : ''}`}>
      <th scope="row" className="matrix__metric">
        {label}
        <span className="matrix__note">{note}</span>
      </th>
      {zones.map((z) => {
        if (z.result.status !== 'ok') {
          return (
            <td key={z.zone} className="matrix__na">
              {z.result.status === 'error' ? 'error' : '…'}
            </td>
          );
        }
        const v = children(z.result.data);
        return (
          <td key={z.zone} className={v === null ? 'matrix__na' : undefined}>
            {v ?? 'not scored yet'}
          </td>
        );
      })}
    </tr>
  );
}
