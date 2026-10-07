import { useId, useState } from 'react';
import type { FocusEvent } from 'react';
import { Link } from 'react-router';
import type { Zone } from '../api';
import { MAP_HEIGHT, MAP_WIDTH, ZONE_SHAPES } from '../assets/map/sweden';
import { forecastPath, useSelection } from '../store/selection';
import './SwedenMap.css';

/** Svenska kraftnät's names for the bidding zones. */
const ZONE_NAMES: Record<Zone, string> = {
  SE1: 'Luleå',
  SE2: 'Sundsvall',
  SE3: 'Stockholm',
  SE4: 'Malmö',
};

/** Accessible name of a zone on the map, e.g. "SE3, Stockholm (selected, stale)". */
function zoneLinkLabel(z: Zone, selected: boolean, stale: boolean): string {
  const notes = [selected && 'selected', stale && 'stale'].filter(Boolean).join(', ');
  return `${z}, ${ZONE_NAMES[z]}${notes ? ` (${notes})` : ''}`;
}

const isFocusVisible = (el: Element) => {
  try {
    return el.matches(':focus-visible');
  } catch {
    return true;
  }
};

/**
 * Schematic map of the four bidding zones; each zone links to its forecast. A companion to
 * the zone switcher, which stays the primary control. Borders are approximate by design
 * (see assets/map/README.md) and the caption says so.
 */
export function SwedenMap({
  zone,
  stale = {},
}: {
  zone: Zone;
  stale?: Partial<Record<Zone, boolean>>;
}) {
  const id = useId();
  const origin = useSelection((s) => s.origin);
  const [focused, setFocused] = useState<Zone | null>(null);
  const focusedShape = ZONE_SHAPES.find((s) => s.zone === focused);
  const onFocus = (z: Zone) => (e: FocusEvent) => {
    setFocused(isFocusVisible(e.currentTarget) ? z : null);
  };

  return (
    <figure className="smap">
      <svg
        className="smap__svg"
        viewBox={`0 0 ${MAP_WIDTH} ${MAP_HEIGHT}`}
        role="group"
        aria-labelledby={`${id}-title`}
        aria-describedby={`${id}-desc`}
      >
        <title id={`${id}-title`}>Bidding zones of Sweden</title>
        <desc id={`${id}-desc`}>
          Schematic map; the zone borders are approximate. From north to south: SE1 Luleå, SE2
          Sundsvall, SE3 Stockholm (with Gotland), SE4 Malmö (with Öland). {zone} is selected.
        </desc>
        {ZONE_SHAPES.map(({ zone: z, d, label: [x, y] }) => {
          const selected = z === zone;
          const isStale = stale[z] ?? false;
          return (
            <Link
              key={z}
              to={forecastPath(z, origin)}
              className="smap__zone"
              aria-label={zoneLinkLabel(z, selected, isStale)}
              aria-current={selected ? 'true' : undefined}
              onFocus={onFocus(z)}
              onBlur={() => {
                setFocused(null);
              }}
            >
              <path className="smap__shape" d={d} />
              <text className="smap__label" x={x} y={y} aria-hidden="true">
                {z}
              </text>
              {isStale && (
                <rect
                  className="smap__stale"
                  data-testid={`stale-${z}`}
                  x={x + 13}
                  y={y - 10.5}
                  width={5}
                  height={5}
                />
              )}
            </Link>
          );
        })}
        {focusedShape && <path className="smap__focus" d={focusedShape.d} />}
      </svg>
      <figcaption className="smap__caption">
        Schematic — zone borders approximate. Outline: Natural Earth.
      </figcaption>
    </figure>
  );
}
