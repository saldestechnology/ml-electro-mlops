import { ApiError, ZONES } from '../api';
import type { ModelCard, Zone } from '../api';
import { ErrorState, Loading } from '../components/States';
import { useApi } from '../hooks/useApi';
import type { Loadable } from '../hooks/useApi';
import { useStatus } from '../hooks/useStatus';
import { formatDateShort, formatStamp } from '../lib/time';
import './ModelPage.css';

type PerZone = { zone: Zone; result: Loadable<ModelCard> };

export function ModelPage() {
  const all = useApi('models', async (c, s) => {
    const settled = await Promise.allSettled(ZONES.map((z) => c.model(z, s)));
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
    <div className="model">
      <header className="grid model__head">
        <h1>Model</h1>
        <p className="lede">
          The champion served in each zone: what it is, which registry version it came from, what it
          was last trained on, and the software it ran with.
        </p>
      </header>
      {all.status === 'loading' && <Loading what="model states" />}
      {all.status === 'error' && (
        <div className="grid">
          <ErrorState error={all.error} what="model states" />
        </div>
      )}
      {all.status === 'ok' && (
        <div className="grid model__cards">
          {all.data.map((z) => (
            <article key={z.zone} className="card" aria-labelledby={`card-${z.zone}`}>
              <h2 id={`card-${z.zone}`} className="card__zone">
                {z.zone}
              </h2>
              {z.result.status === 'ok' && <Card m={z.result.data} />}
              {z.result.status === 'error' &&
                (z.result.error instanceof ApiError && z.result.error.status === 404 ? (
                  <div className="card__empty">
                    <p className="card__emptytitle">No served model</p>
                    <p>{z.zone} has no served state yet. It appears after the first pull.</p>
                  </div>
                ) : (
                  <ErrorState error={z.result.error} what={`the ${z.zone} model`} />
                ))}
            </article>
          ))}
        </div>
      )}
    </div>
  );
}

function Card({ m }: { m: ModelCard }) {
  const { zones } = useStatus();
  const stale = zones.status === 'ok' && zones.data.find((z) => z.zone === m.zone)?.stale;
  const d = (v: string | null) => (v ? formatDateShort(v) + ' ' + v.slice(0, 4) : '—');
  return (
    <>
      <p className="card__spec">{m.spec}</p>
      <p className="card__version">
        <span className="label">Registry version</span>
        <span className="card__big">{m.model_version ? `v${m.model_version}` : '—'}</span>
        <span className="unit">se-price-{m.zone.toLowerCase()}-hourly</span>
      </p>
      <dl className="card__list">
        <Item k="First origin" v={d(m.first_origin)} />
        <Item k="Last origin" v={d(m.last_origin) + (stale ? ' · stale' : '')} />
        <Item k="Origins served" v={String(m.origins_served)} />
        <Item k="Last fit" v={d(m.last_fit)} />
        <Item k="Train start" v={d(m.train_start)} />
        <Item k="Saved" v={formatStamp(m.saved_at)} />
        <Item k="Git" v={m.git_sha.slice(0, 7)} />
      </dl>
      <h3 className="card__sub">Datasets</h3>
      <dl className="card__list card__list--kv">
        {Object.entries(m.datasets).map(([k, v]) => (
          <Item key={k} k={k} v={v} />
        ))}
      </dl>
      <h3 className="card__sub">Packages</h3>
      <dl className="card__list card__list--kv">
        {Object.entries(m.versions).map(([k, v]) => (
          <Item key={k} k={k} v={v} />
        ))}
      </dl>
      <h3 className="card__sub">Backups</h3>
      {m.backups.length ? (
        <ul className="card__backups">
          {m.backups.map((b) => (
            <li key={b}>{b}</li>
          ))}
        </ul>
      ) : (
        <p className="muted card__none">No backups yet.</p>
      )}
    </>
  );
}

function Item({ k, v }: { k: string; v: string }) {
  return (
    <div className="card__item">
      <dt>{k}</dt>
      <dd>{v}</dd>
    </div>
  );
}
