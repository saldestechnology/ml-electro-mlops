import { line } from 'd3-shape';
import { scaleLinear } from 'd3-scale';
import type { Performance } from '../../api';
import { useElementWidth } from '../../hooks/useElementWidth';
import { fmtNum } from '../../lib/format';
import { daysScored } from '../../lib/sample';
import { addDays, formatDateTiny } from '../../lib/time';
import { consecutiveRuns, dailyPoints } from './daily';
import type { DailyDomains, DailyPoint } from './daily';

const M = { top: 8, right: 8, bottom: 24, left: 32 };

/**
 * Daily pinball loss of the model and the naive forecast for one zone. Every scored day is a
 * marker (filled red for the model, open grey for naive); lines join consecutive days only, so
 * a single scored day still shows and a missing day leaves a gap. Callers render the empty
 * state when `dailyPoints` is empty.
 */
export function DailyPinballChart({
  perf,
  domains,
  height = 160,
}: {
  perf: Performance;
  domains: DailyDomains;
  height?: number;
}) {
  const [ref, width] = useElementWidth<HTMLDivElement>(280);
  const iw = Math.max(10, width - M.left - M.right);
  const ih = height - M.top - M.bottom;
  const x = scaleLinear()
    .domain([0, Math.max(1, domains.days - 1)])
    .range([0, iw]);
  const y = scaleLinear().domain([0, domains.max]).range([ih, 0]);
  const pts = dailyPoints(perf, domains);
  const runs = consecutiveRuns(pts).filter((r) => r.length > 1);
  const path = (run: DailyPoint[], key: 'pinball' | 'naive') =>
    line<DailyPoint>()
      .x((p) => x(p.i))
      .y((p) => y(p[key]))(run) ?? '';
  const last = pts[pts.length - 1];
  const label = `${perf.zone} daily pinball loss, ${daysScored(pts.length)}; latest ${
    last ? `${fmtNum(last.pinball, 2)} against naive ${fmtNum(last.naive, 2)}` : 'none'
  }.`;

  return (
    <div ref={ref} className="daily">
      <svg width={width} height={height} role="img" aria-label={label} className="daily__svg">
        <g transform={`translate(${M.left},${M.top})`}>
          {y.ticks(3).map((t) => (
            <g key={t} transform={`translate(0,${y(t)})`}>
              <line x2={iw} className={t === 0 ? 'fan__axis' : 'fan__grid'} />
              <text x={-6} dy="0.32em" className="fan__ylabel">
                {fmtNum(t, 0)}
              </text>
            </g>
          ))}
          {runs.map((r) => (
            <path key={`n${r[0]?.i}`} d={path(r, 'naive')} className="fan__naive" />
          ))}
          {runs.map((r) => (
            <path key={`m${r[0]?.i}`} d={path(r, 'pinball')} className="fan__median" />
          ))}
          {pts.map((p) => (
            <circle key={`n${p.i}`} cx={x(p.i)} cy={y(p.naive)} r={3} className="daily__naive" />
          ))}
          {pts.map((p) => (
            <circle
              key={`m${p.i}`}
              cx={x(p.i)}
              cy={y(p.pinball)}
              r={3.5}
              className="daily__model"
            />
          ))}
          {[...new Set([0, domains.days - 1])].map((i) => (
            <text
              key={i}
              x={x(i)}
              y={ih + 18}
              className="fan__xlabel"
              textAnchor={i === 0 ? 'start' : 'end'}
              style={{ textAnchor: i === 0 ? 'start' : 'end' }}
            >
              {formatDateTiny(addDays(domains.start, i))}
            </text>
          ))}
        </g>
      </svg>
    </div>
  );
}
