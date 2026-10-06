import { line } from 'd3-shape';
import { scaleLinear } from 'd3-scale';
import type { Performance } from '../../api';
import { useElementWidth } from '../../hooks/useElementWidth';
import { fmtNum } from '../../lib/format';
import { addDays, formatDateTiny } from '../../lib/time';
import { dayIndex } from './daily';
import type { DailyDomains } from './daily';

const M = { top: 8, right: 8, bottom: 24, left: 32 };

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
    .domain([0, domains.days - 1])
    .range([0, iw]);
  const y = scaleLinear().domain([0, domains.max]).range([ih, 0]);
  const pts = perf.daily.map((d) => ({ i: dayIndex(domains.start, d.origin_date), d }));
  const path = (key: 'pinball' | 'naive_7d_pinball') =>
    line<(typeof pts)[number]>()
      .defined((p) => p.i >= 0)
      .x((p) => x(p.i))
      .y((p) => y(p.d[key]))(pts) ?? '';
  const last = perf.daily[perf.daily.length - 1];
  const label = `${perf.zone} daily pinball loss over ${perf.daily.length} scored days; latest ${
    last ? `${fmtNum(last.pinball, 2)} against naive ${fmtNum(last.naive_7d_pinball, 2)}` : 'none'
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
          <path d={path('naive_7d_pinball')} className="fan__naive" />
          <path d={path('pinball')} className="fan__median" />
          {[0, domains.days - 1].map((i) => (
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
