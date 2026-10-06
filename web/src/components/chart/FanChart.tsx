import { area, line } from 'd3-shape';
import { scaleLinear } from 'd3-scale';
import { useId, useMemo, useState } from 'react';
import type { KeyboardEvent, PointerEvent } from 'react';
import type { ForecastHour } from '../../api';
import { useElementWidth } from '../../hooks/useElementWidth';
import { fmtNum } from '../../lib/format';
import { dayLayout, slotAt, stepPoints, tickSlots, yDomain } from './scale';
import { Readout } from './Readout';
import './FanChart.css';

const M = { top: 12, right: 8, bottom: 32, left: 44 };

interface Props {
  hours: ForecastHour[];
  height?: number;
  title: string;
}

export function FanChart({ hours, height = 360, title }: Props) {
  const [wrapRef, width] = useElementWidth<HTMLDivElement>();
  const [active, setActive] = useState<number | null>(null);
  const descId = useId();
  const layout = useMemo(() => dayLayout(hours), [hours]);
  const n = hours.length;
  const iw = Math.max(10, width - M.left - M.right);
  const ih = height - M.top - M.bottom;

  const x = scaleLinear().domain([0, n]).range([0, iw]);
  const y = scaleLinear().domain(yDomain(hours)).range([ih, 0]);
  const yTicks = y.ticks(ih < 240 ? 4 : 6);
  const ticks = tickSlots(layout, iw < 420 ? 6 : 3);

  const band = (lo: keyof ForecastHour, hi: keyof ForecastHour) => {
    const pts = hours.flatMap((h, i) => [
      { i, lo: h[lo] as number, hi: h[hi] as number },
      { i: i + 1, lo: h[lo] as number, hi: h[hi] as number },
    ]);
    return (
      area<{ i: number; lo: number; hi: number }>()
        .x((d) => x(d.i))
        .y0((d) => y(d.lo))
        .y1((d) => y(d.hi))(pts) ?? ''
    );
  };
  const path = (values: (number | null)[]) =>
    stepPoints(values)
      .map(
        (run) =>
          line()
            .x((d) => x(d[0]))
            .y((d) => y(d[1]))(run) ?? '',
      )
      .join('');

  const hasActual = hours.some((h) => h.actual !== null);
  const hasNaive = hours.some((h) => h.naive_7d !== null);

  function onPointer(e: PointerEvent<SVGRectElement>) {
    const box = e.currentTarget.getBoundingClientRect();
    setActive(slotAt(e.clientX - box.left, box.width || iw, n));
  }
  function onKey(e: KeyboardEvent<SVGSVGElement>) {
    const cur = active ?? -1;
    let next: number | null = null;
    if (e.key === 'ArrowRight') next = Math.min(n - 1, cur + 1);
    else if (e.key === 'ArrowLeft') next = Math.max(0, cur < 0 ? n - 1 : cur - 1);
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = n - 1;
    else if (e.key === 'Escape') {
      setActive(null);
      return;
    }
    if (next !== null) {
      e.preventDefault();
      setActive(next);
    }
  }

  const activeHour = active !== null ? hours[active] : undefined;
  const activeSlot = active !== null ? layout.slots[active] : undefined;

  return (
    <figure className="fan">
      <div className="fan__plot" ref={wrapRef}>
        <svg
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          role="application"
          aria-roledescription="chart"
          aria-label={title}
          aria-describedby={descId}
          tabIndex={0}
          onKeyDown={onKey}
          onBlur={() => {
            setActive(null);
          }}
          className="fan__svg"
        >
          <g transform={`translate(${M.left},${M.top})`}>
            {yTicks.map((t) => (
              <g key={t} transform={`translate(0,${y(t)})`}>
                <line x2={iw} className={t === 0 ? 'fan__zero' : 'fan__grid'} />
                <text x={-8} dy="0.32em" className="fan__ylabel">
                  {fmtNum(t, 0)}
                </text>
              </g>
            ))}
            {activeSlot && (
              <rect
                x={x(activeSlot.index)}
                width={Math.max(1, x(1) - x(0))}
                y={0}
                height={ih}
                className="fan__hover"
              />
            )}
            <path d={band('q05', 'q95')} className="fan__band90" />
            <path d={band('q25', 'q75')} className="fan__band50" />
            {hasNaive && <path d={path(hours.map((h) => h.naive_7d))} className="fan__naive" />}
            <path d={path(hours.map((h) => h.q50))} className="fan__median" />
            {hasActual && <path d={path(hours.map((h) => h.actual))} className="fan__actual" />}
            {layout.dst === 'long' && layout.transitionHour !== null && (
              <DstMark
                x={x(layout.slots.find((s) => s.repeated)?.index ?? 0)}
                h={ih}
                label="clocks back"
              />
            )}
            {layout.dst === 'short' && layout.transitionHour !== null && (
              <DstMark
                x={x(layout.slots.findIndex((s) => s.hour > (layout.transitionHour ?? 0)))}
                h={ih}
                label="clocks forward"
              />
            )}
            <line y1={ih} y2={ih} x2={iw} className="fan__base" />
            {ticks.map((s) => (
              <g key={s.index} transform={`translate(${x(s.index)},${ih})`}>
                <line y2={5} className="fan__base" />
                <text y={20} className="fan__xlabel">
                  {s.label}
                </text>
              </g>
            ))}
            <rect
              width={iw}
              height={ih}
              className="fan__capture"
              onPointerMove={onPointer}
              onPointerDown={onPointer}
              onPointerLeave={() => {
                setActive(null);
              }}
            />
          </g>
        </svg>
        <p id={descId} className="visually-hidden">
          Hourly price forecast in EUR/MWh for {n} delivery hours. Focus the chart and use the left
          and right arrow keys to read the quantiles for each hour.
        </p>
      </div>
      <Readout hour={activeHour} slot={activeSlot} />
    </figure>
  );
}

function DstMark({ x, h, label }: { x: number; h: number; label: string }) {
  return (
    <g transform={`translate(${x},0)`} className="fan__dst">
      <line y2={h} />
      <text x={4} y={10}>
        {label}
      </text>
    </g>
  );
}
