import type { ReactNode } from 'react';
import { ApiError } from '../api';
import { Icon } from './Icon';

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <section className="state" aria-label={title}>
      <span className="state__icon">
        <Icon name="circle-dashed" size={32} />
      </span>
      <h2 className="state__title">{title}</h2>
      {children && <div className="state__body">{children}</div>}
    </section>
  );
}

export function ErrorState({ error, what }: { error: Error; what: string }) {
  const detail = error instanceof ApiError ? error.detail : error.message;
  return (
    <section className="state" role="alert">
      <span className="state__icon">
        <Icon name="alert-triangle" size={32} />
      </span>
      <h2 className="state__title">Could not load {what}</h2>
      <p className="state__body">{detail}</p>
    </section>
  );
}

export function Loading({ what }: { what: string }) {
  return (
    <p className="loading" role="status">
      Loading {what}…
    </p>
  );
}
