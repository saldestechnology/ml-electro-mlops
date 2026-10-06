import { useFixtures } from '../api';
import { useStatus } from '../hooks/useStatus';
import { formatDateTiny } from '../lib/time';
import './StatusLine.css';

/** One quiet line: environment, build, and whether each zone's forecast is current. */
export function StatusLine() {
  const { health, zones } = useStatus();
  const sample = useFixtures || (health.status === 'ok' && health.data.env === 'fixtures');
  return (
    <div className="status" aria-label="System status">
      <div className="page status__inner">
        {sample && (
          <span className="status__sample" title="Served from src/fixtures, not the API">
            Sample data
          </span>
        )}
        <span className="status__item">
          <span className="status__key">env</span>{' '}
          {health.status === 'ok'
            ? health.data.env
            : health.status === 'error'
              ? 'unreachable'
              : '…'}
        </span>
        <span className="status__item">
          <span className="status__key">build</span>{' '}
          {health.status === 'ok' ? health.data.git_sha.slice(0, 7) : '…'}
        </span>
        <ul className="status__zones">
          {zones.status === 'ok' &&
            zones.data.map((z) => (
              <li
                key={z.zone}
                className={`status__zone ${z.stale ? 'status__zone--stale' : ''}`}
                title={
                  z.latest_origin
                    ? `latest origin ${z.latest_origin}, forecasting ${z.target_date ?? '?'}`
                    : 'no forecast yet'
                }
              >
                <span className="status__mark" aria-hidden="true" />
                {z.zone}{' '}
                {z.stale
                  ? z.latest_origin
                    ? `stale · last forecast ${formatDateTiny(z.latest_origin)}`
                    : 'no forecast'
                  : 'fresh'}
              </li>
            ))}
          {zones.status === 'error' && <li className="status__zone">zones unavailable</li>}
        </ul>
      </div>
    </div>
  );
}
