export function Legend({ hasActual, hasNaive }: { hasActual: boolean; hasNaive: boolean }) {
  return (
    <ul className="legend" aria-label="Legend">
      <li>
        <svg width="24" height="12" aria-hidden="true">
          <line x1="0" x2="24" y1="6" y2="6" className="fan__median" />
        </svg>
        Median q50
      </li>
      <li>
        <svg width="16" height="12" aria-hidden="true">
          <rect width="16" height="12" className="fan__band50" />
        </svg>
        50% band q25–q75
      </li>
      <li>
        <svg width="16" height="12" aria-hidden="true">
          <rect width="16" height="12" className="fan__band90" />
        </svg>
        90% band q05–q95
      </li>
      {hasActual && (
        <li>
          <svg width="24" height="12" aria-hidden="true">
            <line x1="0" x2="24" y1="6" y2="6" className="fan__actual" />
          </svg>
          Actual
        </li>
      )}
      {hasNaive && (
        <li>
          <svg width="24" height="12" aria-hidden="true">
            <line x1="0" x2="24" y1="6" y2="6" className="fan__naive" />
          </svg>
          Naive 7-day
        </li>
      )}
    </ul>
  );
}
