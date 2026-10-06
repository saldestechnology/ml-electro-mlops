import { Link, useNavigate, useSearchParams } from 'react-router';
import { isZone } from '../api';
import type { Forecast, Zone } from '../api';
import { FanChart } from '../components/chart/FanChart';
import { dayLayout } from '../components/chart/scale';
import type { DayLayout } from '../components/chart/scale';
import { HourlyTable } from '../components/HourlyTable';
import { Icon } from '../components/Icon';
import { Legend } from '../components/chart/Legend';
import { EmptyState, ErrorState, Loading } from '../components/States';
import { SwedenMap } from '../components/SwedenMap';
import { ZoneSwitcher } from '../components/ZoneSwitcher';
import { useApi } from '../hooks/useApi';
import { useStatus } from '../hooks/useStatus';
import { fmtNum, fmtSigned } from '../lib/format';
import { forecastError, summarise } from '../lib/summary';
import type { ForecastError } from '../lib/summary';
import {
  daysBetween,
  formatDateLong,
  formatDateShort,
  formatStamp,
  pad2,
  stockholmToday,
} from '../lib/time';
import './ForecastPage.css';

export const DEFAULT_ZONE: Zone = 'SE3';

export function ForecastPage() {
  const [params] = useSearchParams();
  const zp = params.get('zone');
  const zone: Zone = isZone(zp) ? zp : DEFAULT_ZONE;
  const origin = params.get('origin');

  const origins = useApi(`origins:${zone}`, (c, s) => c.origins(zone, s));
  const hasOrigins = origins.status === 'ok' && origins.data.origins.length > 0;
  const forecast = useApi(hasOrigins ? `forecast:${zone}:${origin ?? 'latest'}` : null, (c, s) =>
    c.forecast(zone, origin, s),
  );

  return (
    <div className="forecast">
      <div className="grid forecast__head">
        <h1 className="visually-hidden">Day-ahead price forecast, {zone}</h1>
        <div className="forecast__zones">
          <ZoneSwitcher zone={zone} />
        </div>
      </div>
      {origins.status === 'loading' && <Loading what="origins" />}
      {origins.status === 'error' && (
        <div className="grid">
          <ErrorState error={origins.error} what={`origins for ${zone}`} />
        </div>
      )}
      {origins.status === 'ok' && !hasOrigins && (
        <div className="grid">
          <EmptyState title={`No forecasts for ${zone} yet`}>
            <p>
              The served model writes its first forecast at 09:05 Stockholm time on the morning it
              starts serving. The fan chart, key figures and hourly table appear here then.
            </p>
          </EmptyState>
        </div>
      )}
      {hasOrigins && forecast.status === 'loading' && <Loading what="forecast" />}
      {hasOrigins && forecast.status === 'error' && (
        <div className="grid">
          <ErrorState error={forecast.error} what={`the forecast for ${zone}`} />
        </div>
      )}
      {origins.status === 'ok' && forecast.status === 'ok' && (
        <ForecastView zone={zone} forecast={forecast.data} origins={origins.data.origins} />
      )}
    </div>
  );
}

function ForecastView({
  zone,
  forecast,
  origins,
}: {
  zone: Zone;
  forecast: Forecast;
  origins: string[];
}) {
  const layout = dayLayout(forecast.hours);
  const s = summarise(forecast.hours);
  const err = forecastError(forecast.hours);
  const hasActual = s.hoursWithActual > 0;
  const hasNaive = forecast.hours.some((h) => h.naive_7d !== null);
  const label = (i: number) => layout.slots[i]?.label ?? '';
  const { zones } = useStatus();
  // the same staleness the zone switcher marks
  const stale =
    zones.status === 'ok' ? Object.fromEntries(zones.data.map((z) => [z.zone, z.stale])) : {};

  return (
    <div className="grid forecast__body">
      <aside className="forecast__meta" aria-label="About this forecast">
        <p className="label">Delivery day</p>
        <p className="forecast__day">{formatDateLong(forecast.target_date)}</p>
        <DstNote layout={layout} />
        <MetaBlock zone={zone} forecast={forecast} origins={origins} />
        <div className="forecast__map">
          <SwedenMap zone={zone} stale={stale} />
        </div>
      </aside>

      <section className="forecast__main" aria-label="Forecast">
        <dl className="figures">
          <Figure
            label="Daily mean, median"
            value={fmtNum(s.meanMedian)}
            note={s.actualMean !== null ? `actual ${fmtNum(s.actualMean)}` : undefined}
            signal
          />
          <Figure
            label="Peak hour"
            value={fmtNum(s.peak.value)}
            note={`at ${label(s.peak.index)}:00`}
          />
          <Figure
            label="Lowest hour"
            value={fmtNum(s.trough.value)}
            note={`at ${label(s.trough.index)}:00`}
          />
          <Figure label="90% band width" value={fmtNum(s.band90)} note="mean of q95 − q05" />
        </dl>

        <div className="forecast__chart">
          <Legend hasActual={hasActual} hasNaive={hasNaive} />
          {!hasActual && (
            <p className="forecast__awaiting">
              Actual prices for this day are not in the dataset yet; they are overlaid once
              published.
            </p>
          )}
          <FanChart
            hours={forecast.hours}
            title={`${zone} price forecast for ${formatDateLong(forecast.target_date)}, EUR/MWh by hour`}
          />
        </div>

        {err && <ErrorSummary err={err} />}

        <HourlyTable hours={forecast.hours} layout={layout} />
      </section>
    </div>
  );
}

function Figure({
  label,
  value,
  note,
  signal = false,
}: {
  label: string;
  value: string;
  note?: string | undefined;
  signal?: boolean;
}) {
  return (
    <div className={`figure ${signal ? 'figure--signal' : ''}`}>
      <dt className="label">{label}</dt>
      <dd>
        <span className="figure__value">{value}</span> <span className="unit">EUR/MWh</span>
        {note && <span className="figure__note">{note}</span>}
      </dd>
    </div>
  );
}

function ErrorSummary({ err }: { err: ForecastError }) {
  const coverage =
    err.hours === err.totalHours
      ? `all ${String(err.totalHours)} hours`
      : `${String(err.hours)} of ${String(err.totalHours)} hours`;
  const outside = err.aboveQ95 + err.belowQ05;
  return (
    <section className="fcerr" aria-labelledby="fcerr-title">
      <h2 id="fcerr-title" className="fcerr__title">
        Forecast error <span className="unit">EUR/MWh</span>
      </h2>
      <p className="fcerr__lede">
        Bias of the median, q50 − actual, over {coverage}. Negative: the forecast was too low.
      </p>
      <dl className="fcerr__parts">
        {err.parts.map((p) => {
          const top = p.key === err.largest;
          return (
            <div key={p.key} className={`fcerr__cell ${top ? 'fcerr__cell--signal' : ''}`}>
              <dt className="label">
                {p.label} <span className="fcerr__hours">{`${pad2(p.from)}–${pad2(p.to)}`}</span>
              </dt>
              <dd className="fcerr__value">
                {fmtSigned(p.bias)}
                {top && <span className="visually-hidden"> (largest)</span>}
              </dd>
            </div>
          );
        })}
      </dl>
      <dl className="fcerr__day">
        <div className="fcerr__cell">
          <dt className="label">Mean absolute error, median</dt>
          <dd className="fcerr__value">{fmtNum(err.mae)}</dd>
        </div>
        <div className="fcerr__cell">
          <dt className="label">Outside the 90% band</dt>
          <dd>
            <span className="fcerr__value">{outside}</span>{' '}
            <span className="unit">of {err.hours} h</span>
            <span className="fcerr__note">
              <span className="nowrap">{err.aboveQ95} above q95</span> ·{' '}
              <span className="nowrap">{err.belowQ05} below q05</span>
            </span>
          </dd>
        </div>
      </dl>
    </section>
  );
}

function DstNote({ layout }: { layout: DayLayout }) {
  if (layout.dst === 'none' || layout.transitionHour === null) return null;
  const h = pad2(layout.transitionHour);
  return (
    <p className="forecast__dst">
      {layout.dst === 'long'
        ? `25-hour day. Clocks go back at 03:00, so ${h}:00 occurs twice; the second pass is shown as ${h}′.`
        : `23-hour day. Clocks go forward at 02:00, so there is no ${h}:00 hour.`}
    </p>
  );
}

function MetaBlock({
  zone,
  forecast,
  origins,
}: {
  zone: Zone;
  forecast: Forecast;
  origins: string[];
}) {
  const navigate = useNavigate();
  const { zones } = useStatus();
  const summary = zones.status === 'ok' ? zones.data.find((z) => z.zone === zone) : undefined;
  const latest = origins[0];
  const isLatest = forecast.origin_date === latest;
  const selectId = 'origin-picker';

  return (
    <div className="meta">
      {summary?.stale && isLatest && (
        <div className="stale" role="status">
          <Icon name="alert-triangle" size={20} />
          <p>
            <strong>Stale: no forecast made today.</strong> The newest {zone} forecast was made{' '}
            {agoPhrase(forecast.origin_date)} and covers {formatDateShort(forecast.target_date)}.
          </p>
        </div>
      )}
      {!isLatest && latest && (
        <div className="past" role="status">
          <p>
            Viewing an earlier origin.{' '}
            <Link to={`/?zone=${zone}`}>Back to the latest ({formatDateShort(latest)})</Link>
          </p>
        </div>
      )}

      <div className="meta__row">
        <label className="label" htmlFor={selectId}>
          Origin (forecast made on)
        </label>
        <div className="select">
          <select
            id={selectId}
            value={forecast.origin_date}
            onChange={(e) => {
              const v = e.target.value;
              void navigate(v === latest ? `/?zone=${zone}` : `/?zone=${zone}&origin=${v}`);
            }}
          >
            {origins.map((o, i) => (
              <option key={o} value={o}>
                {formatDateShort(o)} {o.slice(0, 4)}
                {i === 0 ? ' · latest' : ''}
              </option>
            ))}
          </select>
          <Icon name="chevron-down" size={16} />
        </div>
      </div>
      <dl className="meta__list">
        <div className="meta__row">
          <dt className="label">Made at</dt>
          <dd>{formatStamp(forecast.forecast_made_at)}</dd>
        </div>
        <div className="meta__row">
          <dt className="label">Model</dt>
          <dd>
            {forecast.model}
            <span className="muted"> · registry v{forecast.model_version}</span>
          </dd>
        </div>
        <div className="meta__row">
          <dt className="label">Origin</dt>
          <dd>{formatStamp(forecast.origin)}</dd>
        </div>
      </dl>
    </div>
  );
}

/** "2 days ago, on Sun 4 Oct" (or just "on Sun 4 Oct" when the clock disagrees). */
function agoPhrase(originDate: string): string {
  const n = daysBetween(originDate, stockholmToday());
  const on = `on ${formatDateShort(originDate)}`;
  if (n === 1) return `yesterday, ${on}`;
  return n > 1 ? `${n} days ago, ${on}` : on;
}
