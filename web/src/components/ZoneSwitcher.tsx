import { Link } from 'react-router';
import { ZONES } from '../api';
import type { Zone } from '../api';
import { useStatus } from '../hooks/useStatus';
import { forecastPath, useSelection } from '../store/selection';
import './ZoneSwitcher.css';

export function ZoneSwitcher({ zone }: { zone: Zone }) {
  const { zones } = useStatus();
  const origin = useSelection((s) => s.origin);
  const stale = (z: Zone) =>
    zones.status === 'ok' && (zones.data.find((s) => s.zone === z)?.stale ?? false);
  return (
    <nav className="zones" aria-label="Bidding zone">
      <ul className="zones__list">
        {ZONES.map((z) => (
          <li key={z}>
            <Link
              to={forecastPath(z, origin)}
              className="zones__link"
              aria-current={z === zone ? 'true' : undefined}
            >
              {z}
              {stale(z) && (
                <span className="zones__stale" title="stale">
                  <span className="visually-hidden"> (stale)</span>
                </span>
              )}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}
